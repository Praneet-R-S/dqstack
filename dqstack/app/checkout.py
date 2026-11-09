"""Checkout and the paid-bill rule (section 7, build step 9).

1. One step. When payment is confirmed, the bill, its payments and one sale movement
   per line are saved together, or none of it is.
2. The bill number is the safety key. It is made on the device before payment; the
   same bill arriving twice (double press, network retry, offline resend) is
   recognised and changes nothing.
3. Never block a customer: selling past zero completes, stock goes negative, and a
   "count needed" alert is raised.
4. An unknown barcode sells as a miscellaneous item at a typed price; no product's
   stock changes until a manager maps the barcode.
5. No editing a paid bill: corrections are a refund plus a new bill. Refunds need a
   manager PIN and a reason; a resaleable item goes back to stock, a damaged one is
   booked in and written off, so it shows as wastage, not stock.
6. Discounts and price overrides need permission and are logged with the cashier,
   the original price and the reason.

Prices on the floor include GST; the tax inside each line is recorded for the
Z-report and receipts. Checkout is a stock register beside a GST invoice system
until a chartered accountant confirms it meets the invoice rules (section 15).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import audit, ledger
from .db import utcnow
from .formats import rupees
from .models import (Alert, Barcode, Bill, BillLine, BillPayment, Company, DailySales, Person,
                     PriceRecord, Product, Store, TillDevice)
from .permissions import allowed
from .security import _aware, verify_password

METHODS = ("cash", "upi", "card")


# ===================================================================== scanning

def scan(db: Session, company_id: str, code: str) -> dict:
    """What a scanned code adds: a product and quantity (a case barcode adds the case
    in single units; a scale label adds its weight), or "unknown"."""
    code = code.strip()
    b = db.scalar(select(Barcode).where(Barcode.company_id == company_id, Barcode.code == code))
    if b is not None:
        return {"found": True, "product_id": b.product_id, "qty_milli": b.units_per_scan * 1000}
    # Weight-embedded scale labels: 2 + 5-digit item code + 5-digit grams + check digit.
    if len(code) == 13 and code.isdigit() and code[0] == "2":
        b = db.scalar(select(Barcode).where(Barcode.company_id == company_id,
                                            Barcode.code == code[:7]))
        if b is not None:
            return {"found": True, "product_id": b.product_id, "qty_milli": int(code[7:12]),
                    "weighed": True}
    p = db.scalar(select(Product).where(Product.company_id == company_id,
                                        func.upper(Product.sku) == code.upper()))
    if p is not None:
        return {"found": True, "product_id": p.id, "qty_milli": 1000}
    return {"found": False, "message": f"Barcode {code} is not in your catalogue. Sell it as a "
                                       "misc item, or ask a manager to add it."}


def price_today(db: Session, p: Product, store_id: str, on: date) -> int | None:
    rec = db.scalar(select(PriceRecord).where(
        PriceRecord.product_id == p.id, PriceRecord.start_date <= on,
        (PriceRecord.end_date.is_(None)) | (PriceRecord.end_date >= on),
        (PriceRecord.store_id.is_(None)) | (PriceRecord.store_id == store_id))
        .order_by(PriceRecord.store_id.is_(None), PriceRecord.start_date.desc()).limit(1))
    return rec.price_paise if rec is not None else p.price_paise


def tax_inside(gross_paise: int, rate_bp: int | None) -> int:
    """GST contained in a GST-inclusive amount."""
    if not rate_bp:
        return 0
    return round(gross_paise * rate_bp / (10_000 + rate_bp))


def manager_for_pin(db: Session, company_id: str, store_id: str, pin: str | None,
                    manager_id: str | None = None) -> Person | None:
    """A person who may refund in this store and whose PIN this is."""
    if not pin:
        return None
    q = select(Person).where(Person.company_id == company_id, Person.status == "active",
                             Person.manager_pin_hash.is_not(None))
    if manager_id:
        q = q.where(Person.id == manager_id)
    for p in db.scalars(q):
        stores = {s.id for s in p.stores}
        if allowed(p.role, "sales.refund", store_id, stores) and \
                verify_password(p.manager_pin_hash, pin):
            return p
    return None


def _business_day(store: Store, tz: ZoneInfo, at: datetime) -> date:
    return at.astimezone(tz).date()


def _tz(db: Session, company_id: str) -> ZoneInfo:
    try:
        return ZoneInfo(db.get(Company, company_id).timezone or "Asia/Kolkata")
    except Exception:  # noqa: BLE001
        return ZoneInfo("Asia/Kolkata")


def _alert(db: Session, company_id: str, store_id: str | None, product_id: str | None,
           kind: str, severity: str, message: str, key: str, link: str | None = None) -> None:
    if db.scalar(select(Alert.id).where(Alert.company_id == company_id, Alert.dedupe_key == key)):
        return
    db.add(Alert(company_id=company_id, store_id=store_id, product_id=product_id, kind=kind,
                 severity=severity, message=message, dedupe_key=key, link=link))


# ===================================================================== paying

def _build_lines(db: Session, c, store: Store, lines: list[dict], on: date,
                 manager: Person | None, trusted: bool = False
                 ) -> tuple[list[BillLine], int, int, int]:
    built, total, tax, disc = [], 0, 0, 0
    for i, ln in enumerate(lines):
        qty = int(ln.get("qty_milli") or 0)
        price = int(ln.get("unit_price_paise") if ln.get("unit_price_paise") is not None else -1)
        discount = int(ln.get("discount_paise") or 0)
        if qty <= 0:
            raise HTTPException(422, f"Line {i + 1}: the quantity must be more than zero.")
        p = None
        if ln.get("product_id"):
            p = db.get(Product, ln["product_id"])
            if p is None or p.company_id != c.company_id:
                raise HTTPException(404, f"Line {i + 1}: that product was not found.")
            if p.unit == "each" and qty % 1000:
                raise HTTPException(422, f"{p.name} is sold in whole units.")
            listed = price_today(db, p, store.id, on)
            if price < 0:
                if listed is None:
                    raise HTTPException(422, f"{p.name} has no selling price. Type the price.")
                price = listed
            overridden = (listed is not None and price < listed) or discount > 0
            if overridden and not trusted:
                if not (c.can("sales.refund", store.id) or manager is not None):
                    raise HTTPException(403, f"A discount or lower price on {p.name} needs a "
                                             "manager's PIN.")
                if not (ln.get("override_reason") or "").strip():
                    raise HTTPException(422, f"Say why {p.name} is cheaper than its price.")
        else:
            if not (ln.get("description") or "").strip() or price < 0:
                raise HTTPException(422, f"Line {i + 1}: a misc item needs a description and "
                                         "a price.")
            listed, overridden = None, False
        gross = qty * price // 1000 - discount
        if gross < 0:
            raise HTTPException(422, f"Line {i + 1}: the discount is more than the line.")
        t = tax_inside(gross, p.gst_rate_bp if p else None)
        built.append(BillLine(company_id=c.company_id, product_id=p.id if p else None,
                              description=None if p else ln["description"].strip()[:300],
                              unmapped_barcode=(ln.get("barcode") or None) if not p else None,
                              qty_milli=qty, unit_price_paise=price, discount_paise=discount,
                              tax_paise=t, original_price_paise=listed if overridden else None,
                              override_reason=(ln.get("override_reason") or None) if overridden
                              else None))
        total += gross
        tax += t
        disc += discount + (max(listed - price, 0) * qty // 1000 if overridden and listed else 0)
    return built, total, tax, disc


def _check_payments(payments: list[dict], total: int) -> tuple[list[BillPayment], int]:
    if not payments:
        raise HTTPException(422, "Take a payment before completing the bill.")
    out, paid, change = [], 0, 0
    for pmt in payments:
        method = pmt.get("method")
        if method not in METHODS:
            raise HTTPException(422, "Payment must be cash, UPI or card.")
        amount = int(pmt.get("amount_paise") or 0)
        if amount <= 0:
            raise HTTPException(422, "Each payment must be more than zero.")
        tendered = pmt.get("tendered_paise")
        if method == "cash" and tendered is not None:
            if int(tendered) < amount:
                raise HTTPException(422, "The cash received is less than the amount.")
            change += int(tendered) - amount
        out.append(BillPayment(method=method, amount_paise=amount,
                               tendered_paise=int(tendered) if tendered is not None else None,
                               reference=(pmt.get("reference") or None)))
        paid += amount
    if paid != total:
        raise HTTPException(422, f"The payments add up to {rupees(paid)} but the bill is "
                                 f"{rupees(total)}.")
    return out, change


def pay(db: Session, c, store: Store, body: dict, trusted: bool = False
        ) -> tuple[Bill, bool]:
    """Complete a bill: the paid-bill rule. Returns (bill, duplicate). `trusted` is
    for an existing POS, whose own controls already approved its prices."""
    number = (body.get("bill_number") or "").strip()
    if not number:
        raise HTTPException(422, "The bill needs its number from the till.")
    seen = db.scalar(select(Bill).where(Bill.company_id == c.company_id,
                                        Bill.store_id == store.id, Bill.bill_number == number))
    if seen is not None and seen.state in ("paid", "refund"):
        return seen, True
    if seen is not None and seen.state == "voided":
        raise HTTPException(409, f"Bill {number} was voided. Start a new bill.")
    tz = _tz(db, c.company_id)
    at = _device_time(body.get("paid_at"))
    manager = manager_for_pin(db, c.company_id, store.id, body.get("manager_pin"),
                              body.get("manager_id"))
    lines, total, tax, disc = _build_lines(db, c, store, body.get("lines") or [],
                                           _business_day(store, tz, at), manager, trusted)
    if not lines:
        raise HTTPException(422, "The bill has no items.")
    payments, change = _check_payments(body.get("payments") or [], total)
    bill = seen or Bill(company_id=c.company_id, store_id=store.id, bill_number=number)
    if seen is not None:
        bill.lines.clear()
        bill.payments.clear()
        db.flush()
    bill.till = (body.get("till") or None)
    bill.device_id = (body.get("device_id") or None)
    bill.cashier_id = getattr(c.person, "id", None)
    bill.source = "pos" if trusted else "checkout"
    bill.state, bill.paid_at = "paid", at
    bill.total_paise, bill.tax_paise, bill.discount_paise = total, tax, disc
    bill.change_paise = change
    bill.made_offline = bool(body.get("offline"))
    bill.business_day = _business_day(store, tz, at)
    bill.approved_by = manager.id if manager else None
    for ln in lines:
        bill.lines.append(ln)
    for pmt in payments:
        pmt.company_id = c.company_id
        bill.payments.append(pmt)
    if seen is None:
        db.add(bill)
    db.flush()
    ledger.lock_rows(db, c.company_id, store.id, [ln.product_id for ln in bill.lines])
    for i, ln in enumerate(bill.lines):
        if ln.product_id is None:
            if ln.unmapped_barcode:
                _alert(db, c.company_id, store.id, None, "unmapped_barcode", "attention",
                       f"Barcode {ln.unmapped_barcode} was sold as a misc item. Add it to a "
                       "product so its stock is counted.", f"unmapped_barcode:{ln.unmapped_barcode}",
                       link="#/stock?q=" + ln.unmapped_barcode)
            continue
        res = ledger.post(db, company_id=c.company_id, store_id=store.id,
                          product_id=ln.product_id, qty_milli=-ln.qty_milli, type="sale",
                          source_type="bill", source_id=bill.id,
                          person_id=getattr(c.person, "id", None),
                          device=bill.device_id or c.device, at=at,
                          idempotency_key=f"bill:{bill.id}:{i}")
        if res.on_hand_milli < 0:
            p = db.get(Product, ln.product_id)
            _alert(db, c.company_id, store.id, p.id, "negative_stock", "risk",
                   f"{p.name} was sold with none on the books in {store.name}. Count it.",
                   f"negative_stock:{store.id}:{p.id}:{bill.business_day.isoformat()}")
    audit.log(db, c, "bill.paid", "bill", bill.id, after={
        "number": number, "total": rupees(total), "lines": len(bill.lines),
        "offline": bill.made_offline, "methods": sorted({p.method for p in bill.payments}),
        "overrides": [{"original": l.original_price_paise, "price": l.unit_price_paise,
                       "reason": l.override_reason} for l in bill.lines if l.override_reason]},
        store_id=store.id)
    return bill, False


def _device_time(raw) -> datetime:
    """The device's clock, trusted only within a week behind and never ahead."""
    now = utcnow()
    if not raw:
        return now
    at = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    at = at.astimezone(timezone.utc)
    return min(max(at, now - timedelta(days=7)), now)


def hold(db: Session, c, store: Store, body: dict) -> Bill:
    """An open or held bill, shown as "in open bills" on the stock screen for
    information only. Nothing moves stock."""
    number = (body.get("bill_number") or "").strip()
    if not number:
        raise HTTPException(422, "The bill needs its number from the till.")
    bill = db.scalar(select(Bill).where(Bill.company_id == c.company_id,
                                        Bill.store_id == store.id, Bill.bill_number == number))
    if bill is not None and bill.state not in ("open", "held"):
        raise HTTPException(409, f"Bill {number} is already {bill.state}.")
    lines, total, tax, disc = _build_lines(db, c, store, body.get("lines") or [], date.today(),
                                           None)
    if bill is None:
        bill = Bill(company_id=c.company_id, store_id=store.id, bill_number=number)
        db.add(bill)
    else:
        bill.lines.clear()
        db.flush()
    bill.state = "held" if body.get("held") else "open"
    bill.cashier_id, bill.device_id = c.person.id, body.get("device_id")
    bill.total_paise, bill.tax_paise, bill.discount_paise = total, tax, disc
    for ln in lines:
        bill.lines.append(ln)
    db.flush()
    return bill


def void_bill(db: Session, c, bill: Bill, reason: str) -> Bill:
    """Before payment only. No stock change; logged with the cashier and the reason."""
    if bill.state in ("paid", "refund"):
        raise HTTPException(409, "A paid bill cannot be voided. Refund it instead.")
    if bill.state == "voided":
        return bill
    bill.state, bill.voided_at, bill.void_reason = "voided", utcnow(), reason[:200]
    audit.log(db, c, "bill.voided", "bill", bill.id, after={"number": bill.bill_number,
                                                           "reason": reason}, store_id=bill.store_id)
    return bill


def void_by_number(db: Session, c, store: Store, number: str, reason: str, lines: list[dict]
                   ) -> Bill:
    bill = db.scalar(select(Bill).where(Bill.company_id == c.company_id,
                                        Bill.store_id == store.id, Bill.bill_number == number))
    if bill is None:
        # Voided on the till before it was ever sent: kept for the record.
        bill = hold(db, c, store, {"bill_number": number, "lines": lines}) if lines else \
            Bill(company_id=c.company_id, store_id=store.id, bill_number=number, state="open",
                 cashier_id=c.person.id)
        if bill.id is None:
            db.add(bill)
            db.flush()
    return void_bill(db, c, bill, reason)


# ===================================================================== refunds

def refund(db: Session, c, store: Store, body: dict, require_pin: bool = True
           ) -> tuple[Bill, bool]:
    """`require_pin` is off only for an existing POS, which approved the refund itself."""
    number = (body.get("refund_number") or "").strip()
    if not number:
        raise HTTPException(422, "The refund needs its number from the till.")
    seen = db.scalar(select(Bill).where(Bill.company_id == c.company_id,
                                        Bill.store_id == store.id, Bill.bill_number == number))
    if seen is not None:
        return seen, True
    reason = (body.get("reason") or "").strip()
    if len(reason) < 3:
        raise HTTPException(422, "Give the reason for the refund.")
    manager = manager_for_pin(db, c.company_id, store.id, body.get("manager_pin"),
                              body.get("manager_id"))
    if manager is None and require_pin:
        raise HTTPException(403, "A refund needs a manager's PIN.")
    orig = db.scalar(select(Bill).where(Bill.company_id == c.company_id,
                                        Bill.store_id == store.id,
                                        Bill.bill_number == (body.get("original_bill_number") or "")))
    if orig is None or orig.state != "paid":
        raise HTTPException(404, "That bill was not found as a paid bill in this store.")
    sold = {}
    for l in orig.lines:
        if l.product_id:
            q, v = sold.get(l.product_id, (0, 0))
            sold[l.product_id] = (q + l.qty_milli, v + l.qty_milli * l.unit_price_paise // 1000
                                  - l.discount_paise)
    back = {}
    for r in db.scalars(select(Bill).where(Bill.refund_of == orig.id)):
        for l in r.lines:
            back[l.product_id] = back.get(l.product_id, 0) + l.qty_milli
    method = body.get("method") or "cash"
    if method not in METHODS:
        raise HTTPException(422, "Refund method must be cash, UPI or card.")
    tz = _tz(db, c.company_id)
    at = utcnow()
    bill = Bill(company_id=c.company_id, store_id=store.id, bill_number=number, state="refund",
                cashier_id=getattr(c.person, "id", None), refund_of=orig.id,
                refund_reason=reason[:200], approved_by=manager.id if manager else None,
                paid_at=at, source="checkout" if require_pin else "pos", business_day=_business_day(store, tz, at),
                device_id=body.get("device_id"))
    total = 0
    lines = body.get("lines") or []
    if not lines:
        raise HTTPException(422, "Choose what is being returned.")
    for ln in lines:
        pid, qty = ln.get("product_id"), int(ln.get("qty_milli") or 0)
        if pid not in sold:
            raise HTTPException(422, "That product is not on the original bill.")
        q_sold, v_sold = sold[pid]
        if qty <= 0 or qty > q_sold - back.get(pid, 0):
            raise HTTPException(422, "You cannot return more than was bought.")
        value = v_sold * qty // q_sold
        p = db.get(Product, pid)
        bill.lines.append(BillLine(company_id=c.company_id, product_id=pid, qty_milli=qty,
                                   unit_price_paise=value * 1000 // qty,
                                   tax_paise=tax_inside(value, p.gst_rate_bp),
                                   resaleable=bool(ln.get("resaleable", True))))
        total += value
    bill.total_paise = -total
    bill.payments.append(BillPayment(company_id=c.company_id, method=method, amount_paise=-total))
    db.add(bill)
    db.flush()
    ledger.lock_rows(db, c.company_id, store.id, [l.product_id for l in bill.lines])
    for i, l in enumerate(bill.lines):
        ledger.post(db, company_id=c.company_id, store_id=store.id, product_id=l.product_id,
                    qty_milli=l.qty_milli, type="customer_return", reason=reason,
                    source_type="refund", source_id=bill.id,
                    person_id=getattr(c.person, "id", None),
                    idempotency_key=f"refund:{bill.id}:{i}")
        if not l.resaleable:
            # Damaged: it came back into the store, and it is written off at once.
            ledger.post(db, company_id=c.company_id, store_id=store.id, product_id=l.product_id,
                        qty_milli=-l.qty_milli, type="damage", reason=f"Returned damaged: {reason}",
                        source_type="refund", source_id=bill.id,
                        person_id=getattr(c.person, "id", None),
                        idempotency_key=f"refund:{bill.id}:{i}:damaged")
    audit.log(db, c, "bill.refunded", "bill", bill.id, after={
        "number": number, "original": orig.bill_number, "amount": rupees(total),
        "reason": reason, "approved_by": manager.name if manager else "the POS"},
        store_id=store.id)
    return bill, False


# ===================================================================== devices and day end

def heartbeat(db: Session, c, store: Store, device_id: str, pending: int,
              name: str | None = None) -> TillDevice:
    d = db.scalar(select(TillDevice).where(TillDevice.company_id == c.company_id,
                                           TillDevice.device_id == device_id))
    if d is None:
        d = TillDevice(company_id=c.company_id, store_id=store.id, device_id=device_id)
        db.add(d)
    d.store_id, d.pending, d.last_seen = store.id, max(int(pending), 0), utcnow()
    if name:
        d.name = name[:64]
    db.flush()
    return d


def z_report(db: Session, company_id: str, store: Store, day: date,
             cashier_id: str | None = None) -> dict:
    """End of day: bills, totals by payment method, refunds, voids, cash expected.
    With cashier_id, only that cashier's bills: their shift, for handing over the cash."""
    mine = (Bill.cashier_id == cashier_id) if cashier_id else True
    bills = list(db.scalars(select(Bill).where(Bill.store_id == store.id,
                                               Bill.business_day == day, mine)))
    paid = [b for b in bills if b.state == "paid"]
    refunds = [b for b in bills if b.state == "refund"]
    tz = _tz(db, company_id)
    start = datetime.combine(day, time(0, 0), tzinfo=tz).astimezone(timezone.utc)
    voids = db.scalar(select(func.count()).select_from(Bill).where(
        Bill.store_id == store.id, Bill.state == "voided", Bill.voided_at >= start,
        Bill.voided_at < start + timedelta(days=1), mine)) or 0
    by_method = {m: 0 for m in METHODS}
    for b in paid + refunds:
        for p in b.payments:
            by_method[p.method] = by_method.get(p.method, 0) + p.amount_paise
    recent = utcnow() - timedelta(hours=24)
    waiting = sum(d.pending for d in db.scalars(select(TillDevice).where(
        TillDevice.store_id == store.id)) if d.last_seen and _aware(d.last_seen) >= recent)
    return {"day": day.isoformat(), "bills": len(paid),
            "sales_paise": sum(b.total_paise for b in paid),
            "tax_paise": sum(b.tax_paise for b in paid),
            "discounts_paise": sum(b.discount_paise for b in paid),
            "refunds": len(refunds), "refunds_paise": -sum(b.total_paise for b in refunds),
            "voids": int(voids), "by_method_paise": by_method,
            "cash_expected_paise": by_method["cash"], "devices_waiting": waiting,
            "made_offline": sum(1 for b in paid if b.made_offline)}


def daily_from_bills(db: Session, company_id: str, store: Store, day: date) -> int:
    """The daily sales table for a connected store, from its paid bills less refunds.
    Stock moved when each bill was paid, so these rows record it as already posted."""
    units: dict[str, list[int]] = {}
    for b in db.scalars(select(Bill).where(Bill.store_id == store.id, Bill.business_day == day,
                                           Bill.state.in_(("paid", "refund")))):
        sign = -1 if b.state == "refund" else 1
        for l in b.lines:
            if l.product_id is None:
                continue
            v = units.setdefault(l.product_id, [0, 0])
            v[0] += sign * l.qty_milli
            v[1] += sign * (l.qty_milli * l.unit_price_paise // 1000 - l.discount_paise)
    rows = {r.product_id: r for r in db.scalars(select(DailySales).where(
        DailySales.store_id == store.id, DailySales.day == day))}
    for pid, (u, rev) in units.items():
        u, rev = max(u, 0), max(rev, 0)
        r = rows.get(pid)
        if r is None:
            r = DailySales(company_id=company_id, store_id=store.id, product_id=pid, day=day)
            db.add(r)
        r.units_milli, r.revenue_paise, r.posted_milli = u, rev, u
        r.avg_price_paise = rev * 1000 // u if u else None
        r.source, r.updated_at = "checkout", utcnow()
    db.flush()
    return len(units)


def receipt_text(db: Session, bill: Bill, width: int = 32) -> str:
    co = db.get(Company, bill.company_id)
    store = db.get(Store, bill.store_id)
    names = {p.id: p.name for p in db.scalars(select(Product).where(
        Product.id.in_([l.product_id for l in bill.lines if l.product_id])))}
    out = [co.name[:width].center(width), store.name[:width].center(width)]
    if co.gstin:
        out.append(f"GSTIN {co.gstin}".center(width))
    out.append(f"Bill {bill.bill_number}"[:width])
    out.append("-" * width)
    for l in bill.lines:
        name = names.get(l.product_id) or l.description or "Item"
        amount = l.qty_milli * l.unit_price_paise // 1000 - l.discount_paise
        out.append(name[:width])
        left = f"  {l.qty_milli / 1000:g} x {l.unit_price_paise / 100:.2f}"
        right = f"{amount / 100:.2f}"
        out.append(left + right.rjust(width - len(left)))
    out.append("-" * width)
    tot = f"{abs(bill.total_paise) / 100:.2f}"
    label = "Refund" if bill.state == "refund" else "Total"
    out.append(label + tot.rjust(width - len(label)))
    if bill.tax_paise:
        g = f"{bill.tax_paise / 100:.2f}"
        out.append("incl. GST" + g.rjust(width - 9))
    for p in bill.payments:
        a = f"{abs(p.amount_paise) / 100:.2f}"
        out.append(p.method.upper() + a.rjust(width - len(p.method)))
    if bill.change_paise:
        ch = f"{bill.change_paise / 100:.2f}"
        out.append("Change" + ch.rjust(width - 6))
    out.append("Thank you".center(width))
    return "\n".join(out)


def receipt_message(db: Session, bill: Bill) -> tuple[str, list[str]]:
    """The receipt as a phone message (WhatsApp or SMS): the text, and the same facts as
    the parameters of the approved WhatsApp receipt template (see whatsapp.py). Amounts
    are written "Rs." because a rupee sign turns an SMS into a shorter Unicode one."""
    co = db.get(Company, bill.company_id)
    store = db.get(Store, bill.store_id)
    names = {p.id: p.name for p in db.scalars(select(Product).where(
        Product.id.in_([l.product_id for l in bill.lines if l.product_id])))}
    tz = _tz(db, bill.company_id)
    at = _aware(bill.paid_at or bill.created_at or utcnow()).astimezone(tz)
    zone = "IST" if tz.key == "Asia/Kolkata" else tz.key
    when = f"{at:%d %b %Y, %I:%M %p} {zone}"

    def rs(paise: int) -> str:
        return f"Rs. {abs(paise) / 100:.2f}"

    items = []
    for l in bill.lines:
        name = names.get(l.product_id) or l.description or "Item"
        amount = l.qty_milli * l.unit_price_paise // 1000 - l.discount_paise
        items.append(f"{name} x {l.qty_milli / 1000:g} = {rs(amount)}")
    label = "Refund" if bill.state == "refund" else "Total"
    paid_by = ", ".join(p.method.upper() for p in bill.payments) or "-"
    shop = store.name if store.name.strip().lower() == co.name.strip().lower() \
        else f"{co.name}, {store.name}"
    head = [shop] + ([f"GSTIN {co.gstin}"] if co.gstin else [])
    text = "\n".join(head + [f"Bill {bill.bill_number}", when, ""] + items + [
        "", f"{label} {rs(bill.total_paise)}" + (f" (incl. GST {rs(bill.tax_paise)})"
                                                 if bill.tax_paise else ""),
        f"Paid by {paid_by}", "Thank you"])
    params = [shop + (f" (GSTIN {co.gstin})" if co.gstin else ""), bill.bill_number, when,
              "; ".join(items) or "-", f"{rs(bill.total_paise)} by {paid_by}"]
    return text, params
