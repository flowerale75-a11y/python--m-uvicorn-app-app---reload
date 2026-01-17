from __future__ import annotations

from typing import Any, Dict, List
from io import BytesIO

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, conint, confloat

from openpyxl import Workbook

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

    # NEW: price per bunch (this is what you want to enter)
    price_per_bunch: confloat(ge=0) = 0.0


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
    total_kilos = 0.0
    total_invoice = 0.0
    total_weighted_boxes = 0.0
    total_boxes = 0

    basics: List[Dict[str, Any]] = []
    for ln in s.lines:
        bt = norm_box_type(ln.box_type)
        boxes = int(ln.boxes)

        # KG per box auto-fill from defaults if 0
        kg_default = float(s.kg_defaults.get(bt, 0.0))
        kg_per_box = float(ln.kg_per_box) if float(ln.kg_per_box) > 0 else kg_default

        # NEW: invoice derived from price per bunch
        price_per_bunch = float(ln.price_per_bunch)
        invoice_per_box = price_per_bunch * int(ln.bunch_per_box)
        invoice_line = invoice_per_box * boxes

        kg_line = kg_per_box * boxes

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
                "price_per_bunch": price_per_bunch,
                "invoice_per_box": invoice_per_box,
                "invoice_line": invoice_line,
                "weight_factor": w,
                "weighted_boxes": weighted_boxes,
            }
        )

        total_boxes += boxes
        total_kilos += kg_line
        total_invoice += invoice_line
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


def build_excel(payload: ShipmentIn, result: Dict[str, Any]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Landed Cost"

    t = result["totals"]

    # Header
    ws.append(["AWB", result.get("awb", "")])
    ws.append(["Rate per KG", t["rate_per_kg"]])
    ws.append(["Duty Rate", t["duty_rate"]])
    ws.append(["Miami -> NY Total", t["miami_to_ny_total"]])
    ws.append(["Margin A", t["margin_a"]])
    ws.append(["Margin B", t["margin_b"]])
    ws.append(["Default KG/FB", t["kg_defaults"].get("FB", "")])
    ws.append(["Default KG/HB", t["kg_defaults"].get("HB", "")])
    ws.append(["Default KG/QB", t["kg_defaults"].get("QB", "")])
    ws.append(["Weight FB", t["box_weights"].get("FB", "")])
    ws.append(["Weight HB", t["box_weights"].get("HB", "")])
    ws.append(["Weight QB", t["box_weights"].get("QB", "")])

    ws.append([])
    ws.append(["TOTAL BOXES", t["total_boxes"]])
    ws.append(["TOTAL WEIGHTED BOXES", t["total_weighted_boxes"]])
    ws.append(["TOTAL KILOS", t["total_kilos"]])
    ws.append(["TOTAL INVOICE", t["total_invoice"]])
    ws.append(["FREIGHT TOTAL", t["freight_total"]])
    ws.append(["DUTY TOTAL", t["duties_total"]])
    ws.append(["GRAND LANDED TOTAL", t["grand_landed_total"]])
    ws.append([])

    # Table header
    ws.append([
        "#",
        "FINCA","ORIGEN","PRODUCT",
        "BOX TYPE","BOXES",
        "BUNCH/BOX","STEMS/BUNCH","STEMS/BOX",
        "KG/BOX","KG LINE",
        "PRICE/BUNCH",
        "INVOICE/BOX","INVOICE LINE",
        "FREIGHT ALLOC","DUTY ALLOC","MIAMI ALLOC",
        "LANDED LINE",
        "COST/BOX","COST/BUNCH","COST/STEM",
        "SELL/BOX @A","SELL/BOX @B",
        "SELL/BUNCH @A","SELL/BUNCH @B"
    ])

    # Rows
    for i, ln in enumerate(result["lines"], start=1):
        ws.append([
            i,
            ln["finca"], ln["origin"], ln["product"],
            ln["box_type"], ln["boxes"],
            ln["bunch_per_box"], ln["stems_per_bunch"], ln["stems_per_box"],
            ln["kg_per_box"], ln["kg_line"],
            ln["price_per_bunch"],
            ln["invoice_per_box"], ln["invoice_line"],
            ln["freight_alloc"], ln["duty_alloc"], ln["miami_alloc"],
            ln["landed_line"],
            ln["cost_per_box"], ln["cost_per_bunch"], ln["cost_per_stem"],
            ln["sell_box_m1"], ln["sell_box_m2"],
            ln["sell_bunch_m1"], ln["sell_bunch_m2"]
        ])

    bio = BytesIO()
    wb.save(bio)
    return bio.getvalue()


# -----------------------------
# API
# -----------------------------
@app.post("/calculate_shipment")
def api_calculate_shipment(payload: ShipmentIn):
    return JSONResponse(calculate_shipment(payload))


@app.post("/export.xlsx")
def export_xlsx(payload: ShipmentIn):
    result = calculate_shipment(payload)
    content = build_excel(payload, result)
    return StreamingResponse(
        BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=landed_cost.xlsx"},
    )


# -----------------------------
# UI
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
    <h1>Flower Landed Cost (Shipment Header + Lines)</h1>

    <div class="card">
      <div class="muted">
        Freight by kilos • Duty by invoice (22% default) • Miami→NY by weighted box size •
        <span class="pill
