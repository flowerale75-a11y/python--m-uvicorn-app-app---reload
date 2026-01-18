from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Dict, List, Optional

import requests
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from jose import jwt, JWTError
from passlib.context import CryptContext
from pydantic import BaseModel, conint, confloat, EmailStr
from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
from openpyxl import Workbook

# -----------------------------
# Config
# -----------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me").strip()
SESSION_COOKIE = os.getenv("SESSION_COOKIE", "bn_session").strip()
BASE_URL = os.getenv("BASE_URL", "").strip()  # e.g. https://flower-landed-cost.onrender.com

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()  # for verification

if not DATABASE_URL:
    # Render Postgres provides DATABASE_URL. For local dev, set it.
    # Example: postgresql+psycopg2://user:pass@localhost:5432/db
    raise RuntimeError("DATABASE_URL env var is required")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


# -----------------------------
# DB Models
# -----------------------------
class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(200), default="New Tenant")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    users: Mapped[List["User"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    settings: Mapped["TenantSettings"] = relationship(back_populates="tenant", uselist=False, cascade="all, delete-orphan")
    products: Mapped[List["ProductTemplate"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    shipments: Mapped[List["Shipment"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    role: Mapped[str] = mapped_column(String(50), default="owner")  # owner/manager/staff/readonly
    password_hash: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    google_sub: Mapped[Optional[str]] = mapped_column(String(200), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    tenant: Mapped["Tenant"] = relationship(back_populates="users")


class TenantSettings(Base):
    __tablename__ = "tenant_settings"

    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True)

    duty_rate: Mapped[float] = mapped_column(Float, default=0.22)
    margin_a: Mapped[float] = mapped_column(Float, default=0.35)
    margin_b: Mapped[float] = mapped_column(Float, default=0.40)

    kg_fb: Mapped[float] = mapped_column(Float, default=30.0)
    kg_hb: Mapped[float] = mapped_column(Float, default=15.0)
    kg_qb: Mapped[float] = mapped_column(Float, default=7.5)

    w_fb: Mapped[float] = mapped_column(Float, default=1.0)
    w_hb: Mapped[float] = mapped_column(Float, default=0.5)
    w_qb: Mapped[float] = mapped_column(Float, default=0.25)

    tenant: Mapped["Tenant"] = relationship(back_populates="settings")


class ProductTemplate(Base):
    __tablename__ = "product_templates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True)

    # memorize by product name (normalized)
    product_name: Mapped[str] = mapped_column(String(300), index=True)
    box_type: Mapped[str] = mapped_column(String(10), default="HB")
    bunch_per_box: Mapped[int] = mapped_column(Integer, default=12)
    stems_per_bunch: Mapped[int] = mapped_column(Integer, default=25)

    # Optional defaults
    kg_per_box: Mapped[float] = mapped_column(Float, default=0.0)
    price_per_bunch: Mapped[float] = mapped_column(Float, default=0.0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    tenant: Mapped["Tenant"] = relationship(back_populates="products")


class Shipment(Base):
    __tablename__ = "shipments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    created_by_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    awb: Mapped[str] = mapped_column(String(100), default="")
    rate_per_kg: Mapped[float] = mapped_column(Float, default=0.0)
    duty_rate: Mapped[float] = mapped_column(Float, default=0.22)
    miami_to_ny_total: Mapped[float] = mapped_column(Float, default=0.0)

    margin_a: Mapped[float] = mapped_column(Float, default=0.35)
    margin_b: Mapped[float] = mapped_column(Float, default=0.40)

    kg_fb: Mapped[float] = mapped_column(Float, default=30.0)
    kg_hb: Mapped[float] = mapped_column(Float, default=15.0)
    kg_qb: Mapped[float] = mapped_column(Float, default=7.5)

    w_fb: Mapped[float] = mapped_column(Float, default=1.0)
    w_hb: Mapped[float] = mapped_column(Float, default=0.5)
    w_qb: Mapped[float] = mapped_column(Float, default=0.25)

    totals_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # cached totals json (optional)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    tenant: Mapped["Tenant"] = relationship(back_populates="shipments")
    lines: Mapped[List["ShipmentLine"]] = relationship(back_populates="shipment", cascade="all, delete-orphan")


class ShipmentLine(Base):
    __tablename__ = "shipment_lines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    shipment_id: Mapped[str] = mapped_column(String(36), ForeignKey("shipments.id", ondelete="CASCADE"), index=True)

    finca: Mapped[str] = mapped_column(String(200), default="")
    origin: Mapped[str] = mapped_column(String(100), default="")
    product: Mapped[str] = mapped_column(String(300), default="")

    box_type: Mapped[str] = mapped_column(String(10), default="HB")
    boxes: Mapped[int] = mapped_column(Integer, default=0)
    bunch_per_box: Mapped[int] = mapped_column(Integer, default=12)
    stems_per_bunch: Mapped[int] = mapped_column(Integer, default=25)

    kg_per_box: Mapped[float] = mapped_column(Float, default=0.0)
    price_per_bunch: Mapped[float] = mapped_column(Float, default=0.0)

    shipment: Mapped["Shipment"] = relationship(back_populates="lines")


# Create tables on startup (MVP). For enterprise, replace with Alembic migrations later.
Base.metadata.create_all(engine)


# -----------------------------
# Auth + Session helpers
# -----------------------------
def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def normalize_product_name(name: str) -> str:
    s = (name or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s


def hash_password(pw: str) -> str:
    return pwd_context.hash(pw)


def verify_password(pw: str, hashed: str) -> bool:
    return pwd_context.verify(pw, hashed)


def create_session_token(user: User) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user.id,
        "tenant_id": user.tenant_id,
        "role": user.role,
        "email": user.email,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=7)).timestamp()),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


def decode_session_token(token: str) -> dict:
    return jwt.decode(token, SECRET_KEY, algorithms=["HS256"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user(request: Request, db=Depends(get_db)) -> User:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        data = decode_session_token(token)
        user_id = data.get("sub")
        tenant_id = data.get("tenant_id")
        if not user_id or not tenant_id:
            raise HTTPException(status_code=401, detail="Invalid session")
        user = db.get(User, user_id)
        if not user or user.tenant_id != tenant_id:
            raise HTTPException(status_code=401, detail="Invalid session")
        return user
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid session")


# -----------------------------
# Calculator Models
# -----------------------------
class LineIn(BaseModel):
    finca: str = ""
    origin: str = ""
    product: str = ""

    box_type: str = "HB"
    boxes: conint(ge=0) = 0

    bunch_per_box: conint(ge=1) = 12
    stems_per_bunch: conint(ge=1) = 25

    kg_per_box: confloat(ge=0) = 0.0  # 0 => auto from defaults
    price_per_bunch: confloat(ge=0) = 0.0  # YOU BUY BY BUNCH


class ShipmentIn(BaseModel):
    awb: str = ""
    rate_per_kg: confloat(ge=0) = 0.0
    duty_rate: confloat(ge=0, le=1) = 0.22
    miami_to_ny_total: confloat(ge=0) = 0.0

    margin_a: confloat(ge=0, le=0.95) = 0.35
    margin_b: confloat(ge=0, le=0.95) = 0.40

    kg_defaults: Dict[str, confloat(ge=0)]
    box_weights: Dict[str, confloat(ge=0)]

    lines: List[LineIn]


def safe_div(a: float, b: float) -> float:
    return a / b if b else 0.0


def norm_box(bt: str) -> str:
    bt = (bt or "").strip().upper()
    return bt if bt in ("FB", "HB", "QB") else "HB"


def calc_shipment(s: ShipmentIn) -> dict:
    total_kilos = 0.0
    total_invoice = 0.0
    total_weighted_boxes = 0.0
    total_boxes = 0

    base = []
    for ln in s.lines:
        bt = norm_box(ln.box_type)
        boxes = int(ln.boxes)

        kg_box = float(ln.kg_per_box or 0.0)
        if kg_box <= 0:
            kg_box = float(s.kg_defaults.get(bt, 0.0))

        kg_line = kg_box * boxes

        invoice_box = float(ln.price_per_bunch) * int(ln.bunch_per_box)
        invoice_line = invoice_box * boxes

        w = float(s.box_weights.get(bt, 1.0))
        weighted = boxes * w

        base.append(
            {
                "finca": ln.finca,
                "origin": ln.origin,
                "product": ln.product,
                "box_type": bt,
                "boxes": boxes,
                "bunch_per_box": int(ln.bunch_per_box),
                "stems_per_bunch": int(ln.stems_per_bunch),
                "kg_per_box_used": kg_box,
                "kg_line": kg_line,
                "price_per_bunch": float(ln.price_per_bunch),
                "invoice_box": invoice_box,
                "invoice_line": invoice_line,
                "weighted_boxes": weighted,
            }
        )

        total_boxes += boxes
        total_kilos += kg_line
        total_invoice += invoice_line
        total_weighted_boxes += weighted

    freight_total = total_kilos * float(s.rate_per_kg)
    duty_total = total_invoice * float(s.duty_rate)
    miami_total = float(s.miami_to_ny_total)

    out_lines = []
    grand_landed = 0.0

    for r in base:
        freight_alloc = safe_div(r["kg_line"], total_kilos) * freight_total
        duty_alloc = safe_div(r["invoice_line"], total_invoice) * duty_total
        miami_alloc = safe_div(r["weighted_boxes"], total_weighted_boxes) * miami_total

        landed = r["invoice_line"] + freight_alloc + duty_alloc + miami_alloc
        grand_landed += landed

        cost_box = safe_div(landed, r["boxes"])
        cost_bunch = safe_div(cost_box, r["bunch_per_box"])

        m1 = float(s.margin_a)
        m2 = float(s.margin_b)

        sell_box_m1 = safe_div(cost_box, (1 - m1))
        sell_box_m2 = safe_div(cost_box, (1 - m2))
        sell_bunch_m1 = safe_div(sell_box_m1, r["bunch_per_box"])
        sell_bunch_m2 = safe_div(sell_box_m2, r["bunch_per_box"])

        out_lines.append(
            {
                **r,
                "freight_alloc": freight_alloc,
                "duty_alloc": duty_alloc,
                "miami_alloc": miami_alloc,
                "landed_line": landed,
                "cost_per_box": cost_box,
                "cost_per_bunch": cost_bunch,
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


def build_excel(result: dict) -> bytes:
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
        "SELL/BOX 35%",
        "SELL/BOX 40%",
        "SELL/BUNCH 35%",
        "SELL/BUNCH 40%",
    ]
    ws.append(headers)

    for r in result["lines"]:
        ws.append(
            [
                result.get("awb", ""),
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

    ws2 = wb.create_sheet("Totals")
    t = result["totals"]
    for k, v in t.items():
        ws2.append([k, v])

    bio = BytesIO()
    wb.save(bio)
    return bio.getvalue()


# -----------------------------
# FastAPI App + Static
# -----------------------------
app = FastAPI(title="BloomNext Landed Cost SaaS")

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def root(request: Request):
    # If authenticated -> calculator UI, else login UI
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        try:
            decode_session_token(token)
            return FileResponse("static/index.html")
        except Exception:
            pass
    return FileResponse("static/login.html")


# -----------------------------
# Auth Schemas
# -----------------------------
class SignupIn(BaseModel):
    email: EmailStr
    password: str
    company_name: Optional[str] = None
    name: Optional[str] = None


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class GoogleIn(BaseModel):
    id_token: str


# -----------------------------
# Auth Endpoints
# -----------------------------
@app.post("/auth/signup")
def auth_signup(payload: SignupIn, db=Depends(get_db)):
    email = normalize_email(payload.email)
    existing = db.execute(select(User).where(User.email == email)).scalars().first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already in use")

    tenant = Tenant(name=(payload.company_name or f"{email.split('@')[0]} Company").strip())
    db.add(tenant)
    db.flush()  # get tenant id

    settings = TenantSettings(tenant_id=tenant.id)  # defaults
    db.add(settings)

    user = User(
        tenant_id=tenant.id,
        email=email,
        name=(payload.name or "").strip(),
        role="owner",
        password_hash=hash_password(payload.password),
    )
    db.add(user)
    db.commit()

    token = create_session_token(user)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        secure=True,  # Render uses HTTPS
        samesite="lax",
        max_age=7 * 24 * 3600,
        path="/",
    )
    return resp


@app.post("/auth/login")
def auth_login(payload: LoginIn, db=Depends(get_db)):
    email = normalize_email(payload.email)
    user = db.execute(select(User).where(User.email == email)).scalars().first()
    if not user or not user.password_hash:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_session_token(user)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=7 * 24 * 3600,
        path="/",
    )
    return resp


@app.post("/auth/logout")
def auth_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(key=SESSION_COOKIE, path="/")
    return resp


@app.get("/auth/me")
def auth_me(user: User = Depends(get_current_user), db=Depends(get_db)):
    tenant = db.get(Tenant, user.tenant_id)
    return {
        "ok": True,
        "user": {"id": user.id, "email": user.email, "name": user.name, "role": user.role},
        "tenant": {"id": tenant.id, "name": tenant.name if tenant else ""},
    }


@app.post("/auth/google")
def auth_google(payload: GoogleIn, db=Depends(get_db)):
    """
    Frontend uses Google Identity Services to obtain an id_token.
    Backend verifies it and logs user in (create tenant+user on first time).
    """
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=500, detail="GOOGLE_CLIENT_ID not configured")

    try:
        # google-auth verifies token by fetching Google certs
        from google.oauth2 import id_token as google_id_token
        from google.auth.transport import requests as google_requests

        info = google_id_token.verify_oauth2_token(
            payload.id_token,
            google_requests.Request(),
            GOOGLE_CLIENT_ID,
        )
        email = normalize_email(info.get("email", ""))
        sub = info.get("sub", "")
        name = (info.get("name") or "").strip()
        if not email or not sub:
            raise HTTPException(status_code=401, detail="Invalid Google token")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid Google token")

    # Find existing user by google_sub OR email
    user = db.execute(select(User).where(User.google_sub == sub)).scalars().first()
    if not user:
        user = db.execute(select(User).where(User.email == email)).scalars().first()

    if user:
        # Link google_sub if missing
        if not user.google_sub:
            user.google_sub = sub
        if not user.name and name:
            user.name = name
        db.commit()
    else:
        # Create new tenant + user
        tenant = Tenant(name=f"{email.split('@')[0]} Company")
        db.add(tenant)
        db.flush()
        db.add(TenantSettings(tenant_id=tenant.id))

        user = User(
            tenant_id=tenant.id,
            email=email,
            name=name,
            role="owner",
            password_hash=None,  # google-only initially
            google_sub=sub,
        )
        db.add(user)
        db.commit()

    token = create_session_token(user)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=7 * 24 * 3600,
        path="/",
    )
    return resp


# -----------------------------
# Tenant Settings
# -----------------------------
class SettingsOut(BaseModel):
    duty_rate: float
    margin_a: float
    margin_b: float
    kg_defaults: Dict[str, float]
    box_weights: Dict[str, float]


class SettingsIn(BaseModel):
    duty_rate: confloat(ge=0, le=1)
    margin_a: confloat(ge=0, le=0.95)
    margin_b: confloat(ge=0, le=0.95)
    kg_defaults: Dict[str, confloat(ge=0)]
    box_weights: Dict[str, confloat(ge=0)]


@app.get("/api/settings")
def get_settings(user: User = Depends(get_current_user), db=Depends(get_db)):
    s = db.get(TenantSettings, user.tenant_id)
    if not s:
        s = TenantSettings(tenant_id=user.tenant_id)
        db.add(s)
        db.commit()

    return SettingsOut(
        duty_rate=s.duty_rate,
        margin_a=s.margin_a,
        margin_b=s.margin_b,
        kg_defaults={"FB": s.kg_fb, "HB": s.kg_hb, "QB": s.kg_qb},
        box_weights={"FB": s.w_fb, "HB": s.w_hb, "QB": s.w_qb},
    )


@app.put("/api/settings")
def update_settings(payload: SettingsIn, user: User = Depends(get_current_user), db=Depends(get_db)):
    s = db.get(TenantSettings, user.tenant_id)
    if not s:
        s = TenantSettings(tenant_id=user.tenant_id)
        db.add(s)

    s.duty_rate = float(payload.duty_rate)
    s.margin_a = float(payload.margin_a)
    s.margin_b = float(payload.margin_b)

    s.kg_fb = float(payload.kg_defaults.get("FB", s.kg_fb))
    s.kg_hb = float(payload.kg_defaults.get("HB", s.kg_hb))
    s.kg_qb = float(payload.kg_defaults.get("QB", s.kg_qb))

    s.w_fb = float(payload.box_weights.get("FB", s.w_fb))
    s.w_hb = float(payload.box_weights.get("HB", s.w_hb))
    s.w_qb = float(payload.box_weights.get("QB", s.w_qb))

    db.commit()
    return {"ok": True}


# -----------------------------
# Product Templates (memorize per product name)
# -----------------------------
class ProductTemplateIn(BaseModel):
    product_name: str
    box_type: str = "HB"
    bunch_per_box: conint(ge=1) = 12
    stems_per_bunch: conint(ge=1) = 25
    kg_per_box: confloat(ge=0) = 0.0
    price_per_bunch: confloat(ge=0) = 0.0


@app.get("/api/products/lookup")
def lookup_product(name: str, user: User = Depends(get_current_user), db=Depends(get_db)):
    key = normalize_product_name(name)
    pt = db.execute(
        select(ProductTemplate).where(
            ProductTemplate.tenant_id == user.tenant_id,
            ProductTemplate.product_name == key,
        )
    ).scalars().first()

    if not pt:
        return {"ok": True, "found": False}

    return {
        "ok": True,
        "found": True,
        "template": {
            "product_name": name,
            "box_type": pt.box_type,
            "bunch_per_box": pt.bunch_per_box,
            "stems_per_bunch": pt.stems_per_bunch,
            "kg_per_box": pt.kg_per_box,
            "price_per_bunch": pt.price_per_bunch,
        },
    }


@app.post("/api/products")
def upsert_product(payload: ProductTemplateIn, user: User = Depends(get_current_user), db=Depends(get_db)):
    key = normalize_product_name(payload.product_name)
    if not key:
        raise HTTPException(status_code=400, detail="Product name required")

    pt = db.execute(
        select(ProductTemplate).where(
            ProductTemplate.tenant_id == user.tenant_id,
            ProductTemplate.product_name == key,
        )
    ).scalars().first()

    if not pt:
        pt = ProductTemplate(tenant_id=user.tenant_id, product_name=key)
        db.add(pt)

    pt.box_type = norm_box(payload.box_type)
    pt.bunch_per_box = int(payload.bunch_per_box)
    pt.stems_per_bunch = int(payload.stems_per_bunch)
    pt.kg_per_box = float(payload.kg_per_box)
    pt.price_per_bunch = float(payload.price_per_bunch)

    db.commit()
    return {"ok": True}


# -----------------------------
# Calculate + Export (requires auth)
# -----------------------------
@app.post("/api/calculate")
def api_calculate(payload: ShipmentIn, user: User = Depends(get_current_user)):
    return JSONResponse(calc_shipment(payload))


@app.post("/api/export.xlsx")
def api_export(payload: ShipmentIn, user: User = Depends(get_current_user)):
    result = calc_shipment(payload)
    content = build_excel(result)
    return StreamingResponse(
        BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="landed_cost.xlsx"'},
    )


# -----------------------------
# Shipments (save + list)
# -----------------------------
class SaveShipmentIn(ShipmentIn):
    pass


@app.post("/api/shipments")
def save_shipment(payload: SaveShipmentIn, user: User = Depends(get_current_user), db=Depends(get_db)):
    # compute first
    result = calc_shipment(payload)

    # create shipment record
    sh = Shipment(
        tenant_id=user.tenant_id,
        created_by_user_id=user.id,
        awb=payload.awb,
        rate_per_kg=float(payload.rate_per_kg),
        duty_rate=float(payload.duty_rate),
        miami_to_ny_total=float(payload.miami_to_ny_total),
        margin_a=float(payload.margin_a),
        margin_b=float(payload.margin_b),
        kg_fb=float(payload.kg_defaults.get("FB", 0.0)),
        kg_hb=float(payload.kg_defaults.get("HB", 0.0)),
        kg_qb=float(payload.kg_defaults.get("QB", 0.0)),
        w_fb=float(payload.box_weights.get("FB", 0.0)),
        w_hb=float(payload.box_weights.get("HB", 0.0)),
        w_qb=float(payload.box_weights.get("QB", 0.0)),
        totals_json=str(result["totals"]),
    )
    db.add(sh)
    db.flush()

    for ln in payload.lines:
        db.add(
            ShipmentLine(
                shipment_id=sh.id,
                finca=ln.finca,
                origin=ln.origin,
                product=ln.product,
                box_type=norm_box(ln.box_type),
                boxes=int(ln.boxes),
                bunch_per_box=int(ln.bunch_per_box),
                stems_per_bunch=int(ln.stems_per_bunch),
                kg_per_box=float(ln.kg_per_box),
                price_per_bunch=float(ln.price_per_bunch),
            )
        )

    db.commit()
    return {"ok": True, "shipment_id": sh.id, "result": result}


@app.get("/api/shipments")
def list_shipments(user: User = Depends(get_current_user), db=Depends(get_db), limit: int = 50):
    q = (
        select(Shipment)
        .where(Shipment.tenant_id == user.tenant_id)
        .order_by(Shipment.created_at.desc())
        .limit(min(max(limit, 1), 200))
    )
    rows = db.execute(q).scalars().all()
    return {
        "ok": True,
        "shipments": [
            {
                "id": r.id,
                "awb": r.awb,
                "created_at": r.created_at.isoformat(),
                "rate_per_kg": r.rate_per_kg,
                "duty_rate": r.duty_rate,
                "miami_to_ny_total": r.miami_to_ny_total,
            }
            for r in rows
        ],
    }


@app.get("/api/shipments/{shipment_id}")
def get_shipment(shipment_id: str, user: User = Depends(get_current_user), db=Depends(get_db)):
    sh = db.get(Shipment, shipment_id)
    if not sh or sh.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="Not found")

    lines = db.execute(select(ShipmentLine).where(ShipmentLine.shipment_id == sh.id)).scalars().all()
    payload = {
        "awb": sh.awb,
        "rate_per_kg": sh.rate_per_kg,
        "duty_rate": sh.duty_rate,
        "miami_to_ny_total": sh.miami_to_ny_total,
        "margin_a": sh.margin_a,
        "margin_b": sh.margin_b,
        "kg_defaults": {"FB": sh.kg_fb, "HB": sh.kg_hb, "QB": sh.kg_qb},
        "box_weights": {"FB": sh.w_fb, "HB": sh.w_hb, "QB": sh.w_qb},
        "lines": [
            {
                "finca": l.finca,
                "origin": l.origin,
                "product": l.product,
                "box_type": l.box_type,
                "boxes": l.boxes,
                "bunch_per_box": l.bunch_per_box,
                "stems_per_bunch": l.stems_per_bunch,
                "kg_per_box": l.kg_per_box,
                "price_per_bunch": l.price_per_bunch,
            }
            for l in lines
        ],
    }
    return {"ok": True, "shipment": payload, "result": calc_shipment(ShipmentIn(**payload))}


@app.get("/api/shipments/{shipment_id}/export.xlsx")
def export_shipment_xlsx(shipment_id: str, user: User = Depends(get_current_user), db=Depends(get_db)):
    sh = db.get(Shipment, shipment_id)
    if not sh or sh.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="Not found")

    lines = db.execute(select(ShipmentLine).where(ShipmentLine.shipment_id == sh.id)).scalars().all()
    payload = ShipmentIn(
        awb=sh.awb,
        rate_per_kg=sh.rate_per_kg,
        duty_rate=sh.duty_rate,
        miami_to_ny_total=sh.miami_to_ny_total,
        margin_a=sh.margin_a,
        margin_b=sh.margin_b,
        kg_defaults={"FB": sh.kg_fb, "HB": sh.kg_hb, "QB": sh.kg_qb},
        box_weights={"FB": sh.w_fb, "HB": sh.w_hb, "QB": sh.w_qb},
        lines=[
            LineIn(
                finca=l.finca,
                origin=l.origin,
                product=l.product,
                box_type=l.box_type,
                boxes=l.boxes,
                bunch_per_box=l.bunch_per_box,
                stems_per_bunch=l.stems_per_bunch,
                kg_per_box=l.kg_per_box,
                price_per_bunch=l.price_per_bunch,
            )
            for l in lines
        ],
    )

    result = calc_shipment(payload)
    content = build_excel(result)
    filename = f"landed_cost_{sh.awb or sh.id}.xlsx"
    return StreamingResponse(
        BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
