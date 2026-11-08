"""Floor work: counting, receiving, transfers and wastage (section 9, build step 5).

Counting while the store trades. A count is compared with the stock *at the moment
that item was counted*, rebuilt from the movements up to that time. A sale a minute
later is therefore not a difference, and a count that reaches the server an hour
later from a phone that was offline is judged against the right number. The
adjustment is stamped at the counting time for the same reason.

Small differences (within both business-rule limits, 5 units and Rs 500 by default)
post by themselves; larger ones wait for a manager. A product whose stock was
unknown is not a difference at all: its first count sets its stock.

Everything here takes an idempotency key from the device, so work done offline and
sent twice is applied once.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import audit, ledger
from . import rules as rules_mod
from .db import utcnow
from .formats import qty as fmt_qty
from .formats import rupees
from .models import (Company, CountLine, CountSession, DailySales, Delivery, DeliveryLine,
                     Product, PurchaseOrder, PurchaseOrderLine, StockMovement, StockOnHand,
                     Store, Transfer, TransferLine)
from .security import _aware

WASTAGE_REASONS = {"expired": "wastage", "spoiled": "wastage", "theft_found": "wastage",
                   "damaged": "damage", "other": "wastage"}
REJECT_REASONS = ("damaged", "wrong_item", "expired", "short_dated")


# ===================================================================== counting

def on_hand_at(db: Session, store_id: str, product_id: str, at: datetime) -> int:
    v = db.scalar(select(func.coalesce(func.sum(StockMovement.qty_milli), 0)).where(
        StockMovement.store_id == store_id, StockMovement.product_id == product_id,
        StockMovement.at <= at))
    return int(v or 0)


def _clamp_time(at: datetime | None, session: CountSession) -> datetime:
    """Trust the device's clock only within sensible bounds."""
    now = utcnow()
    if at is None:
        return now
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    # Phones send local time with an offset (+05:30). Store and compare in UTC only:
    # SQLite keeps datetimes without their offset, so a mixed one would be 5.5 h off.
    at = at.astimezone(timezone.utc)
    earliest = _aware(session.created_at) - timedelta(days=1)
    return min(max(at, earliest), now)


def abc_classes(db: Session, company_id: str, store_id: str, days: int = 90) -> dict[str, str]:
    """Class A: products making the first 80% of sales value; B the next 15%; C the
    rest and anything unsold. A product's stored class, when the engine has set one,
    wins."""
    since = date.today() - timedelta(days=days)
    revenue = sorted(((int(v or 0), pid) for pid, v in db.execute(
        select(DailySales.product_id, func.sum(DailySales.revenue_paise)).where(
            DailySales.company_id == company_id, DailySales.store_id == store_id,
            DailySales.day >= since).group_by(DailySales.product_id))), reverse=True)
    total = sum(v for v, _ in revenue) or 1
    out: dict[str, str] = {}
    running = 0
    for v, pid in revenue:
        share = running / total
        out[pid] = "A" if share < 0.80 else ("B" if share < 0.95 else "C")
        running += v
    return out


def due_list(db: Session, company_id: str, store: Store, today: date | None = None) -> list[dict]:
    """The Stock take list: what is due for a cycle count, most valuable first."""
    today = today or date.today()
    co = db.get(Company, company_id)
    intervals = rules_mod.effective(co.rules)["count_interval_days"]
    computed = abc_classes(db, company_id, store.id)
    soh = {r.product_id: r for r in db.scalars(select(StockOnHand).where(
        StockOnHand.store_id == store.id))}
    out = []
    for p in db.scalars(select(Product).where(Product.company_id == company_id,
                                              Product.status != "discontinued")):
        cls = p.abc_class or computed.get(p.id, "C")
        row = soh.get(p.id)
        last = _aware(row.last_counted_at) if row and row.last_counted_at else None
        interval = int(intervals.get(cls, 91))
        due_on = (last.date() + timedelta(days=interval)) if last else today
        if due_on > today:
            continue
        oh = row.on_hand_milli if row else 0
        cost = (row.avg_cost_paise if row and row.avg_cost_paise is not None else p.cost_paise) or 0
        out.append({"product_id": p.id, "sku": p.sku, "name": p.name, "unit": p.unit,
                    "abc_class": cls, "last_counted_at": last.isoformat() if last else None,
                    "due_since": due_on.isoformat(), "known": bool(row and row.known),
                    "value_paise": max(oh, 0) * cost // 1000})
    out.sort(key=lambda r: ("ABC".index(r["abc_class"]), -r["value_paise"], r["name"]))
    return out


def _limits(db: Session, company_id: str) -> tuple[int, int]:
    r = rules_mod.effective(db.get(Company, company_id).rules)
    return int(r["count_auto_post_units"]) * 1000, int(r["count_auto_post_paise"])


def _post_line(db: Session, c, session: CountSession, line: CountLine, *, first: bool) -> None:
    """Post a line's difference (or set first stock) and mark the product counted."""
    row = ledger._soh(db, c.company_id, session.store_id, line.product_id)
    if first:
        typ = "opening" if row.opening_milli is None and not db.scalar(
            select(func.count()).select_from(StockMovement).where(
                StockMovement.store_id == session.store_id,
                StockMovement.product_id == line.product_id)) else "count_adjustment"
        if typ == "opening" and line.counted_milli == 0:
            row.opening_milli = 0
            line.movement_id = None
        elif line.difference_milli != 0 or typ == "opening":
            qty = line.counted_milli if typ == "opening" else line.difference_milli
            res = ledger.post(db, company_id=c.company_id, store_id=session.store_id,
                              product_id=line.product_id, qty_milli=qty, type=typ,
                              reason="First count", source_type="count", source_id=line.id,
                              person_id=line.counted_by, device=line.device,
                              unit_cost_paise=line.unit_cost_paise if typ == "opening" else None,
                              idempotency_key=f"count:{line.id}", at=line.counted_at,
                              makes_known=True)
            line.movement_id = res.movement.id
    elif line.difference_milli != 0:
        res = ledger.post(db, company_id=c.company_id, store_id=session.store_id,
                          product_id=line.product_id, qty_milli=line.difference_milli,
                          type="count_adjustment", reason=line.reason or "Count difference",
                          source_type="count", source_id=line.id, person_id=line.counted_by,
                          device=line.device, idempotency_key=f"count:{line.id}",
                          at=line.counted_at)
        line.movement_id = res.movement.id
    row.known = True
    counted = _aware(line.counted_at)
    if row.last_counted_at is None or _aware(row.last_counted_at) < counted:
        row.last_counted_at = counted


def record_count(db: Session, c, session: CountSession, product: Product, counted_milli: int,
                 counted_at: datetime | None, key: str | None, device: str | None,
                 note: str | None) -> tuple[CountLine, bool]:
    """Returns (line, duplicate)."""
    if key:
        seen = db.scalar(select(CountLine).where(CountLine.company_id == c.company_id,
                                                 CountLine.idempotency_key == key))
        if seen is not None:
            return seen, True
    if session.status == "closed":
        raise HTTPException(409, "This count is closed. Start a new one.")
    if counted_milli < 0:
        raise HTTPException(422, "A count cannot be negative.")
    if product.unit == "each" and counted_milli % 1000:
        raise HTTPException(422, f"{product.name} is counted in whole units.")
    at = _clamp_time(counted_at, session)
    soh = db.get(StockOnHand, {"store_id": session.store_id, "product_id": product.id})
    first = soh is None or not soh.known
    system = on_hand_at(db, session.store_id, product.id, at)
    cost = (soh.avg_cost_paise if soh and soh.avg_cost_paise is not None else product.cost_paise)
    line = CountLine(company_id=c.company_id, session_id=session.id, product_id=product.id,
                     counted_milli=counted_milli, system_milli=system,
                     difference_milli=counted_milli - system, unit_cost_paise=cost,
                     counted_by=c.person.id, counted_at=at, device=device, note=note,
                     idempotency_key=key, status="waiting")
    db.add(line)
    db.flush()
    # Two different people counting the same product in one count: keep both and let
    # the manager pick or recount (section 13).
    others = list(db.scalars(select(CountLine).where(
        CountLine.session_id == session.id, CountLine.product_id == product.id,
        CountLine.id != line.id, CountLine.status.in_(("posted", "confirmed", "waiting",
                                                       "conflict")))))
    by_other = [o for o in others if o.counted_by != c.person.id]
    mine = [o for o in others if o.counted_by == c.person.id]
    for o in mine:
        if o.status in ("waiting", "conflict"):
            o.status = "superseded"           # the same person counted again; the later stands
    if by_other and line.difference_milli != 0:
        line.status = "conflict"
        for o in by_other:
            if o.status == "waiting":
                o.status = "conflict"
        return line, False
    if first:
        _post_line(db, c, session, line, first=True)
        line.status = "posted"
        return line, False
    if line.difference_milli == 0:
        line.status = "confirmed"
        _post_line(db, c, session, line, first=False)
        return line, False
    units, paise = _limits(db, c.company_id)
    value = abs(line.difference_milli) * (cost or 0) // 1000
    if abs(line.difference_milli) <= units and value <= paise:
        _post_line(db, c, session, line, first=False)
        line.status = "posted"
    return line, False


def decide_line(db: Session, c, line: CountLine, action: str, reason: str) -> CountLine:
    """approve (post the difference), reject (post nothing; ask for a recount) or pick
    (choose this one of two disagreeing counts)."""
    session = db.get(CountSession, line.session_id)
    c.require("stock.approve_diff", session.store_id)
    if line.status not in ("waiting", "conflict"):
        raise HTTPException(409, "This count line has already been decided.")
    if not reason or len(reason.strip()) < 3:
        raise HTTPException(422, "Give a reason, so the decision makes sense later.")
    line.reason = reason.strip()
    line.decided_by = c.person.id
    line.decided_at = utcnow()
    if action in ("approve", "pick"):
        if action == "approve" and line.status == "conflict":
            raise HTTPException(409, "Two people counted this product differently. Pick one "
                                     "count or ask for a recount.")
        if action == "pick":
            for o in db.scalars(select(CountLine).where(
                    CountLine.session_id == line.session_id,
                    CountLine.product_id == line.product_id, CountLine.id != line.id,
                    CountLine.status == "conflict")):
                o.status = "superseded"
            # Re-judge against the stock at this line's counting time, which now includes
            # anything the other count already posted.
            line.system_milli = on_hand_at(db, session.store_id, line.product_id,
                                           _aware(line.counted_at))
            line.difference_milli = line.counted_milli - line.system_milli
        _post_line(db, c, session, line, first=False)
        line.status = "posted" if line.difference_milli else "confirmed"
    elif action == "reject":
        line.status = "rejected"
    else:
        raise HTTPException(422, "Action must be approve, reject or pick.")
    p = db.get(Product, line.product_id)
    audit.log(db, c, f"count.{action}", "count_line", line.id, after={
        "product": p.sku, "counted": fmt_qty(line.counted_milli, p.unit),
        "system": fmt_qty(line.system_milli, p.unit), "reason": line.reason},
        store_id=session.store_id)
    maybe_close(db, session)
    return line


def maybe_close(db: Session, session: CountSession) -> None:
    if session.status != "submitted":
        return
    db.flush()                      # the session does not autoflush; count current states
    waiting = db.scalar(select(func.count()).select_from(CountLine).where(
        CountLine.session_id == session.id, CountLine.status.in_(("waiting", "conflict"))))
    if not waiting:
        session.status = "closed"
        session.closed_at = utcnow()


# ===================================================================== receiving

def receive(db: Session, c, *, store: Store, po: PurchaseOrder | None, supplier_id: str | None,
            invoice_number: str | None, lines: list[dict], key: str | None,
            close_short: bool, note: str | None) -> tuple[Delivery, bool]:
    """Book in a delivery. Good units go to stock at their cost; damaged, wrong or
    expired units are recorded for supplier credit and never touch stock. Against an
    order, what is still due stays on order unless the purchaser closes it short."""
    if key:
        seen = db.scalar(select(Delivery).where(Delivery.company_id == c.company_id,
                                                Delivery.idempotency_key == key))
        if seen is not None:
            return seen, True
    if po is not None:
        if po.store_id != store.id:
            raise HTTPException(409, "That order is for a different store.")
        if po.status not in ("approved", "sent", "partly_received"):
            raise HTTPException(409, f"That order is {po.status.replace('_', ' ')}, so nothing "
                                     "can be received against it.")
        supplier_id = po.supplier_id
    if not lines:
        raise HTTPException(422, "A delivery needs at least one line.")
    po_lines = {l.product_id: l for l in (po.lines if po else [])}
    d = Delivery(company_id=c.company_id, store_id=store.id, po_id=po.id if po else None,
                 supplier_id=supplier_id, invoice_number=invoice_number, received_by=c.person.id,
                 note=note, idempotency_key=key)
    db.add(d)
    db.flush()
    seen_products: set[str] = set()
    for i, ln in enumerate(lines):
        p = db.get(Product, ln["product_id"])
        if p is None or p.company_id != c.company_id:
            raise HTTPException(404, "A product on this delivery was not found.")
        if p.id in seen_products:
            raise HTTPException(422, f"{p.name} is on the delivery twice; put it on one line.")
        seen_products.add(p.id)
        good, bad = int(ln.get("received_milli") or 0), int(ln.get("damaged_milli") or 0)
        if good < 0 or bad < 0 or (good == 0 and bad == 0):
            raise HTTPException(422, f"Enter how many {p.name} arrived.")
        if p.unit == "each" and (good % 1000 or bad % 1000):
            raise HTTPException(422, f"{p.name} is received in whole units.")
        reason = ln.get("reject_reason") or ("damaged" if bad else None)
        if reason and reason not in REJECT_REASONS:
            raise HTTPException(422, "A rejected quantity is damaged, wrong_item, expired or "
                                     "short_dated.")
        pl = po_lines.get(p.id)
        cost = ln.get("unit_cost_paise")
        if cost is None:
            cost = pl.unit_cost_paise if pl and pl.unit_cost_paise is not None else p.cost_paise
        expiry = ln.get("expiry")
        if isinstance(expiry, str):
            expiry = date.fromisoformat(expiry)
        if p.perishable and good and expiry is None:
            raise HTTPException(422, f"{p.name} is perishable: enter its expiry date.")
        dl = DeliveryLine(company_id=c.company_id, delivery_id=d.id, product_id=p.id,
                          received_milli=good, damaged_milli=bad, reject_reason=reason if bad else None,
                          credit_status="pending" if bad else None, unit_cost_paise=cost,
                          batch=ln.get("batch"), expiry=expiry)
        db.add(dl)
        if good:
            ledger.post(db, company_id=c.company_id, store_id=store.id, product_id=p.id,
                        qty_milli=good, type="delivery",
                        reason=f"Invoice {invoice_number}" if invoice_number else None,
                        source_type="delivery", source_id=d.id, person_id=c.person.id,
                        device=c.device, unit_cost_paise=cost, batch=ln.get("batch"),
                        expiry=expiry, idempotency_key=f"delivery:{d.id}:{i}")
        if pl is not None:
            pl.received_milli += good
    if po is not None:
        outstanding = any(l.received_milli < l.qty_milli for l in po.lines)
        if close_short or not outstanding:
            po.status = "received"
            po.closed_short = bool(close_short and outstanding)
        else:
            po.status = "partly_received"
    db.flush()
    return d, False


def delivery_dict(db: Session, d: Delivery) -> dict:
    names = {p.id: p for p in db.scalars(select(Product).where(
        Product.id.in_([l.product_id for l in d.lines])))}
    return {"id": d.id, "store_id": d.store_id, "po_id": d.po_id, "supplier_id": d.supplier_id,
            "invoice_number": d.invoice_number, "note": d.note,
            "created_at": d.created_at.isoformat() if d.created_at else None,
            "lines": [{"product_id": l.product_id, "sku": names[l.product_id].sku,
                       "name": names[l.product_id].name, "received_milli": l.received_milli,
                       "damaged_milli": l.damaged_milli, "reject_reason": l.reject_reason,
                       "credit_status": l.credit_status, "unit_cost_paise": l.unit_cost_paise,
                       "batch": l.batch, "expiry": l.expiry.isoformat() if l.expiry else None}
                      for l in d.lines]}


# ===================================================================== transfers

def send_transfer(db: Session, c, *, from_store: Store, to_store: Store, lines: list[dict],
                  key: str | None, note: str | None) -> tuple[Transfer, bool]:
    if key:
        seen = db.scalar(select(Transfer).where(Transfer.company_id == c.company_id,
                                                Transfer.idempotency_key == key))
        if seen is not None:
            return seen, True
    if from_store.id == to_store.id:
        raise HTTPException(422, "A transfer goes between two different stores.")
    if not lines:
        raise HTTPException(422, "A transfer needs at least one line.")
    t = Transfer(company_id=c.company_id, from_store_id=from_store.id, to_store_id=to_store.id,
                 status="in_transit", sent_by=c.person.id, note=note, idempotency_key=key)
    db.add(t)
    db.flush()
    seen_products: set[str] = set()
    for i, ln in enumerate(lines):
        p = db.get(Product, ln["product_id"])
        if p is None or p.company_id != c.company_id:
            raise HTTPException(404, "A product on this transfer was not found.")
        if p.id in seen_products:
            raise HTTPException(422, f"{p.name} is on the transfer twice; put it on one line.")
        seen_products.add(p.id)
        q = int(ln.get("qty_milli") or 0)
        if q <= 0:
            raise HTTPException(422, f"Enter how many {p.name} are being sent.")
        if p.unit == "each" and q % 1000:
            raise HTTPException(422, f"{p.name} is sent in whole units.")
        res = ledger.post(db, company_id=c.company_id, store_id=from_store.id, product_id=p.id,
                          qty_milli=-q, type="transfer_out", reason=f"To {to_store.name}",
                          source_type="transfer", source_id=t.id, person_id=c.person.id,
                          device=c.device, idempotency_key=f"transfer:{t.id}:out:{i}")
        db.add(TransferLine(company_id=c.company_id, transfer_id=t.id, product_id=p.id,
                            sent_milli=q, unit_cost_paise=res.movement.unit_cost_paise))
    db.flush()
    return t, False


def receive_transfer(db: Session, c, t: Transfer, received: dict[str, int],
                     key: str | None) -> tuple[Transfer, list[dict]]:
    """The receiving store confirms what arrived. Any shortfall is logged as a
    transfer loss: it left one store and never reached the other."""
    if t.status == "received":
        if key and t.receive_key == key:
            return t, []
        raise HTTPException(409, "This transfer has already been received.")
    to_store = db.get(Store, t.to_store_id)
    losses = []
    for i, line in enumerate(t.lines):
        got = int(received.get(line.product_id, line.sent_milli))
        if got < 0 or got > line.sent_milli:
            p = db.get(Product, line.product_id)
            raise HTTPException(422, f"Received {p.name} must be between 0 and the "
                                     f"{fmt_qty(line.sent_milli, p.unit)} sent.")
        line.received_milli = got
        if got:
            ledger.post(db, company_id=c.company_id, store_id=to_store.id,
                        product_id=line.product_id, qty_milli=got, type="transfer_in",
                        source_type="transfer", source_id=t.id, person_id=c.person.id,
                        device=c.device, unit_cost_paise=line.unit_cost_paise,
                        idempotency_key=f"transfer:{t.id}:in:{i}")
        if got < line.sent_milli:
            p = db.get(Product, line.product_id)
            short = line.sent_milli - got
            losses.append({"product_id": p.id, "sku": p.sku, "name": p.name,
                           "lost_milli": short,
                           "value_paise": short * (line.unit_cost_paise or 0) // 1000})
    t.status = "received"
    t.received_by = c.person.id
    t.received_at = utcnow()
    t.receive_key = key
    if losses:
        audit.log(db, c, "transfer.loss", "transfer", t.id, after={
            "lost": [{"sku": l["sku"], "units": l["lost_milli"] / 1000,
                      "value": rupees(l["value_paise"])} for l in losses]},
            store_id=to_store.id)
    return t, losses


def transfer_dict(db: Session, t: Transfer) -> dict:
    names = {p.id: p for p in db.scalars(select(Product).where(
        Product.id.in_([l.product_id for l in t.lines])))}
    return {"id": t.id, "from_store_id": t.from_store_id, "to_store_id": t.to_store_id,
            "status": t.status, "note": t.note,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "received_at": t.received_at.isoformat() if t.received_at else None,
            "lines": [{"product_id": l.product_id, "sku": names[l.product_id].sku,
                       "name": names[l.product_id].name, "sent_milli": l.sent_milli,
                       "received_milli": l.received_milli,
                       "lost_milli": (l.sent_milli - l.received_milli)
                       if l.received_milli is not None else 0} for l in t.lines]}
