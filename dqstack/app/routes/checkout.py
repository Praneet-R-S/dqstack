"""The Checkout and Bills screens (build step 9).

    GET  /api/checkout/scan?code=            what a scan adds
    POST /api/checkout/bills                 complete a paid bill (the paid-bill rule)
    POST /api/checkout/sync                  an offline till's bills, in order
    POST /api/checkout/hold                  an open or held bill (no stock change)
    POST /api/checkout/void                  void before payment, with a reason
    POST /api/checkout/refunds               refund with a manager PIN and a reason
    POST /api/checkout/heartbeat             a till reports how many bills it holds
    GET  /api/checkout/bills                 find a bill (number, date, amount)
    GET  /api/checkout/bills/{id}            one bill, its receipt and a WhatsApp link
    GET  /api/checkout/z-report?store_id&day the end-of-day report
"""

from __future__ import annotations

from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import checkout as K
from ..db import get_db
from ..deps import ctx
from ..models import Bill, Person, Store
from ..scope import Ctx, get_or_404, store_or_404

router = APIRouter(prefix="/api/checkout", tags=["checkout"])


def _till_store(db: Session, c: Ctx, store_id: str) -> Store:
    store = store_or_404(db, c, store_id)
    c.require("sales.bill", store.id)
    if store.mode != "connected":
        raise HTTPException(409, f"{store.name} runs standalone: its sales come from the daily "
                                 "file. Switch it to connected in Settings to use Checkout.")
    return store


def bill_dict(b: Bill, db: Session | None = None) -> dict:
    d = {"id": b.id, "bill_number": b.bill_number, "store_id": b.store_id, "state": b.state,
         "total_paise": b.total_paise, "tax_paise": b.tax_paise,
         "discount_paise": b.discount_paise, "change_paise": b.change_paise,
         "paid_at": b.paid_at.isoformat() if b.paid_at else None,
         "business_day": b.business_day.isoformat() if b.business_day else None,
         "made_offline": b.made_offline, "refund_of": b.refund_of,
         "void_reason": b.void_reason, "refund_reason": b.refund_reason,
         "lines": [{"product_id": l.product_id, "description": l.description,
                    "unmapped_barcode": l.unmapped_barcode, "qty_milli": l.qty_milli,
                    "unit_price_paise": l.unit_price_paise, "discount_paise": l.discount_paise,
                    "tax_paise": l.tax_paise, "original_price_paise": l.original_price_paise,
                    "override_reason": l.override_reason, "resaleable": l.resaleable}
                   for l in b.lines],
         "payments": [{"method": p.method, "amount_paise": p.amount_paise,
                       "tendered_paise": p.tendered_paise, "reference": p.reference}
                      for p in b.payments]}
    return d


@router.get("/scan")
def scan(code: str = Query(min_length=1, max_length=64), c: Ctx = Depends(ctx),
         db: Session = Depends(get_db)):
    c.require("data.read")
    return K.scan(db, c.company_id, code)


class LineIn(BaseModel):
    product_id: str | None = None
    barcode: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=300)
    qty_milli: int
    unit_price_paise: int | None = Field(default=None, ge=0)
    discount_paise: int = Field(default=0, ge=0)
    override_reason: str | None = Field(default=None, max_length=200)


class PaymentIn(BaseModel):
    method: str
    amount_paise: int
    tendered_paise: int | None = None
    reference: str | None = Field(default=None, max_length=64)


class BillIn(BaseModel):
    store_id: str
    bill_number: str = Field(min_length=1, max_length=64)
    device_id: str | None = Field(default=None, max_length=64)
    till: str | None = Field(default=None, max_length=32)
    offline: bool = False
    paid_at: str | None = None
    lines: list[LineIn] = Field(max_length=500)
    payments: list[PaymentIn] = Field(max_length=10)
    manager_pin: str | None = Field(default=None, max_length=8)
    manager_id: str | None = None


@router.post("/bills", status_code=201)
def pay(body: BillIn, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    store = _till_store(db, c, body.store_id)
    bill, dup = K.pay(db, c, store, body.model_dump())
    db.commit()
    return {**bill_dict(bill), "duplicate": dup}


class SyncIn(BaseModel):
    store_id: str
    device_id: str = Field(min_length=1, max_length=64)
    bills: list[BillIn] = Field(max_length=500)
    pending_after: int = 0


@router.post("/sync")
def sync(body: SyncIn, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    """Bills a till completed while offline, in the order they happened. Each is
    applied once however often the queue is sent; one bad bill does not hold up the
    rest, and is reported so the till can show it."""
    store = _till_store(db, c, body.store_id)
    results = []
    for b in body.bills:
        try:
            bill, dup = K.pay(db, c, store, {**b.model_dump(), "offline": True,
                                             "device_id": body.device_id})
            db.commit()
            results.append({"bill_number": b.bill_number, "status": "duplicate" if dup else "posted"})
        except HTTPException as e:
            db.rollback()
            results.append({"bill_number": b.bill_number, "status": "refused", "error": e.detail})
    K.heartbeat(db, c, store, body.device_id, body.pending_after)
    db.commit()
    return {"results": results,
            "posted": sum(r["status"] == "posted" for r in results),
            "duplicates": sum(r["status"] == "duplicate" for r in results),
            "refused": sum(r["status"] == "refused" for r in results)}


class HoldIn(BaseModel):
    store_id: str
    bill_number: str = Field(min_length=1, max_length=64)
    device_id: str | None = None
    held: bool = True
    lines: list[LineIn] = Field(max_length=500)


@router.post("/hold")
def hold(body: HoldIn, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    store = _till_store(db, c, body.store_id)
    bill = K.hold(db, c, store, body.model_dump())
    db.commit()
    return bill_dict(bill)


class VoidIn(BaseModel):
    store_id: str
    bill_number: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=3, max_length=200)
    lines: list[LineIn] = Field(default_factory=list, max_length=500)


@router.post("/void")
def void(body: VoidIn, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    store = _till_store(db, c, body.store_id)
    bill = K.void_by_number(db, c, store, body.bill_number, body.reason,
                            [l.model_dump() for l in body.lines])
    db.commit()
    return bill_dict(bill)


class RefundLineIn(BaseModel):
    product_id: str
    qty_milli: int
    resaleable: bool = True


class RefundIn(BaseModel):
    store_id: str
    refund_number: str = Field(min_length=1, max_length=64)
    original_bill_number: str = Field(min_length=1, max_length=64)
    lines: list[RefundLineIn] = Field(max_length=500)
    reason: str = Field(min_length=1, max_length=200)
    method: str = "cash"
    manager_pin: str | None = Field(default=None, max_length=8)
    manager_id: str | None = None
    device_id: str | None = None


@router.post("/refunds", status_code=201)
def refund(body: RefundIn, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    store = _till_store(db, c, body.store_id)
    bill, dup = K.refund(db, c, store, body.model_dump())
    db.commit()
    return {**bill_dict(bill), "duplicate": dup}


class HeartbeatIn(BaseModel):
    store_id: str
    device_id: str = Field(min_length=1, max_length=64)
    pending: int = Field(default=0, ge=0)
    name: str | None = Field(default=None, max_length=64)


@router.post("/heartbeat")
def heartbeat(body: HeartbeatIn, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    store = _till_store(db, c, body.store_id)
    d = K.heartbeat(db, c, store, body.device_id, body.pending, body.name)
    db.commit()
    return {"device_id": d.device_id, "pending": d.pending}


@router.get("/bills")
def find_bills(store_id: str, c: Ctx = Depends(ctx), db: Session = Depends(get_db),
               day: date | None = None, number: str | None = Query(None, max_length=64),
               amount_paise: int | None = None, limit: int = Query(50, ge=1, le=500)):
    store = store_or_404(db, c, store_id)
    c.require("data.read", store.id)
    q = select(Bill).where(Bill.company_id == c.company_id, Bill.store_id == store.id)
    if day:
        q = q.where(Bill.business_day == day)
    if number:
        q = q.where(Bill.bill_number.ilike(f"%{number.strip()}%"))
    if amount_paise is not None:
        q = q.where(Bill.total_paise == amount_paise)
    return [bill_dict(b) for b in db.scalars(q.order_by(Bill.created_at.desc()).limit(limit))]


@router.get("/bills/{bill_id}")
def get_bill(bill_id: str, c: Ctx = Depends(ctx), db: Session = Depends(get_db),
             phone: str | None = Query(None, max_length=20)):
    b = get_or_404(db, c, Bill, bill_id, what="That bill")
    text = K.receipt_text(db, b)
    out = {**bill_dict(b), "receipt": text}
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if len(digits) == 10:
        digits = "91" + digits
    if len(digits) >= 11:
        out["whatsapp_link"] = f"https://wa.me/{digits}?text={quote(text)}"
    names = {p.id: p.name for p in db.scalars(select(Person).where(
        Person.id.in_([x for x in (b.cashier_id, b.approved_by) if x])))}
    out["cashier"], out["approved_by"] = names.get(b.cashier_id), names.get(b.approved_by)
    return out


@router.get("/z-report")
def z_report(store_id: str, c: Ctx = Depends(ctx), db: Session = Depends(get_db),
             day: date | None = None):
    store = store_or_404(db, c, store_id)
    c.require("data.read", store.id)
    from ..sales import local_today, tz_of
    return K.z_report(db, c.company_id, store, day or local_today(tz_of(db, c.company_id)))
