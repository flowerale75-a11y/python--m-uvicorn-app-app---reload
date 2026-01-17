from __future__ import annotations

from typing import Any, Dict, List

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, conint, confloat

app = FastAPI(title="Flower Landed Cost - Excel Model (Shipment + Lines)")


# -----------------------------
# Models
# -----------------------------
class LineIn(BaseModel):
    finca: str = ""
    origin: str = ""
    product: str = ""

    box_type: str = "HB"  # FB/HB/QB
    boxes: conint(ge=0) = 0

    bunch_per_box: conint(ge=1) = 12
    stems_per_bunch: conint(ge=1) = 25

    # If 0, backend will auto-fill from shipment kg_defaults by box_type
    kg_per_box: confloat(ge=0) = 0.0

    # Invoice cost per box
    invoice_per_box: confloat(ge=0) = 0.0


class ShipmentIn(BaseModel):
    awb: str = ""
    rate_per_kg: confloat(ge=0) = 0.0

    # Duty/taxes based on invoice total (default 22% but editable)
    duty_rate: confloat(ge=0, le=1) = 0.22

    # Miami -> NY trucking total allocated by "box size weight"
    miami_to_ny_total: confloat(ge=0) = 0.0

    # Box-size weights (editable)
    box_weights: Dict[str, confloat(ge=0)] = Field(
        default_factory=lambda: {"FB": 1.0, "HB": 0.5, "QB": 0.25}
    )

    # KG defaults by box type (editable)
    kg_defaults: Dict[str, confloat(ge=0)] = Field(
        default_factory=lambda: {"FB": 30.0, "HB": 15.0, "QB": 7.5}
    )

    # Margins editable
    margin_a: confloat(ge=0, le=0.95) = 0.35
    margin_b: confloat(ge=0, le=0.95) = 0.40

    lines: List[LineIn] = Field(default_factory=list)


def r4(x: float) -> float:
    return float(round(float(x), 4))


def safe_div(a: float, b: float) -> float:
    return a / b if b and b != 0 else 0.0


def norm_box_type(bt: str) -> str:
    bt = (bt or "").strip().upper()
    return bt if bt in ("FB", "HB", "QB") else "FB"


# -----------------------------
# Excel-like calculation engine
# -----------------------------
def calculate_shipment(s: ShipmentIn) -> Dict[str, Any]:
    # Totals
    total_kilos = 0.0
    total_invoice = 0.0
    total_weighted_boxes = 0.0
    total_boxes = 0

    # Precompute per-line basics
    basics: List[Dict[str, Any]] = []
    for ln in s.lines:
        bt = norm_box_type(ln.box_type)
        boxes = int(ln.boxes)

        # KG per box auto-fill from defaults if 0
        kg_default = float(s.kg_defaults.get(bt, 0.0))
        kg_per_box = float(ln.kg_per_box) if float(ln.kg_per_box) > 0 else kg_default

        kg_line = kg_per_box * boxes
        inv_line = float(ln.invoice_per_box) * boxes

        w = float(s.box_weights.get(bt, 1.0))
        weighted_boxes = boxes * w

        stems_per_box = int(ln.bunch_per_box) * int(ln.stems_per_bunch)

        basics.append(
            {
                "finca": ln.finca,
                "origin": ln.origin,
                "product": ln.product,
                "box_type": bt,
                "boxes": boxes,
                "bunch_per_box": int(ln.bunch_per_box),
                "stems_per_bunch": int(ln.stems_per_bunch),
                "stems_per_box": stems_per_box,
                "kg_per_box": kg_per_box,
                "kg_line": kg_line,
                "invoice_per_box": float(ln.invoice_per_box),
                "invoice_line": inv_line,
                "weight_factor": w,
                "weighted_boxes": weighted_boxes,
            }
        )

        total_boxes += boxes
        total_kilos += kg_line
        total_invoice += inv_line
        total_weighted_boxes += weighted_boxes

    freight_total = total_kilos * float(s.rate_per_kg)
    duties_total = total_invoice * float(s.duty_rate)

    out_lines: List[Dict[str, Any]] = []
    for b in basics:
        freight_alloc = safe_div(b["kg_line"], total_kilos) * freight_total
        duty_alloc = safe_div(b["invoice_line"], total_invoice) * duties_total
        miami_alloc = safe_div(b["weighted_boxes"], total_weighted_boxes) * float(s.miami_to_ny_total)

        landed_line = b["invoice_line"] + freight_alloc + duty_alloc + miami_alloc

        cost_per_box = safe_div(landed_line, b["boxes"])
        cost_per_bunch = safe_div(cost_per_box, b["bunch_per_box"])
        cost_per_stem = safe_div(cost_per_box, b["stems_per_box"])

        m1 = float(s.margin_a)
        m2 = float(s.margin_b)

        sell_box_m1 = safe_div(cost_per_box, (1.0 - m1))
        sell_box_m2 = safe_div(cost_per_box, (1.0 - m2))

        sell_bunch_m1 = safe_div(sell_box_m1, b["bunch_per_box"])
        sell_bunch_m2 = safe_div(sell_box_m2, b["bunch_per_box"])

        out_lines.append(
            {
                **b,
                "freight_alloc": r4(freight_alloc),
                "duty_alloc": r4(duty_alloc),
                "miami_alloc": r4(miami_alloc),
                "landed_line": r4(landed_line),
                "cost_per_box": r4(cost_per_box),
                "cost_per_bunch": r4(cost_per_bunch),
                "cost_per_stem": r4(cost_per_stem),
                "sell_box_m1": r4(sell_box_m1),
                "sell_box_m2": r4(sell_box_m2),
                "sell_bunch_m1": r4(sell_bunch_m1),
                "sell_bunch_m2": r4(sell_bunch_m2),
            }
        )

    totals = {
        "total_boxes": int(total_boxes),
        "total_weighted_boxes": r4(total_weighted_boxes),
        "total_kilos": r4(total_kilos),
        "rate_per_kg": r4(float(s.rate_per_kg)),
        "freight_total": r4(freight_total),
        "total_invoice": r4(total_invoice),
        "duty_rate": r4(float(s.duty_rate)),
        "duties_total": r4(duties_total),
        "miami_to_ny_total": r4(float(s.miami_to_ny_total)),
        "margin_a": r4(float(s.margin_a)),
        "margin_b": r4(float(s.margin_b)),
        "grand_landed_total": r4(total_invoice + freight_total + duties_total + float(s.miami_to_ny_total)),
        "box_weights": {k: r4(v) for k, v in s.box_weights.items()},
        "kg_defaults": {k: r4(v) for k, v in s.kg_defaults.items()},
    }

    return {"awb": s.awb, "totals": totals, "lines": out_lines}


# -----------------------------
# API
# -----------------------------
@app.post("/calculate_shipment")
def api_calculate_shipment(payload: ShipmentIn):
    result = calculate_shipment(payload)
    return JSONResponse(result)


# -----------------------------
# UI (Excel-like)
# -----------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    html = r"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Flower Landed Cost (Excel Model)</title>
  <style>
    :root{
      --bg:#eaf4ff; --card:#fff; --text:#0b1a2a; --muted:#4b6075; --border:#cfe2f7;
      --btn:#2b6cb0; --btn2:#1f4f86; --soft:#f7fbff; --danger:#c53030;
    }
    body{ margin:0; font-family:Arial,sans-serif; background:var(--bg); color:var(--text); }
    .wrap{ max-width:1700px; margin:18px auto; padding:0 12px; }
    h1{ margin:0 0 12px 0; font-size:20px; }
    .card{
      background:var(--card); border:1px solid var(--border); border-radius:12px;
      padding:14px; box-shadow:0 2px 8px rgba(0,0,0,.06); margin-bottom:12px;
    }
    label{ display:block; font-size:12px; color:var(--muted); margin:10px 0 6px; }
    input, select{
      width:100%; padding:9px; border:1px solid var(--border); border-radius:10px;
      background:var(--soft); color:var(--text); box-sizing:border-box;
    }
    .grid{ display:grid; grid-template-columns: repeat(6, 1fr); gap:10px; }
    @media (max-width: 1100px){ .grid{ grid-template-columns: repeat(2, 1fr); } }
    .btns{ display:flex; gap:10px; flex-wrap:wrap; margin-top:12px; }
    button{
      padding:10px 14px; border-radius:10px; border:1px solid var(--border);
      background:var(--btn); color:white; cursor:pointer; font-weight:700;
    }
    button.secondary{ background:white; color:var(--btn2); }
    button.danger{ background:var(--danger); color:white; border-color:#f2b8b8; }

    table{ width:100%; border-collapse:collapse; }
    th, td{ padding:7px; border-bottom:1px solid var(--border); font-size:12px; text-align:left; white-space:nowrap; }
    th{ position:sticky; top:0; background:var(--card); z-index:1; }
    .table-wrap{ overflow:auto; border:1px solid var(--border); border-radius:12px; }
    .muted{ color:var(--muted); font-size:12px; }
    .kpis{ display:grid; grid-template-columns: repeat(6, 1fr); gap:10px; margin-top:10px; }
    @media (max-width: 1100px){ .kpis{ grid-template-columns: repeat(2, 1fr); } }
    .kpi{ background:var(--soft); border:1px solid var(--border); border-radius:12px; padding:10px; }
    .kpi .v{ font-size:16px; font-weight:800; margin-top:4px; }
    .num{ text-align:right; }
    .small{ font-size:11px; }
    .pill{ display:inline-block; padding:3px 8px; border-radius:999px; border:1px solid var(--border); background:var(--soft); color:var(--muted); }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Flower Landed Cost (Excel Model: Shipment Header + Lines)</h1>

    <div class="card">
      <div class="muted">
        Rules: Freight by kilos • Duty by invoice (default 22%) • Miami→NY by weighted box size • Margins editable •
        <span class="pill">KG/Box auto-fills from Box Type defaults (editable)</span>
      </div>

      <div class="grid" style="margin-top:8px;">
        <div>
          <label>AWB</label>
          <input id="awb" placeholder="AWB-123456" />
        </div>
        <div>
          <label>Rate per KG ($/kg)</label>
          <input id="rate_per_kg" type="number" step="0.0001" value="0" />
        </div>
        <div>
          <label>Duty rate (invoice %) e.g. 0.22</label>
          <input id="duty_rate" type="number" step="0.0001" value="0.22" />
        </div>
        <div>
          <label>Miami → NY total ($)</label>
          <input id="miami_to_ny_total" type="number" step="0.01" value="0" />
        </div>
        <div>
          <label>Margin A</label>
          <input id="margin_a" type="number" step="0.01" value="0.35" />
        </div>
        <div>
          <label>Margin B</label>
          <input id="margin_b" type="number" step="0.01" value="0.40" />
        </div>
      </div>

      <div class="grid" style="margin-top:8px;">
        <div>
          <label>Miami→NY Weight FB</label>
          <input id="w_fb" type="number" step="0.01" value="1.0" />
        </div>
        <div>
          <label>Miami→NY Weight HB</label>
          <input id="w_hb" type="number" step="0.01" value="0.5" />
        </div>
        <div>
          <label>Miami→NY Weight QB</label>
          <input id="w_qb" type="number" step="0.01" value="0.25" />
        </div>

        <div>
          <label>Default KG/FB</label>
          <input id="kg_fb" type="number" step="0.01" value="30" />
        </div>
        <div>
          <label>Default KG/HB</label>
          <input id="kg_hb" type="number" step="0.01" value="15" />
        </div>
        <div>
          <label>Default KG/QB</label>
          <input id="kg_qb" type="number" step="0.01" value="7.5" />
        </div>
      </div>

      <div class="btns">
        <button onclick="addLine()">Add Line</button>
        <button class="secondary" onclick="addSample()">Add Sample Lines</button>
        <button class="secondary" onclick="clearLines()">Clear Lines</button>
        <button onclick="calculate()">Calculate Shipment</button>
      </div>
    </div>

    <div class="card">
      <h3 style="margin:0 0 8px 0;">Lines (like Excel rows)</h3>
      <div class="table-wrap">
        <table id="lines_table">
          <thead>
            <tr>
              <th>#</th>
              <th>FINCA</th>
              <th>ORIGEN</th>
              <th>PRODUCTO</th>
              <th>BOX TYPE</th>
              <th class="num">BOXES</th>
              <th class="num">BUNCH/BOX</th>
              <th class="num">STEMS/BUNCH</th>
              <th class="num">KG/BOX</th>
              <th class="num">INVOICE/BOX</th>
              <th class="small">Actions</th>
            </tr>
          </thead>
          <tbody id="lines_body"></tbody>
        </table>
      </div>
      <div class="muted" style="margin-top:8px;">
        KG/BOX auto-fills when you change box type. If you type a custom KG/BOX, it becomes an override for that line.
      </div>
    </div>

    <div class="card">
      <h3 style="margin:0 0 8px 0;">Shipment Totals</h3>
      <div class="kpis" id="kpis"></div>
    </div>

    <div class="card">
      <h3 style="margin:0 0 8px 0;">Calculated Output (per line)</h3>
      <div class="table-wrap">
        <table id="out_table">
          <thead>
            <tr>
              <th>#</th>
              <th>PRODUCT</th>
              <th>BOX</th>
              <th class="num">Boxes</th>
              <th class="num">KG Line</th>
              <th class="num">Invoice Line</th>
              <th class="num">Freight Alloc</th>
              <th class="num">Duty Alloc</th>
              <th class="num">Miami Alloc</th>
              <th class="num">Landed Line</th>
              <th class="num">Cost/Box</th>
              <th class="num">Cost/Bunch</th>
              <th class="num">Sell/Box @A</th>
              <th class="num">Sell/Box @B</th>
              <th class="num">Sell/Bunch @A</th>
              <th class="num">Sell/Bunch @B</th>
            </tr>
          </thead>
          <tbody id="out_body"></tbody>
        </table>
      </div>
    </div>

  </div>

<script>
function $(id){ return document.getElementById(id); }
function num(id){ return parseFloat($(id).value || "0"); }
function val(id){ return ($(id).value || "").toString(); }

function kgDefaultForBoxType(bt){
  bt = (bt || "").toUpperCase();
  if(bt === "FB") return num("kg_fb");
  if(bt === "HB") return num("kg_hb");
  if(bt === "QB") return num("kg_qb");
  return num("kg_fb");
}

let lines = [];
// Track whether kg_per_box is overridden by user for each line
let kgOverride = [];

function renderLines(){
  const tbody = $("lines_body");
  tbody.innerHTML = "";
  lines.forEach((ln, idx) => {
    const tr = document.createElement("tr");

    tr.innerHTML = `
      <td>${idx+1}</td>
      <td><input data-i="${idx}" data-k="finca" value="${escapeHtml(ln.finca||"")}" /></td>
      <td><input data-i="${idx}" data-k="origin" value="${escapeHtml(ln.origin||"")}" /></td>
      <td><input data-i="${idx}" data-k="product" value="${escapeHtml(ln.product||"")}" /></td>
      <td>
        <select data-i="${idx}" data-k="box_type">
          <option value="FB" ${ln.box_type==="FB"?"selected":""}>FB</option>
          <option value="HB" ${ln.box_type==="HB"?"selected":""}>HB</option>
          <option value="QB" ${ln.box_type==="QB"?"selected":""}>QB</option>
        </select>
      </td>
      <td class="num"><input data-i="${idx}" data-k="boxes" type="number" min="0" step="1" value="${ln.boxes}" /></td>
      <td class="num"><input data-i="${idx}" data-k="bunch_per_box" type="number" min="1" step="1" value="${ln.bunch_per_box}" /></td>
      <td class="num"><input data-i="${idx}" data-k="stems_per_bunch" type="number" min="1" step="1" value="${ln.stems_per_bunch}" /></td>
      <td class="num"><input data-i="${idx}" data-k="kg_per_box" type="number" min="0" step="0.01" value="${ln.kg_per_box}" /></td>
      <td class="num"><input data-i="${idx}" data-k="invoice_per_box" type="number" min="0" step="0.01" value="${ln.invoice_per_box}" /></td>
      <td class="small">
        <button class="secondary" onclick="resetKg(${idx})" type="button">Reset KG</button>
        <button class="danger" onclick="removeLine(${idx})" type="button">X</button>
      </td>
    `;
    tbody.appendChild(tr);
  });

  // Bind input listeners
  tbody.querySelectorAll("input,select").forEach(el => {
    el.addEventListener("input", onCellEdit);
    el.addEventListener("change", onCellEdit);
  });
}

function onCellEdit(e){
  const el = e.target;
  const idx = parseInt(el.getAttribute("data-i"), 10);
  const key = el.getAttribute("data-k");
  let v = el.value;

  if(key === "boxes" || key === "bunch_per_box" || key === "stems_per_bunch"){
    v = parseInt(v || "0", 10);
  }
  if(key === "kg_per_box" || key === "invoice_per_box"){
    v = parseFloat(v || "0");
  }

  // If box type changes and KG not overridden, auto-fill
  if(key === "box_type"){
    lines[idx][key] = (v || "FB").toUpperCase();
    if(!kgOverride[idx]){
      lines[idx].kg_per_box = kgDefaultForBoxType(lines[idx].box_type);
      renderLines();
      return;
    }
  }

  // If user types KG/BOX, mark override
  if(key === "kg_per_box"){
    kgOverride[idx] = true;
  }

  lines[idx][key] = v;
}

function addLine(){
  lines.push({
    finca: "", origin: "", product: "",
    box_type: "HB",
    boxes: 0,
    bunch_per_box: 12,
    stems_per_bunch: 25,
    kg_per_box: kgDefaultForBoxType("HB"),
    invoice_per_box: 0
  });
  kgOverride.push(false);
  renderLines();
}

function addSample(){
  lines = [
    {finca:"Polo", origin:"ECU", product:"ROSES MONDIAL 60", box_type:"HB", boxes:10, bunch_per_box:12, stems_per_bunch:25, kg_per_box:kgDefaultForBoxType("HB"), invoice_per_box:22},
    {finca:"Queens", origin:"ECU", product:"GYPSOPHILA", box_type:"QB", boxes:20, bunch_per_box:10, stems_per_bunch:10, kg_per_box:kgDefaultForBoxType("QB"), invoice_per_box:18},
    {finca:"Golden", origin:"COL", product:"GREENS", box_type:"FB", boxes:6, bunch_per_box:20, stems_per_bunch:5, kg_per_box:kgDefaultForBoxType("FB"), invoice_per_box:35}
  ];
  kgOverride = [false,false,false];
  renderLines();
}

function clearLines(){
  lines = [];
  kgOverride = [];
  renderLines();
  $("out_body").innerHTML = "";
  $("kpis").innerHTML = "";
}

function removeLine(i){
  lines.splice(i,1);
  kgOverride.splice(i,1);
  renderLines();
}

function resetKg(i){
  kgOverride[i] = false;
  const bt = (lines[i].box_type || "FB").toUpperCase();
  lines[i].kg_per_box = kgDefaultForBoxType(bt);
  renderLines();
}

function buildPayload(){
  return {
    awb: val("awb"),
    rate_per_kg: num("rate_per_kg"),
    duty_rate: num("duty_rate"),
    miami_to_ny_total: num("miami_to_ny_total"),
    box_weights: { FB: num("w_fb"), HB: num("w_hb"), QB: num("w_qb") },
    kg_defaults: { FB: num("kg_fb"), HB: num("kg_hb"), QB: num("kg_qb") },
    margin_a: num("margin_a"),
    margin_b: num("margin_b"),
    lines: lines.map(ln => ({
      finca: ln.finca || "",
      origin: ln.origin || "",
      product: ln.product || "",
      box_type: (ln.box_type || "FB").toUpperCase(),
      boxes: parseInt(ln.boxes || 0, 10),
      bunch_per_box: parseInt(ln.bunch_per_box || 12, 10),
      stems_per_bunch: parseInt(ln.stems_per_bunch || 25, 10),
      kg_per_box: parseFloat(ln.kg_per_box || 0),
      invoice_per_box: parseFloat(ln.invoice_per_box || 0),
    }))
  };
}

async function calculate(){
  const payload = buildPayload();
  const res = await fetch("/calculate_shipment", {
    method: "POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify(payload)
  });
  if(!res.ok){
    const t = await res.text();
    alert("Error: " + t);
    return;
  }
  const data = await res.json();
  renderKPIs(data.totals);
  renderOutput(data.lines, data.totals);
}

function renderKPIs(t){
  const k = $("kpis");
  k.innerHTML = "";
  const items = [
    ["Total Boxes", t.total_boxes],
    ["Weighted Boxes", t.total_weighted_boxes],
    ["Total Kilos", t.total_kilos],
    ["Total Invoice", "$ " + t.total_invoice],
    ["Freight Total", "$ " + t.freight_total],
    ["Duty Total", "$ " + t.duties_total],
    ["Miami→NY Total", "$ " + t.miami_to_ny_total],
    ["Grand Landed", "$ " + t.grand_landed_total],
  ];
  items.forEach(([name, value]) => {
    const div = document.createElement("div");
    div.className = "kpi";
    div.innerHTML = `<div class="muted">${escapeHtml(name)}</div><div class="v">${escapeHtml(String(value))}</div>`;
    k.appendChild(div);
  });
}

function renderOutput(linesOut, totals){
  const tbody = $("out_body");
  tbody.innerHTML = "";
  linesOut.forEach((ln, idx) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${idx+1}</td>
      <td>${escapeHtml(ln.product || "")}</td>
      <td>${escapeHtml(ln.box_type || "")}</td>
      <td class="num">${ln.boxes}</td>
      <td class="num">${ln.kg_line}</td>
      <td class="num">${ln.invoice_line}</td>
      <td class="num">${ln.freight_alloc}</td>
      <td class="num">${ln.duty_alloc}</td>
      <td class="num">${ln.miami_alloc}</td>
      <td class="num">${ln.landed_line}</td>
      <td class="num">${ln.cost_per_box}</td>
      <td class="num">${ln.cost_per_bunch}</td>
      <td class="num">${ln.sell_box_m1}</td>
      <td class="num">${ln.sell_box_m2}</td>
      <td class="num">${ln.sell_bunch_m1}</td>
      <td class="num">${ln.sell_bunch_m2}</td>
    `;
    tbody.appendChild(tr);
  });
}

function escapeHtml(str){
  return (str ?? "").toString()
    .replaceAll("&","&amp;")
    .replaceAll("<","&lt;")
    .replaceAll(">","&gt;")
    .replaceAll('"',"&quot;")
    .replaceAll("'","&#039;");
}

// Start with one empty line
addLine();
</script>
</body>
</html>
"""
    return HTMLResponse(html)
