"""Sales coming in for standalone stores, the daily sales table, and Day close
(section 6 and the daily cycle, build step 6).

A day's sales file, or the totals typed on the Sales entry screen, does two things:
it sets that store's rows in the daily sales table (what the forecasting engine
reads every night), and it moves stock by posting sale movements. Each row remembers
how many units it has already posted, so a replacement file posts only the
difference and the same file twice posts nothing. A late file for a day already
closed replaces that day and marks it for tonight's re-run.

Stock is anchored at its last count. Sales for a day before a product was last
counted are recorded for the forecast but do not move stock: the count already saw
the shelf after they happened.

Sales *history* (onboarding) only fills the daily sales table. Two years of past
sales must not empty today's stock.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import audit, ledger
from . import rules as rules_mod
from .db import utcnow
from .models import (Bill, CalendarEvent, Company, DailySales, DayClose, Delivery, Product,
                     StockMovement, StockOnHand, Store)
from .security import _aware


def tz_of(db: Session, company_id: str) -> ZoneInfo:
    co = db.get(Company, company_id)
    try:
        return ZoneInfo(co.timezone or "Asia/Kolkata")
    except Exception:  # noqa: BLE001 -- a bad stored zone falls back to IST
        return ZoneInfo("Asia/Kolkata")


def close_instant(store: Store, tz: ZoneInfo, day: date) -> datetime:
    """The moment the store's day closes, in UTC: when a day's sales are stamped."""
    hh, mm = (store.day_close_time or "22:00").split(":")
    return datetime.combine(day, time(int(hh), int(mm)), tzinfo=tz).astimezone(timezone.utc)


def local_today(tz: ZoneInfo, now: datetime | None = None) -> date:
    return (now or utcnow()).astimezone(tz).date()


def apply_day_totals(db: Session, c, store: Store, day: date,
                     totals: dict[str, tuple[int, int]], *, replace_day: bool, source: str,
                     post_stock: bool, job_id: str | None = None, rec=None) -> dict:
    """Set a store's daily sales for one day from {product_id: (units_milli,
    revenue_paise)}. With replace_day, products sold that day but missing from
    `totals` go to zero. Returns counts of what changed."""
    tz = tz_of(db, c.company_id)
    at = close_instant(store, tz, day)
    rows = {r.product_id: r for r in db.scalars(select(DailySales).where(
        DailySales.store_id == store.id, DailySales.day == day))}
    if replace_day:
        for pid, r in rows.items():
            if pid not in totals and (r.units_milli or r.revenue_paise or r.posted_milli):
                if r.source in ("checkout", "pos"):
                    continue                    # till sales are never replaced by a file
                totals = {**totals, pid: (0, 0)}
    anchors = {}
    if post_stock and totals:
        anchors = {r.product_id: _aware(r.baseline_at) for r in db.scalars(
            select(StockOnHand).where(StockOnHand.store_id == store.id,
                                      StockOnHand.product_id.in_(list(totals))))
            if r.baseline_at is not None}
    out = {"new": 0, "updated": 0, "same": 0, "stock_moves": 0, "absorbed_by_count": 0}
    now = utcnow()
    for pid, (units, revenue) in totals.items():
        r = rows.get(pid)
        before = None
        if r is None:
            r = DailySales(company_id=c.company_id, store_id=store.id, product_id=pid, day=day,
                           units_milli=0, revenue_paise=0, posted_milli=0, source=source)
            db.add(r)
            kind = "new"
        else:
            if r.units_milli == units and r.revenue_paise == revenue and \
                    (not post_stock or r.posted_milli == units or
                     (anchors.get(pid) and at < anchors[pid])):
                out["same"] += 1
                continue
            before = {"units_milli": r.units_milli, "revenue_paise": r.revenue_paise,
                      "posted_milli": r.posted_milli, "source": r.source}
            kind = "updated"
        r.units_milli = units
        r.revenue_paise = revenue
        r.avg_price_paise = (revenue * 1000 // units) if units else None
        r.source = source if source != "history" or r.source == "history" else r.source
        r.import_job_id = job_id or r.import_job_id
        r.updated_at = now
        if post_stock:
            anchor = anchors.get(pid)
            if anchor is not None and at < anchor:
                out["absorbed_by_count"] += 1
            else:
                delta = units - r.posted_milli
                if delta:
                    ledger.post(db, company_id=c.company_id, store_id=store.id, product_id=pid,
                                qty_milli=-delta, type="sale",
                                reason=f"Sales for {day:%d %b %Y}" if delta > 0
                                else f"Corrected sales for {day:%d %b %Y}",
                                source_type="sales_upload" if delta > 0 else "sales_correction",
                                source_id=job_id, person_id=c.person.id, device=c.device, at=at)
                    out["stock_moves"] += 1
                r.posted_milli = units
        out[kind] += 1
        if rec is not None:
            db.flush()
            rec.add("daily_sales", r.id, "insert" if before is None else "update", before=before,
                    after={"units_milli": r.units_milli, "revenue_paise": r.revenue_paise,
                           "posted_milli": r.posted_milli, "source": r.source})
    closed = db.scalar(select(DayClose).where(DayClose.store_id == store.id, DayClose.day == day))
    if closed is not None and (out["new"] or out["updated"]):
        closed.late_data_at = now
    db.flush()
    mark_out_of_stock(db, c.company_id, store, day)
    return out


def undo_daily_sales(db: Session, c, change) -> str | None:
    """Put a daily-sales row back and give back any stock its import posted."""
    r = db.get(DailySales, change.entity_id)
    a, b = change.after or {}, change.before
    if r is None:
        return "a daily sales row no longer exists"
    if r.units_milli != a.get("units_milli") or r.posted_milli != a.get("posted_milli"):
        return f"sales for {r.day:%d %b %Y} were changed again after this import; left as they are"
    target_posted = b["posted_milli"] if b else 0
    delta = r.posted_milli - target_posted          # units this import took from stock
    if delta:
        store = db.get(Store, r.store_id)
        ledger.post(db, company_id=r.company_id, store_id=r.store_id, product_id=r.product_id,
                    qty_milli=delta, type="sale", reason="Sales import undone",
                    source_type="sales_correction", source_id=change.job_id,
                    person_id=c.person.id, at=close_instant(store, tz_of(db, r.company_id), r.day))
    if b is None:
        db.delete(r)
    else:
        r.units_milli, r.revenue_paise = b["units_milli"], b["revenue_paise"]
        r.posted_milli, r.source = b["posted_milli"], b["source"]
        r.avg_price_paise = (r.revenue_paise * 1000 // r.units_milli) if r.units_milli else None
    return None


def mark_out_of_stock(db: Session, company_id: str, store: Store, day: date) -> int:
    """Flag product-days with nothing on the shelf at close, where stock is known.
    A day with no sales because the shelf was empty is not low demand, and the
    forecasting engine must be told so. Where stock is unknown the engine's own
    estimator decides later ('estimated')."""
    at = close_instant(store, tz_of(db, company_id), day)
    known = {r.product_id: r for r in db.scalars(select(StockOnHand).where(
        StockOnHand.store_id == store.id, StockOnHand.known.is_(True)))}
    if not known:
        return 0
    levels = dict(db.execute(select(StockMovement.product_id, func.sum(StockMovement.qty_milli))
                             .where(StockMovement.store_id == store.id, StockMovement.at <= at)
                             .group_by(StockMovement.product_id)).all())
    rows = {r.product_id: r for r in db.scalars(select(DailySales).where(
        DailySales.store_id == store.id, DailySales.day == day))}
    flagged = 0
    for pid, soh in known.items():
        since = _aware(soh.known_since) if soh.known_since else None
        if since is None or since > at:
            continue                    # not yet recorded at that close: say nothing
        empty = int(levels.get(pid) or 0) <= 0
        r = rows.get(pid)
        if empty:
            if r is None:
                r = DailySales(company_id=company_id, store_id=store.id, product_id=pid, day=day,
                               units_milli=0, revenue_paise=0, posted_milli=0,
                               source="ledger")
                db.add(r)
            if not r.out_of_stock:
                r.out_of_stock, r.oos_source = True, "ledger"
                flagged += 1
        elif r is not None and r.out_of_stock and r.oos_source == "ledger":
            r.out_of_stock, r.oos_source = False, None
    db.flush()
    return flagged


# ===================================================================== day close

def day_close_status(db: Session, c, store: Store, day: date) -> dict:
    tz = tz_of(db, c.company_id)
    closed = db.scalar(select(DayClose).where(DayClose.store_id == store.id, DayClose.day == day))
    start = datetime.combine(day, time(0, 0), tzinfo=tz).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    open_bills = list(db.scalars(select(Bill).where(Bill.store_id == store.id,
                                                    Bill.state.in_(("open", "held")))))
    deliveries = db.scalar(select(func.count()).select_from(Delivery).where(
        Delivery.store_id == store.id, Delivery.created_at >= start, Delivery.created_at < end)) or 0
    sales_rows = db.scalar(select(func.count()).select_from(DailySales).where(
        DailySales.store_id == store.id, DailySales.day == day,
        DailySales.source.in_(("upload", "entry", "checkout", "pos")))) or 0
    units = db.scalar(select(func.coalesce(func.sum(DailySales.units_milli), 0)).where(
        DailySales.store_id == store.id, DailySales.day == day)) or 0
    revenue = db.scalar(select(func.coalesce(func.sum(DailySales.revenue_paise), 0)).where(
        DailySales.store_id == store.id, DailySales.day == day)) or 0
    z = None
    if store.mode == "connected":
        try:
            from .checkout import z_report
            z = z_report(db, c.company_id, store, day)
        except ImportError:
            z = None
    checks = []
    # The words follow the day being closed: "today" only when it is today.
    when = "today" if day == local_today(tz) else f"on {day:%d %b}"
    whose = "Today's" if when == "today" else f"The {day:%d %b}"
    if store.mode == "standalone":
        checks.append({"key": "sales", "ok": sales_rows > 0,
                       "text": f"{whose} sales are in" if sales_rows else
                       f"Upload {whose[0].lower() + whose[1:]} sales file or type the totals on "
                       "the Sales screen"})
    else:
        pending = z["devices_waiting"] if z else 0
        checks.append({"key": "tills", "ok": pending == 0,
                       "text": "Every till has sent its bills" if pending == 0 else
                       f"{pending} bill(s) are still waiting on a till's device"})
    checks.append({"key": "open_bills", "ok": not open_bills,
                   "text": "No bill is left open" if not open_bills else
                   f"{len(open_bills)} open bill(s) will be voided when you close"})
    checks.append({"key": "deliveries", "ok": True,
                   "text": (f"{deliveries} {'delivery' if deliveries == 1 else 'deliveries'} "
                            f"received {when}") if deliveries else
                   f"No deliveries received {when}. If one came, receive it first."})
    return {"store_id": store.id, "day": day.isoformat(), "mode": store.mode,
            "closed": closed is not None,
            "closed_automatically": bool(closed and closed.automatic),
            "closed_at": closed.created_at.isoformat() if closed and closed.created_at else None,
            "late_data": bool(closed and closed.late_data_at),
            "checks": checks, "open_bills": [{"id": b.id, "bill_number": b.bill_number,
                                              "total_paise": b.total_paise} for b in open_bills],
            "sales_units_milli": int(units), "sales_revenue_paise": int(revenue),
            "z_report": z,
            "cash_expected_paise": z["cash_expected_paise"] if z else None}


def close_day(db: Session, c, store: Store, day: date, *, cash_counted_paise: int | None,
              void_open_bills: bool, no_sales: bool, store_was_closed: bool,
              note: str | None) -> DayClose:
    if db.scalar(select(DayClose.id).where(DayClose.store_id == store.id, DayClose.day == day)):
        raise HTTPException(409, f"{store.name} is already closed for {day:%d %b %Y}.")
    status = day_close_status(db, c, store, day)
    if store.mode == "standalone" and not status["checks"][0]["ok"] and not (no_sales or store_was_closed):
        raise HTTPException(409, "Upload today's sales file or type the totals first. If the "
                                 "store sold nothing or was closed, say so.")
    if store.mode == "connected" and status["z_report"] and status["z_report"]["devices_waiting"]:
        raise HTTPException(409, "Some tills still have bills waiting to send. Connect them to "
                                 "the internet, then close the day.")
    if status["open_bills"]:
        if not void_open_bills:
            raise HTTPException(409, f"{len(status['open_bills'])} bill(s) are still open. "
                                     "Confirm that they should be voided.")
        from .checkout import void_bill
        for b in db.scalars(select(Bill).where(Bill.store_id == store.id,
                                               Bill.state.in_(("open", "held")))):
            void_bill(db, c, b, "Left open at day close")
    if store_was_closed:
        db.add(CalendarEvent(company_id=c.company_id, name=f"{store.name} closed", day=day,
                             kind="closure", store_ids=[store.id], source="company"))
    if store.mode == "connected":
        from .checkout import daily_from_bills
        daily_from_bills(db, c.company_id, store, day)
    mark_out_of_stock(db, c.company_id, store, day)
    dc = DayClose(company_id=c.company_id, store_id=store.id, day=day, closed_by=c.person.id,
                  automatic=False, cash_counted_paise=cash_counted_paise,
                  cash_expected_paise=status["cash_expected_paise"],
                  store_was_closed=store_was_closed, note=note,
                  report={"sales_units_milli": status["sales_units_milli"],
                          "sales_revenue_paise": status["sales_revenue_paise"],
                          "z_report": status["z_report"]})
    db.add(dc)
    db.flush()
    audit.log(db, c, "day.closed", "day_close", dc.id, after={
        "day": day.isoformat(), "cash_counted_paise": cash_counted_paise,
        "cash_expected_paise": dc.cash_expected_paise, "store_was_closed": store_was_closed},
        store_id=store.id)
    return dc


def auto_close(db: Session, now: datetime | None = None) -> list[DayClose]:
    """Close every store whose local time has passed the company's automatic close
    time without anyone closing it, flagged 'closed automatically'. Open bills are
    left as they are and listed: voiding needs a person's confirmation."""
    now = now or utcnow()
    made = []
    for co in db.scalars(select(Company).where(Company.status == "active")):
        tz = ZoneInfo(co.timezone or "Asia/Kolkata")
        local = now.astimezone(tz)
        hh, mm = rules_mod.effective(co.rules)["auto_close_time"].split(":")
        due_day = local.date() if local.time() >= time(int(hh), int(mm)) else \
            local.date() - timedelta(days=1)
        for store in db.scalars(select(Store).where(Store.company_id == co.id,
                                                    Store.active.is_(True))):
            if db.scalar(select(DayClose.id).where(DayClose.store_id == store.id,
                                                   DayClose.day == due_day)):
                continue
            if store.created_at and _aware(store.created_at).astimezone(tz).date() > due_day:
                continue
            open_bills = db.scalar(select(func.count()).select_from(Bill).where(
                Bill.store_id == store.id, Bill.state.in_(("open", "held")))) or 0
            dc = DayClose(company_id=co.id, store_id=store.id, day=due_day, automatic=True,
                          report={"note": "Closed automatically", "open_bills": open_bills})
            db.add(dc)
            made.append(dc)
    db.flush()
    return made
