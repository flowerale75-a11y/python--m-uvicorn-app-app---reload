from __future__ import annotations

from typing import Dict, List
from io import BytesIO

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, conint, confloat
from openpyxl import Workbook

app = FastAPI(title="Flower Landed Cost")


# =========================
# Models
# =========================
class LineIn(BaseModel):
    finca: str = ""
    origin: str = ""
    product: str = ""

    box_type: str = "HB"
    boxes: conint(ge=0) = 0

    bunch_per_box: conint(ge=1) = 12
    stems_per_bunch: conint(ge=1) = 25

    kg_per_box: confloat(ge=0) = 0.0
    price_per_bunch: confloat(ge=0) = 0.0


class ShipmentIn(BaseModel):
    awb: str = ""
    rate_per_kg: confloat(ge=0) = 0.0
    duty_rate: confloat(ge=0, le=1) = 0.22
    miami_to_ny_total: confloat(ge=0) = 0.0

    box_weights: Dict[str, confloat(ge=0)] = Field(
        default_factory=lambda: {"FB": 1.0, "HB": 0.5, "QB": 0.25}
    )
    kg_defaults: Dict[str, confloat(ge=0)] = Field(
        default_factory=lambda: {"FB": 30.0, "HB": 15.0, "QB": 7.5}
    )

    margin_a: confloat(ge=0, le=0.95) = 0.35
    margin_b: confloat(ge=0, le=0.95) = 0.40

    lines: List[LineIn] = Field(default_factory=list)


# =========================
# Helpers
# =========================
def safe_div(a, b):
    return a / b if b else 0


def norm_box(bt: str):
    bt = bt.upper().strip()
    return bt if bt in ("FB", "HB", "QB") else "HB"


# =========================
# Core Calculation
# =========================
def calculate(s: ShipmentIn):
    total_kilos = 0
    total_invoice = 0
    total_weighted_boxes = 0

    rows = []

    for ln in s.lines:
        bt = norm_box(ln.box_type)
        kg_box = ln.kg_per_box or s.kg_defaults.get(bt, 0)
        kilos = kg_box * ln.boxes

        invoice_box = ln.price_per_bunch * ln.bunch_per_box
        invoice_line = invoice_box * ln.boxes

        weight = s.box_weights.get(bt, 1)
        weighted_boxes = ln.boxes * weight

        rows.append({
            "product": ln.product,
            "box_type": bt,
            "boxes": ln.boxes,
            "kg_line": kilos,
            "invoice_line": invoice_line,
            "weighted_boxes": weighted_boxes,
            "bunch_per_box": ln.bunch_per_box
        })

        total_kilos += kilos
        total_invoice += invoice_line
        total_weighted_boxes += weighted_boxes

    freight_total = total_kilos * s.rate_per_kg
    duty_total = total_invoice * s.duty_rate

    for r in rows:
        r["freight"] = safe_div(r["kg_line"], total_kilos) * freight_total
        r["duty"] = safe_div(r["invoice_line"], total_invoice) * duty_total
        r["miami"] = safe_div(r["weighted_boxes"], total_weighted_boxes) * s.miami_to_ny_total

        r["landed"] = r["invoice_line"] + r["freight"] + r["duty"] + r["miami"]
        r["cost_box"] = safe_div(r["landed"], r["boxes"])
        r["cost_bunch"] = safe_div(r["cost_box"], r["bunch_per_box"])
        r["sell_35"] = safe_div(r["cost_box"], (1 - s.margin_a))
        r["sell_40"] = safe_div(r["cost_box"], (1 - s.margin_b))

    return {
        "awb": s.awb,
        "totals": {
            "total_kilos": total_kilos,
            "total_invoice": total_invoice,
            "freight_total": freight_total,
            "duty_total": duty_total,
            "miami_total": s.miami_to_ny_total
        },
        "lines": rows
    }


# =========================
# API
# =========================
@app.post("/calculate")
def calc_api(payload: ShipmentIn):
    return JSONResponse(calculate(payload))


@app.post("/export.xlsx")
def export_excel(payload: ShipmentIn):
    result = calculate(payload)
    wb = Workbook()
    ws = wb.active
    ws.title = "Landed Cost"

    ws.append(["AWB", result["awb"]])
    ws.append([])
    ws.append([
        "Product", "Box", "Boxes", "Kg", "Invoice",
        "Freight", "Duty", "Miami", "Landed",
        "Cost/Box", "Sell 35%", "Sell 40%"
    ])

    for r in result["lines"]:
        ws.append([
            r["product"], r["box_type"], r["boxes"], r["kg_line"],
            r["invoice_line"], r["freight"], r["duty"], r["miami"],
            r["landed"], r["cost_box"], r["sell_35"], r["sell_40"]
        ])

    bio = BytesIO()
    wb.save(bio)

    return StreamingResponse(
        BytesIO(bio.getvalue()),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=landed_cost.xlsx"},
    )


# =========================
# Minimal UI (safe)
# =========================
@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <h2>Flower Landed Cost</h2>
    <p>Backend is running correctly.</p>
    <p>Use POST /calculate and /export.xlsx</p>
    """
