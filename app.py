from __future__ import annotations

from io import BytesIO
from typing import Dict, List

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, conint, confloat
from openpyxl import Workbook

app = FastAPI(title="Flower Landed Cost")


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

    # if 0 -> auto from shipment kg_defaults
    kg_per_box: confloat(ge=0) = 0.0

    # NEW: you input price per bunch
    price_per_bunch: confloat(ge=0) = 0.0


class ShipmentIn(BaseModel):
    awb: str = ""
    rate_per_kg: confloat(ge=0) = 0.0

    # one duty rate on invoice (default 22%, editable)
    duty_rate: confloat(ge=0, le=1) = 0.22

    # Miami -> NY total allocated by weighted box size
    miami_to_ny_total: confloat(ge=0) = 0.0

    # allocation weights by box type (editable)
    box_weights: Dict[str, confloat(ge=0)] = Field(
        default_factory=lambda: {"FB": 1.0, "HB": 0.5, "QB": 0.25}
    )

    # kg defaults (editable)
    kg_defaults: Dict[str, confloat(ge=0)] = Field(
        default_factory=lambda: {"FB": 30.0, "HB": 15.0, "QB": 7.5}
    )

    # margins editable
    margin_a: confloat(ge=0, le=0.95) = 0.35
    margin_b: confloat(ge=0, le=0.95) = 0.40

    lines: List[LineIn] = Field(default_factory=list)


# -----------------------------
# Helpers
# -----------------------------
def safe_div(a: float, b: float) -> float:
    return a / b if b else 0.0


def norm_box(bt: str) -> str:
    bt = (bt or "").strip().upper()
    return bt if bt in ("FB", "HB", "QB") else "HB"


# -----------------------------
# Core calc (Excel logic)
# -----------------------------
def calculate_shipment(s: ShipmentIn) -> dict:
    total_kilos = 0.0
    total_invoice = 0.0
    total_weighted_boxes = 0.0
    total_boxes = 0

    rows = []
    for ln in s.lines:
        bt = norm_box(ln.box_type)
        boxes = int(ln.boxes)

        # KG per box auto from defaults if not provided
        kg_box = float(ln.kg_per_box) if float(ln.kg_per_box) > 0 else float(s.kg_defaults.get(bt, 0.0))
        kg_line = kg_box * boxes

        # invoice derived from price per bunch * bunch_per_box
        invoice_box = float(ln.price_per_bunch) * int(ln.bunch_per_box)
        invoice_line = invoice_box * boxes

        w = float(s.box_weights.get(bt, 1.0))
        weighted_boxes = boxes * w

        stems_per_box = int(ln.bunch_per_box) * int(ln.stems_per_bunch)

        rows.append(
            {
                "finca": ln.finca,
                "origin": ln.origin,
                "product": ln.product,
                "box_type": bt,
                "boxes": boxes,
                "bunch_per_box": int(ln.bunch_per_box),
                "stems_per_bunch": int(ln.stems_per_bunch),
                "stems_per_box": stems_per_box,
                "kg_per_box": kg_box,
                "kg_line": kg_line,
                "price_per_bunch": float(ln.price_per_bunch),
                "invoice_per_box": invoice_box,
                "invoice_line": invoice_line,
                "weighted_boxes": weighted_boxes,
            }
        )

        total_boxes += boxes
        total_kilos += kg_line
        total_invoice += invoice_line
        total_weighted_boxes += weighted_boxes

    freight_total = total_kilos * float(s.rate_per_kg)
    duty_total = total_invoice * float(s.duty_rate)

    for r in rows:
        r["freight_alloc"] = safe_div(r["kg_line"], total_kilos) * freight_total
        r["duty_alloc"] = safe_div(r["invoice_line"], total_invoice) * duty_total
        r["miami_alloc"] = safe_div(r["weighted_boxes"], total_weighted_boxes) * float(s.miami_to_ny_total)

        r["landed_line"] = r["invoice_line"] + r["freight_alloc"] + r["duty_alloc"] + r["miami_alloc"]
        r["cost_per_box"] = safe_div(r["landed_line"], r["boxes"])
        r["cost_per_bunch"] = safe_div(r["cost_per_box"], r["bunch_per_box"])
        r["cost_per_stem"] = safe_div(r["cost_per_box"], r["stems_per_box"])

        m1 = float(s.margin_a)
        m2 = float(s.margin_b)
        r["sell_box_m1"] = safe_div(r["cost_per_box"], (1.0 - m1))
        r["sell_box_m2"] = safe_div(r["cost_per_box"], (1.0 - m2))
        r["sell_bunch_m1"] = safe_div(r["sell_box_m1"], r["bunch_per_box"])
        r["sell_bunch_m2"] = safe_div(r["sell_box_m2"], r["bunch_per_box"])

    totals = {
        "total_boxes": total_boxes,
        "total_kilos": total_kilos,
        "total_invoice": total_invoice,
        "freight_total": freight_total,
        "duty_total": duty_total,
        "miami_to_ny_total": float(s.miami_to_ny_total),
        "grand_landed_total": total_invoice + freight_total + duty_total + float(s.miami_to_ny_total),
        "total_weighted_boxes": total_weighted_boxes,
        "rate_per_kg": float(s.rate_per_kg),
        "duty_rate": float(s.duty_rate),
        "margin_a": float(s.margin_a),
        "margin_b": float(s.margin_b),
        "kg_defaults": dict(s.kg_defaults),
        "box_weights": dict(s.box_weights),
    }

    return {"awb": s.awb, "totals": totals, "lines": rows}


def build_excel(result: dict) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Landed Cost"

    t = result["totals"]

    ws.append(["AWB", result.get("awb", "")])
    ws.append(["Rate per KG", t["rate_per_kg"]])
    ws.append(["Duty rate", t["duty_rate"]])
    ws.append(["Miami -> NY total", t["miami_to_ny_total"]])
    ws.append(["Margin A", t["margin_a"]])
    ws.append(["Margin B", t["margin_b"]])
    ws.append(["Default KG FB", t["kg_defaults"].get("FB", "")])
    ws.append(["Default KG HB", t["kg_defaults"].get("HB", "")])
    ws.append(["Default KG QB", t["kg_defaults"].get("QB", "")])
    ws.append(["Weight FB", t["box_weights"].get("FB", "")])
    ws.append(["Weight HB", t["box_weights"].get("HB", "")])
    ws.append(["Weight QB", t["box_weights"].get("QB", "")])

    ws.append([])
    ws.append(["TOTAL BOXES", t["total_boxes"]])
    ws.append(["TOTAL KILOS", t["total_kilos"]])
    ws.append(["TOTAL INVOICE", t["total_invoice"]])
    ws.append(["FREIGHT TOTAL", t["freight_total"]])
    ws.append(["DUTY TOTAL", t["duty_total"]])
    ws.append(["GRAND LANDED", t["grand_landed_total"]])
    ws.append([])

    ws.append([
        "#",
        "FINCA","ORIGEN","PRODUCT","BOX TYPE","BOXES",
        "BUNCH/BOX","STEMS/BUNCH","KG/BOX","PRICE/BUNCH",
        "INVOICE/BOX","INVOICE LINE","KG LINE",
        "FREIGHT ALLOC","DUTY ALLOC","MIAMI ALLOC",
        "LANDED LINE",
        "COST/BOX","COST/BUNCH","COST/STEM",
        "SELL BOX A","SELL BOX B","SELL BUNCH A","SELL BUNCH B"
    ])

    for i, r in enumerate(result["lines"], start=1):
        ws.append([
            i,
            r["finca"], r["origin"], r["product"], r["box_type"], r["boxes"],
            r["bunch_per_box"], r["stems_per_bunch"],
            r["kg_per_box"], r["price_per_bunch"],
            r["invoice_per_box"], r["invoice_line"], r["kg_line"],
            r["freight_alloc"], r["duty_alloc"], r["miami_alloc"],
            r["landed_line"],
            r["cost_per_box"], r["cost_per_bunch"], r["cost_per_stem"],
            r["sell_box_m1"], r["sell_box_m2"], r["sell_bunch_m1"], r["sell_bunch_m2"]
        ])

    bio = BytesIO()
    wb.save(bio)
    return bio.getvalue()


# -----------------------------
# API
# -----------------------------
@app.post("/calculate")
def api_calculate(payload: ShipmentIn):
    return JSONResponse(calculate_shipment(payload))


@app.post("/export.xlsx")
def api_export_xlsx(payload: ShipmentIn):
    result = calculate_shipment(payload)
    content = build_excel(result)
    return StreamingResponse(
        BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=landed_cost.xlsx"},
    )


# -----------------------------
# Static UI
# -----------------------------
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def ui():
    # Serve static/index.html as the main page
    return FileResponse("static/index.html")
