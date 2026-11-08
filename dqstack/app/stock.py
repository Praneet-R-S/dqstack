"""The stock list, product history and valuation (section 9).

Read-side views over the ledger. Each figure is computed with grouped queries over
the whole store at once, so a store with 10,000 products is a handful of queries,
not 10,000 of them.

Status, one per row, the most urgent first:
  Count needed  stock is unknown, or below zero (sold more than the system had)
  Out           nothing on hand
  Expiring      stock estimated to expire within the warning window
  Low           below the stock target with no order placed
  Overstocked   more days of cover than the business rule allows
  OK
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import rules as rules_mod
from .ledger import LABELS
from .models import (Barcode, Bill, BillLine, Company, DailySales, ForecastRun, Person, Product,
                     PurchaseOrder, PurchaseOrderLine, Recommendation, StockMovement,
                     StockOnHand, Store, SupplierProduct, Transfer, TransferLine)

OPEN_PO = ("approved", "sent", "partly_received")
OPEN_BILL = ("open", "held")
STATUS_ORDER = ("Count needed", "Out", "Expiring", "Low", "Overstocked", "OK")


def on_order(db: Session, company_id: str, store_ids: list[str]) -> dict:
    rows = db.execute(
        select(PurchaseOrder.store_id, PurchaseOrderLine.product_id,
               func.sum(PurchaseOrderLine.qty_milli - PurchaseOrderLine.received_milli))
        .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.po_id)
        .where(PurchaseOrder.company_id == company_id, PurchaseOrder.status.in_(OPEN_PO),
               PurchaseOrder.store_id.in_(store_ids))
        .group_by(PurchaseOrder.store_id, PurchaseOrderLine.product_id))
    return {(s, p): max(int(q or 0), 0) for s, p, q in rows}


def in_transit(db: Session, company_id: str, store_ids: list[str]) -> dict:
    rows = db.execute(
        select(Transfer.to_store_id, TransferLine.product_id, func.sum(TransferLine.sent_milli))
        .join(Transfer, Transfer.id == TransferLine.transfer_id)
        .where(Transfer.company_id == company_id, Transfer.status == "in_transit",
               Transfer.to_store_id.in_(store_ids))
        .group_by(Transfer.to_store_id, TransferLine.product_id))
    return {(s, p): int(q or 0) for s, p, q in rows}


def in_open_bills(db: Session, company_id: str, store_ids: list[str]) -> dict:
    rows = db.execute(
        select(Bill.store_id, BillLine.product_id, func.sum(BillLine.qty_milli))
        .join(Bill, Bill.id == BillLine.bill_id)
        .where(Bill.company_id == company_id, Bill.state.in_(OPEN_BILL),
               Bill.store_id.in_(store_ids), BillLine.product_id.is_not(None))
        .group_by(Bill.store_id, BillLine.product_id))
    return {(s, p): int(q or 0) for s, p, q in rows}


def daily_rate(db: Session, company_id: str, store_ids: list[str], days: int = 28,
               today: date | None = None) -> dict:
    """Average units per selling day over the last `days`, ignoring out-of-stock days
    (a day with nothing on the shelf says nothing about demand)."""
    today = today or date.today()
    start = today - timedelta(days=days)
    rows = db.execute(
        select(DailySales.store_id, DailySales.product_id, func.sum(DailySales.units_milli),
               func.count())
        .where(DailySales.company_id == company_id, DailySales.store_id.in_(store_ids),
               DailySales.day >= start, DailySales.day < today,
               DailySales.out_of_stock.is_(False))
        .group_by(DailySales.store_id, DailySales.product_id))
    return {(s, p): (int(u or 0) / n if n else 0.0) for s, p, u, n in rows}


def latest_targets(db: Session, company_id: str, store_ids: list[str]) -> dict:
    run = db.scalar(select(ForecastRun.id).where(
        ForecastRun.company_id == company_id, ForecastRun.status == "succeeded")
        .order_by(ForecastRun.created_at.desc()).limit(1))
    if not run:
        return {}
    rows = db.execute(select(Recommendation.store_id, Recommendation.product_id,
                             Recommendation.target_milli)
                      .where(Recommendation.run_id == run,
                             Recommendation.store_id.in_(store_ids)))
    return {(s, p): t for s, p, t in rows if t is not None}


def expiring(db: Session, company_id: str, store_ids: list[str], on_hand: dict,
             within_days: int, today: date | None = None) -> dict:
    """Estimated quantity expiring within `within_days`, first-expiry-first-out: what is
    still on the shelf is assumed to be the latest-expiring receipts, because sales and
    write-offs take the oldest first. Returns {(store, product): (qty, earliest expiry)}."""
    today = today or date.today()
    horizon = today + timedelta(days=within_days)
    lots: dict = defaultdict(list)
    for s, p, q, exp in db.execute(
            select(StockMovement.store_id, StockMovement.product_id, StockMovement.qty_milli,
                   StockMovement.expiry)
            .where(StockMovement.company_id == company_id, StockMovement.store_id.in_(store_ids),
                   StockMovement.expiry.is_not(None), StockMovement.qty_milli > 0)):
        lots[(s, p)].append((exp, q))
    out = {}
    for key, items in lots.items():
        remaining = on_hand.get(key, 0)
        if remaining <= 0:
            continue
        soon_qty, soon_day = 0, None
        for exp, q in sorted(items, reverse=True):           # latest expiry first
            take = min(q, remaining)
            remaining -= take
            if exp <= horizon and take > 0:
                soon_qty += take
                soon_day = exp if soon_day is None else min(soon_day, exp)
            if remaining <= 0:
                break
        if soon_qty:
            out[key] = (soon_qty, soon_day)
    return out


def stock_rows(db: Session, company_id: str, store: Store, *, category: str | None = None,
               supplier_id: str | None = None, status: str | None = None,
               q: str | None = None, include_discontinued: bool = True,
               today: date | None = None, product_id: str | None = None) -> list[dict]:
    co = db.get(Company, company_id)
    r = rules_mod.effective(co.rules)
    sid = [store.id]
    pq = select(Product).where(Product.company_id == company_id)
    if category:
        pq = pq.where(Product.category == category)
    if supplier_id:
        pq = pq.where(Product.id.in_(select(SupplierProduct.product_id).where(
            SupplierProduct.supplier_id == supplier_id)))
    if not include_discontinued:
        pq = pq.where(Product.status != "discontinued")
    if product_id:
        pq = pq.where(Product.id == product_id)
    if q:
        like = f"%{q.strip()}%"
        pq = pq.where(or_(Product.name.ilike(like), Product.sku.ilike(like),
                          Product.id.in_(select(Barcode.product_id).where(
                              Barcode.company_id == company_id, Barcode.code == q.strip()))))
    products = list(db.scalars(pq.order_by(Product.name)))
    soh = {row.product_id: row for row in db.scalars(select(StockOnHand).where(
        StockOnHand.store_id == store.id))}
    oo = on_order(db, company_id, sid)
    it = in_transit(db, company_id, sid)
    ob = in_open_bills(db, company_id, sid)
    rate = daily_rate(db, company_id, sid, today=today)
    targets = latest_targets(db, company_id, sid)
    hand = {(store.id, pid): row.on_hand_milli for pid, row in soh.items()}
    exp = expiring(db, company_id, sid, hand, int(r["expiry_warning_days"]), today=today)
    out = []
    for p in products:
        row = soh.get(p.id)
        key = (store.id, p.id)
        oh = row.on_hand_milli if row else 0
        known = bool(row and row.known)
        per_day = rate.get(key, 0.0)
        cover = (oh / per_day) if per_day > 0 and oh > 0 else (0.0 if oh <= 0 and known else None)
        target = targets.get(key)
        e = exp.get(key)
        limit = r["perishable_cover_days"] if p.perishable else r["overstock_cover_days"]
        if not known or oh < 0:
            st = "Count needed"
        elif oh == 0:
            st = "Out"
        elif e:
            st = "Expiring"
        elif target is not None and oh < target and oo.get(key, 0) == 0:
            st = "Low"
        elif cover is not None and cover > limit:
            st = "Overstocked"
        else:
            st = "OK"
        if status and st != status:
            continue
        cost = row.avg_cost_paise if row and row.avg_cost_paise is not None else p.cost_paise
        out.append({
            "product_id": p.id, "sku": p.sku, "name": p.name, "category": p.category,
            "unit": p.unit, "product_status": p.status, "abc_class": p.abc_class,
            "on_hand_milli": oh, "known": known, "in_open_bills_milli": ob.get(key, 0),
            "on_order_milli": oo.get(key, 0), "in_transit_milli": it.get(key, 0),
            "days_of_cover": round(cover, 1) if cover is not None else None,
            "target_milli": target,
            "last_counted_at": row.last_counted_at.isoformat() if row and row.last_counted_at else None,
            "avg_cost_paise": cost,
            "value_paise": (oh * cost // 1000) if cost is not None and oh > 0 else 0,
            "expiring_milli": e[0] if e else 0,
            "expiring_on": e[1].isoformat() if e else None,
            "status": st})
    return out


def history(db: Session, company_id: str, store_id: str, product_id: str,
            limit: int = 200) -> list[dict]:
    """Every movement for one product in one store, newest first, each with the
    running balance after it."""
    rows = list(db.scalars(select(StockMovement).where(
        StockMovement.company_id == company_id, StockMovement.store_id == store_id,
        StockMovement.product_id == product_id).order_by(StockMovement.at, StockMovement.id)))
    names = {p.id: p.name for p in db.scalars(select(Person).where(
        Person.company_id == company_id))}
    balance = 0
    out = []
    for m in rows:
        balance += m.qty_milli
        out.append({"id": m.id, "at": m.at.isoformat(), "type": m.type,
                    "label": LABELS.get(m.type, m.type), "qty_milli": m.qty_milli,
                    "balance_milli": balance, "reason": m.reason,
                    "source_type": m.source_type, "source_id": m.source_id,
                    "person": names.get(m.person_id), "device": m.device,
                    "unit_cost_paise": m.unit_cost_paise, "batch": m.batch,
                    "expiry": m.expiry.isoformat() if m.expiry else None})
    out.reverse()
    return out[:limit]


def valuation(db: Session, company_id: str, store_ids: list[str]) -> dict:
    """Stock value at average cost, by store and category. Negative stock is valued at
    zero: it is a counting problem, not an asset."""
    rows = db.execute(
        select(StockOnHand.store_id, Product.category, StockOnHand.on_hand_milli,
               func.coalesce(StockOnHand.avg_cost_paise, Product.cost_paise))
        .join(Product, Product.id == StockOnHand.product_id)
        .where(StockOnHand.company_id == company_id, StockOnHand.store_id.in_(store_ids)))
    by_store: dict = defaultdict(lambda: {"value_paise": 0, "units_milli": 0, "categories": {}})
    total = 0
    unvalued = 0
    for sid, cat, oh, cost in rows:
        if oh <= 0:
            continue
        if cost is None:
            unvalued += 1
            continue
        v = oh * cost // 1000
        s = by_store[sid]
        s["value_paise"] += v
        s["units_milli"] += oh
        c = s["categories"].setdefault(cat or "Uncategorised", {"value_paise": 0, "units_milli": 0})
        c["value_paise"] += v
        c["units_milli"] += oh
        total += v
    return {"total_paise": total, "stores": dict(by_store), "products_without_cost": unvalued}
