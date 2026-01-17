from __future__ import annotations

import csv
import io
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
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

    invoice_value_per_box: PositiveFloat

    weight_kg_per_box: PositiveFloat
    air_rate_per_kg: PositiveFloat

    # Shipment-level totals allocated across boxes in this row
    admin_fee_total: float = 0.0
    customs_total: float = 0.0
    trucking_total: float = 0.0

    # Duty
    duty_rate: float = 0.21
    duty_base: str = Field("invoice", description="invoice (default)")

    # Packing structure
    bunches_per_box: conint(ge=1) = 12
    stems_per_bunch: conint(ge=1) = 25

    # Margin
    target_margin: float = 0.35  # 35%


class CalcOutput(BaseModel):
    cost_per_box: float
    cost_per_bunch: float
    cost_per_stem: float
    suggested_sell_per_box: float
    suggested_sell_per_bunch: float
    suggested_sell_per_stem: float


# -----------------------------
# Core calculation
# -----------------------------
def _safe_float(x: float) -> float:
    try:
        return float(x)
    except Exception:
        return 0.0


def calc(inp: CalcInput) -> CalcOutput:
    boxes = int(inp.boxes)

    invoice = float(inp.invoice_value_per_box)

    air_cost_per_box = float(inp.weight_kg_per_box) * float(inp.air_rate_per_kg)

    admin_per_box = _safe_float(inp.admin_fee_total) / boxes
    customs_per_box = _safe_float(inp.customs_total) / boxes
    trucking_per_box = _safe_float(inp.trucking_total) / boxes

    if (inp.duty_base or "").lower() == "invoice":
        duty_per_box = _safe_float(inp.duty_rate) * invoice
    else:
        # Future extension: duty on landed base (kept invoice for now)
        duty_per_box = _safe_float(inp.duty_rate) * invoice

    cost_per_box = (
        invoice
        + air_cost_per_box
        + admin_per_box
        + customs_per_box
        + trucking_per_box
        + duty_per_box
    )

    bunches_per_box = int(inp.bunches_per_box)
    stems_per_bunch = int(inp.stems_per_bunch)
    stems_per_box = bunches_per_box * stems_per_bunch

    cost_per_bunch = cost_per_box / bunches_per_box
    cost_per_stem = cost_per_box / stems_per_box

    m = _safe_float(inp.target_margin)
    if m < 0:
        m = 0.0
    if m > 0.95:
        m = 0.95

    sell_per_box = cost_per_box / (1.0 - m)
    sell_per_bunch = sell_per_box / bunches_per_box
    sell_per_stem = sell_per_box / stems_per_box

    return CalcOutput(
        cost_per_box=round(cost_per_box, 4),
        cost_per_bunch=round(cost_per_bunch, 4),
        cost_per_stem=round(cost_per_stem, 4),
        suggested_sell_per_box=round(sell_per_box, 4),
        suggested_sell_per_bunch=round(sell_per_bunch, 4),
        suggested_sell_per_stem=round(sell_per_stem, 4),
    )


# -----------------------------
# API routes
# -----------------------------
@app.post("/calculate", response_model=CalcOutput)
def calculate(inp: CalcInput) -> CalcOutput:
    out = calc(inp)

    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
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
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                created_at,
                inp.product_name.strip(),
                inp.origin.strip(),
                inp.awb.strip(),
                inp.box_type.strip(),
                int(inp.boxes),
                float(inp.invoice_value_per_box),
                float(inp.weight_kg_per_box),
                float(inp.air_rate_per_kg),
                float(inp.admin_fee_total),
                float(inp.customs_total),
                float(inp.trucking_total),
                float(inp.duty_rate),
                (inp.duty_base or "invoice").strip(),
                int(inp.bunches_per_box),
                int(inp.stem
