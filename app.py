from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

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

    kg_per_box: confloat(ge=0) = 0.0

    invoice_per_box: confloat(ge=0) = 0.0  # invoice cost (farm price) per box


class ShipmentIn(BaseModel):
    awb: str = ""
    rate_per_kg: confloat(ge=0) = 0.0

    # Duties/taxes based on invoice total (default 22% but editable)
    duty_rate: confloat(ge=0, le=1) = 0.22

    # Miami -> NY trucking total allocated by "box size weight"
    miami_to_ny_total: confloat(ge=0) = 0.0

    # Box-size weights (editable)
    box_weights: Dict[str, confloat(ge=0)] = Field(
        default_factory=lambda: {"FB": 1.0, "HB": 0.5, "QB": 0.25}
    )

    # Margins editable (defaults 35% & 40%)
    margin_a: confloat(ge=0, le=0.95) = 0.35
    margin_b: confloat(ge=0, le=0.95) = 0.40

    lines: List[LineIn] = Field(default_factory=list)


def r4(x: float) -> float:
    return float(round(x, 4))


def safe_div(a: float, b: float) -> float:
    return a / b if b and b != 0 else 0.0


# -----------------------------
# Excel-like calculation engine
# -----------------------------
def calculate_shipment(s: ShipmentIn) -> Dict[str, Any]:
    # Totals
    total_kilos = 0.0
    total_invoice = 0.0
    total_weighted_boxes = 0.0

    # Precompute per-line basics
    basics: List[Dict[str, Any]] = []
    for ln in s.lines:
        boxes = int(ln.boxes)
        kg_line = float(ln.kg_per_box) * boxes
        inv_line = float(ln.invoice_per_box) * boxes

        w = float(s.box_weights.get(ln.box_type.upper().strip(), 1.0))
        weighted_boxes = boxes * w

        stems_per_box = int(ln.bunch_per_box) * int(ln.stems_per_bunch)

        basics.append(
            {
                "finca": ln.finca,
                "origin": ln.origin,
                "product": ln.product,
                "box_type": ln.box_type.upper().strip(),
                "boxes": boxes,
                "bunch_per_box": int(ln.bunch_per_box),
                "stems_per_bunch": int(ln.stems_per_bunch),
                "stems_per_box": stems_per_box,
                "kg_per_box": float(ln.kg_per_box),
                "kg_line": kg_line,
                "invoice_per_box": float(ln.invoice_per_box),
                "invoice_line": inv_line,
                "weight_factor": w,
                "weighted_boxes": weighted_boxes,
            }
        )

        total_kilos += kg_line
        total_invoice += inv_line
        total_weighted_boxes += weighted_boxes

    freight_total = total_kilos * float(s.rate_per_kg)
    duties_total = total_invoice * float(s.duty_rate)

    # Allocate:
    # 1) Freight by kilos
    # 2) Duties by invoice
    # 3) Miami->NY by weighted box size
    out_lines: List[Dict[str, Any]] = []

    for b in basics:
        freight_alloc = safe_div(b["kg_line"], total_kilos) * freight_total
        duty_alloc = safe_div(b["invoice_line"], total_invoice) * duties_total
        miami_alloc = safe_div(b["weighted_boxes"], total_weighted_boxes) * float(s.miami_to_ny_total)

        landed_line = b["invoice_line"] + freight_alloc + duty_alloc + miami_alloc

        cost_per_box = safe_div(landed_line, b["boxes"])
        cost_per_bunch = safe_div(cost_per_box, b["bunch_per_box"])
        cost_per_stem = safe_div(cost_per_box, b["stems_per_box"])

        # Suggested sell prices at both margins
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
        "total_kilos": r4(total_kilos),
        "rate_per_kg": r4(float(s.rate_per_kg)),
        "freight_total": r4(freight_total),
        "total_invoice": r4(total_invoice),
        "duty_rate": r4(float(s.duty_rate)),
        "duties_total": r4(duties_total),
        "miami_to_ny_total": r4(float(s.miami_to_ny_total)),
        "total_weighted_boxes": r4(total_weighted_boxes),
        "margin_a": r4(float(s.margin_a)),
        "margin_b": r4(float(s.margin_b)),
        "grand_landed_total": r4(total_invoice + freight_total + duties_total + float(s.miami_to_ny_total)),
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
    .wrap{ max-width:1600px; margin:18px auto; padding:0 12px; }
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
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Flower Landed Cost (Excel Model: Shipment Header + Lines)</h1>

    <div class="card">
      <div class="muted">
        Rules: Freight by kilos • Duties by invoice (default 22%) • Miami→NY by weighted box size (FB/HB/QB) • Margins editable.
      </div>

      <div class="grid" style="margin-top:8px;">
        <div>
          <label>AWB</label>
          <input id="awb" placeholder="AWB-123456" />
        </div>
        <div>
          <label>Rate per KG ($)</label>
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
          <label>Margin A (default 0.35)</label>
          <input id="margin_a" type="number" step="0.01" value="0.35" />
        </div>
        <div>
          <label>Margin B (default 0.40)</label>
          <input id="margin_b" type="number" step="0.01" value="0.40" />
        </div>
      </div>

      <div class="grid" style="margin-top:8px;">
        <div>
          <label>Weight FB</label>
          <input id="w_fb" type="number" step="0.01" value="1.0" />
        </div>
        <div>
          <label>Weight HB</label>
          <input id="w_hb" type="number" step="0.01" value="0.5" />
        </div>
        <div>
          <label>Weight QB</label>
          <input id="w_qb" type="number" step="0.01" value="0.25" />
        </div>
        <div style="grid-column: span 3;">
          <label>Tip</label>
          <div class="muted">If Grand Transportation charges by “box size”, adjust FB/HB/QB weights here.</div>
