"""Purchase orders: draft -> approved -> sent -> partly received -> received, or
cancelled (section 4). Build step 5 needs orders to receive against; step 8 adds
the suggested orders, PDF, email and WhatsApp on top of this lifecycle.

Who may do what (section 2): a Purchaser creates orders for their stores, a Store
manager approves them, Owners and Admins do both.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db, utcnow
from ..deps import ctx
from ..models import Person, Product, PurchaseOrder, PurchaseOrderLine, Store, Supplier
from ..scope import Ctx, get_or_404, store_or_404

router = APIRouter(prefix="/api/orders", tags=["orders"])

STATUSES = ("draft", "approved", "sent", "partly_received", "received", "cancelled")


def order_dict(db: Session, po: PurchaseOrder) -> dict:
    prods = {p.id: p for p in db.scalars(select(Product).where(
        Product.id.in_([l.product_id for l in po.lines])))} if po.lines else {}
    sup = db.get(Supplier, po.supplier_id) if po.supplier_id else None
    store = db.get(Store, po.store_id)
    lines = []
    total = 0
    for l in po.lines:
        p = prods.get(l.product_id)
        value = l.qty_milli * (l.unit_cost_paise or 0) // 1000
        total += value
        lines.append({"id": l.id, "product_id": l.product_id, "sku": p.sku if p else None,
                      "name": p.name if p else None, "unit": p.unit if p else "each",
                      "case_pack": p.case_pack if p else 1, "qty_milli": l.qty_milli,
                      "received_milli": l.received_milli,
                      "due_milli": 0 if po.status in ("received", "cancelled")
                      else max(l.qty_milli - l.received_milli, 0),
                      "unit_cost_paise": l.unit_cost_paise, "value_paise": value,
                      "reason": l.reason})
    return {"id": po.id, "number": po.number, "status": po.status, "store_id": po.store_id,
            "store": store.name if store else None, "supplier_id": po.supplier_id,
            "supplier": sup.name if sup else None,
            "supplier_min_order_paise": sup.min_order_paise if sup else 0,
            "expected_date": po.expected_date.isoformat() if po.expected_date else None,
            "origin": po.origin, "notes": po.notes, "closed_short": po.closed_short,
            "created_at": po.created_at.isoformat() if po.created_at else None,
            "approved_at": po.approved_at.isoformat() if po.approved_at else None,
            "sent_at": po.sent_at.isoformat() if po.sent_at else None,
            "total_paise": total, "lines": lines}


def next_number(db: Session, company_id: str) -> str:
    prefix = f"PO-{utcnow():%y%m}-"
    n = db.scalar(select(func.count()).select_from(PurchaseOrder).where(
        PurchaseOrder.company_id == company_id, PurchaseOrder.number.like(prefix + "%"))) or 0
    return f"{prefix}{n + 1:04d}"


class LineIn(BaseModel):
    product_id: str
    qty_milli: int = Field(gt=0)
    unit_cost_paise: int | None = Field(default=None, ge=0)
    reason: str | None = Field(default=None, max_length=300)


class OrderIn(BaseModel):
    store_id: str
    supplier_id: str | None = None
    lines: list[LineIn] = Field(min_length=1, max_length=5000)
    expected_date: date | None = None
    notes: str | None = Field(default=None, max_length=2000)
    origin: str = "manual"


def _lines(db: Session, c: Ctx, items: list[LineIn], supplier_id: str | None) -> list[PurchaseOrderLine]:
    from ..models import SupplierProduct
    seen: set[str] = set()
    out = []
    for ln in items:
        p = get_or_404(db, c, Product, ln.product_id, store_attr=None, what="A product on this order")
        if p.id in seen:
            raise HTTPException(422, f"{p.name} is on the order twice; put it on one line.")
        seen.add(p.id)
        if p.status == "discontinued":
            raise HTTPException(409, f"{p.name} is discontinued, so it cannot be ordered.")
        if p.unit == "each" and ln.qty_milli % 1000:
            raise HTTPException(422, f"{p.name} is ordered in whole units.")
        cost = ln.unit_cost_paise
        if cost is None and supplier_id:
            cost = db.scalar(select(SupplierProduct.cost_paise).where(
                SupplierProduct.supplier_id == supplier_id, SupplierProduct.product_id == p.id))
        if cost is None:
            cost = p.cost_paise
        out.append(PurchaseOrderLine(company_id=c.company_id, product_id=p.id,
                                     qty_milli=ln.qty_milli, unit_cost_paise=cost,
                                     reason=ln.reason))
    return out


def create_order(db: Session, c: Ctx, body: OrderIn) -> PurchaseOrder:
    store = store_or_404(db, c, body.store_id)
    c.require("orders.create", store.id)
    if body.supplier_id:
        get_or_404(db, c, Supplier, body.supplier_id, store_attr=None, what="That supplier")
    for attempt in range(5):
        po = PurchaseOrder(company_id=c.company_id, number=next_number(db, c.company_id),
                           supplier_id=body.supplier_id, store_id=store.id, status="draft",
                           expected_date=body.expected_date, created_by=c.person.id,
                           origin=body.origin if body.origin in ("manual", "suggested") else "manual",
                           notes=body.notes)
        po.lines = _lines(db, c, body.lines, body.supplier_id)
        db.add(po)
        if db.get_bind().dialect.name != "postgresql":
            db.flush()                 # SQLite serialises writers: no race to lose
            break
        try:
            with db.begin_nested():
                db.flush()
            break
        except IntegrityError:
            db.expunge(po)
            if attempt == 4:
                raise HTTPException(409, "Could not number the order; please try again.") from None
    audit.log(db, c, "order.created", "purchase_order", po.id,
              after={"number": po.number, "lines": len(po.lines)}, store_id=store.id)
    return po


@router.post("", status_code=201)
def create(body: OrderIn, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    po = create_order(db, c, body)
    db.commit()
    return order_dict(db, po)


@router.get("")
def list_orders(c: Ctx = Depends(ctx), db: Session = Depends(get_db),
                store_id: str | None = None, status: str | None = None,
                supplier_id: str | None = None, limit: int = Query(100, ge=1, le=1000)):
    c.require("data.read")
    ids = [store_or_404(db, c, store_id).id] if store_id else c.visible_store_ids(db)
    q = select(PurchaseOrder).where(PurchaseOrder.company_id == c.company_id,
                                    PurchaseOrder.store_id.in_(ids))
    if status:
        q = q.where(PurchaseOrder.status.in_(status.split(",")))
    if supplier_id:
        q = q.where(PurchaseOrder.supplier_id == supplier_id)
    return [order_dict(db, po) for po in db.scalars(
        q.order_by(PurchaseOrder.created_at.desc()).limit(limit))]


@router.get("/{order_id}")
def get_order(order_id: str, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    po = get_or_404(db, c, PurchaseOrder, order_id, what="That order")
    out = order_dict(db, po)
    from ..models import Delivery
    from ..floor import delivery_dict
    out["deliveries"] = [delivery_dict(db, d) for d in db.scalars(
        select(Delivery).where(Delivery.po_id == po.id).order_by(Delivery.created_at))]
    names = {p.id: p.name for p in db.scalars(select(Person).where(
        Person.id.in_([x for x in (po.created_by, po.approved_by) if x])))}
    out["created_by"] = names.get(po.created_by)
    out["approved_by"] = names.get(po.approved_by)
    return out


class OrderPatch(BaseModel):
    lines: list[LineIn] | None = Field(default=None, max_length=5000)
    expected_date: date | None = None
    notes: str | None = Field(default=None, max_length=2000)
    supplier_id: str | None = None


@router.patch("/{order_id}")
def edit(order_id: str, body: OrderPatch, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    po = get_or_404(db, c, PurchaseOrder, order_id, what="That order")
    c.require("orders.create", po.store_id)
    if po.status != "draft":
        raise HTTPException(409, "Only a draft order can be changed. Cancel it and make a new "
                                 "one instead.")
    before = order_dict(db, po)
    if body.supplier_id is not None:
        get_or_404(db, c, Supplier, body.supplier_id, store_attr=None, what="That supplier")
        po.supplier_id = body.supplier_id
    if body.lines is not None:
        if not body.lines:
            raise HTTPException(422, "An order needs at least one line. Cancel it instead.")
        po.lines = _lines(db, c, body.lines, po.supplier_id)
    if "expected_date" in body.model_fields_set:
        po.expected_date = body.expected_date
    if body.notes is not None:
        po.notes = body.notes
    db.flush()
    audit.log(db, c, "order.changed", "purchase_order", po.id,
              {"total_paise": before["total_paise"], "lines": len(before["lines"])},
              {"total_paise": order_dict(db, po)["total_paise"], "lines": len(po.lines)},
              store_id=po.store_id)
    db.commit()
    return order_dict(db, po)


@router.post("/{order_id}/approve")
def approve(order_id: str, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    po = get_or_404(db, c, PurchaseOrder, order_id, what="That order")
    c.require("orders.approve", po.store_id)
    if po.status != "draft":
        raise HTTPException(409, f"This order is already {po.status.replace('_', ' ')}.")
    po.status = "approved"
    po.approved_by = c.person.id
    po.approved_at = utcnow()
    audit.log(db, c, "order.approved", "purchase_order", po.id,
              after={"number": po.number, "total_paise": order_dict(db, po)["total_paise"]},
              store_id=po.store_id)
    db.commit()
    return order_dict(db, po)


@router.post("/{order_id}/mark-sent")
def mark_sent(order_id: str, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    """For an order sent outside the app (phoned in, handed over)."""
    po = get_or_404(db, c, PurchaseOrder, order_id, what="That order")
    c.require("orders.create", po.store_id)
    if po.status not in ("approved", "sent"):
        raise HTTPException(409, "Approve the order before sending it." if po.status == "draft"
                            else f"This order is already {po.status.replace('_', ' ')}.")
    if po.status == "approved":
        po.status = "sent"
        po.sent_at = utcnow()
        audit.log(db, c, "order.sent", "purchase_order", po.id, after={"channel": "outside"},
                  store_id=po.store_id)
        db.commit()
    return order_dict(db, po)


class ReasonIn(BaseModel):
    reason: str = Field(min_length=3, max_length=300)


@router.post("/{order_id}/cancel")
def cancel(order_id: str, body: ReasonIn, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    """Irreversible, so it needs a reason (interface rule 4)."""
    po = get_or_404(db, c, PurchaseOrder, order_id, what="That order")
    c.require("orders.approve", po.store_id)
    if po.status in ("received", "cancelled"):
        raise HTTPException(409, f"This order is already {po.status}.")
    if any(l.received_milli for l in po.lines):
        raise HTTPException(409, "Part of this order has arrived. Close it short instead.")
    po.status = "cancelled"
    audit.log(db, c, "order.cancelled", "purchase_order", po.id, after={"reason": body.reason},
              store_id=po.store_id)
    db.commit()
    return order_dict(db, po)


@router.post("/{order_id}/close-short")
def close_short(order_id: str, body: ReasonIn, c: Ctx = Depends(ctx),
                db: Session = Depends(get_db)):
    """The supplier will not send the rest: stop counting it as on order."""
    po = get_or_404(db, c, PurchaseOrder, order_id, what="That order")
    c.require("orders.create", po.store_id)
    if po.status != "partly_received":
        raise HTTPException(409, "Only a partly received order can be closed short.")
    po.status = "received"
    po.closed_short = True
    audit.log(db, c, "order.closed_short", "purchase_order", po.id,
              after={"reason": body.reason}, store_id=po.store_id)
    db.commit()
    return order_dict(db, po)
