from __future__ import annotations

import csv
import io
import sqlite3
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from pydantic import BaseModel, Field, PositiveFloat, conint

DB_PATH = "data.db"

app = FastAPI(title="Flower Landed Cost Calculator")


# -----------------------------
# Database
# -----------------------------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,

                product_name TEXT NOT NULL,
                origin TEXT NOT NULL,
                awb TEXT NOT NULL,

                box_type TEXT NOT NULL,
                boxes INTEGER NOT NULL,

                invoice_value_per_box REAL NOT NULL,

                weight_kg_per_box REAL NOT NULL,
                air_rate_per_kg REAL NOT NULL,

                admin_fee_total REAL NOT NULL,
                customs_total REAL NOT NULL,
                trucking_total REAL NOT NULL,

                duty_rate REAL NOT NULL,
                duty_base TEXT NOT NULL,

                bunches_per_box INTEGER NOT NULL,
                stems_per_bunch INTEGER NOT NULL,

                target_margin REAL NOT NULL,

                cost_per_box REAL NOT NULL,
                cost_per_bunch REAL NOT NULL,
                cost_per_stem REAL NOT NULL,

                suggested_sell_per_box REAL NOT NULL,
                suggested_sell_per_bunch REAL NOT NULL,
                suggested_sell_per_stem REAL NOT NULL
            )
            """
        )


@app.on_event("startup")
def _startup():
    init_db()


# -----------------------------
# Models
# -----------------------------
class CalcInput(BaseModel):
    product_name: str = Field(..., min_length=1)
    origin: str = Field(..., min_length=1)
    awb: str = Field(..., min_length=1)

    box_type: str = Field(..., min_length=1)  # FB/HB/QB/etc
    boxes: conint(ge=1) = 1

    # Commercial invoice value per box (what you pay the farm)
    invoice_value_per_box: PositiveFloat

    # Air freight
    weight_kg_per_box: PositiveFloat
    air_rate_per_kg: PositiveFloat

    # Shipment-level totals that get allocated across all boxes in this row
    admin_fee_total: float = 0.0
    customs_total: float = 0.0
    trucking_total: float = 0.0

    # Duty / tax (0.21 means 21%)
    duty_rate: float = 0.21
    duty_base: str = Field("invoice", description="invoice")

    # Packing structure
    bunches_per_box: conint(ge=1) = 12
    stems_per_bunch: conint(ge=1) = 25

    # Margin
    target_margin: float = 0.35  # 35%


# -----------------------------
# Core calculation
# -----------------------------
def safe_float(x: float) -> float:
    try:
        return float(x)
    except Exception:
        return 0.0


def calc(input: CalcInput) -> dict:
    boxes = int(input.boxes)

    invoice = float(input.invoice_value_per_box)
    air_cost_per_box = float(input.weight_kg_per_box) * float(input.air_rate_per_kg)

    admin_per_box = safe_float(input.admin_fee_total) / boxes
    customs_per_box = safe_float(input.customs_total) / boxes
    trucking_per_box = safe_float(input.trucking_total) / boxes

    # Duty base: invoice (for now)
    duty_per_box = safe_float(input.duty_rate) * invoice

    cost_per_box = (
        invoice
        + air_cost_per_box
        + admin_per_box
        + customs_per_box
        + trucking_per_box
        + duty_per_box
    )

    bunches_per_box = int(input.bunches_per_box)
    stems_per_bunch = int(input.stems_per_bunch)
    stems_per_box = bunches_per_box * stems_per_bunch

    cost_per_bunch = cost_per_box / bunches_per_box
    cost_per_stem = cost_per_box / stems_per_box

    m = safe_float(input.target_margin)
    m = 0.0 if m < 0 else (0.95 if m > 0.95 else m)  # prevent divide by near-zero
    sell_per_box = cost_per_box / (1.0 - m)
    sell_per_bunch = sell_per_box / bunches_per_box
    sell_per_stem = sell_per_box / stems_per_box

    return {
        "cost_per_box": round(cost_per_box, 4),
        "cost_per_bunch": round(cost_per_bunch, 4),
        "cost_per_stem": round(cost_per_stem, 4),
        "suggested_sell_per_box": round(sell_per_box, 4),
        "suggested_sell_per_bunch": round(sell_per_bunch, 4),
        "suggested_sell_per_stem": round(sell_per_stem, 4),
    }


# -----------------------------
# Routes
# -----------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    html = r"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Flower Landed Cost</title>
  <style>
    :root{
      --bg:#eaf4ff; --card:#fff; --text:#0b1a2a; --muted:#4b6075; --border:#cfe2f7;
      --btn:#2b6cb0; --btn2:#1f4f86; --soft:#f7fbff; --danger:#c53030;
    }
    body{ margin:0; font-family:Arial,sans-serif; background:var(--bg); color:var(--text); }
    .wrap{ max-width:1200px; margin:18px auto; padding:0 12px; }
    h1{ margin:0 0 12px 0; font-size:22px; }
    .grid{ display:grid; grid-template-columns: 1fr 1fr; gap:12px; }
    @media (max-width: 900px){ .grid{ grid-template-columns: 1fr; } }
    .card{
      background:var(--card);
      border:1px solid var(--border);
      border-radius:12px;
      padding:14px;
      box-shadow:0 2px 8px rgba(0,0,0,.06);
    }
    label{ display:block; font-size:12px; color:var(--muted); margin:10px 0 6px; }
    input, select{
      width:100%; padding:10px; border:1px solid var(--border); border-radius:10px;
      background:var(--soft); color:var(--text);
      box-sizing:border-box;
    }
    .row{ display:grid; grid-template-columns: 1fr 1fr; gap:10px; }
    .btns{ display:flex; gap:10px; flex-wrap:wrap; margin-top:12px; }
    button{
      padding:10px 14px; border-radius:10px; border:1px solid var(--border);
      background:var(--btn); color:white; cursor:pointer; font-weight:700;
    }
    button.secondary{ background:white; color:var(--btn2); }
    button.danger{ background:var(--danger); color:white; border-color:#f2b8b8; }
    table{ width:100%; border-collapse:collapse; }
    th, td{ padding:8px; border-bottom:1px solid var(--border); font-size:13px; text-align:left; }
    .kpi{ display:grid; grid-template-columns: repeat(3, 1fr); gap:10px; margin-top:10px; }
    @media (max-width: 900px){ .kpi{ grid-template-columns: 1fr; } }
    .kpi .box{ background:var(--soft); border:1px solid var(--border); border-radius:12px; padding:10px; }
    .kpi .big{ font-size:18px; font-weight:800; margin-top:4px; }
    .muted{ color:var(--muted); font-size:12px; }
    .pill{
      display:inline-block; padding:4px 8px; border-radius:999px;
      background:var(--soft); border:1px solid var(--border);
      font-size:12px; color:var(--muted);
    }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Flower Landed Cost Calculator</h1>

    <div class="grid">
      <div class="card">
        <h3 style="margin:0 0 6px 0;">Inputs</h3>
        <div class="muted">
          Clean MVP. <span class="pill">Auto-remembers per Product name</span>
          <div style="margin-top:6px;" class="muted" id="mem_status">Memory: —</div>
        </div>

        <label>Product name (this is the memory key)</label>
        <input id="product_name" placeholder="ROSES MONDIAL 60 CM" />

        <div class="row">
          <div>
            <label>Origin</label>
            <input id="origin" placeholder="ECUADOR" />
          </div>
          <div>
            <label>AWB (Shipment key)</label>
            <input id="awb" placeholder="AWB-123456" />
          </div>
        </div>

        <div class="row">
          <div>
            <label>Box type</label>
            <select id="box_type">
              <option value="FB">FB</option>
              <option value="HB" selected>HB</option>
              <option value="QB">QB</option>
            </select>
          </div>
          <div>
            <label># Boxes (for allocation)</label>
            <input id="boxes" type="number" min="1" value="1" />
          </div>
        </div>

        <label>Invoice value per box ($)</label>
        <input id="invoice_value_per_box" type="number" step="0.01" value="0" />

        <div class="row">
          <div>
            <label>Weight kg per box</label>
            <input id="weight_kg_per_box" type="number" step="0.01" value="0" />
          </div>
          <div>
            <label>Air rate per kg ($)</label>
            <input id="air_rate_per_kg" type="number" step="0.01" value="0" />
          </div>
        </div>

        <h3 style="margin:14px 0 6px 0;">Shipment-level totals (allocated across boxes)</h3>
        <div class="row">
          <div>
            <label>Admin fee total ($)</label>
            <input id="admin_fee_total" type="number" step="0.01" value="0" />
          </div>
          <div>
            <label>Customs total ($)</label>
            <input id="customs_total" type="number" step="0.01" value="0" />
          </div>
        </div>
        <label>Trucking total ($)</label>
        <input id="trucking_total" type="number" step="0.01" value="0" />

        <div class="row">
          <div>
            <label>Duty rate (e.g. 0.21 for 21%)</label>
            <input id="duty_rate" type="number" step="0.0001" value="0.21" />
          </div>
          <div>
            <label>Duty base</label>
            <select id="duty_base">
              <option value="invoice" selected>invoice</option>
            </select>
          </div>
        </div>

        <h3 style="margin:14px 0 6px 0;">Packing</h3>
        <div class="row">
          <div>
            <label>Bunches per box</label>
            <input id="bunches_per_box" type="number" min="1" value="12" />
          </div>
          <div>
            <label>Stems per bunch</label>
            <input id="stems_per_bunch" type="number" min="1" value="25" />
          </div>
        </div>

        <label>Target margin (default 0.35)</label>
        <input id="target_margin" type="number" step="0.01" value="0.35" />

        <div class="btns">
          <button onclick="doCalc()">Calculate & Save</button>
          <button class="secondary" onclick="loadHistory()">Refresh History</button>
          <button class="secondary" onclick="exportCSV()">Export CSV</button>

          <button class="secondary" onclick="saveForThisProduct()">Save for this Product</button>
          <button class="secondary" onclick="clearThisProduct()">Clear this Product</button>
          <button class="danger" onclick="clearAllProducts()">Clear ALL saved products</button>
        </div>
      </div>

      <div class="card">
        <h3 style="margin:0 0 6px 0;">Results</h3>
        <div class="kpi">
          <div class="box">
            <div class="muted">Cost / Box</div>
            <div class="big" id="r_cost_box">—</div>
          </div>
          <div class="box">
            <div class="muted">Cost / Bunch</div>
            <div class="big" id="r_cost_bunch">—</div>
          </div>
          <div class="box">
            <div class="muted">Cost / Stem</div>
            <div class="big" id="r_cost_stem">—</div>
          </div>
          <div class="box">
            <div class="muted">Suggested Sell / Box</div>
            <div class="big" id="r_sell_box">—</div>
          </div>
          <div class="box">
            <div class="muted">Suggested Sell / Bunch</div>
            <div class="big" id="r_sell_bunch">—</div>
          </div>
          <div class="box">
            <div class="muted">Suggested Sell / Stem</div>
            <div class="big" id="r_sell_stem">—</div>
          </div>
        </div>

        <h3 style="margin:14px 0 6px 0;">History (latest)</h3>
        <div class="muted" style="margin-bottom:8px;">Saved automatically when you calculate.</div>
        <div style="overflow:auto;">
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Product</th>
                <th>AWB</th>
                <th>Boxes</th>
                <th>Cost/Box</th>
                <th>Sell/Box</th>
              </tr>
            </thead>
            <tbody id="history_body"></tbody>
          </table>
        </div>
      </div>
    </div>
  </div>

<script>
const STORE_KEY = "flower_landed_cost_products_v1";

function $(id){ return document.getElementById(id); }
function v(id){ return $(id).value; }
function setv(id, val){ $(id).value = (val ?? ""); }

function num(id){ return parseFloat(v(id) || "0"); }
function intv(id){ return parseInt(v(id) || "0", 10); }

const FIELD_IDS = [
  "product_name","origin","awb",
  "box_type","boxes",
  "invoice_value_per_box",
  "weight_kg_per_box","air_rate_per_kg",
  "admin_fee_total","customs_total","trucking_total",
  "duty_rate","duty_base",
  "bunches_per_box","stems_per_bunch",
  "target_margin"
];

function normalizeProductName(name){
  return (name || "").toString().trim().toLowerCase().replace(/\s+/g, " ");
}

function getStore(){
  try{
    const raw = localStorage.getItem(STORE_KEY);
    if(!raw) return { products: {}, lastProductKey: "" };
    const parsed = JSON.parse(raw);
    return {
      products: parsed.products || {},
      lastProductKey: parsed.lastProductKey || ""
    };
  }catch(e){
    return { products: {}, lastProductKey: "" };
  }
}

function setStore(store){
  localStorage.setItem(STORE_KEY, JSON.stringify(store));
}

function collectInputs(){
  const data = {};
  for(const id of FIELD_IDS){
    data[id] = v(id);
  }
  return data;
}

function applyInputs(data){
  if(!data) return;
  for(const id of FIELD_IDS){
    if(Object.prototype.hasOwnProperty.call(data, id)){
      setv(id, data[id]);
    }
  }
}

function updateMemStatus(msg){
  $("mem_status").textContent = "Memory: " + msg;
}

function saveForThisProduct(){
  const name = v("product_name");
  const key = normalizeProductName(name);
  if(!key){
    alert("Type a Product name first.");
    return;
  }
  const store = getStore();
  store.products[key] = collectInputs();
  store.lastProductKey = key;
  setStore(store);
  updateMemStatus(`Saved for "${name}"`);
}

function loadForThisProduct(){
  const name = v("product_name");
  const key = normalizeProductName(name);
  if(!key){
    updateMemStatus("No product name");
    return false;
  }
  const store = getStore();
  const saved = store.products[key];
  if(saved){
    applyInputs(saved);
    store.lastProductKey = key;
    setStore(store);
    updateMemStatus(`Loaded saved inputs for "${name}"`);
    return true;
  }else{
    store.lastProductKey = key;
    setStore(store);
    updateMemStatus(`No saved inputs for "${name}"`);
    return false;
  }
}

function clearThisProduct(){
  const name = v("product_name");
  const key = normalizeProductName(name);
  if(!key){
    alert("Type a Product name first.");
    return;
  }
  const store = getStore();
  if(store.products[key]){
    delete store.products[key];
    setStore(store);
    updateMemStatus(`Cleared saved inputs for "${name}"`);
  }else{
    updateMemStatus(`Nothing to clear for "${name}"`);
  }
}

function clearAllProducts(){
  if(!confirm("Clear ALL saved products? This cannot be undone.")) return;
  localStorage.removeItem(STORE_KEY);
  updateMemStatus("Cleared ALL saved products");
}

function autoSaveTyping(){
  const name = v("product_name");
  const key = normalizeProductName(name);
  if(!key) return;

  const store = getStore();
  store.products[key] = collectInputs();
  store.lastProductKey = key;
  setStore(store);
  updateMemStatus(`Auto-saved for "${name}"`);
}

function setResult(out){
  $("r_cost_box").textContent   = "$ " + out.cost_per_box.toFixed(4);
  $("r_cost_bunch").textContent = "$ " + out.cost_per_bunch.toFixed(4);
  $("r_cost_stem").textContent  = "$ " + out.cost_per_stem.toFixed(4);

  $("r_sell_box").textContent   = "$ " + out.suggested_sell_per_box.toFixed(4);
  $("r_sell_bunch").textContent = "$ " + out.suggested_sell_per_bunch.toFixed(4);
  $("r_sell_stem").textContent  = "$ " + out.suggested_sell_per_stem.toFixed(4);
}

async function doCalc(){
  const payload = {
    product_name: v("product_name"),
    origin: v("origin"),
    awb: v("awb"),

    box_type: v("box_type"),
    boxes: intv("boxes"),

    invoice_value_per_box: num("invoice_value_per_box"),

    weight_kg_per_box: num("weight_kg_per_box"),
    air_rate_per_kg: num("air_rate_per_kg"),

    admin_fee_total: num("admin_fee_total"),
    customs_total: num("customs_total"),
    trucking_total: num("trucking_total"),

    duty_rate: num("duty_rate"),
    duty_base: v("duty_base"),

    bunches_per_box: intv("bunches_per_box"),
    stems_per_bunch: intv("stems_per_bunch"),

    target_margin: num("target_margin")
  };

  saveForThisProduct();

  const res = await fetch("/calculate", {
    method: "POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify(payload)
  });

  if(!res.ok){
    const text = await res.text();
    alert("Error: " + text);
    return;
  }

  const out = await res.json();
  setResult(out);
  loadHistory();
}

async function loadHistory(){
  const res = await fetch("/history?limit=50");
  const data = await res.json();
  const tbody = $("history_body");
  tbody.innerHTML = "";

  for(const row of data){
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${row.created_at}</td>
      <td>${escapeHtml(row.product_name)}</td>
      <td>${escapeHtml(row.awb)}</td>
      <td>${row.boxes}</td>
      <td>$ ${Number(row.cost_per_box).toFixed(4)}</td>
      <td>$ ${Number(row.suggested_sell_per_box).toFixed(4)}</td>
    `;
    tbody.appendChild(tr);
  }
}

function exportCSV(){
  window.location.href = "/export.csv";
}

function escapeHtml(str){
  return (str ?? "").toString()
    .replaceAll("&","&amp;")
    .replaceAll("<","&lt;")
    .replaceAll(">","&gt;")
    .replaceAll('"',"&quot;")
    .replaceAll("'","&#039;");
}

function loadLastProductOnOpen(){
  const store = getStore();
  if(store.lastProductKey && store.products[store.lastProductKey]){
    const saved = store.products[store.lastProductKey];
    applyInputs(saved);
    const displayName = saved.product_name || store.lastProductKey;
    updateMemStatus(`Loaded last product: "${displayName}"`);
    return true;
  }
  updateMemStatus("No saved products yet");
  return false;
}

window.addEventListener("DOMContentLoaded", () => {
  loadLastProductOnOpen();
  loadHistory();

  $("product_name").addEventListener("change", () => {
    loadForThisProduct();
  });
  $("product_name").addEventListener("blur", () => {
    loadForThisProduct();
  });

  for(const id of FIELD_IDS){
    const el = $(id);
    if(!el) continue;
    if(id === "product_name") continue;

    el.addEventListener("change", autoSaveTyping);
    el.addEventListener("input", autoSaveTyping);
  }
});
</script>
</body>
</html>
"""
    return HTMLResponse(html)


@app.post("/calculate")
def calculate(payload: CalcInput):
    out = calc(payload)

    created_at = datetime.now(timezone.utc).isoformat()

    with db() as conn:
        conn.execute(
            """
            INSERT INTO history (
                created_at,
                product_name, origin, awb,
                box_type, boxes,
                invoice_value_per_box,
                weight_kg_per_box, air_rate_per_kg,
                admin_fee_total, customs_total, trucking_total,
                duty_rate, duty_base,
                bunches_per_box, stems_per_bunch,
                target_margin,
                cost_per_box, cost_per_bunch, cost_per_stem,
                suggested_sell_per_box, suggested_sell_per_bunch, suggested_sell_per_stem
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                created_at,
                payload.product_name, payload.origin, payload.awb,
                payload.box_type, int(payload.boxes),
                float(payload.invoice_value_per_box),
                float(payload.weight_kg_per_box), float(payload.air_rate_per_kg),
                float(payload.admin_fee_total), float(payload.customs_total), float(payload.trucking_total),
                float(payload.duty_rate), str(payload.duty_base),
                int(payload.bunches_per_box), int(payload.stems_per_bunch),
                float(payload.target_margin),
                out["cost_per_box"], out["cost_per_bunch"], out["cost_per_stem"],
                out["suggested_sell_per_box"], out["suggested_sell_per_bunch"], out["suggested_sell_per_stem"],
            ),
        )

    return JSONResponse(out)


@app.get("/history")
def history(limit: int = 50):
    limit = 1 if limit < 1 else (200 if limit > 200 else limit)

    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM history ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    data = []
    for r in rows:
        data.append(
            {
                "id": r["id"],
                "created_at": r["created_at"],
                "product_name": r["product_name"],
                "origin": r["origin"],
                "awb": r["awb"],
                "box_type": r["box_type"],
                "boxes": r["boxes"],
                "cost_per_box": r["cost_per_box"],
                "suggested_sell_per_box": r["suggested_sell_per_box"],
            }
        )
    return JSONResponse(data)


@app.get("/export.csv")
def export_csv():
    with db() as conn:
        rows = conn.execute("SELECT * FROM history ORDER BY id DESC").fetchall()

    output = io.StringIO()
    writer = csv.writer(output)

    if rows:
        writer.writerow(rows[0].keys())
        for r in rows:
            writer.writerow([r[k] for k in r.keys()])
    else:
        writer.writerow(["no_data"])

    output.seek(0)
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode("utf-8")),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=flower_landed_cost_history.csv"},
    )
