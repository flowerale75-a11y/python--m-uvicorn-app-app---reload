from __future__ import annotations

from typing import Dict, List
from io import BytesIO

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, conint, confloat

from openpyxl import Workbook


# -----------------------------
# App + Static UI
# -----------------------------
app = FastAPI(title="Flower Landed Cost")

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def ui():
    # Serve the Excel-style UI
    return FileResponse("static/index.html")


@app.get("/health")
def health():
    return {"status": "ok"}


# -----------------------------
# Models
# -----------------------------
class Line(BaseModel):
    finca: str = ""
    origin: str = ""
    product: str = ""

    box_type: str = "HB"
    boxes: conint(ge=0) = 0

    bunch_per_box: conint(ge=1) = 12
    stems_per_bunch: conint(ge=1) = 25

    kg_per_box: confloat(ge=0) = 0.0  # if 0 -> auto from defaults
    price_per_bunch: confloat(ge=0) = 0.0  # YOU BUY BY BUNCH


class Shipment(BaseModel):
    awb: str = ""

    rate_per_kg: confloat(ge=0) = 0.0
    duty_rate: confloat(ge=0, le=1) = 0.22

    miami_to_ny_total: confloat(ge=0) = 0.0

    margin_a: confloat(ge=0, le=0.95) = 0.35
    margin_b: confloat(ge=0, le=0.95) = 0.40

    # Editable defaults (Excel settings)
    kg_defaults: Dict[str, confloat(ge=0)]  # {"FB": 30, "HB": 15, "QB": 7.5}
    box_weights: Dict[str, confloat(ge=0)]  # {"FB": 1, "HB": 0.5, "QB": 0.25}

    lines: List[Line]


# -----------------------------
# Helpers
# -----------------------------
def safe_div(a: float, b: float) -> float:
    return a / b if b else 0.0


def normalize_box_type(bt: str) -> str:
    bt = (bt or "").strip().upper()
    return bt if bt in ("FB", "HB", "QB") else "HB"


def calc_core(s: Shipment) -> dict:
    # Totals used for proportional allocations
    total_kilos = 0.0
    total_invoice = 0.0
    total_weighted_boxes = 0.0
    total_boxes = 0

    pre = []

    for ln in s.lines:
        bt = normalize_box_type(ln.box_type)

        kg_box = float(ln.kg_per_box or 0.0)
        if kg_box <= 0:
            kg_box = float(s.kg_defaults.get(bt, 0.0))

        kg_line = kg_box * ln.boxes

        # Price is per bunch (always)
        invoice_box = float(ln.price_per_bunch) * int(ln.bunch_per_box)
        invoice_line = invoice_box * ln.boxes

        w = float(s.box_weights.get(bt, 1.0))
        weighted_boxes = ln.boxes * w

        pre.append(
            {
                "finca": ln.finca,
                "origin": ln.origin,
                "product": ln.product,
                "box_type": bt,
                "boxes": int(ln.boxes),
                "bunch_per_box": int(ln.bunch_per_box),
                "stems_per_bunch": int(ln.stems_per_bunch),
                "price_per_bunch": float(ln.price_per_bunch),
                "kg_per_box_used": float(kg_box),
                "kg_line": float(kg_line),
                "invoice_box": float(invoice_box),
                "invoice_line": float(invoice_line),
                "weighted_boxes": float(weighted_boxes),
            }
        )

        total_boxes += int(ln.boxes)
        total_kilos += kg_line
        total_invoice += invoice_line
        total_weighted_boxes += weighted_boxes

    freight_total = total_kilos * float(s.rate_per_kg)
    duty_total = total_invoice * float(s.duty_rate)
    miami_total = float(s.miami_to_ny_total)

    out_lines = []
    grand_landed = 0.0

    for r in pre:
        freight_alloc = safe_div(r["kg_line"], total_kilos) * freight_total
        duty_alloc = safe_div(r["invoice_line"], total_invoice) * duty_total
        miami_alloc = safe_div(r["weighted_boxes"], total_weighted_boxes) * miami_total

        landed_line = r["invoice_line"] + freight_alloc + duty_alloc + miami_alloc
        grand_landed += landed_line

        cost_per_box = safe_div(landed_line, r["boxes"])
        cost_per_bunch = safe_div(cost_per_box, r["bunch_per_box"])

        sell_box_m1 = safe_div(cost_per_box, (1 - float(s.margin_a)))
        sell_box_m2 = safe_div(cost_per_box, (1 - float(s.margin_b)))

        sell_bunch_m1 = safe_div(sell_box_m1, r["bunch_per_box"])
        sell_bunch_m2 = safe_div(sell_box_m2, r["bunch_per_box"])

        out_lines.append(
            {
                "finca": r["finca"],
                "origin": r["origin"],
                "product": r["product"],
                "box_type": r["box_type"],
                "boxes": r["boxes"],
                "bunch_per_box": r["bunch_per_box"],
                "stems_per_bunch": r["stems_per_bunch"],
                "price_per_bunch": r["price_per_bunch"],
                "kg_per_box_used": r["kg_per_box_used"],
                "kg_line": r["kg_line"],
                "invoice_box": r["invoice_box"],
                "invoice_line": r["invoice_line"],
                "freight_alloc": freight_alloc,
                "duty_alloc": duty_alloc,
                "miami_alloc": miami_alloc,
                "landed_line": landed_line,
                "cost_per_box": cost_per_box,
                "cost_per_bunch": cost_per_bunch,
                "sell_box_m1": sell_box_m1,
                "sell_box_m2": sell_box_m2,
                "sell_bunch_m1": sell_bunch_m1,
                "sell_bunch_m2": sell_bunch_m2,
            }
        )

    totals = {
        "total_boxes": total_boxes,
        "total_weighted_boxes": round(total_weighted_boxes, 4),
        "total_kilos": round(total_kilos, 4),
        "total_invoice": round(total_invoice, 2),
        "freight_total": round(freight_total, 2),
        "duty_total": round(duty_total, 2),
        "miami_to_ny_total": round(miami_total, 2),
        "grand_landed_total": round(grand_landed, 2),
    }

    return {"awb": s.awb, "totals": totals, "lines": out_lines}


# -----------------------------
# API
# -----------------------------
@app.post("/calculate")
def calculate(s: Shipment):
    return JSONResponse(calc_core(s))


@app.post("/export.xlsx")
def export_xlsx(s: Shipment):
    data = calc_core(s)

    wb = Workbook()
    ws = wb.active
    ws.title = "Landed Cost"

    # Header (Excel friendly)
    headers = [
        "AWB",
        "FINCA",
        "ORIGIN",
        "PRODUCT",
        "BOX TYPE",
        "BOXES",
        "BUNCH/BOX",
        "STEMS/BUNCH",
        "PRICE/BUNCH",
        "INVOICE/BOX",
        "INVOICE LINE",
        "KG/BOX",
        "KG LINE",
        "FREIGHT ALLOC",
        "DUTY ALLOC",
        "MIAMI->NY ALLOC",
        "LANDED LINE",
        "COST/BOX",
        "COST/BUNCH",
        "SELL/BOX 35%",
        "SELL/BOX 40%",
        "SELL/BUNCH 35%",
        "SELL/BUNCH 40%",
    ]
    ws.append(headers)

    for r in data["lines"]:
        ws.append(
            [
                data["awb"],
                r["finca"],
                r["origin"],
                r["product"],
                r["box_type"],
                r["boxes"],
                r["bunch_per_box"],
                r["stems_per_bunch"],
                round(r["price_per_bunch"], 4),
                round(r["invoice_box"], 4),
                round(r["invoice_line"], 2),
                round(r["kg_per_box_used"], 4),
                round(r["kg_line"], 4),
                round(r["freight_alloc"], 2),
                round(r["duty_alloc"], 2),
                round(r["miami_alloc"], 2),
                round(r["landed_line"], 2),
                round(r["cost_per_box"], 4),
                round(r["cost_per_bunch"], 4),
                round(r["sell_box_m1"], 4),
                round(r["sell_box_m2"], 4),
                round(r["sell_bunch_m1"], 4),
                round(r["sell_bunch_m2"], 4),
            ]
        )

    # Totals section
    ws2 = wb.create_sheet("Totals")
    t = data["totals"]
    ws2.append(["AWB", data["awb"]])
    ws2.append(["Total Boxes", t["total_boxes"]])
    ws2.append(["Total Weighted Boxes", t["total_weighted_boxes"]])
    ws2.append(["Total Kilos", t["total_kilos"]])
    ws2.append(["Total Invoice", t["total_invoice"]])
    ws2.append(["Freight Total", t["freight_total"]])
    ws2.append(["Duty Total", t["duty_total"]])
    ws2.append(["Miami->NY Total", t["miami_to_ny_total"]])
    ws2.append(["Grand Landed Total", t["grand_landed_total"]])
    ws2.append([])
    ws2.append(["Rate per KG", float(s.rate_per_kg)])
    ws2.append(["Duty Rate", float(s.duty_rate)])
    ws2.append(["Margin A", float(s.margin_a)])
    ws2.append(["Margin B", float(s.margin_b)])
    ws2.append(["KG Defaults", str(dict(s.kg_defaults))])
    ws2.append(["Box Weights", str(dict(s.box_weights))])

    # Stream to browser
    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)

    filename = "landed_cost.xlsx"
    return StreamingResponse(
        bio,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
