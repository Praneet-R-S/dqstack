"""The floor screens: Count, Receive, Transfers and Wastage (build step 5).

Every action accepts an idempotency `key` made on the device, so a phone that was
offline can send its queue again without anything happening twice.
"""

from __future__ import annotations

import base64
import binascii
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import audit, floor, ledger
from ..db import get_db, utcnow
from ..deps import ctx
from ..formats import qty as fmt_qty
from ..models import (Attachment, CountLine, CountSession, Delivery, DeliveryLine, Person,
                      Product, PurchaseOrder, StockMovement, Store, Transfer)
from ..scope import Ctx, get_or_404, store_or_404

router = APIRouter(prefix="/api", tags=["floor"])


# ===================================================================== counts

@router.get("/counts/due")
def due(store_id: str, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    store = store_or_404(db, c, store_id)
    c.require("stock.count", store.id)
    rows = floor.due_list(db, c.company_id, store)
    return {"store_id": store.id, "total": len(rows), "rows": rows}


class SessionIn(BaseModel):
    store_id: str
    kind: str = "cycle"
    blind: bool = False
    name: str | None = Field(default=None, max_length=120)
    product_ids: list[str] = Field(default_factory=list)
    key: str | None = Field(default=None, max_length=128)


def _can_see_expected(c: Ctx, session: CountSession) -> bool:
    return not session.blind or c.can("stock.approve_diff", session.store_id)


def session_dict(db: Session, c: Ctx, s: CountSession) -> dict:
    show = _can_see_expected(c, s)
    prods = {p.id: p for p in db.scalars(select(Product).where(
        Product.id.in_({l.product_id for l in s.lines} | set(s.product_ids or []))))}
    names = {p.id: p.name for p in db.scalars(select(Person).where(
        Person.company_id == c.company_id))}
    lines = []
    for l in sorted(s.lines, key=lambda x: x.counted_at):
        p = prods.get(l.product_id)
        d = {"id": l.id, "product_id": l.product_id, "sku": p.sku if p else None,
             "name": p.name if p else None, "unit": p.unit if p else "each",
             "counted_milli": l.counted_milli, "status": l.status,
             "counted_by": names.get(l.counted_by), "counted_at": l.counted_at.isoformat(),
             "note": l.note, "reason": l.reason}
        if show:
            d.update({"system_milli": l.system_milli, "difference_milli": l.difference_milli,
                      "difference_value_paise": abs(l.difference_milli) * (l.unit_cost_paise or 0)
                      // 1000})
        lines.append(d)
    counted = {l.product_id for l in s.lines}
    todo = [{"product_id": pid, "sku": prods[pid].sku, "name": prods[pid].name,
             "unit": prods[pid].unit} for pid in (s.product_ids or [])
            if pid not in counted and pid in prods]
    return {"id": s.id, "store_id": s.store_id, "kind": s.kind, "name": s.name, "blind": s.blind,
            "status": s.status, "shows_expected": show,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "submitted_at": s.submitted_at.isoformat() if s.submitted_at else None,
            "closed_at": s.closed_at.isoformat() if s.closed_at else None,
            "lines": lines, "still_to_count": todo,
            "waiting": sum(1 for l in s.lines if l.status in ("waiting", "conflict"))}


@router.post("/counts", status_code=201)
def start_count(body: SessionIn, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    store = store_or_404(db, c, body.store_id)
    c.require("stock.count", store.id)
    if body.kind not in ("cycle", "full", "opening"):
        raise HTTPException(422, "A count is cycle, full or opening.")
    if body.key:
        seen = db.scalar(select(CountSession).where(CountSession.company_id == c.company_id,
                                                    CountSession.idempotency_key == body.key))
        if seen is not None:
            return session_dict(db, c, seen)
    ids = body.product_ids
    if body.kind == "cycle" and not ids:
        ids = [r["product_id"] for r in floor.due_list(db, c.company_id, store)]
    for pid in ids:
        get_or_404(db, c, Product, pid, store_attr=None, what="A product on this count")
    s = CountSession(company_id=c.company_id, store_id=store.id, kind=body.kind,
                     blind=body.blind, name=body.name, product_ids=ids, started_by=c.person.id,
                     idempotency_key=body.key)
    db.add(s)
    db.flush()
    audit.log(db, c, "count.started", "count", s.id, after={"kind": s.kind, "blind": s.blind,
                                                            "products": len(ids)},
              store_id=store.id)
    db.commit()
    return session_dict(db, c, s)


@router.get("/counts")
def list_counts(c: Ctx = Depends(ctx), db: Session = Depends(get_db),
                store_id: str | None = None, status: str | None = None,
                limit: int = Query(50, ge=1, le=500)):
    c.require("data.read")
    ids = [store_or_404(db, c, store_id).id] if store_id else c.visible_store_ids(db)
    q = select(CountSession).where(CountSession.company_id == c.company_id,
                                   CountSession.store_id.in_(ids))
    if status:
        q = q.where(CountSession.status == status)
    out = []
    for s in db.scalars(q.order_by(CountSession.created_at.desc()).limit(limit)):
        out.append({"id": s.id, "store_id": s.store_id, "kind": s.kind, "name": s.name,
                    "status": s.status, "blind": s.blind, "lines": len(s.lines),
                    "waiting": sum(1 for l in s.lines if l.status in ("waiting", "conflict")),
                    "created_at": s.created_at.isoformat() if s.created_at else None})
    return out


@router.get("/counts/approvals")
def approvals(c: Ctx = Depends(ctx), db: Session = Depends(get_db), store_id: str | None = None):
    """Count differences waiting for a manager, across every open count."""
    ids = [store_or_404(db, c, store_id).id] if store_id else c.visible_store_ids(db)
    ids = [i for i in ids if c.can("stock.approve_diff", i)]
    if not ids:
        raise HTTPException(403, "Only a manager approves count differences.")
    rows = db.execute(select(CountLine, CountSession, Product).join(
        CountSession, CountSession.id == CountLine.session_id).join(
        Product, Product.id == CountLine.product_id).where(
        CountLine.company_id == c.company_id, CountSession.store_id.in_(ids),
        CountLine.status.in_(("waiting", "conflict"))).order_by(CountLine.counted_at))
    names = {p.id: p.name for p in db.scalars(select(Person).where(
        Person.company_id == c.company_id))}
    return [{"line_id": l.id, "session_id": s.id, "store_id": s.store_id, "product_id": p.id,
             "sku": p.sku, "name": p.name, "unit": p.unit, "status": l.status,
             "counted_milli": l.counted_milli, "system_milli": l.system_milli,
             "difference_milli": l.difference_milli,
             "difference_value_paise": abs(l.difference_milli) * (l.unit_cost_paise or 0) // 1000,
             "counted_by": names.get(l.counted_by), "counted_at": l.counted_at.isoformat(),
             "note": l.note} for l, s, p in rows]


@router.get("/counts/{session_id}")
def get_count(session_id: str, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    s = get_or_404(db, c, CountSession, session_id, what="That count")
    c.require("data.read", s.store_id)
    return session_dict(db, c, s)


class LineIn(BaseModel):
    product_id: str
    counted_milli: int
    counted_at: datetime | None = None
    key: str | None = Field(default=None, max_length=128)
    note: str | None = Field(default=None, max_length=300)


def _line_result(db: Session, c: Ctx, s: CountSession, line: CountLine, dup: bool) -> dict:
    p = db.get(Product, line.product_id)
    d = {"line_id": line.id, "status": line.status, "duplicate": dup, "product_id": p.id,
         "counted_milli": line.counted_milli}
    if _can_see_expected(c, s):
        d.update({"system_milli": line.system_milli, "difference_milli": line.difference_milli})
    d["message"] = {
        "posted": "Saved. Stock updated." if line.difference_milli else "Saved.",
        "confirmed": "Saved. Matches the system.",
        "waiting": "Saved. The difference is large, so a manager will review it.",
        "conflict": "Saved. Someone else counted this differently; a manager will pick one.",
    }.get(line.status, "Saved.")
    return d


@router.post("/counts/{session_id}/lines")
def add_line(session_id: str, body: LineIn, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    s = get_or_404(db, c, CountSession, session_id, what="That count")
    c.require("stock.count", s.store_id)
    p = get_or_404(db, c, Product, body.product_id, store_attr=None, what="That product")
    line, dup = floor.record_count(db, c, s, p, body.counted_milli, body.counted_at, body.key,
                                   c.device, body.note)
    db.commit()
    return _line_result(db, c, s, line, dup)


class BatchIn(BaseModel):
    lines: list[LineIn] = Field(max_length=2000)


@router.post("/counts/{session_id}/lines/batch")
def add_lines(session_id: str, body: BatchIn, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    """An offline phone's queue, in the order it was counted. Each line is applied
    once, however many times the queue is sent."""
    s = get_or_404(db, c, CountSession, session_id, what="That count")
    c.require("stock.count", s.store_id)
    results = []
    for ln in body.lines:
        p = get_or_404(db, c, Product, ln.product_id, store_attr=None, what="That product")
        line, dup = floor.record_count(db, c, s, p, ln.counted_milli, ln.counted_at, ln.key,
                                       c.device, ln.note)
        results.append(_line_result(db, c, s, line, dup))
    db.commit()
    return {"results": results, "applied": sum(1 for r in results if not r["duplicate"]),
            "repeats": sum(1 for r in results if r["duplicate"])}


@router.post("/counts/{session_id}/submit")
def submit_count(session_id: str, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    s = get_or_404(db, c, CountSession, session_id, what="That count")
    c.require("stock.count", s.store_id)
    if s.status == "open":
        s.status = "submitted"
        s.submitted_at = utcnow()
        floor.maybe_close(db, s)
        audit.log(db, c, "count.submitted", "count", s.id, after={"lines": len(s.lines)},
                  store_id=s.store_id)
        db.commit()
    return session_dict(db, c, s)


class DecideIn(BaseModel):
    action: str
    reason: str = Field(min_length=1, max_length=300)


@router.post("/counts/lines/{line_id}/decide")
def decide(line_id: str, body: DecideIn, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    line = get_or_404(db, c, CountLine, line_id, store_attr=None, what="That count line")
    s = db.get(CountSession, line.session_id)
    store_or_404(db, c, s.store_id)
    floor.decide_line(db, c, line, body.action, body.reason)
    db.commit()
    return _line_result(db, c, s, line, False)


# ===================================================================== receiving

@router.get("/receive/orders")
def open_orders(store_id: str, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    store = store_or_404(db, c, store_id)
    c.require("stock.receive", store.id)
    from .orders import order_dict
    q = select(PurchaseOrder).where(PurchaseOrder.company_id == c.company_id,
                                    PurchaseOrder.store_id == store.id,
                                    PurchaseOrder.status.in_(("approved", "sent", "partly_received")))
    return [order_dict(db, po) for po in db.scalars(q.order_by(PurchaseOrder.created_at))]


class DeliveryLineIn(BaseModel):
    product_id: str
    received_milli: int = 0
    damaged_milli: int = 0
    reject_reason: str | None = None
    unit_cost_paise: int | None = Field(default=None, ge=0)
    batch: str | None = Field(default=None, max_length=64)
    expiry: date | None = None


class DeliveryIn(BaseModel):
    store_id: str
    po_id: str | None = None
    supplier_id: str | None = None
    invoice_number: str | None = Field(default=None, max_length=64)
    lines: list[DeliveryLineIn] = Field(max_length=2000)
    close_short: bool = False
    note: str | None = Field(default=None, max_length=300)
    key: str | None = Field(default=None, max_length=128)


@router.post("/deliveries", status_code=201)
def receive(body: DeliveryIn, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    store = store_or_404(db, c, body.store_id)
    c.require("stock.receive", store.id)
    po = get_or_404(db, c, PurchaseOrder, body.po_id, what="That order") if body.po_id else None
    if body.close_short and po is not None:
        c.require("orders.create", store.id)
    d, dup = floor.receive(db, c, store=store, po=po, supplier_id=body.supplier_id,
                           invoice_number=body.invoice_number,
                           lines=[l.model_dump() for l in body.lines], key=body.key,
                           close_short=body.close_short, note=body.note)
    if not dup:
        good = sum(l.received_milli for l in d.lines)
        bad = sum(l.damaged_milli for l in d.lines)
        audit.log(db, c, "delivery.received", "delivery", d.id, after={
            "po": po.number if po else None, "invoice": d.invoice_number,
            "lines": len(d.lines), "good_units": good / 1000, "rejected_units": bad / 1000,
            "order_status": po.status if po else None}, store_id=store.id)
    db.commit()
    out = floor.delivery_dict(db, d)
    out["duplicate"] = dup
    if po is not None:
        from .orders import order_dict
        out["order"] = order_dict(db, po)
    return out


@router.get("/deliveries")
def list_deliveries(c: Ctx = Depends(ctx), db: Session = Depends(get_db),
                    store_id: str | None = None, limit: int = Query(50, ge=1, le=500)):
    c.require("data.read")
    ids = [store_or_404(db, c, store_id).id] if store_id else c.visible_store_ids(db)
    q = (select(Delivery).where(Delivery.company_id == c.company_id, Delivery.store_id.in_(ids))
         .order_by(Delivery.created_at.desc()).limit(limit))
    return [floor.delivery_dict(db, d) for d in db.scalars(q)]


@router.get("/deliveries/credits")
def credits(c: Ctx = Depends(ctx), db: Session = Depends(get_db), store_id: str | None = None):
    """Rejected units waiting for a supplier credit note."""
    c.require("data.read")
    ids = [store_or_404(db, c, store_id).id] if store_id else c.visible_store_ids(db)
    rows = db.execute(select(DeliveryLine, Delivery, Product).join(
        Delivery, Delivery.id == DeliveryLine.delivery_id).join(
        Product, Product.id == DeliveryLine.product_id).where(
        Delivery.company_id == c.company_id, Delivery.store_id.in_(ids),
        DeliveryLine.credit_status == "pending").order_by(Delivery.created_at))
    return [{"delivery_id": d.id, "line_id": l.id, "store_id": d.store_id,
             "supplier_id": d.supplier_id, "invoice_number": d.invoice_number,
             "sku": p.sku, "name": p.name, "units_milli": l.damaged_milli,
             "reason": l.reject_reason,
             "value_paise": l.damaged_milli * (l.unit_cost_paise or 0) // 1000,
             "received_on": d.created_at.isoformat() if d.created_at else None}
            for l, d, p in rows]


@router.post("/deliveries/lines/{line_id}/credited")
def mark_credited(line_id: str, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    line = get_or_404(db, c, DeliveryLine, line_id, store_attr=None, what="That delivery line")
    d = db.get(Delivery, line.delivery_id)
    store_or_404(db, c, d.store_id)
    c.require("orders.create", d.store_id)
    line.credit_status = "credited"
    audit.log(db, c, "delivery.credited", "delivery", d.id, after={"line": line.id},
              store_id=d.store_id)
    db.commit()
    return {"ok": True}


@router.get("/deliveries/{delivery_id}")
def get_delivery(delivery_id: str, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    d = get_or_404(db, c, Delivery, delivery_id, what="That delivery")
    return floor.delivery_dict(db, d)


# ===================================================================== transfers

class TransferLineIn(BaseModel):
    product_id: str
    qty_milli: int


class TransferIn(BaseModel):
    from_store_id: str
    to_store_id: str
    lines: list[TransferLineIn] = Field(max_length=2000)
    note: str | None = Field(default=None, max_length=300)
    key: str | None = Field(default=None, max_length=128)


@router.post("/transfers", status_code=201)
def send(body: TransferIn, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    src = store_or_404(db, c, body.from_store_id)
    c.require("stock.transfer", src.id)
    dst = db.get(Store, body.to_store_id)
    if dst is None or dst.company_id != c.company_id:
        raise HTTPException(404, "The receiving store was not found.")
    t, dup = floor.send_transfer(db, c, from_store=src, to_store=dst,
                                 lines=[l.model_dump() for l in body.lines], key=body.key,
                                 note=body.note)
    if not dup:
        audit.log(db, c, "transfer.sent", "transfer", t.id, after={
            "to": dst.code, "lines": len(t.lines)}, store_id=src.id)
    db.commit()
    return {**floor.transfer_dict(db, t), "duplicate": dup}


class ReceiveTransferIn(BaseModel):
    lines: list[TransferLineIn] = Field(default_factory=list, max_length=2000)
    key: str | None = Field(default=None, max_length=128)


@router.post("/transfers/{transfer_id}/receive")
def receive_transfer(transfer_id: str, body: ReceiveTransferIn, c: Ctx = Depends(ctx),
                     db: Session = Depends(get_db)):
    t = get_or_404(db, c, Transfer, transfer_id, store_attr=None, what="That transfer")
    if not c.all_stores and t.to_store_id not in c.store_ids and t.from_store_id not in c.store_ids:
        raise HTTPException(404, "That transfer was not found.")
    c.require("stock.transfer", t.to_store_id)
    t, losses = floor.receive_transfer(db, c, t, {l.product_id: l.qty_milli for l in body.lines},
                                       body.key)
    db.commit()
    return {**floor.transfer_dict(db, t), "losses": losses}


@router.get("/transfers")
def list_transfers(c: Ctx = Depends(ctx), db: Session = Depends(get_db),
                   store_id: str | None = None, status: str | None = None,
                   limit: int = Query(50, ge=1, le=500)):
    c.require("data.read")
    ids = [store_or_404(db, c, store_id).id] if store_id else c.visible_store_ids(db)
    q = select(Transfer).where(Transfer.company_id == c.company_id,
                               or_(Transfer.from_store_id.in_(ids), Transfer.to_store_id.in_(ids)))
    if status:
        q = q.where(Transfer.status == status)
    return [floor.transfer_dict(db, t) for t in db.scalars(
        q.order_by(Transfer.created_at.desc()).limit(limit))]


@router.get("/transfers/{transfer_id}")
def get_transfer(transfer_id: str, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    t = get_or_404(db, c, Transfer, transfer_id, store_attr=None, what="That transfer")
    if not c.all_stores and t.to_store_id not in c.store_ids and t.from_store_id not in c.store_ids:
        raise HTTPException(404, "That transfer was not found.")
    return floor.transfer_dict(db, t)


# ===================================================================== wastage

class WastageIn(BaseModel):
    store_id: str
    product_id: str
    qty_milli: int = Field(gt=0)
    reason: str
    note: str | None = Field(default=None, max_length=300)
    photo_base64: str | None = Field(default=None, max_length=3_000_000)
    photo_type: str | None = Field(default=None, max_length=64)
    key: str | None = Field(default=None, max_length=128)


@router.post("/wastage", status_code=201)
def write_off(body: WastageIn, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    """Expired, damaged, spoiled or found stolen: posted as a movement at cost, with an
    optional photo. Never counted as demand by the forecast."""
    store = store_or_404(db, c, body.store_id)
    c.require("stock.wastage", store.id)
    p = get_or_404(db, c, Product, body.product_id, store_attr=None, what="That product")
    typ = floor.WASTAGE_REASONS.get(body.reason)
    if typ is None:
        raise HTTPException(422, "Reason must be expired, damaged, spoiled, theft_found or other.")
    if body.reason == "other" and not (body.note or "").strip():
        raise HTTPException(422, "Say what happened when the reason is other.")
    if p.unit == "each" and body.qty_milli % 1000:
        raise HTTPException(422, f"{p.name} is written off in whole units.")
    photo = None
    if body.photo_base64:
        if (body.photo_type or "") not in ("image/jpeg", "image/png", "image/webp"):
            raise HTTPException(422, "The photo must be a JPEG, PNG or WebP image.")
        try:
            photo = base64.b64decode(body.photo_base64, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(422, "The photo could not be read.") from None
        if len(photo) > 2_000_000:
            raise HTTPException(422, "The photo is larger than 2 MB.")
    reason = body.reason.replace("_", " ") + (f": {body.note.strip()}" if body.note else "")
    res = ledger.post(db, company_id=c.company_id, store_id=store.id, product_id=p.id,
                      qty_milli=-body.qty_milli, type=typ, reason=reason, source_type="wastage",
                      person_id=c.person.id, device=c.device, idempotency_key=body.key)
    photo_id = None
    if photo is not None and not res.duplicate:
        a = Attachment(company_id=c.company_id, owner_type="movement", owner_id=res.movement.id,
                       content_type=body.photo_type, size=len(photo), content=photo,
                       person_id=c.person.id)
        db.add(a)
        db.flush()
        photo_id = a.id
    if not res.duplicate:
        audit.log(db, c, "stock.written_off", "product", p.id, after={
            "qty": fmt_qty(body.qty_milli, p.unit), "reason": reason,
            "value_paise": body.qty_milli * (res.movement.unit_cost_paise or 0) // 1000},
            store_id=store.id)
    db.commit()
    return {"movement_id": res.movement.id, "duplicate": res.duplicate,
            "on_hand_milli": res.on_hand_milli, "photo_id": photo_id,
            "value_paise": body.qty_milli * (res.movement.unit_cost_paise or 0) // 1000}


@router.get("/wastage")
def list_wastage(c: Ctx = Depends(ctx), db: Session = Depends(get_db),
                 store_id: str | None = None, days: int = Query(30, ge=1, le=730)):
    c.require("data.read")
    ids = [store_or_404(db, c, store_id).id] if store_id else c.visible_store_ids(db)
    since = utcnow() - timedelta(days=days)
    rows = db.execute(select(StockMovement, Product).join(
        Product, Product.id == StockMovement.product_id).where(
        StockMovement.company_id == c.company_id, StockMovement.store_id.in_(ids),
        StockMovement.type.in_(("wastage", "damage")), StockMovement.at >= since)
        .order_by(StockMovement.at.desc()))
    photos = {a.owner_id: a.id for a in db.scalars(select(Attachment).where(
        Attachment.company_id == c.company_id, Attachment.owner_type == "movement"))}
    names = {p.id: p.name for p in db.scalars(select(Person).where(
        Person.company_id == c.company_id))}
    return [{"movement_id": m.id, "at": m.at.isoformat(), "store_id": m.store_id,
             "sku": p.sku, "name": p.name, "unit": p.unit, "qty_milli": -m.qty_milli,
             "type": m.type, "reason": m.reason, "person": names.get(m.person_id),
             "value_paise": -m.qty_milli * (m.unit_cost_paise or 0) // 1000,
             "photo_id": photos.get(m.id)} for m, p in rows]


@router.get("/attachments/{attachment_id}")
def attachment(attachment_id: str, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    a = get_or_404(db, c, Attachment, attachment_id, store_attr=None, what="That photo")
    if a.owner_type == "movement":
        m = db.get(StockMovement, a.owner_id)
        if m is not None:
            store_or_404(db, c, m.store_id)
    return Response(a.content, media_type=a.content_type,
                    headers={"Cache-Control": "private, max-age=3600"})
