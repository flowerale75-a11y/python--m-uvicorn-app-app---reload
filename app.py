from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import os
import sqlite3
from datetime import datetime
import csv
import io
import json
from typing import Optional, Dict, Any, List

app = FastAPI(title="Flower Landed Cost")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DB_PATH = os.path.join(BASE_DIR, "data.db")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Default ground freight Miami -> NY per box type (your existing mapping)
GROUND_COST_BY_BOX = {"FB": 6.0, "HB": 16.0, "QB": 8.0}


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_column(conn: sqlite3.Connection, table: str, col: str, col_def: str) -> None:
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")


def init_db() -> None:
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS presets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS calc_history_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,

                finca TEXT NOT NULL,
                origen TEXT NOT NULL,
                producto TEXT NOT NULL,
                awb TEXT NOT NULL,

                box_type TEXT NOT NULL,
                boxes INTEGER NOT NULL,

                bunch_per_box REAL NOT NULL,
                stems_per_bunch INTEGER NOT NULL,
                stems_per_box REAL NOT NULL,

                price_per_bunch REAL NOT NULL,
                stem_price REAL NOT NULL,
                farm_cost_per_box REAL NOT NULL,

                peso_kilo REAL NOT NULL,
                rate REAL NOT NULL,
                rate_kilo REAL NOT NULL,

                duties REAL NOT NULL,
                arancel REAL NOT NULL,

                admin_fee_per_box REAL NOT NULL,
                broker_fee_per_box REAL NOT NULL,
                extra_cost_manual REAL NOT NULL,
                extra_cost_total REAL NOT NULL,

                landed_price_miami REAL NOT NULL,
                freight_mia_ny REAL NOT NULL,
                landed_price_nyc REAL NOT NULL,

                bunch_real_price REAL NOT NULL,
                total_investment REAL NOT NULL,

                sell_35_per_bunch REAL NOT NULL,
                sell_40_per_bunch REAL NOT NULL,

                payload_json TEXT NOT NULL,
                result_json TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS product_catalog (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_name TEXT NOT NULL UNIQUE,
                default_origen TEXT NOT NULL,
                default_box_type TEXT NOT NULL,
                default_bunch_per_box REAL NOT NULL,
                default_stems_per_bunch INTEGER NOT NULL,
                default_peso_kilo REAL NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

        # Safe migrations for old dbs
        ensure_column(conn, "calc_history_v2", "updated_at", "TEXT")

        # ✅ NEW: shipment_id so you can group totals per shipment
        ensure_column(conn, "calc_history_v2", "shipment_id", "TEXT")

        # Helpful index for faster grouping
        conn.execute("CREATE INDEX IF NOT EXISTS idx_calc_history_shipment_id ON calc_history_v2(shipment_id)")

        conn.commit()


init_db()


class LineInput(BaseModel):
    finca: str = Field(min_length=1, default="Liberty")
    origen: str = Field(min_length=1, default="MIA")
    producto: str = Field(min_length=1, default="Roses")
    awb: str = Field(default="")

    # ✅ NEW (optional): if you want a custom grouping key, otherwise AWB is used
    shipment_id: str = Field(default="")

    box_type: str = Field(default="FB")
    boxes: int = Field(ge=1, default=1)

    bunch_per_box: float = Field(gt=0, default=20)
    stems_per_bunch: int = Field(ge=1, default=10)

    price_per_bunch: float = Field(ge=0, default=0)

    peso_kilo: float = Field(ge=0, default=28)
    rate: float = Field(ge=0, default=2.5)

    duties_pct: float = Field(ge=0, default=7)
    arancel_pct: float = Field(ge=0, default=10)

    # $5 fee is TOTAL per shipment
    admin_fee_total: float = Field(ge=0, default=5)

    # total boxes in the whole shipment (used to allocate admin fee per box)
    boxes_in_shipment: int = Field(ge=1, default=1)

    # broker fee in your system currently is per-box (keep as you coded)
    broker_fee_per_box: float = Field(ge=0, default=0)

    extra_cost_per_box: float = Field(ge=0, default=0)

    freight_mia_ny_override: float = Field(ge=0, default=0)


class PresetCreate(BaseModel):
    name: str = Field(min_length=1)
    payload: LineInput


class ProductCatalogCreate(BaseModel):
    product_name: str = Field(min_length=1)
    default_origen: str = Field(min_length=1, default="MIA")
    default_box_type: str = Field(min_length=1, default="FB")
    default_bunch_per_box: float = Field(gt=0, default=20)
    default_stems_per_bunch: int = Field(ge=1, default=10)
    default_peso_kilo: float = Field(ge=0, default=28)


class UpdateHistoryRequest(BaseModel):
    history_id: int
    payload: LineInput


@app.get("/", response_class=HTMLResponse)
def home():
    index_path = os.path.join(STATIC_DIR, "index.html")
    with open(index_path, "r", encoding="utf-8") as f:
        return f.read()


def _normalize_shipment_id(d: LineInput) -> str:
    """
    Grouping key for totals per shipment.

    Priority:
      1) d.shipment_id if provided
      2) d.awb if provided
      3) fallback: date + origen
    """
    if (d.shipment_id or "").strip():
        return d.shipment_id.strip()
    if (d.awb or "").strip():
        return d.awb.strip()
    # fallback grouping if user doesn't use AWB
    return f"{datetime.now().strftime('%Y-%m-%d')}-{d.origen.strip().upper()}"


def compute_line(d: LineInput) -> dict:
    box_type = d.box_type.upper()
    shipment_id = _normalize_shipment_id(d)

    stems_per_box = d.bunch_per_box * d.stems_per_bunch
    stem_price = (d.price_per_bunch / d.stems_per_bunch) if d.stems_per_bunch else 0.0

    # Flowers cost per box (invoice value)
    farm_cost_per_box = d.price_per_bunch * d.bunch_per_box

    # Freight MIA per box (kg * rate)
    rate_kilo = d.peso_kilo * d.rate

    duties = rate_kilo * (d.duties_pct / 100)
    arancel = rate_kilo * (d.arancel_pct / 100)

    # Admin fee is TOTAL per shipment => allocate per box using boxes_in_shipment
    admin_fee_per_box = d.admin_fee_total / d.boxes_in_shipment if d.boxes_in_shipment else 0.0

    # Extra total per box = allocated admin + broker per box + manual extra
    extra_cost_total = admin_fee_per_box + d.broker_fee_per_box + d.extra_cost_per_box

    landed_price_miami = farm_cost_per_box + rate_kilo + duties + arancel + extra_cost_total

    default_freight = GROUND_COST_BY_BOX.get(box_type, 0.0)
    freight_mia_ny = d.freight_mia_ny_override if d.freight_mia_ny_override > 0 else default_freight

    landed_price_nyc = landed_price_miami + freight_mia_ny

    bunch_real_price = landed_price_nyc / d.bunch_per_box

    # ✅ THIS ALREADY multiplies by boxes (line total investment)
    total_investment = landed_price_nyc * d.boxes

    sell_35 = bunch_real_price / (1 - 0.35) if (1 - 0.35) else 0.0
    sell_40 = bunch_real_price / (1 - 0.40) if (1 - 0.40) else 0.0

    # ✅ NEW: line totals separated (so shipment totals are easy + transparent)
    flowers_total_line = farm_cost_per_box * d.boxes
    airfreight_total_line = rate_kilo * d.boxes
    duties_total_line = duties * d.boxes
    arancel_total_line = arancel * d.boxes
    extras_total_line = extra_cost_total * d.boxes
    ground_total_line = freight_mia_ny * d.boxes

    transport_total_line = airfreight_total_line + duties_total_line + arancel_total_line + extras_total_line + ground_total_line
    grand_total_line = flowers_total_line + transport_total_line

    return {
        "SHIPMENT_ID": shipment_id,

        "FINCA": d.finca,
        "ORIGEN": d.origen,
        "PRODUCTO": d.producto,
        "AWB": d.awb,
        "BOX_TYPE": box_type,
        "#BOXES": d.boxes,

        "BUNCH_PER_BOX": float(d.bunch_per_box),
        "STEMS_PER_BUNCH": int(d.stems_per_bunch),
        "STEMS_PER_BOX": float(stems_per_box),

        "PRICE_PER_BUNCH": float(d.price_per_bunch),
        "STEM_PRICE": float(stem_price),
        "FARM_COST_PER_BOX": float(farm_cost_per_box),

        "PESO_KILO": float(d.peso_kilo),
        "RATE": float(d.rate),
        "RATE_X_KILO": float(rate_kilo),

        "DUTIES": float(duties),
        "ARANCEL": float(arancel),

        "ADMIN_FEE_PER_BOX": float(admin_fee_per_box),
        "BROKER_FEE_PER_BOX": float(d.broker_fee_per_box),
        "EXTRA_COST_MANUAL": float(d.extra_cost_per_box),
        "EXTRA_COST_TOTAL": float(extra_cost_total),

        "LANDED_PRICE_MIAMI": float(landed_price_miami),
        "FREIGHT_MIAMI_TO_NY": float(freight_mia_ny),
        "LANDED_PRICE_NYC": float(landed_price_nyc),

        "BUNCH_REAL_PRICE": float(bunch_real_price),
        "TOTAL_INVESTMENT": float(total_investment),

        "MARGIN_35_SELL_PER_BUNCH": float(sell_35),
        "MARGIN_40_SELL_PER_BUNCH": float(sell_40),

        # ✅ New line totals (separated)
        "LINE_TOTALS": {
            "flowers_total": round(flowers_total_line, 2),
            "transport_total": round(transport_total_line, 2),
            "transport_breakdown": {
                "airfreight_total": round(airfreight_total_line, 2),
                "duties_total": round(duties_total_line, 2),
                "arancel_total": round(arancel_total_line, 2),
                "extras_total": round(extras_total_line, 2),
                "ground_mia_ny_total": round(ground_total_line, 2),
            },
            "grand_total": round(grand_total_line, 2),
        },

        "payload": d.model_dump(),
    }


def insert_history(payload: LineInput, result: dict) -> int:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    payload_json = payload.model_dump_json()
    result_json = json.dumps(result, ensure_ascii=False)

    cols = [
        "created_at", "updated_at",
        "shipment_id",
        "finca", "origen", "producto", "awb",
        "box_type", "boxes",
        "bunch_per_box", "stems_per_bunch", "stems_per_box",
        "price_per_bunch", "stem_price", "farm_cost_per_box",
        "peso_kilo", "rate", "rate_kilo",
        "duties", "arancel",
        "admin_fee_per_box", "broker_fee_per_box", "extra_cost_manual", "extra_cost_total",
        "landed_price_miami", "freight_mia_ny", "landed_price_nyc",
        "bunch_real_price", "total_investment",
        "sell_35_per_bunch", "sell_40_per_bunch",
        "payload_json", "result_json"
    ]

    values = [
        now, None,
        result["SHIPMENT_ID"],
        result["FINCA"], result["ORIGEN"], result["PRODUCTO"], result["AWB"],
        result["BOX_TYPE"], result["#BOXES"],
        result["BUNCH_PER_BOX"], result["STEMS_PER_BUNCH"], result["STEMS_PER_BOX"],
        result["PRICE_PER_BUNCH"], result["STEM_PRICE"], result["FARM_COST_PER_BOX"],
        result["PESO_KILO"], result["RATE"], result["RATE_X_KILO"],
        result["DUTIES"], result["ARANCEL"],
        result["ADMIN_FEE_PER_BOX"], result["BROKER_FEE_PER_BOX"], result["EXTRA_COST_MANUAL"], result["EXTRA_COST_TOTAL"],
        result["LANDED_PRICE_MIAMI"], result["FREIGHT_MIAMI_TO_NY"], result["LANDED_PRICE_NYC"],
        result["BUNCH_REAL_PRICE"], result["TOTAL_INVESTMENT"],
        result["MARGIN_35_SELL_PER_BUNCH"], result["MARGIN_40_SELL_PER_BUNCH"],
        payload_json, result_json
    ]

    placeholders = ",".join(["?"] * len(values))
    sql = f"INSERT INTO calc_history_v2 ({','.join(cols)}) VALUES ({placeholders})"

    with get_conn() as conn:
        cur = conn.execute(sql, values)
        conn.commit()
        return int(cur.lastrowid)


def update_history_row(history_id: int, payload: LineInput, result: dict) -> bool:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    payload_json = payload.model_dump_json()
    result_json = json.dumps(result, ensure_ascii=False)

    cols = [
        ("updated_at", now),
        ("shipment_id", result["SHIPMENT_ID"]),
        ("finca", result["FINCA"]),
        ("origen", result["ORIGEN"]),
        ("producto", result["PRODUCTO"]),
        ("awb", result["AWB"]),
        ("box_type", result["BOX_TYPE"]),
        ("boxes", result["#BOXES"]),
        ("bunch_per_box", result["BUNCH_PER_BOX"]),
        ("stems_per_bunch", result["STEMS_PER_BUNCH"]),
        ("stems_per_box", result["STEMS_PER_BOX"]),
        ("price_per_bunch", result["PRICE_PER_BUNCH"]),
        ("stem_price", result["STEM_PRICE"]),
        ("farm_cost_per_box", result["FARM_COST_PER_BOX"]),
        ("peso_kilo", result["PESO_KILO"]),
        ("rate", result["RATE"]),
        ("rate_kilo", result["RATE_X_KILO"]),
        ("duties", result["DUTIES"]),
        ("arancel", result["ARANCEL"]),
        ("admin_fee_per_box", result["ADMIN_FEE_PER_BOX"]),
        ("broker_fee_per_box", result["BROKER_FEE_PER_BOX"]),
        ("extra_cost_manual", result["EXTRA_COST_MANUAL"]),
        ("extra_cost_total", result["EXTRA_COST_TOTAL"]),
        ("landed_price_miami", result["LANDED_PRICE_MIAMI"]),
        ("freight_mia_ny", result["FREIGHT_MIAMI_TO_NY"]),
        ("landed_price_nyc", result["LANDED_PRICE_NYC"]),
        ("bunch_real_price", result["BUNCH_REAL_PRICE"]),
        ("total_investment", result["TOTAL_INVESTMENT"]),
        ("sell_35_per_bunch", result["MARGIN_35_SELL_PER_BUNCH"]),
        ("sell_40_per_bunch", result["MARGIN_40_SELL_PER_BUNCH"]),
        ("payload_json", payload_json),
        ("result_json", result_json),
    ]

    set_clause = ", ".join([f"{c[0]}=?" for c in cols])
    values = [c[1] for c in cols] + [history_id]

    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE calc_history_v2 SET {set_clause} WHERE id = ?",
            values
        )
        conn.commit()
        return cur.rowcount > 0


# ------------------ Core endpoints ------------------
@app.post("/calculate")
def calculate(d: LineInput):
    result = compute_line(d)
    hid = insert_history(d, result)
    result["history_id"] = hid

    # ✅ Also include updated shipment totals in the response
    result["SHIPMENT_SUMMARY"] = shipment_summary_internal(result["SHIPMENT_ID"])
    return result


@app.post("/history/update")
def history_update(req: UpdateHistoryRequest):
    result = compute_line(req.payload)
    ok = update_history_row(req.history_id, req.payload, result)
    return {
        "updated": ok,
        "history_id": req.history_id,
        "result": result,
        "SHIPMENT_SUMMARY": shipment_summary_internal(result["SHIPMENT_ID"]),
    }


@app.delete("/history/{history_id}")
def history_delete(history_id: int):
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM calc_history_v2 WHERE id = ?", (history_id,))
        conn.commit()
        return {"deleted": cur.rowcount > 0, "history_id": history_id}


# ------------------ Shipment Totals ------------------
def shipment_summary_internal(shipment_id: str) -> Dict[str, Any]:
    """
    Computes totals for one shipment_id from the stored per-box fields.

    Transportation includes:
      - air freight (rate_kilo)
      - duties
      - arancel
      - extra_cost_total (allocated admin + broker + manual)
      - freight_mia_ny (ground)
    Flowers investment = farm_cost_per_box * boxes
    Grand total = flowers + transport
    """
    shipment_id = (shipment_id or "").strip()
    if not shipment_id:
        return {"found": False, "error": "shipment_id is required"}

    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, created_at, finca, origen, producto, awb, shipment_id,
                   box_type, boxes,
                   farm_cost_per_box, rate_kilo, duties, arancel, extra_cost_total, freight_mia_ny,
                   landed_price_nyc, total_investment
            FROM calc_history_v2
            WHERE shipment_id = ?
            ORDER BY id ASC
            """,
            (shipment_id,),
        ).fetchall()

    if not rows:
        return {"found": False, "shipment_id": shipment_id, "lines": 0}

    flowers_total = 0.0
    airfreight_total = 0.0
    duties_total = 0.0
    arancel_total = 0.0
    extras_total = 0.0
    ground_total = 0.0
    grand_total = 0.0
    boxes_total = 0

    for r in rows:
        b = int(r["boxes"])
        boxes_total += b

        flowers_total += float(r["farm_cost_per_box"]) * b
        airfreight_total += float(r["rate_kilo"]) * b
        duties_total += float(r["duties"]) * b
        arancel_total += float(r["arancel"]) * b
        extras_total += float(r["extra_cost_total"]) * b
        ground_total += float(r["freight_mia_ny"]) * b

        # stored already as landed * boxes
        grand_total += float(r["total_investment"])

    transport_total = airfreight_total + duties_total + arancel_total + extras_total + ground_total

    return {
        "found": True,
        "shipment_id": shipment_id,
        "lines": len(rows),
        "boxes_total": boxes_total,
        "totals": {
            "total_investment_flowers": round(flowers_total, 2),
            "total_transportation": round(transport_total, 2),
            "grand_total": round(grand_total, 2),
        },
        "transportation_breakdown": {
            "air_freight_total": round(airfreight_total, 2),
            "duties_total": round(duties_total, 2),
            "arancel_total": round(arancel_total, 2),
            "extras_total": round(extras_total, 2),  # includes allocated admin + broker + manual
            "ground_mia_ny_total": round(ground_total, 2),
        }
    }


@app.get("/shipment/summary/{shipment_id}")
def shipment_summary(shipment_id: str):
    # Public API response
    return shipment_summary_internal(shipment_id)


@app.get("/shipments")
def list_shipments(limit: int = 20):
    """
    Returns recent shipment_ids with totals so your UI can show a "shipments list".
    """
    limit = max(1, min(200, int(limit)))

    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT shipment_id,
                   MAX(created_at) AS last_created_at,
                   COUNT(*) AS lines,
                   SUM(boxes) AS boxes_total,
                   SUM(farm_cost_per_box * boxes) AS flowers_total,
                   SUM((rate_kilo + duties + arancel + extra_cost_total + freight_mia_ny) * boxes) AS transport_total,
                   SUM(total_investment) AS grand_total
            FROM calc_history_v2
            WHERE shipment_id IS NOT NULL AND shipment_id != ''
            GROUP BY shipment_id
            ORDER BY last_created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    out = []
    for r in rows:
        out.append({
            "shipment_id": r["shipment_id"],
            "last_created_at": r["last_created_at"],
            "lines": int(r["lines"]),
            "boxes_total": int(r["boxes_total"] or 0),
            "total_investment_flowers": round(float(r["flowers_total"] or 0.0), 2),
            "total_transportation": round(float(r["transport_total"] or 0.0), 2),
            "grand_total": round(float(r["grand_total"] or 0.0), 2),
        })

    return {"shipments": out}


# ------------------ Presets ------------------
@app.get("/presets")
def list_presets():
    with get_conn() as conn:
        rows = conn.execute("SELECT id, name, created_at FROM presets ORDER BY id DESC").fetchall()
        return {"presets": [dict(r) for r in rows]}


@app.get("/presets/{preset_id}")
def get_preset(preset_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT id, name, payload_json, created_at FROM presets WHERE id = ?", (preset_id,)).fetchone()
        if not row:
            return {"error": "Preset not found"}
        return dict(row)


@app.post("/presets")
def create_or_replace_preset(p: PresetCreate):
    payload_json = p.payload.model_dump_json()
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO presets (name, payload_json, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                payload_json = excluded.payload_json,
                created_at = excluded.created_at
            """,
            (p.name.strip(), payload_json, now),
        )
        conn.commit()
        row = conn.execute("SELECT id, name, created_at FROM presets WHERE name = ?", (p.name.strip(),)).fetchone()
        return {"saved": True, "preset": dict(row)}


@app.delete("/presets/{preset_id}")
def delete_preset(preset_id: int):
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM presets WHERE id = ?", (preset_id,))
        conn.commit()
        return {"deleted": cur.rowcount > 0, "preset_id": preset_id}


# ------------------ Product Catalog ------------------
@app.get("/catalog/products")
def list_products():
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, product_name, default_origen, default_box_type,
                   default_bunch_per_box, default_stems_per_bunch, default_peso_kilo, created_at
            FROM product_catalog
            ORDER BY product_name ASC
            """
        ).fetchall()
        return {"products": [dict(r) for r in rows]}


@app.get("/catalog/products/lookup")
def lookup_product(name: str):
    name = (name or "").strip()
    if not name:
        return {"found": False}

    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT id, product_name, default_origen, default_box_type,
                   default_bunch_per_box, default_stems_per_bunch, default_peso_kilo
            FROM product_catalog
            WHERE lower(product_name) = lower(?)
            """,
            (name,),
        ).fetchone()

        if not row:
            return {"found": False}

        return {"found": True, "product": dict(row)}


@app.post("/catalog/products")
def upsert_product(p: ProductCatalogCreate):
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO product_catalog (
                product_name, default_origen, default_box_type,
                default_bunch_per_box, default_stems_per_bunch, default_peso_kilo,
                created_at
            )
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(product_name) DO UPDATE SET
                default_origen = excluded.default_origen,
                default_box_type = excluded.default_box_type,
                default_bunch_per_box = excluded.default_bunch_per_box,
                default_stems_per_bunch = excluded.default_stems_per_bunch,
                default_peso_kilo = excluded.default_peso_kilo,
                created_at = excluded.created_at
            """,
            (
                p.product_name.strip(),
                p.default_origen.strip(),
                p.default_box_type.strip().upper(),
                float(p.default_bunch_per_box),
                int(p.default_stems_per_bunch),
                float(p.default_peso_kilo),
                now,
            ),
        )
        conn.commit()

    return {"saved": True, "product_name": p.product_name.strip()}


# ------------------ History & Export ------------------
@app.get("/history")
def history(limit: int = 50):
    limit = max(1, min(500, int(limit)))
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, created_at, updated_at, shipment_id, finca, origen, producto, awb, box_type, boxes,
                   landed_price_nyc, bunch_real_price, total_investment, sell_35_per_bunch, sell_40_per_bunch
            FROM calc_history_v2
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return {"history": [dict(r) for r in rows]}


@app.get("/history/{history_id}")
def history_item(history_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, created_at, updated_at, shipment_id, payload_json, result_json FROM calc_history_v2 WHERE id = ?",
            (history_id,),
        ).fetchone()
        if not row:
            return {"error": "Not found"}
        return dict(row)


@app.get("/export/excel.csv")
def export_excel_csv(limit: int = 500):
    limit = max(1, min(5000, int(limit)))
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM calc_history_v2
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    output = io.StringIO()
    writer = csv.writer(output)

    header = [
        "SHIPMENT_ID",
        "FINCA","ORIGEN","PRODUCTO","#BOXES",
        "BUNCH_PER_BOX","STEMS_PER_BUNCH","STEMS_PER_BOX",
        "PRICE_PER_BUNCH","STEM_PRICE",
        "AWB",
        "PESO_KILO","RATE","RATE_X_KILO",
        "DUTIES","ARANCEL","EXTRA_COST",
        "LANDED_PRICE_MIAMI",
        "FREIGHT_MIAMI_TO_NY",
        "LANDED_PRICE_NYC",
        "BUNCH_REAL_PRICE",
        "TOTAL_INVESTMENT",
        "SELL_35_PER_BUNCH",
        "SELL_40_PER_BUNCH",
        "created_at","updated_at","id"
    ]
    writer.writerow(header)

    for r in rows:
        writer.writerow([
            r.get("shipment_id", ""),
            r["finca"], r["origen"], r["producto"], r["boxes"],
            r["bunch_per_box"], r["stems_per_bunch"], r["stems_per_box"],
            r["price_per_bunch"], r["stem_price"],
            r["awb"],
            r["peso_kilo"], r["rate"], r["rate_kilo"],
            r["duties"], r["arancel"], r["extra_cost_total"],
            r["landed_price_miami"],
            r["freight_mia_ny"],
            r["landed_price_nyc"],
            r["bunch_real_price"],
            r["total_investment"],
            r["sell_35_per_bunch"],
            r["sell_40_per_bunch"],
            r["created_at"], r["updated_at"], r["id"]
        ])

    return {"filename": "excel_export.csv", "csv": output.getvalue()}


@app.get("/health")
def health():
    return {"status": "ok"}
