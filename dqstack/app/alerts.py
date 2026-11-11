"""Alerts: what needs attention, raised once, sent to the people whose job it is
(sections 9 and 14, build step 11).

`evaluate()` looks at the company as it is now and raises an alert for each
condition. Every alert carries a dedupe key -- the kind, the store, the product and
the day or week -- so evaluating every few minutes raises each one once. When the
condition clears, the alert resolves itself.

Who hears about what follows the notification table of section 14. In-app is
always on; email and WhatsApp go to people who chose them. WhatsApp needs the
WhatsApp Business API set up on the server (whatsapp.py); without it the choice is
kept but nothing is sent, and the settings screen says so.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import rules as rules_mod
from . import stock as stock_mod
from .db import utcnow
from .formats import rupees
from .mailer import MailError, send
from .models import (Alert, Company, CountLine, CountSession, DailySales, DayClose, Person,
                     Product, PurchaseOrder, Recommendation, StockMovement, StockOnHand, Store)
from .security import _aware

log = logging.getLogger("dqstack.alerts")

# kind -> (roles that receive it, severity, where to act)
KINDS: dict[str, tuple[tuple[str, ...], str, str]] = {
    "out_of_stock": (("store_manager",), "risk", "#/stock?status=Out"),
    "low_stock": (("store_manager",), "attention", "#/stock?status=Low"),
    "negative_stock": (("store_manager", "stock_clerk"), "risk", "#/count"),
    "expiring": (("stock_clerk", "store_manager"), "attention", "#/stock?status=Expiring"),
    "overstocked": (("store_manager",), "attention", "#/stock?status=Overstocked"),
    "count_overdue": (("stock_clerk", "store_manager"), "attention", "#/count"),
    "count_waiting": (("store_manager",), "attention", "#/count/approvals"),
    "shrinkage": (("store_manager", "owner"), "risk", "#/reports/shrinkage"),
    "today_ready": (("store_manager", "purchaser"), "info", "#/today"),
    "orders_waiting": (("purchaser", "store_manager"), "attention", "#/orders"),
    "delivery_overdue": (("purchaser",), "attention", "#/orders"),
    "sales_missing": (("store_manager", "admin"), "attention", "#/sales"),
    "unmapped_barcode": (("store_manager",), "attention", "#/products"),
    "forecast_nightly_failed": (("admin", "owner"), "risk", "#/model"),
    "forecast_train_failed": (("admin", "owner"), "risk", "#/model"),
    "accuracy_drop": (("admin", "owner"), "attention", "#/model"),
    # A nightly file from the SFTP drop that was not (fully) recorded (filedrop.py).
    "file_drop_failed": (("admin",), "risk", "#/settings/integrations"),
    # The Owner's summaries (summaries.py): Owners, and Admins who tick them.
    "owner_summary": (("owner",), "info", "#/reports"),
    "owner_monthly": (("owner",), "info", "#/reports"),
}
LABELS = {
    "out_of_stock": "Out of stock", "low_stock": "Below target, nothing ordered",
    "negative_stock": "Count needed", "expiring": "Expiring soon", "overstocked": "Overstocked",
    "count_overdue": "Count overdue", "count_waiting": "Count difference to approve",
    "shrinkage": "Unusual shrinkage", "today_ready": "Today's review is ready",
    "orders_waiting": "Draft orders to approve", "delivery_overdue": "Delivery overdue",
    "sales_missing": "Sales not uploaded", "unmapped_barcode": "Barcode to add",
    "forecast_nightly_failed": "Forecast did not run", "forecast_train_failed": "Training failed",
    "accuracy_drop": "Forecast accuracy dropped",
    "file_drop_failed": "Nightly file not recorded",
    "owner_summary": "Weekly owner summary", "owner_monthly": "Monthly owner report",
}
# Sent by summaries.py with their own text and workbook, never by deliver(); an Admin
# receives them only after ticking them under My alert settings.
SUMMARY_KINDS = ("owner_summary", "owner_monthly")
OPT_IN = SUMMARY_KINDS


class _Raiser:
    def __init__(self, db: Session, company_id: str):
        self.db = db
        self.cid = company_id
        self.live: set[str] = set()
        self.new: list[Alert] = []
        self.existing = {a.dedupe_key: a for a in db.scalars(select(Alert).where(
            Alert.company_id == company_id))}

    def __call__(self, kind: str, key: str, message: str, store_id: str | None = None,
                 product_id: str | None = None) -> None:
        full = f"{kind}:{key}"
        self.live.add(full)
        if full in self.existing:
            a = self.existing[full]
            if a.resolved_at is not None and kind in CONDITIONS:
                a.resolved_at = None            # the condition came back the same day
            return
        roles, severity, link = KINDS[kind]
        a = Alert(company_id=self.cid, store_id=store_id, product_id=product_id, kind=kind,
                  severity=severity, message=message[:500], dedupe_key=full, link=link)
        self.db.add(a)
        self.existing[full] = a
        self.new.append(a)


# Conditions that resolve themselves when they stop being true.
CONDITIONS = {"out_of_stock", "low_stock", "negative_stock", "expiring", "overstocked",
              "count_overdue", "count_waiting", "orders_waiting", "delivery_overdue"}


def evaluate(db: Session, company_id: str, now: datetime | None = None) -> list[Alert]:
    """Raise what is true now; resolve what no longer is. Returns the new alerts."""
    now = now or utcnow()
    co = db.get(Company, company_id)
    r = rules_mod.effective(co.rules)
    from .sales import local_today, tz_of
    tz = tz_of(db, company_id)
    today = local_today(tz, now)
    week = f"{today.isocalendar()[0]}w{today.isocalendar()[1]}"
    raise_ = _Raiser(db, company_id)
    stores = list(db.scalars(select(Store).where(Store.company_id == company_id,
                                                 Store.active.is_(True))))
    products = {p.id: p for p in db.scalars(select(Product).where(
        Product.company_id == company_id, Product.status != "discontinued"))}
    for st in stores:
        rows = stock_mod.stock_rows(db, company_id, st, include_discontinued=False, today=today)
        for row in rows:
            pid, name = row["product_id"], row["name"]
            k = f"{st.id}:{pid}:{today.isoformat()}"
            if not row["known"]:
                continue
            if row["on_hand_milli"] < 0:
                raise_("negative_stock", k, f"{name} in {st.name} shows below zero. Count it.",
                       st.id, pid)
            elif row["status"] == "Out":
                raise_("out_of_stock", k, f"{name} is out of stock in {st.name}.", st.id, pid)
            elif row["status"] == "Low":
                raise_("low_stock", k, f"{name} in {st.name} is below its stock target and "
                                       "nothing is on order.", st.id, pid)
            elif row["status"] == "Overstocked":
                raise_("overstocked", f"{st.id}:{pid}:{week}",
                       f"{name} in {st.name} has {row['days_of_cover']} days of cover.", st.id, pid)
            if row["expiring_milli"] > 0:
                raise_("expiring", k, f"{row['expiring_milli'] / 1000:g} of {name} in {st.name} "
                                      f"expire by {row['expiring_on']}.", st.id, pid)
        # Cycle counts overdue (one alert per store per day, with the count).
        from .floor import due_list
        overdue = [d for d in due_list(db, company_id, st, today) if d["last_counted_at"]
                   and (today - date.fromisoformat(d["due_since"])).days > 0]
        if overdue:
            raise_("count_overdue", f"{st.id}:{today.isoformat()}",
                   f"{len(overdue)} products in {st.name} are past their count date.", st.id)
        waiting = db.scalar(select(func.count()).select_from(CountLine).join(
            CountSession, CountSession.id == CountLine.session_id).where(
            CountSession.store_id == st.id, CountLine.status.in_(("waiting", "conflict")))) or 0
        if waiting:
            raise_("count_waiting", f"{st.id}:{today.isoformat()}",
                   f"{waiting} count difference(s) in {st.name} wait for approval.", st.id)
        # Shrinkage: count losses this week above the business rule.
        week_start = today - timedelta(days=today.weekday())
        loss = db.scalar(select(func.coalesce(func.sum(
            StockMovement.qty_milli * func.coalesce(StockMovement.unit_cost_paise, 0)), 0)).where(
            StockMovement.store_id == st.id, StockMovement.type == "count_adjustment",
            StockMovement.qty_milli < 0,
            StockMovement.at >= datetime.combine(week_start, datetime.min.time(), tzinfo=tz))) or 0
        loss = -int(loss) // 1000
        if loss > int(r.get("shrinkage_alert_paise", 500_000)):
            raise_("shrinkage", f"{st.id}:{week}", f"Count losses in {st.name} this week are "
                                                   f"{rupees(loss, decimals=False)}.", st.id)
        # A standalone store with no sales for yesterday, after its close.
        if st.mode == "standalone":
            y = today - timedelta(days=1)
            has = db.scalar(select(func.count()).select_from(DailySales).where(
                DailySales.store_id == st.id, DailySales.day == y,
                DailySales.source.in_(("upload", "entry")))) or 0
            closed = db.scalar(select(DayClose).where(DayClose.store_id == st.id,
                                                      DayClose.day == y))
            if not has and not (closed and closed.store_was_closed):
                raise_("sales_missing", f"{st.id}:{y.isoformat()}",
                       f"{st.name} has not sent its sales for {y:%d %b}. The forecast will run "
                       "on data to the last day received.", st.id)
    # Orders: drafts waiting, deliveries overdue.
    drafts = db.scalar(select(func.count()).select_from(PurchaseOrder).where(
        PurchaseOrder.company_id == company_id, PurchaseOrder.status == "draft")) or 0
    if drafts:
        raise_("orders_waiting", today.isoformat(), f"{drafts} draft order(s) wait for approval.")
    for po in db.scalars(select(PurchaseOrder).where(
            PurchaseOrder.company_id == company_id,
            PurchaseOrder.status.in_(("approved", "sent", "partly_received")),
            PurchaseOrder.expected_date < today)):
        raise_("delivery_overdue", f"{po.id}:{today.isoformat()}",
               f"Order {po.number} was expected on {po.expected_date:%d %b} and has not "
               "fully arrived.", po.store_id)
    # Today's review is ready (once per forecast).
    from .forecast.jobs import current_run
    cur = current_run(db, company_id)
    if cur is not None:
        for st in stores:
            n = db.scalar(select(func.count()).select_from(Recommendation).where(
                Recommendation.run_id == cur.id, Recommendation.store_id == st.id,
                Recommendation.flagged.is_(True))) or 0
            if n:
                raise_("today_ready", f"{cur.id}:{st.id}",
                       f"{n} products in {st.name} are worth a look today.", st.id)
    # Resolve conditions that are no longer true today.
    for key, a in raise_.existing.items():
        if a.kind in CONDITIONS and a.resolved_at is None and key not in raise_.live:
            a.resolved_at = now
    db.flush()
    return raise_.new


def recipients(db: Session, a: Alert) -> list[Person]:
    roles = KINDS.get(a.kind, ((), "", ""))[0]
    people = db.scalars(select(Person).where(Person.company_id == a.company_id,
                                             Person.status == "active"))
    out = []
    for p in people:
        if p.role not in roles and not (p.role in ("owner", "admin") and "admin" in roles):
            continue
        if a.store_id and p.role not in ("owner", "admin") and \
                a.store_id not in {s.id for s in p.stores}:
            continue
        out.append(p)
    return out


def deliver(db: Session, company_id: str) -> int:
    """Send new alerts to the people who chose email or WhatsApp (and did not mute that
    kind). Each alert is sent once: it is marked notified whether or not a message got
    through, and a failure is logged. WhatsApp alerts raised together reach a person as
    one message. Returns how many messages were sent."""
    from . import whatsapp as wa
    sent = 0
    by_whatsapp: dict[str, tuple[Person, list[Alert]]] = {}
    for a in db.scalars(select(Alert).where(Alert.company_id == company_id,
                                            Alert.notified_at.is_(None),
                                            Alert.resolved_at.is_(None),
                                            Alert.kind.not_in(SUMMARY_KINDS))):
        for p in recipients(db, a):
            prefs = p.notify or {}
            if a.kind in prefs.get("muted", []):
                continue
            if prefs.get("email") and p.email:
                try:
                    send(p.email, f"DQ-Stack: {LABELS.get(a.kind, a.kind)}",
                         f"Hello {p.name},\n\n{a.message}\n")
                    sent += 1
                except MailError as e:
                    log.warning("alert %s not emailed to person %s: %s", a.id, p.id, e)
            if prefs.get("whatsapp") and p.phone and wa.configured():
                by_whatsapp.setdefault(p.id, (p, []))[1].append(a)
        a.notified_at = utcnow()
    for p, items in by_whatsapp.values():
        lines = [f"{LABELS.get(a.kind, a.kind)}: {a.message}" for a in items]
        if len(lines) == 1:
            text = f"Hello {p.name}, a DQ-Stack alert.\n\n{lines[0]}"
        else:
            shown = lines[:10] + ([f"and {len(lines) - 10} more in the app"]
                                  if len(lines) > 10 else [])
            text = (f"Hello {p.name}, {len(lines)} DQ-Stack alerts.\n\n"
                    + "\n".join("- " + s for s in shown))
        try:
            wa.send(p.phone, text, "alert", [p.name, "; ".join(lines)])
            sent += 1
        except wa.WhatsAppError as e:
            log.warning("%d alert(s) not sent by WhatsApp to person %s: %s", len(items), p.id, e)
    db.flush()
    return sent


def visible_kinds(role: str) -> set[str]:
    if role in ("owner", "admin"):
        return set(KINDS)
    return {k for k, (roles, _, _) in KINDS.items() if role in roles}
