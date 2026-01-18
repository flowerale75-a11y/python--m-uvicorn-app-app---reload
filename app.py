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

    kg_per_box: confloat(ge=0) = 0.0  # 0 => auto from defaults
    price_per_bunch: confloat(ge=0) = 0.0  # you buy by bunch


class Shipment(BaseModel):
    awb: str = ""

    rate_per_kg: confloat(ge=0) = 0.0
    duty_rate: confloat(ge=0, le=1) = 0.22

    miami_to_ny_total: confloat(ge=0) = 0.0
    expenses_total: confloat(ge=0) = 0.0  # single bucket for now (later we'll itemize)

    # Target profit (default 35%, editable)
    target_profit_pct: confloat(ge=0, le=0.95) = 0.35

    # Editable defaults
    kg_defaults: Dict[str, confloat(ge=0)]
    box_weights: Dict[str, confloat(ge=0)]

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

        # You buy by bunch:
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

    # NEW: Total Investment (cash in)
    expenses_total = float(s.expenses_total)
    total_investment = total_invoice + freight_total + duty_total + miami_total + expenses_total

    # NEW: Target profit (profit % of SALES)
    tp = float(s.target_profit_pct)
    required_sales = safe_div(total_investment, (1 - tp))
    expected_profit = required_sales - total_investment

    out_lines = []
    grand_landed_lines = 0.0  # sum of landed_line allocations (invoice+freight+duty+miami only)

    for r in pre:
        freight_alloc = safe_div(r["kg_line"], total_kilos) * freight_total
        duty_alloc = safe_div(r["invoice_line"], total_invoice) * duty_total
        miami_alloc = safe_div(r["weighted_boxes"], total_weighted_boxes) * miami_total

        landed_line = r["invoice_line"] + freight_alloc + duty_alloc + miami_alloc
        grand_landed_lines += landed_line

        cost_per_box = safe_div(landed_line, r["boxes"])
        cost_per_bunch = safe_div(cost_per_box, r["bunch_per_box"])

        out_lines.append(
            {
                **r,
                "freight_alloc": freight_alloc,
                "duty_alloc": duty_alloc,
                "miami_alloc": miami_alloc,
                "landed_line": landed_line,
                "cost_per_box": cost_per_box,
                "cost_per_bunch": cost_per_bunch,
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

        # NEW investment + target profit
        "expenses_total": round(expenses_total, 2),
        "total_investment": round(total_investment, 2),
        "target_profit_pct": round(tp, 4),
        "required_sales": round(required_sales, 2),
        "expected_profit": round(expected_profit, 2),

        # For reference: landed allocations without expenses
        "grand_landed_without_expenses": round(grand_landed_lines, 2),
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
            ]
        )

    # Totals / Investment summary
    ws2 = wb.create_sheet("Investment Summary")
    t = data["totals"]

    ws2.append(["AWB", data["awb"]])
    ws2.append(["Total Invoice", t["total_invoice"]])
    ws2.append(["Freight Total", t["freight_total"]])
    ws2.append(["Duty Total", t["duty_total"]])
    ws2.append(["Miami->NY Total", t["miami_to_ny_total"]])
    ws2.append(["Expenses Total (placeholder)", t["expenses_total"]])
    ws2.append(["TOTAL INVESTMENT", t["total_investment"]])
    ws2.append([])
    ws2.append(["Target Profit % (of sales)", t["target_profit_pct"]])
    ws2.append(["Required Sales", t["required_sales"]])
    ws2.append(["Expected Profit $", t["expected_profit"]])
    ws2.append([])
    ws2.append(["Total Boxes", t["total_boxes"]])
    ws2.append(["Total Kilos", t["total_kilos"]])
    ws2.append(["Grand Landed (no expenses)", t["grand_landed_without_expenses"]])

    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)

    filename = "shipment_investment.xlsx"
    return StreamingResponse(
        bio,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
