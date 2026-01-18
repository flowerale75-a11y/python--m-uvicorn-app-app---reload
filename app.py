from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, conint, confloat
from typing import List, Dict
from io import BytesIO
from openpyxl import Workbook

app = FastAPI(title="Flower Landed Cost API")


class Line(BaseModel):
    product: str
    box_type: str = "HB"
    boxes: conint(ge=0)
    bunch_per_box: conint(ge=1)
    price_per_bunch: confloat(ge=0)
    kg_per_box: confloat(ge=0)


class Shipment(BaseModel):
    awb: str
    rate_per_kg: confloat(ge=0)
    duty_rate: confloat(ge=0, le=1)
    miami_to_ny_total: confloat(ge=0)

    kg_defaults: Dict[str, float]
    box_weights: Dict[str, float]

    margin_35: float = 0.35
    margin_40: float = 0.40

    lines: List[Line]


def safe_div(a, b):
    return a / b if b else 0


@app.post("/calculate")
def calculate(s: Shipment):
    total_kilos = 0
    total_invoice = 0
    total_weighted_boxes = 0
    rows = []

    for ln in s.lines:
        kg_box = ln.kg_per_box or s.kg_defaults.get(ln.box_type, 0)
        kg_line = kg_box * ln.boxes

        invoice_box = ln.price_per_bunch * ln.bunch_per_box
        invoice_line = invoice_box * ln.boxes

        weighted_boxes = ln.boxes * s.box_weights.get(ln.box_type, 1)

        rows.append({
            "product": ln.product,
            "boxes": ln.boxes,
            "kg": kg_line,
            "invoice": invoice_line,
            "weighted_boxes": weighted_boxes,
            "bunch_per_box": ln.bunch_per_box
        })

        total_kilos += kg_line
        total_invoice += invoice_line
        total_weighted_boxes += weighted_boxes

    freight_total = total_kilos * s.rate_per_kg
    duty_total = total_invoice * s.duty_rate

    for r in rows:
        r["freight"] = safe_div(r["kg"], total_kilos) * freight_total
        r["duty"] = safe_div(r["invoice"], total_invoice) * duty_total
        r["miami"] = safe_div(r["weighted_boxes"], total_weighted_boxes) * s.miami_to_ny_total
        r["landed"] = r["invoice"] + r["freight"] + r["duty"] + r["miami"]
        r["cost_box"] = safe_div(r["landed"], r["boxes"])
        r["sell_35"] = safe_div(r["cost_box"], (1 - s.margin_35))
        r["sell_40"] = safe_div(r["cost_box"], (1 - s.margin_40))

    return {
        "awb": s.awb,
        "totals": {
            "kilos": total_kilos,
            "invoice": total_invoice,
            "freight": freight_total,
            "duty": duty_total,
            "miami": s.miami_to_ny_total
        },
        "lines": rows
    }


@app.get("/")
def health():
    return {"status": "OK", "message": "Backend running"}
