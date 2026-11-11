"""The Owner's weekly summary and monthly report (section 9, "Monthly. Accuracy report,
count compliance, shrinkage and wastage summaries emailed to the Owner"; section 14,
"Weekly owner summary -- Owner").

Weekly, from Monday 07:00 in the company's time zone, for the week Monday to Sunday just
ended: sales against the week before, stock value now, products out of stock, shrinkage
and wastage at cost (expiry losses named), forecast accuracy, orders sent and cycle counts
done against those due.

Monthly, from the 1st at 07:00, for the month just ended: forecast accuracy and bias by
store, cycle counts done against due by ABC class, shrinkage, and wastage with expiry
losses, with every report for the month attached in one Excel workbook.

Who: Owners, unless they untick it under My alert settings; Admins only once they tick
it there. How: in the app always (an alert that opens Reports for the period); by email
to people who chose email, and to an Owner who has not yet saved alert settings at all
(the specification has the Owner emailed); a short WhatsApp text to people who chose
WhatsApp. The workbook goes by email only.

Once per company and period, whatever the worker does: the period's alert, with the
dedupe key owner_summary:2026-W45 or owner_monthly:2026-10 (the period summarised, not
the day it is sent), is claimed in its own transaction before anything is sent and
marked sent afterwards. A worker that stops in between sends that period on its next
pass; one that restarts later finds it done. A worker that was off at 07:00 sends the
latest due period when it comes back. A company created after a period ended gets none
for it.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import alerts as A
from . import reports as R
from . import stock as stock_mod
from .config import settings
from .db import utcnow
from .formats import day as fmt_day
from .formats import group_indian, rupees_short
from .mailer import MailError, send
from .models import Alert, Company, DailySales, Person, Product, PurchaseOrder, StockOnHand, Store
from .security import _aware

log = logging.getLogger("dqstack.summaries")

WEEKLY, MONTHLY = A.SUMMARY_KINDS
SEND_AT = time(7, 0)
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# ===================================================================== periods

def label(kind: str, start: date) -> str:
    if kind == WEEKLY:
        y, w, _ = start.isocalendar()
        return f"{y}-W{w:02d}"
    return f"{start:%Y-%m}"


def period_of(kind: str, lab: str) -> tuple[date, date]:
    if kind == WEEKLY:
        y, w = lab.split("-W")
        s = date.fromisocalendar(int(y), int(w), 1)
        return s, s + timedelta(days=6)
    y, m = lab.split("-")
    s = date(int(y), int(m), 1)
    return s, (s + timedelta(days=32)).replace(day=1) - timedelta(days=1)


def latest_periods(tz, now: datetime) -> list[tuple[str, date, date]]:
    """The latest week and month whose summaries were due by `now`: (kind, first, last)."""
    local = now.astimezone(tz)
    today = local.date()
    monday = today - timedelta(days=today.weekday())
    if local < datetime.combine(monday, SEND_AT, tzinfo=tz):
        monday -= timedelta(days=7)
    first = today.replace(day=1)
    if local < datetime.combine(first, SEND_AT, tzinfo=tz):
        first = (first - timedelta(days=1)).replace(day=1)
    m_end = first - timedelta(days=1)
    return [(WEEKLY, monday - timedelta(days=7), monday - timedelta(days=1)),
            (MONTHLY, m_end.replace(day=1), m_end)]


def _period(start: date, end: date) -> str:
    return f"{fmt_day(start)} to {fmt_day(end)}"


# ===================================================================== figures

def _revenue(db: Session, cid: str, ids: list[str], start: date, end: date) -> int:
    return int(db.scalar(select(func.coalesce(func.sum(DailySales.revenue_paise), 0)).where(
        DailySales.company_id == cid, DailySales.store_id.in_(ids), DailySales.day >= start,
        DailySales.day <= end)) or 0)


def weekly_figures(db: Session, cid: str, ids: list[str], start: date, end: date) -> dict:
    """Everything in the weekly summary, as numbers (paise, counts, fractions)."""
    _, lo, hi = R._bounds(db, cid, start, end)
    shr = R.shrinkage(db, cid, ids, start, end)["totals"]
    was = R.wastage(db, cid, ids, start, end)["totals"]
    acc = R.accuracy_by_store(db, cid, ids, start, end).get(None)
    counts = R.count_compliance(db, cid, ids, start, end)["totals"]
    days = (end - start).days + 1
    return {
        "sales_paise": _revenue(db, cid, ids, start, end),
        "previous_sales_paise": _revenue(db, cid, ids, start - timedelta(days=days),
                                         start - timedelta(days=1)),
        "stock_value_paise": stock_mod.valuation(db, cid, ids)["total_paise"],
        # Known stock at zero, as the Stock screen's "Out" and the out-of-stock alert.
        "out_of_stock_now": int(db.scalar(select(func.count()).select_from(StockOnHand).join(
            Product, Product.id == StockOnHand.product_id).where(
            StockOnHand.company_id == cid, StockOnHand.store_id.in_(ids),
            StockOnHand.known.is_(True), StockOnHand.on_hand_milli == 0,
            Product.status != "discontinued")) or 0),
        "empty_shelf_days": int(db.scalar(select(func.count()).select_from(DailySales).where(
            DailySales.company_id == cid, DailySales.store_id.in_(ids),
            DailySales.day >= start, DailySales.day <= end,
            DailySales.out_of_stock.is_(True))) or 0),
        "shrinkage_paise": int(shr.get("lost_paise") or 0),
        "wastage_paise": int(was.get("value_paise") or 0),
        "expiry_paise": int(was.get("expiry_paise") or 0),
        "forecast_error": acc["error"] if acc else None,
        "forecast_bias": acc["bias"] if acc else None,
        "orders_sent": int(db.scalar(select(func.count()).select_from(PurchaseOrder).where(
            PurchaseOrder.company_id == cid, PurchaseOrder.store_id.in_(ids),
            PurchaseOrder.sent_at >= lo, PurchaseOrder.sent_at < hi)) or 0),
        "counts_due": counts["counts_due"], "counts_done": counts["counts_done"],
    }


def monthly_figures(db: Session, cid: str, ids: list[str], start: date, end: date) -> dict:
    names = {s.id: s.name for s in db.scalars(select(Store).where(Store.id.in_(ids)))}
    acc = R.accuracy_by_store(db, cid, ids, start, end)
    by_class: dict = {}
    for (_, cls), v in R.count_schedule(db, cid, ids, start, end).items():
        c = by_class.setdefault(cls, {"due": 0, "done": 0})
        c["due"] += v["due"]
        c["done"] += v["done"]
    shr = R.shrinkage(db, cid, ids, start, end)["totals"]
    was = R.wastage(db, cid, ids, start, end)["totals"]
    lost, net = int(shr.get("lost_paise") or 0), int(shr.get("net_paise") or 0)
    return {
        "accuracy": sorted(((names[sid], v) for sid, v in acc.items() if sid is not None),
                           key=lambda t: t[0]),
        "accuracy_all": acc.get(None),
        "counts": dict(sorted(by_class.items())),
        "counts_due": sum(c["due"] for c in by_class.values()),
        "counts_done": sum(c["done"] for c in by_class.values()),
        "shrinkage_lost_paise": lost, "shrinkage_found_paise": net + lost,
        "shrinkage_net_paise": net,
        "wastage_paise": int(was.get("value_paise") or 0),
        "expiry_paise": int(was.get("expiry_paise") or 0),
    }


# ===================================================================== words

def _pct(x: float) -> str:
    return f"{round(abs(x) * 100)}%"


def _n(n: int, one: str, many: str | None = None) -> str:
    return f"{group_indian(n)} {one if n == 1 else (many or one + 's')}"


def accuracy_words(error: float | None, bias: float | None) -> str:
    if error is None:
        return "not scored (no forecast yet, or nothing sold to score it against)"
    b = bias or 0
    lean = ("neither high nor low on balance" if round(b * 100) == 0
            else f"{_pct(b)} {'high' if b > 0 else 'low'} on balance")
    return f"off by {_pct(error)} of what sold, {lean}"


def _change(now: int, before: int) -> tuple[str, str]:
    """('up 20% on the week before (₹3.50 L)', 'up 20%')"""
    if before <= 0:
        return "with no sales the week before", "no sales the week before"
    ch = (now - before) / before
    if round(ch * 100) == 0:
        return f"the same as the week before ({rupees_short(before)})", "level"
    word = "up" if ch > 0 else "down"
    return f"{word} {_pct(ch)} on the week before ({rupees_short(before)})", f"{word} {_pct(ch)}"


def _counts(done: int, due: int) -> str:
    if not due:
        return "none were due"
    return f"{group_indian(done)} of {group_indian(due)} due counts done ({_pct(done / due)})"


def weekly_text(company: str, start: date, end: date, f: dict) -> tuple[str, list[str], str]:
    """(subject, body lines, short text for WhatsApp and the app)."""
    change, change_short = _change(f["sales_paise"], f["previous_sales_paise"])
    loss = f["shrinkage_paise"] + f["wastage_paise"]
    err = (f"off by {_pct(f['forecast_error'])}" if f["forecast_error"] is not None
           else "not scored")
    lines = [
        f"Here is how {company} did in the week from {_period(start, end)}.",
        "",
        f"Sales: {rupees_short(f['sales_paise'])}, {change}.",
        f"Stock at cost: {rupees_short(f['stock_value_paise'])} now.",
        f"Out of stock: {_n(f['out_of_stock_now'], 'product')} now; "
        f"{_n(f['empty_shelf_days'], 'product-day')} on an empty shelf last week.",
        f"Losses at cost: {rupees_short(loss)}. Shrinkage (stock missing at counts) "
        f"{rupees_short(f['shrinkage_paise'])}; wastage {rupees_short(f['wastage_paise'])}, "
        f"of which {rupees_short(f['expiry_paise'])} expired.",
        f"Forecast: {accuracy_words(f['forecast_error'], f['forecast_bias'])}.",
        f"Orders sent to suppliers: {group_indian(f['orders_sent'])}.",
        f"Stock counts: {_counts(f['counts_done'], f['counts_due'])}.",
    ]
    short = (f"{company}, week of {start:%d %b}: sales {rupees_short(f['sales_paise'])} "
             f"({change_short}), stock {rupees_short(f['stock_value_paise'])}, "
             f"{group_indian(f['out_of_stock_now'])} out of stock, losses {rupees_short(loss)}, "
             f"forecast {err}, {_n(f['orders_sent'], 'order')} sent, counts "
             f"{group_indian(f['counts_done'])} of {group_indian(f['counts_due'])} done.")
    return f"DQ-Stack weekly summary: {company}, {_period(start, end)}", lines, short


def monthly_text(company: str, start: date, end: date, f: dict) -> tuple[str, list[str], str]:
    month = f"{start:%B %Y}"
    lines = [f"{company}'s report for {month}. Every report for the month is in the attached "
             "Excel workbook, a sheet each.", "",
             "Forecast accuracy by store. Error is how much of what sold the forecast missed; "
             "bias is whether it ran high or low."]
    for name, a in f["accuracy"]:
        lines.append(f"  {name}: {accuracy_words(a['error'], a['bias'])}.")
    a = f["accuracy_all"]
    lines.append(f"  All stores: {accuracy_words(a['error'] if a else None, a['bias'] if a else None)}.")
    lines += ["", "Cycle counts done against those due, by ABC class:"]
    for cls, c in f["counts"].items():
        lines.append(f"  {cls}: {_counts(c['done'], c['due'])}.")
    lines.append(f"  All: {_counts(f['counts_done'], f['counts_due'])}.")
    net = f["shrinkage_net_paise"]
    net_words = (f"net {rupees_short(-net)} lost" if net < 0 else
                 f"net {rupees_short(net)} gained" if net > 0 else "nothing net")
    lines += ["",
              f"Shrinkage: {rupees_short(f['shrinkage_lost_paise'])} of stock missing at counts, "
              f"{rupees_short(f['shrinkage_found_paise'])} found: {net_words}.",
              f"Wastage: {rupees_short(f['wastage_paise'])} written off at cost, of which "
              f"{rupees_short(f['expiry_paise'])} expired."]
    err = (f"off by {_pct(a['error'])}" if a and a["error"] is not None else "not scored")
    short = (f"{company}, {month}: forecast {err}, counts {group_indian(f['counts_done'])} of "
             f"{group_indian(f['counts_due'])} done, shrinkage "
             f"{rupees_short(f['shrinkage_lost_paise'])}, wastage {rupees_short(f['wastage_paise'])} "
             f"({rupees_short(f['expiry_paise'])} expired). Every report for the month is under "
             "Reports.")
    return f"DQ-Stack monthly report: {company}, {month}", lines, short


def workbook(db: Session, cid: str, ids: list[str], start: date, end: date,
             lines: list[str]) -> bytes:
    """The summary itself, then every report for the month, a sheet each."""
    summary = R._out("Owner summary", [("line", "Summary", "text")],
                     [{"line": s.strip()} for s in lines if s.strip()])
    reps = [summary] + [fn(db, cid, ids, start, end) for fn in R.REPORTS.values()]
    return R.to_xlsx_many(reps, _period(start, end))


# ===================================================================== sending

def recipients(db: Session, cid: str, kind: str) -> list[Person]:
    out = []
    for p in db.scalars(select(Person).where(Person.company_id == cid, Person.status == "active")
                        .order_by(Person.created_at)):
        n = p.notify or {}
        if (p.role == "owner" and kind not in n.get("muted", [])) or \
                (p.role == "admin" and kind in n.get("opted_in", [])):
            out.append(p)
    return out


def wants_email(p: Person) -> bool:
    """The person's email choice; an Owner who has never saved alert settings gets the
    summaries by email, as the specification has it."""
    n = p.notify or {}
    return bool(n["email"]) if "email" in n else p.role == "owner"


def _deliver(db: Session, co: Company, ids: list[str], a: Alert, kind: str, start: date,
             end: date) -> int:
    from . import whatsapp as wa
    try:
        if kind == WEEKLY:
            subject, lines, short = weekly_text(co.name, start, end,
                                                weekly_figures(db, co.id, ids, start, end))
            files = []
        else:
            subject, lines, short = monthly_text(co.name, start, end,
                                                 monthly_figures(db, co.id, ids, start, end))
            files = [(f"dqstack-report-{start:%Y-%m}.xlsx",
                      workbook(db, co.id, ids, start, end, lines), XLSX)]
    except Exception:  # noqa: BLE001 -- marked done so a fault is not retried every minute
        db.rollback()
        log.exception("%s for company %s could not be prepared", kind, co.id)
        a.message = (f"The {A.LABELS[kind].lower()} for {_period(start, end)} could not be "
                     "prepared. The details are in the server's log.")
        a.notified_at = utcnow()
        db.commit()
        return 0
    a.message = short[:500]
    base = settings.public_url
    link = f"{base}/app/#/reports?from={start.isoformat()}&to={end.isoformat()}"
    sent = 0
    for p in recipients(db, co.id, kind):
        role = "an Owner" if p.role == "owner" else "an Admin"
        if wants_email(p) and p.email:
            body = "\n".join([f"Hello {p.name},", ""] + lines + [
                "", f"The detail is under Reports: {link}", "",
                f"You get this as {role} of {co.name}. To stop it, or to have it by WhatsApp "
                f"instead, change My alert settings: {base}/app/#/alerts/settings"])
            try:
                send(p.email, subject, body + "\n", files)
                sent += 1
            except MailError as e:
                log.warning("%s not emailed to person %s: %s", kind, p.id, e)
        if (p.notify or {}).get("whatsapp") and p.phone and wa.configured():
            try:
                wa.send(p.phone, f"Hello {p.name}. {short}", "alert", [p.name, short])
                sent += 1
            except wa.WhatsAppError as e:
                log.warning("%s not sent by WhatsApp to person %s: %s", kind, p.id, e)
    now = utcnow()
    a.notified_at = now
    for old in db.scalars(select(Alert).where(Alert.company_id == co.id, Alert.kind == kind,
                                              Alert.id != a.id, Alert.resolved_at.is_(None))):
        old.resolved_at = now               # the new one replaces it on the bell
    db.commit()
    return sent


def run(db: Session, company_id: str, now: datetime | None = None,
        kinds: tuple[str, ...] = A.SUMMARY_KINDS) -> list[str]:
    """Send whatever summary of `kinds` is due for one company. Returns the periods
    sent (their dedupe keys)."""
    from .sales import tz_of
    now = now or utcnow()
    co = db.get(Company, company_id)
    if co is None or co.status != "active" or co.is_demo:
        return []
    ids = list(db.scalars(select(Store.id).where(Store.company_id == company_id,
                                                 Store.active.is_(True))))
    if not ids:
        return []
    tz = tz_of(db, company_id)
    done: list[str] = []
    # Claimed by a pass that stopped before sending: send them now.
    for a in list(db.scalars(select(Alert).where(Alert.company_id == company_id,
                                                 Alert.kind.in_(kinds),
                                                 Alert.notified_at.is_(None)))):
        try:
            start, end = period_of(a.kind, a.dedupe_key.split(":", 1)[1])
        except (ValueError, IndexError):
            a.notified_at = utcnow()
            db.commit()
            continue
        _deliver(db, co, ids, a, a.kind, start, end)
        done.append(a.dedupe_key)
    created = _aware(co.created_at).astimezone(tz).date() if co.created_at else None
    for kind, start, end in latest_periods(tz, now):
        if kind not in kinds or (created is not None and created > end):
            continue
        key = f"{kind}:{label(kind, start)}"
        if db.scalar(select(Alert.id).where(Alert.company_id == company_id,
                                            Alert.dedupe_key == key)):
            continue
        a = Alert(company_id=company_id, kind=kind, severity="info", dedupe_key=key,
                  message=f"The {A.LABELS[kind].lower()} for {_period(start, end)} is being "
                          "prepared.",
                  link=f"#/reports?from={start.isoformat()}&to={end.isoformat()}")
        db.add(a)
        try:
            db.commit()                     # the claim: one per company and period
        except IntegrityError:
            db.rollback()                   # another worker claimed it a moment ago
            continue
        _deliver(db, co, ids, a, kind, start, end)
        done.append(key)
    return done
