"""Every record in section 4 of the specification.

Three rules hold across the whole schema:

1. Every table except `companies` carries `company_id`, and every query in the
   application filters on it. Company separation is enforced in one place
   (`scope.py`) rather than remembered at each call site.

2. Quantities are integer thousandths (`*_milli`) and money is integer paise
   (`*_paise`). Floating point cannot represent 0.1 exactly, so a ledger summed in
   floats drifts, and "stock on hand equals the sum of movements" -- the property
   the whole inventory checker rests on -- would be true only approximately. With
   integers it is true exactly, which is what the Step 4 test demands. Thousandths
   cover goods sold by weight (1.250 kg = 1250).

3. Stock movements and the activity log are never updated or deleted. Mistakes are
   corrected by a new movement. This is enforced by ORM events here and by database
   triggers in the PostgreSQL migration, so it holds even against code that forgets.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (JSON, BigInteger, Boolean, Date, DateTime, ForeignKey, Index, Integer,
                        LargeBinary, String, Text, UniqueConstraint, event)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, CreatedMixin, IdMixin, utcnow

CO = "companies.id"


def company_fk() -> Mapped[str]:
    return mapped_column(String(32), ForeignKey(CO, ondelete="CASCADE"), index=True,
                         nullable=False)


# =============================================================== tenants and people

ROLES = ("owner", "admin", "store_manager", "purchaser", "stock_clerk", "cashier", "viewer")


class Company(IdMixin, CreatedMixin, Base):
    __tablename__ = "companies"
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    gstin: Mapped[str | None] = mapped_column(String(15))
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata", nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)
    fy_start_month: Mapped[int] = mapped_column(Integer, default=4, nullable=False)  # April
    plan: Mapped[str] = mapped_column(String(32), default="trial", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    # Business rules (section 5 step 10). Defaults live in rules.py and are merged in.
    rules: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # Setup checklist progress (section 5): step key -> ISO timestamp completed.
    setup: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Sign-in security (Settings -> Company): {"two_step": "off" | "admins" | "everyone",
    # "idp": bool}. Null means the defaults in signin.py (two-step off, Google and
    # Microsoft allowed when the server has their keys).
    signin: Mapped[dict | None] = mapped_column(JSON)


class Store(IdMixin, CreatedMixin, Base):
    __tablename__ = "stores"
    __table_args__ = (UniqueConstraint("company_id", "code"),)
    company_id: Mapped[str] = company_fk()
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    address: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str | None] = mapped_column(String(64))       # decides festival calendar
    opening_hours: Mapped[str | None] = mapped_column(String(64))
    day_close_time: Mapped[str] = mapped_column(String(5), default="22:00", nullable=False)
    weekly_off: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    size: Mapped[str | None] = mapped_column(String(16))
    mode: Mapped[str] = mapped_column(String(16), default="standalone", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Person(IdMixin, CreatedMixin, Base):
    __tablename__ = "people"
    # Null only for the platform administrator (the vendor), who belongs to no company.
    company_id: Mapped[str | None] = mapped_column(String(32), ForeignKey(CO, ondelete="CASCADE"),
                                                   index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str | None] = mapped_column(String(254), unique=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(20), unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    manager_pin_hash: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    language: Mapped[str] = mapped_column(String(8), default="en", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="invited", nullable=False)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sign_in: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Alert channels this person chose (section 14): {"email": bool, "whatsapp": bool,
    # "muted": [kinds]}. In-app is always on.
    notify: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # Read-only view; assignments are written as PersonStore rows by
    # `assign_stores()`, so each row carries its company like every other table.
    stores: Mapped[list["Store"]] = relationship(secondary="person_stores", lazy="selectin",
                                                 viewonly=True)


class PersonStore(Base):
    __tablename__ = "person_stores"
    company_id: Mapped[str] = company_fk()
    person_id: Mapped[str] = mapped_column(String(32), ForeignKey("people.id", ondelete="CASCADE"),
                                           primary_key=True)
    store_id: Mapped[str] = mapped_column(String(32), ForeignKey("stores.id", ondelete="CASCADE"),
                                          primary_key=True)


def assign_stores(db, person: "Person", stores: list["Store"]) -> None:
    """Replace a person's store assignments. Callers have already checked that every
    store belongs to the person's company (see `org._valid_stores`)."""
    from sqlalchemy import delete
    db.execute(delete(PersonStore).where(PersonStore.person_id == person.id))
    for st in stores:
        db.add(PersonStore(company_id=person.company_id, person_id=person.id, store_id=st.id))
    db.flush()
    db.expire(person, ["stores"])


class AuthSession(IdMixin, CreatedMixin, Base):
    """A signed-in browser. The cookie holds a random token; only its hash is stored,
    so a copy of the database is not a copy of everyone's sessions."""
    __tablename__ = "auth_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    person_id: Mapped[str] = mapped_column(String(32), ForeignKey("people.id", ondelete="CASCADE"),
                                           index=True, nullable=False)
    company_id: Mapped[str | None] = mapped_column(String(32), index=True)
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip: Mapped[str | None] = mapped_column(String(64))
    device: Mapped[str | None] = mapped_column(String(255))


class Invitation(IdMixin, CreatedMixin, Base):
    __tablename__ = "invitations"
    company_id: Mapped[str] = company_fk()
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str | None] = mapped_column(String(254))
    phone: Mapped[str | None] = mapped_column(String(20))
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    store_ids: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    invited_by: Mapped[str | None] = mapped_column(String(32))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PasswordReset(IdMixin, CreatedMixin, Base):
    __tablename__ = "password_resets"
    # Null only for the platform administrator, mirroring Person.company_id.
    company_id: Mapped[str | None] = mapped_column(String(32), ForeignKey(CO, ondelete="CASCADE"),
                                                   index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    person_id: Mapped[str] = mapped_column(String(32), ForeignKey("people.id", ondelete="CASCADE"),
                                           nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OneTimeCode(IdMixin, CreatedMixin, Base):
    """A 6-digit code sent by text message or email: a password reset by phone
    ("reset") or the second step of signing in ("signin"). Only a keyed hash of the
    code is stored. Five wrong tries and the code is dead (`attempts`); `used_at`
    marks one that was used or replaced. A sign-in code also carries the hash of the
    ticket the browser holds for the half-finished sign-in."""
    __tablename__ = "one_time_codes"
    company_id: Mapped[str | None] = mapped_column(String(32), ForeignKey(CO, ondelete="CASCADE"),
                                                   index=True)
    person_id: Mapped[str] = mapped_column(String(32), ForeignKey("people.id", ondelete="CASCADE"),
                                           index=True, nullable=False)
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)
    channel: Mapped[str] = mapped_column(String(8), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    ticket_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sends: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ActivityLog(IdMixin, Base):
    """Append-only. Who did what, when, from which device, before and after."""
    __tablename__ = "activity_log"
    __table_args__ = (Index("ix_activity_company_at", "company_id", "at"),)
    company_id: Mapped[str | None] = mapped_column(String(32), index=True)
    store_id: Mapped[str | None] = mapped_column(String(32), index=True)
    person_id: Mapped[str | None] = mapped_column(String(32), index=True)
    device: Mapped[str | None] = mapped_column(String(255))
    ip: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity: Mapped[str | None] = mapped_column(String(64))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    before: Mapped[dict | None] = mapped_column(JSON)
    after: Mapped[dict | None] = mapped_column(JSON)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class SupportGrant(IdMixin, CreatedMixin, Base):
    """Temporary access a client grants the platform administrator, logged."""
    __tablename__ = "support_grants"
    company_id: Mapped[str] = company_fk()
    granted_by: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApiKey(IdMixin, CreatedMixin, Base):
    """For an existing POS sending sales (section 14). Only the hash is stored."""
    __tablename__ = "api_keys"
    company_id: Mapped[str] = company_fk()
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    prefix: Mapped[str] = mapped_column(String(12), nullable=False)
    store_id: Mapped[str | None] = mapped_column(String(32))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# =============================================================== catalogue

class Product(IdMixin, CreatedMixin, Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("company_id", "sku"),)
    company_id: Mapped[str] = company_fk()
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    brand: Mapped[str | None] = mapped_column(String(120))
    category: Mapped[str | None] = mapped_column(String(120), index=True)
    department: Mapped[str | None] = mapped_column(String(120))
    pack_size: Mapped[str | None] = mapped_column(String(64))
    unit: Mapped[str] = mapped_column(String(8), default="each", nullable=False)  # each|kg|litre
    mrp_paise: Mapped[int | None] = mapped_column(BigInteger)
    price_paise: Mapped[int | None] = mapped_column(BigInteger)
    cost_paise: Mapped[int | None] = mapped_column(BigInteger)
    gst_rate_bp: Mapped[int | None] = mapped_column(Integer)      # basis points: 1800 = 18%
    hsn: Mapped[str | None] = mapped_column(String(16))
    shelf_life_days: Mapped[int | None] = mapped_column(Integer)
    perishable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    case_pack: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    abc_class: Mapped[str | None] = mapped_column(String(1))
    # Link back to the forecasting engine's series id for the demo company.
    engine_ref: Mapped[str | None] = mapped_column(String(120))


class Barcode(IdMixin, Base):
    __tablename__ = "barcodes"
    __table_args__ = (UniqueConstraint("company_id", "code"),)
    company_id: Mapped[str] = company_fk()
    product_id: Mapped[str] = mapped_column(String(32), ForeignKey("products.id", ondelete="CASCADE"),
                                            index=True, nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    # A case barcode adds the case quantity in single units (section 13).
    units_per_scan: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class Supplier(IdMixin, CreatedMixin, Base):
    __tablename__ = "suppliers"
    __table_args__ = (UniqueConstraint("company_id", "name"),)
    company_id: Mapped[str] = company_fk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    gstin: Mapped[str | None] = mapped_column(String(15))
    contact: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(254))
    phone: Mapped[str | None] = mapped_column(String(20))
    # ISO weekday numbers, Monday = 0. Empty means "any day".
    order_days: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    lead_time_days: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    min_order_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)


class SupplierProduct(IdMixin, Base):
    __tablename__ = "supplier_products"
    __table_args__ = (UniqueConstraint("supplier_id", "product_id"),)
    company_id: Mapped[str] = company_fk()
    supplier_id: Mapped[str] = mapped_column(String(32), ForeignKey("suppliers.id", ondelete="CASCADE"),
                                             index=True, nullable=False)
    product_id: Mapped[str] = mapped_column(String(32), ForeignKey("products.id", ondelete="CASCADE"),
                                            index=True, nullable=False)
    supplier_code: Mapped[str | None] = mapped_column(String(64))
    cost_paise: Mapped[int | None] = mapped_column(BigInteger)
    case_pack: Mapped[int | None] = mapped_column(Integer)
    min_order_qty_milli: Mapped[int | None] = mapped_column(BigInteger)
    preferred: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class PriceRecord(IdMixin, Base):
    """Price and promotion: a planned promotion is known to the forecast in advance."""
    __tablename__ = "prices"
    company_id: Mapped[str] = company_fk()
    product_id: Mapped[str] = mapped_column(String(32), ForeignKey("products.id", ondelete="CASCADE"),
                                            index=True, nullable=False)
    store_id: Mapped[str | None] = mapped_column(String(32), index=True)   # null = all stores
    price_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date)
    promo_type: Mapped[str | None] = mapped_column(String(32))


class CalendarEvent(IdMixin, Base):
    """Festivals, holidays, store closures and local events.

    National and state festivals are copied into each company at sign-up
    (festivals.py), so a company can correct or remove one without touching anyone
    else's calendar. `kind` is festival, holiday, closure or local_event. A closure
    tells the forecast that zero sales that day were not low demand (section 13)."""
    __tablename__ = "calendar_events"
    __table_args__ = (Index("ix_calendar_company_day", "company_id", "day"),)
    company_id: Mapped[str] = company_fk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    day: Mapped[date] = mapped_column(Date, index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), default="festival", nullable=False)
    states: Mapped[list] = mapped_column(JSON, default=list, nullable=False)   # empty = national
    store_ids: Mapped[list] = mapped_column(JSON, default=list, nullable=False)  # empty = all
    approximate: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source: Mapped[str] = mapped_column(String(32), default="company", nullable=False)


# =============================================================== the ledger

MOVEMENT_TYPES = ("opening", "sale", "customer_return", "delivery_received", "supplier_return",
                  "transfer_out", "transfer_in", "count_adjustment", "wastage", "damage")


class StockMovement(IdMixin, Base):
    """The heart of the inventory checker. Never edited or deleted."""
    __tablename__ = "stock_movements"
    __table_args__ = (
        UniqueConstraint("company_id", "idempotency_key"),
        Index("ix_movement_store_product_at", "company_id", "store_id", "product_id", "at"),
    )
    company_id: Mapped[str] = company_fk()
    store_id: Mapped[str] = mapped_column(String(32), ForeignKey("stores.id"), nullable=False)
    product_id: Mapped[str] = mapped_column(String(32), ForeignKey("products.id"), nullable=False)
    qty_milli: Mapped[int] = mapped_column(BigInteger, nullable=False)   # signed
    type: Mapped[str] = mapped_column(String(24), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(200))
    source_type: Mapped[str | None] = mapped_column(String(24))
    source_id: Mapped[str | None] = mapped_column(String(32), index=True)
    person_id: Mapped[str | None] = mapped_column(String(32))
    device: Mapped[str | None] = mapped_column(String(255))
    unit_cost_paise: Mapped[int | None] = mapped_column(BigInteger)
    batch: Mapped[str | None] = mapped_column(String(64))
    expiry: Mapped[date | None] = mapped_column(Date)
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class StockOnHand(Base):
    """Worked out from movements and kept up to date in the same transaction."""
    __tablename__ = "stock_on_hand"
    company_id: Mapped[str] = mapped_column(String(32), ForeignKey(CO, ondelete="CASCADE"),
                                            index=True, nullable=False)
    store_id: Mapped[str] = mapped_column(String(32), ForeignKey("stores.id"), primary_key=True)
    product_id: Mapped[str] = mapped_column(String(32), ForeignKey("products.id"), primary_key=True)
    on_hand_milli: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    avg_cost_paise: Mapped[int | None] = mapped_column(BigInteger)
    known: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # When the stock first became known (opening or first count). Before this moment a
    # zero on the books is "not recorded yet", not "out of stock".
    known_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Opening stock as set (file or first count); None until set, and again if undone.
    opening_milli: Mapped[int | None] = mapped_column(BigInteger)
    last_counted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The latest moment on-hand was set to a known truth: an opening figure or a count.
    # Sales dated before it are already reflected in it, so they do not move stock.
    baseline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_movement_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sale_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# =============================================================== selling

class Bill(IdMixin, CreatedMixin, Base):
    """Only a paid bill creates sale movements (section 7)."""
    __tablename__ = "bills"
    __table_args__ = (UniqueConstraint("company_id", "store_id", "bill_number"),)
    company_id: Mapped[str] = company_fk()
    store_id: Mapped[str] = mapped_column(String(32), ForeignKey("stores.id"), index=True,
                                          nullable=False)
    till: Mapped[str | None] = mapped_column(String(32))
    cashier_id: Mapped[str | None] = mapped_column(String(32))
    # Assigned on the device before payment; the safety key against double posting.
    bill_number: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
    total_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    tax_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    discount_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    void_reason: Mapped[str | None] = mapped_column(String(200))
    refund_of: Mapped[str | None] = mapped_column(String(32))
    refund_reason: Mapped[str | None] = mapped_column(String(200))
    approved_by: Mapped[str | None] = mapped_column(String(32))   # manager PIN holder
    device_id: Mapped[str | None] = mapped_column(String(64))
    made_offline: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    change_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    source: Mapped[str] = mapped_column(String(16), default="checkout", nullable=False)
    business_day: Mapped[date | None] = mapped_column(Date, index=True)
    lines: Mapped[list["BillLine"]] = relationship(cascade="all, delete-orphan", lazy="selectin")
    payments: Mapped[list["BillPayment"]] = relationship(cascade="all, delete-orphan",
                                                         lazy="selectin")


class BillLine(IdMixin, Base):
    __tablename__ = "bill_lines"
    company_id: Mapped[str] = company_fk()
    bill_id: Mapped[str] = mapped_column(String(32), ForeignKey("bills.id", ondelete="CASCADE"),
                                         index=True, nullable=False)
    product_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("products.id"))
    description: Mapped[str | None] = mapped_column(String(300))   # misc items
    unmapped_barcode: Mapped[str | None] = mapped_column(String(64))
    qty_milli: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unit_price_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    discount_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    tax_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    resaleable: Mapped[bool | None] = mapped_column(Boolean)   # for refund lines
    # Price overrides and discounts need permission and are logged (section 7 rule 10).
    original_price_paise: Mapped[int | None] = mapped_column(BigInteger)
    override_reason: Mapped[str | None] = mapped_column(String(200))


class BillPayment(IdMixin, Base):
    __tablename__ = "bill_payments"
    company_id: Mapped[str] = company_fk()
    bill_id: Mapped[str] = mapped_column(String(32), ForeignKey("bills.id", ondelete="CASCADE"),
                                         index=True, nullable=False)
    method: Mapped[str] = mapped_column(String(8), nullable=False)    # cash|upi|card
    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tendered_paise: Mapped[int | None] = mapped_column(BigInteger)
    reference: Mapped[str | None] = mapped_column(String(64))


class TillDevice(IdMixin, Base):
    """A till or phone running Checkout. It reports how many bills it is holding
    while offline, so Day close can wait for them (section 7 rule 9)."""
    __tablename__ = "till_devices"
    __table_args__ = (UniqueConstraint("company_id", "device_id"),)
    company_id: Mapped[str] = company_fk()
    store_id: Mapped[str] = mapped_column(String(32), ForeignKey("stores.id"), nullable=False)
    device_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str | None] = mapped_column(String(64))
    pending: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DailySales(IdMixin, Base):
    """What the forecasting engine reads every night."""
    __tablename__ = "daily_sales"
    __table_args__ = (UniqueConstraint("company_id", "store_id", "product_id", "day"),)
    company_id: Mapped[str] = company_fk()
    store_id: Mapped[str] = mapped_column(String(32), ForeignKey("stores.id"), index=True,
                                          nullable=False)
    product_id: Mapped[str] = mapped_column(String(32), ForeignKey("products.id"), index=True,
                                            nullable=False)
    day: Mapped[date] = mapped_column(Date, index=True, nullable=False)
    units_milli: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    revenue_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    avg_price_paise: Mapped[int | None] = mapped_column(BigInteger)
    out_of_stock: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    oos_source: Mapped[str | None] = mapped_column(String(12))   # ledger|estimated
    # history (forecasting only), upload, entry, checkout or pos
    source: Mapped[str] = mapped_column(String(16), default="upload", nullable=False)
    # Units of this row already posted to the stock ledger. A replacement file posts
    # only the difference, so the same file twice moves stock once.
    posted_milli: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    import_job_id: Mapped[str | None] = mapped_column(String(32), index=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DayClose(IdMixin, CreatedMixin, Base):
    __tablename__ = "day_closes"
    __table_args__ = (UniqueConstraint("company_id", "store_id", "day"),)
    company_id: Mapped[str] = company_fk()
    store_id: Mapped[str] = mapped_column(String(32), ForeignKey("stores.id"), nullable=False)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    closed_by: Mapped[str | None] = mapped_column(String(32))
    automatic: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cash_expected_paise: Mapped[int | None] = mapped_column(BigInteger)
    cash_counted_paise: Mapped[int | None] = mapped_column(BigInteger)
    store_was_closed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    note: Mapped[str | None] = mapped_column(String(300))
    report: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # A sales file for this day arrived after it closed: tonight's run re-reads it.
    late_data_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# =============================================================== buying and receiving

PO_STATES = ("draft", "approved", "sent", "partly_received", "received", "cancelled")


class PurchaseOrder(IdMixin, CreatedMixin, Base):
    __tablename__ = "purchase_orders"
    __table_args__ = (UniqueConstraint("company_id", "number"),)
    company_id: Mapped[str] = company_fk()
    number: Mapped[str] = mapped_column(String(32), nullable=False)
    supplier_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("suppliers.id"),
                                                    index=True)
    store_id: Mapped[str] = mapped_column(String(32), ForeignKey("stores.id"), index=True,
                                          nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)
    expected_date: Mapped[date | None] = mapped_column(Date)
    created_by: Mapped[str | None] = mapped_column(String(32))
    approved_by: Mapped[str | None] = mapped_column(String(32))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    origin: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    closed_short: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    lines: Mapped[list["PurchaseOrderLine"]] = relationship(cascade="all, delete-orphan",
                                                            lazy="selectin")


class PurchaseOrderLine(IdMixin, Base):
    __tablename__ = "purchase_order_lines"
    company_id: Mapped[str] = company_fk()
    po_id: Mapped[str] = mapped_column(String(32), ForeignKey("purchase_orders.id", ondelete="CASCADE"),
                                       index=True, nullable=False)
    product_id: Mapped[str] = mapped_column(String(32), ForeignKey("products.id"), nullable=False)
    qty_milli: Mapped[int] = mapped_column(BigInteger, nullable=False)
    received_milli: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    unit_cost_paise: Mapped[int | None] = mapped_column(BigInteger)
    reason: Mapped[str | None] = mapped_column(String(300))   # why the quantity is what it is


class Delivery(IdMixin, CreatedMixin, Base):
    __tablename__ = "deliveries"
    __table_args__ = (UniqueConstraint("company_id", "idempotency_key"),)
    company_id: Mapped[str] = company_fk()
    store_id: Mapped[str] = mapped_column(String(32), ForeignKey("stores.id"), nullable=False)
    po_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("purchase_orders.id"),
                                              index=True)
    supplier_id: Mapped[str | None] = mapped_column(String(32))
    invoice_number: Mapped[str | None] = mapped_column(String(64))
    received_by: Mapped[str | None] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(String(300))
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    lines: Mapped[list["DeliveryLine"]] = relationship(cascade="all, delete-orphan",
                                                       lazy="selectin")


class DeliveryLine(IdMixin, Base):
    __tablename__ = "delivery_lines"
    company_id: Mapped[str] = company_fk()
    delivery_id: Mapped[str] = mapped_column(String(32), ForeignKey("deliveries.id", ondelete="CASCADE"),
                                             index=True, nullable=False)
    product_id: Mapped[str] = mapped_column(String(32), ForeignKey("products.id"), nullable=False)
    received_milli: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    # Not added to stock: damaged, wrong item or expired on arrival; for supplier credit.
    damaged_milli: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    reject_reason: Mapped[str | None] = mapped_column(String(16))
    credit_status: Mapped[str | None] = mapped_column(String(12))   # pending|credited
    unit_cost_paise: Mapped[int | None] = mapped_column(BigInteger)
    batch: Mapped[str | None] = mapped_column(String(64))
    expiry: Mapped[date | None] = mapped_column(Date)


class Transfer(IdMixin, CreatedMixin, Base):
    __tablename__ = "transfers"
    __table_args__ = (UniqueConstraint("company_id", "idempotency_key"),)
    company_id: Mapped[str] = company_fk()
    from_store_id: Mapped[str] = mapped_column(String(32), ForeignKey("stores.id"), nullable=False)
    to_store_id: Mapped[str] = mapped_column(String(32), ForeignKey("stores.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="in_transit", nullable=False)
    sent_by: Mapped[str | None] = mapped_column(String(32))
    received_by: Mapped[str | None] = mapped_column(String(32))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(String(300))
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    receive_key: Mapped[str | None] = mapped_column(String(128))
    lines: Mapped[list["TransferLine"]] = relationship(cascade="all, delete-orphan",
                                                       lazy="selectin")


class TransferLine(IdMixin, Base):
    __tablename__ = "transfer_lines"
    company_id: Mapped[str] = company_fk()
    transfer_id: Mapped[str] = mapped_column(String(32), ForeignKey("transfers.id", ondelete="CASCADE"),
                                             index=True, nullable=False)
    product_id: Mapped[str] = mapped_column(String(32), ForeignKey("products.id"), nullable=False)
    sent_milli: Mapped[int] = mapped_column(BigInteger, nullable=False)
    received_milli: Mapped[int | None] = mapped_column(BigInteger)
    unit_cost_paise: Mapped[int | None] = mapped_column(BigInteger)   # sender's average cost


class CountSession(IdMixin, CreatedMixin, Base):
    """cycle (what is due), full (year end or audit) or opening (setting first stock).
    open -> submitted -> closed once no line is waiting for a manager."""
    __tablename__ = "count_sessions"
    __table_args__ = (UniqueConstraint("company_id", "idempotency_key"),)
    company_id: Mapped[str] = company_fk()
    store_id: Mapped[str] = mapped_column(String(32), ForeignKey("stores.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(12), default="cycle", nullable=False)
    name: Mapped[str | None] = mapped_column(String(120))
    blind: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
    product_ids: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    started_by: Mapped[str | None] = mapped_column(String(32))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    lines: Mapped[list["CountLine"]] = relationship(cascade="all, delete-orphan", lazy="selectin")


class CountLine(IdMixin, Base):
    __tablename__ = "count_lines"
    __table_args__ = (UniqueConstraint("company_id", "idempotency_key"),)
    company_id: Mapped[str] = company_fk()
    session_id: Mapped[str] = mapped_column(String(32), ForeignKey("count_sessions.id", ondelete="CASCADE"),
                                            index=True, nullable=False)
    product_id: Mapped[str] = mapped_column(String(32), ForeignKey("products.id"), nullable=False)
    counted_milli: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Captured at the moment of counting, so a sale a minute later is not a difference.
    system_milli: Mapped[int] = mapped_column(BigInteger, nullable=False)
    counted_by: Mapped[str | None] = mapped_column(String(32))
    counted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 nullable=False)
    difference_milli: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    unit_cost_paise: Mapped[int | None] = mapped_column(BigInteger)
    # posted (small difference, or approved), confirmed (no difference), waiting (for a
    # manager), conflict (two people disagree), rejected (recount asked), superseded
    status: Mapped[str] = mapped_column(String(16), default="waiting", nullable=False)
    movement_id: Mapped[str | None] = mapped_column(String(32))
    decided_by: Mapped[str | None] = mapped_column(String(32))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(String(300))       # the counter's
    reason: Mapped[str | None] = mapped_column(String(300))     # the manager's
    device: Mapped[str | None] = mapped_column(String(255))
    idempotency_key: Mapped[str | None] = mapped_column(String(128))


# =============================================================== forecasting

class ForecastRun(IdMixin, CreatedMixin, Base):
    """One run of the engine for one company. kind: train (onboarding or weekly
    retrain, with its backtest) or nightly (forecast from the saved model). The
    screens read the latest *succeeded* run, so a failed night leaves yesterday's
    forecast in place, its age shown (section 13)."""
    __tablename__ = "forecast_runs"
    __table_args__ = (Index("ix_forecast_run_company_created", "company_id", "created_at"),)
    company_id: Mapped[str] = company_fk()
    kind: Mapped[str] = mapped_column(String(12), default="nightly", nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), default="schedule", nullable=False)
    tier: Mapped[str | None] = mapped_column(String(12))            # engine|starter
    data_cutoff: Mapped[date | None] = mapped_column(Date)
    model_version: Mapped[str | None] = mapped_column(String(64))
    model_version_id: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="running", nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timings: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    accuracy: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    message: Mapped[str | None] = mapped_column(Text)       # plain-language outcome
    error: Mapped[str | None] = mapped_column(Text)         # technical detail, for support


class ModelVersion(IdMixin, CreatedMixin, Base):
    """A trained model. Becomes active only if it would have earned at least as much
    in the backtest as the active one (the engine's promotion rule); the decision
    and both figures are kept in `promotion`."""
    __tablename__ = "model_versions"
    __table_args__ = (UniqueConstraint("company_id", "version"),)
    company_id: Mapped[str] = company_fk()
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    tier: Mapped[str] = mapped_column(String(12), default="engine", nullable=False)
    status: Mapped[str] = mapped_column(String(12), default="candidate", nullable=False)
    data_cutoff: Mapped[date | None] = mapped_column(Date)
    n_series: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    train_seconds: Mapped[float | None] = mapped_column()
    path: Mapped[str | None] = mapped_column(String(500))
    backtest_profit_paise: Mapped[int | None] = mapped_column(BigInteger)
    backtest: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    promotion: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    promoted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ModelArtifact(Base):
    """The saved model itself (dqstack/live.py), compressed. Kept in the database so a
    database backup is a complete backup and every server sees the same model."""
    __tablename__ = "model_artifacts"
    version_id: Mapped[str] = mapped_column(String(32), ForeignKey("model_versions.id",
                                                                   ondelete="CASCADE"),
                                            primary_key=True)
    company_id: Mapped[str] = company_fk()
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)


class ForecastScore(IdMixin, Base):
    """Monitoring (section 10): each night, the previous forecast's first day scored
    against what sold, per store and category."""
    __tablename__ = "forecast_scores"
    __table_args__ = (UniqueConstraint("run_id", "store_id", "category"),
                      Index("ix_forecast_score_company_day", "company_id", "day"))
    company_id: Mapped[str] = company_fk()
    run_id: Mapped[str] = mapped_column(String(32), ForeignKey("forecast_runs.id",
                                                               ondelete="CASCADE"), nullable=False)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    store_id: Mapped[str] = mapped_column(String(32), nullable=False)
    category: Mapped[str] = mapped_column(String(120), nullable=False)
    n: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_milli: Mapped[int] = mapped_column(BigInteger, nullable=False)
    forecast_milli: Mapped[int] = mapped_column(BigInteger, nullable=False)
    abs_error_milli: Mapped[int] = mapped_column(BigInteger, nullable=False)
    inside_range: Mapped[int] = mapped_column(Integer, nullable=False)


class Recommendation(IdMixin, Base):
    """Per store and product: forecast with range, stock target, suggested order,
    reason and money at risk. What Today and Orders show."""
    __tablename__ = "recommendations"
    __table_args__ = (UniqueConstraint("run_id", "store_id", "product_id"),)
    company_id: Mapped[str] = company_fk()
    run_id: Mapped[str] = mapped_column(String(32), ForeignKey("forecast_runs.id", ondelete="CASCADE"),
                                        index=True, nullable=False)
    store_id: Mapped[str] = mapped_column(String(32), ForeignKey("stores.id"), index=True,
                                          nullable=False)
    product_id: Mapped[str] = mapped_column(String(32), ForeignKey("products.id"), index=True,
                                            nullable=False)
    # Each of the next 28 days: [5%, 50%, 95%] in thousandths.
    daily: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # Demand over the risk period at the engine's nine quantile levels, for the
    # cost-of-being-wrong curve on the Today card.
    risk_q: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    risk_days: Mapped[int] = mapped_column(Integer, default=9, nullable=False)
    lead_time_days: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    supplier_id: Mapped[str | None] = mapped_column(String(32))
    on_hand_milli: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    on_order_milli: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    stock_known: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    suggested_milli: Mapped[int | None] = mapped_column(BigInteger)
    underage_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    overage_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    planning_class: Mapped[str | None] = mapped_column(String(20))
    history_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # No history of its own yet: an estimate from similar products (section 13).
    estimate: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    week1_mid_milli: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    week1_lo_milli: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    week1_hi_milli: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    target_milli: Mapped[int | None] = mapped_column(BigInteger)
    service_level: Mapped[float | None] = mapped_column()
    reasons: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    money_at_risk_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    flagged: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class Decision(IdMixin, CreatedMixin, Base):
    __tablename__ = "decisions"
    company_id: Mapped[str] = company_fk()
    recommendation_id: Mapped[str] = mapped_column(String(32), ForeignKey("recommendations.id",
                                                                          ondelete="CASCADE"),
                                                   index=True, nullable=False)
    store_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    product_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    person_id: Mapped[str | None] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(12), nullable=False)   # approve|change|skip
    value_milli: Mapped[int | None] = mapped_column(BigInteger)
    reason: Mapped[str | None] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(String(500))
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# =============================================================== imports and alerts

class ImportJob(IdMixin, CreatedMixin, Base):
    """One uploaded file and everything that happened to it (section 5, Imports screen).

    uploaded -> checked -> imported -> undone, or failed. The file itself is kept in
    ImportFile so this list stays light; what the import created or changed is kept
    row by row in ImportChange, so undo restores exactly what was there before."""
    __tablename__ = "import_jobs"
    __table_args__ = (Index("ix_import_company_created", "company_id", "created_at"),)
    company_id: Mapped[str] = company_fk()
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    filename: Mapped[str | None] = mapped_column(String(300))
    store_id: Mapped[str | None] = mapped_column(String(32))
    signature: Mapped[str | None] = mapped_column(String(64))   # which source the columns match
    headers: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    mapping: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    rows_read: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rows_accepted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rows_unchanged: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rows_new: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rows_updated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rows_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    errors: Mapped[list] = mapped_column(JSON, default=list, nullable=False)      # first 2,000
    warnings: Mapped[list] = mapped_column(JSON, default=list, nullable=False)    # first 2,000
    status: Mapped[str] = mapped_column(String(16), default="uploaded", nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    person_id: Mapped[str | None] = mapped_column(String(32))
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    undo_report: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class ImportFile(Base):
    __tablename__ = "import_files"
    job_id: Mapped[str] = mapped_column(String(32), ForeignKey("import_jobs.id", ondelete="CASCADE"),
                                        primary_key=True)
    company_id: Mapped[str] = company_fk()
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)


class ImportChange(IdMixin, Base):
    """One record an import created, changed or removed, in order, for undo."""
    __tablename__ = "import_changes"
    __table_args__ = (Index("ix_import_change_job_seq", "job_id", "seq"),)
    company_id: Mapped[str] = company_fk()
    job_id: Mapped[str] = mapped_column(String(32), ForeignKey("import_jobs.id", ondelete="CASCADE"),
                                        nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    entity: Mapped[str] = mapped_column(String(32), nullable=False)
    # A record id, or "store_id:product_id" (65 characters) for per-store records.
    entity_id: Mapped[str] = mapped_column(String(96), nullable=False)
    op: Mapped[str] = mapped_column(String(8), nullable=False)          # insert|update|delete
    before: Mapped[dict | None] = mapped_column(JSON)
    after: Mapped[dict | None] = mapped_column(JSON)


class ColumnMapping(IdMixin, Base):
    """Remembered per source, so the next upload from it needs no matching."""
    __tablename__ = "column_mappings"
    __table_args__ = (UniqueConstraint("company_id", "kind", "signature"),)
    company_id: Mapped[str] = company_fk()
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    signature: Mapped[str] = mapped_column(String(64), nullable=False)
    mapping: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class Alert(IdMixin, CreatedMixin, Base):
    __tablename__ = "alerts"
    __table_args__ = (UniqueConstraint("company_id", "dedupe_key"),)
    company_id: Mapped[str] = company_fk()
    store_id: Mapped[str | None] = mapped_column(String(32), index=True)
    product_id: Mapped[str | None] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(12), default="attention", nullable=False)
    message: Mapped[str] = mapped_column(String(500), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    link: Mapped[str | None] = mapped_column(String(200))     # where to act on it


class PosMessage(IdMixin, CreatedMixin, Base):
    """Each completed-sale, refund and void from an existing POS, by its own id, so a
    repeat is recognised and ignored (section 14)."""
    __tablename__ = "pos_messages"
    __table_args__ = (UniqueConstraint("company_id", "external_id"),)
    company_id: Mapped[str] = company_fk()
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    result: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class Attachment(IdMixin, CreatedMixin, Base):
    """A photo or document attached to something: a wastage entry's photo, a delivery
    note. Kept in the database so every application server sees it."""
    __tablename__ = "attachments"
    __table_args__ = (Index("ix_attachment_owner", "owner_type", "owner_id"),)
    company_id: Mapped[str] = company_fk()
    owner_type: Mapped[str] = mapped_column(String(24), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(32), nullable=False)
    content_type: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    person_id: Mapped[str | None] = mapped_column(String(32))


class Connector(IdMixin, CreatedMixin, Base):
    """A link to another system a company already uses (connectors/): Tally, Zoho
    Inventory. One per kind per company. `settings` holds what an Admin chose (ledger
    names, the warehouse-to-store map); `credentials` holds tokens and client secrets,
    encrypted (connectors/secrets.py) and never sent to a browser or exported.
    `cursor` is where the last successful sync stopped, per document kind."""
    __tablename__ = "connectors"
    __table_args__ = (UniqueConstraint("company_id", "kind"),)
    company_id: Mapped[str] = company_fk()
    kind: Mapped[str] = mapped_column(String(16), nullable=False)             # tally|zoho
    # not_connected | connecting | connected | needs_reconnect | ready (Tally: files only)
    status: Mapped[str] = mapped_column(String(20), default="not_connected", nullable=False)
    settings: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    credentials: Mapped[bytes | None] = mapped_column(LargeBinary)
    cursor: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConnectorRun(IdMixin, Base):
    """One sync (or one Tally file in or out): when, what it read and changed, and in
    plain words why it failed. The import jobs it made are listed so each can be
    opened, and undone, on the Imports screen."""
    __tablename__ = "connector_runs"
    __table_args__ = (Index("ix_connector_run_company_started", "company_id", "started_at"),)
    company_id: Mapped[str] = company_fk()
    connector_id: Mapped[str] = mapped_column(String(32), ForeignKey("connectors.id",
                                                                     ondelete="CASCADE"),
                                              index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), default="schedule", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="running", nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    import_job_ids: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    message: Mapped[str | None] = mapped_column(Text)      # plain-language outcome
    error: Mapped[str | None] = mapped_column(Text)        # technical detail, for support
    person_id: Mapped[str | None] = mapped_column(String(32))


class ConnectorDoc(IdMixin, Base):
    """The last version seen of each outside document a sync reads (a Zoho item,
    invoice or sales order), reduced to what DQ-Stack uses. A day's sales are rebuilt
    from every document of that day, so a changed or voided invoice corrects its day
    instead of adding to it, and an unchanged one is recognised and skipped."""
    __tablename__ = "connector_docs"
    __table_args__ = (UniqueConstraint("connector_id", "doc_kind", "external_id"),
                      Index("ix_connector_doc_day", "connector_id", "doc_kind", "day"))
    company_id: Mapped[str] = company_fk()
    connector_id: Mapped[str] = mapped_column(String(32), ForeignKey("connectors.id",
                                                                     ondelete="CASCADE"),
                                              nullable=False)
    doc_kind: Mapped[str] = mapped_column(String(16), nullable=False)    # item|invoice|salesorder
    external_id: Mapped[str] = mapped_column(String(64), nullable=False)
    day: Mapped[date | None] = mapped_column(Date)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    data: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PlatformStatus(Base):
    """Small facts about the running system, for the platform's health page: the
    worker's last pass, the last backup check. Not company data."""
    __tablename__ = "platform_status"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 nullable=False)


class PurgePermit(Base):
    """The only way a departing company's ledger and log rows can be deleted.

    Section 13: when a client leaves they get a full export, and their data is deleted
    after a confirmed period. The append-only triggers refuse DELETE on stock movements
    and the activity log unless a permit for that company exists, and the purge writes
    and removes its permit inside the same transaction. UPDATE is never permitted."""
    __tablename__ = "purge_permits"
    company_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    granted_by: Mapped[str | None] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 nullable=False)


# =============================================================== immutability

class ImmutableRecordError(RuntimeError):
    pass


def _forbid(kind: str):
    def _raise(mapper, connection, target):  # noqa: ARG001
        raise ImmutableRecordError(
            f"{target.__class__.__name__} records are never {kind}d. "
            "Correct a mistake with a new record instead.")
    return _raise


for _cls in (StockMovement, ActivityLog):
    event.listen(_cls, "before_update", _forbid("update"))
    event.listen(_cls, "before_delete", _forbid("delete"))
