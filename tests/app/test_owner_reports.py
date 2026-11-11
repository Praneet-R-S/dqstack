"""The operations gaps against the specification, closed:

* the Owner's weekly summary and monthly report (section 9 "Monthly ... emailed to the
  Owner"; section 14 "Weekly owner summary -- Owner"), sent once per period;
* stock turn and days of cover, cycle-count compliance, expiry losses within wastage,
  and order performance with emergency orders, each figure checked by hand;
* the nightly file drop by SFTP (section 14), through the same code as the API.
"""

from __future__ import annotations

import io
import json
import os
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from dqstack.app import alerts as A
from dqstack.app import db as dbmod
from dqstack.app import filedrop, ledger, manage, summaries, whatsapp, worker
from dqstack.app.mailer import outbox
from dqstack.app.models import (ActivityLog, Alert, Company, CountLine, CountSession, DailySales,
                                Delivery, ForecastRun, ForecastScore, Person, Product,
                                PurchaseOrder)

from .conftest import invite_and_join

IST = ZoneInfo("Asia/Kolkata")
PRODUCTS = ("SKU,Name,Category,Unit,MRP,Selling price,Cost,Perishable\n"
            "A,Atta,Staples,each,100,100,80,no\n"
            "B,Bread,Bakery,each,40,40,30,no\n"
            "C,Curd,Dairy,each,30,30,20,yes\n"
            "D,Dal,Staples,each,60,60,50,no\n")


def _today():
    return datetime.now(IST).date()


def _at(d, hour=12):
    """Noon (or `hour`) on day d in India, as the UTC moment the database keeps."""
    return datetime.combine(d, time(hour), tzinfo=IST).astimezone(timezone.utc)


def _company(make_client, stores=(("S1", "Andheri"),)):
    owner = make_client()
    owner.signup()
    sids = [owner.post("/api/stores", {"code": c, "name": n}).json()["id"] for c, n in stores]
    owner.import_file("products", PRODUCTS)
    with dbmod.SessionLocal() as db:
        pid = {p.sku: p.id for p in db.scalars(select(Product))}
    return owner, owner.me["company"]["id"], sids, pid


def _post(cid, sid, product_id, q, typ="opening", **kw):
    with dbmod.SessionLocal() as db:
        ledger.post(db, company_id=cid, store_id=sid, product_id=product_id, qty_milli=q,
                    type=typ, **kw)
        db.commit()


def _classes(cid, **by_sku):
    with dbmod.SessionLocal() as db:
        for p in db.scalars(select(Product).where(Product.company_id == cid)):
            p.abc_class = by_sku.get(p.sku, by_sku.get("rest"))
        db.commit()


def _count(cid, sid, product_id, on, status="posted"):
    with dbmod.SessionLocal() as db:
        s = CountSession(company_id=cid, store_id=sid, kind="cycle")
        db.add(s)
        db.flush()
        db.add(CountLine(company_id=cid, session_id=s.id, product_id=product_id, counted_milli=0,
                         system_milli=0, counted_at=_at(on), status=status))
        db.commit()


# ===================================================================== reports

def test_stock_turn_and_days_of_cover_by_hand(make_client):
    owner, cid, (sid,), pid = _company(make_client)
    t = _today()
    start, end = t - timedelta(days=10), t - timedelta(days=1)            # ten days
    _post(cid, sid, pid["A"], 100_000, at=_at(t - timedelta(days=20)))   # 100 Atta at Rs 80
    _post(cid, sid, pid["A"], -30_000, "sale", at=_at(t - timedelta(days=5)))
    _post(cid, sid, pid["A"], 5_000, "customer_return", at=_at(t - timedelta(days=4)))
    _post(cid, sid, pid["A"], 20_000, "delivery", at=_at(t - timedelta(days=3)))
    _post(cid, sid, pid["A"], -10_000, "sale", at=_at(t, 0))             # today: after the period
    _post(cid, sid, pid["B"], 10_000, at=_at(t - timedelta(days=20)))    # 10 Bread, never sold
    rep = owner.get(f"/api/reports/stock_turn?store_id={sid}&from={start}&to={end}").json()
    rows = {r["category"]: r for r in rep["rows"]}
    a = rows["Staples"]
    # Start: 100 x Rs 80 = Rs 8,000. End: 85 on hand now, plus the 10 sold today, = 95 x
    # Rs 80 = Rs 7,600. Cost of goods sold: (30 sold - 5 returned) x Rs 80 = Rs 2,000.
    assert (a["start_value_paise"], a["end_value_paise"], a["on_hand_milli"]) == \
        (800_000, 760_000, 95_000)
    assert (a["sold_milli"], a["cogs_paise"]) == (25_000, 200_000)
    # Turn: 2,000 / ((8,000 + 7,600) / 2) = 0.256 -> 0.26; a year: 0.256 x 365 / 10 = 9.36
    # -> 9.4; days of cover: 7,600 / (2,000 / 10 days) = 38.
    assert (a["turn"], a["turn_year"], a["days_of_cover"]) == (0.26, 9.4, 38.0)
    # Bread did not sell: no turn, and no cover to speak of.
    b = rows["Bakery"]
    assert (b["start_value_paise"], b["cogs_paise"], b["turn"], b["days_of_cover"]) == \
        (30_000, 0, 0.0, None)
    # Totals: (8,000 + 300) and (7,600 + 300); turn 2,000 / 8,100 = 0.247 -> 0.25;
    # cover 7,900 / 200 = 39.5 days.
    tot = rep["totals"]
    assert (tot["start_value_paise"], tot["end_value_paise"], tot["cogs_paise"]) == \
        (830_000, 790_000, 200_000)
    assert (tot["turn"], tot["days_of_cover"]) == (0.25, 39.5)

    # Wastage keeps expiry losses in their own column: 3 Curd expired at Rs 20 = Rs 60;
    # 1 Bread damaged at Rs 30 is wastage but not expiry.
    _post(cid, sid, pid["C"], 5_000, at=_at(t - timedelta(days=20)))
    owner.post("/api/wastage", {"store_id": sid, "product_id": pid["C"], "qty_milli": 3_000,
                                "reason": "expired"})
    owner.post("/api/wastage", {"store_id": sid, "product_id": pid["B"], "qty_milli": 1_000,
                                "reason": "damaged"})
    w = owner.get(f"/api/reports/wastage?store_id={sid}&from={t}&to={t}").json()
    by_reason = {r["reason"]: r for r in w["rows"]}
    assert by_reason["expired"]["expiry_paise"] == 6_000
    assert by_reason["damaged"]["expiry_paise"] == 0
    assert w["totals"] == {"value_paise": 9_000, "expiry_paise": 6_000}
    assert "Of which expired" in [c["label"] for c in w["columns"]]


def test_cycle_count_compliance_by_hand(make_client):
    owner, cid, (sid,), pid = _company(make_client)
    _classes(cid, A="A", B="B", C="C", D="C")          # counted every 7, 28 and 91 days
    t = _today()
    day = lambda n: t - timedelta(days=n)
    # Atta (A): last counted 20 days ago, so due 13 days ago and again 6 days ago: 2 due.
    # Counted 12 and 5 days ago: 2 done. A second person's superseded line changes nothing.
    _count(cid, sid, pid["A"], day(20))
    _count(cid, sid, pid["A"], day(12))
    _count(cid, sid, pid["A"], day(5))
    _count(cid, sid, pid["A"], day(5), status="superseded")
    # Bread (B): last counted 30 days ago, due 2 days ago: 1 due. Its count was sent back
    # for a recount, so 0 done.
    _count(cid, sid, pid["B"], day(30))
    _count(cid, sid, pid["B"], day(1), status="rejected")
    # Curd (C): counted 40 days ago, next due in 51 days: none due; an extra count is
    # counted as made but not as done.
    _count(cid, sid, pid["C"], day(40))
    _count(cid, sid, pid["C"], day(3))
    # Dal (C): never counted, so due from the first day: 1 due. Counted (waiting for a
    # manager) 7 days ago: 1 done.
    _count(cid, sid, pid["D"], day(7), status="waiting")
    rep = owner.get(f"/api/reports/count_compliance?store_id={sid}&from={day(13)}&to={t}").json()
    rows = {r["abc_class"]: r for r in rep["rows"]}
    got = {k: (r["products"], r["counts_due"], r["counts_done"], r["compliance"], r["counts_made"])
           for k, r in rows.items()}
    assert got == {"A": (1, 2, 2, 1.0, 2), "B": (1, 1, 0, 0.0, 0), "C": (2, 1, 1, 1.0, 2)}
    assert rep["totals"] == {"counts_due": 4, "counts_done": 3, "compliance": 0.75,
                             "counts_made": 4}


def test_order_performance_by_hand(make_client):
    owner, cid, (sid,), pid = _company(make_client)
    t = _today()
    m = t - timedelta(days=t.weekday() + 21)                         # a Monday three weeks ago
    sup = owner.post("/api/suppliers", {"name": "Gupta Traders", "order_days": [0, 3],
                                        "lead_time_days": 2}).json()   # Mondays and Thursdays

    def order(origin, approved_on, expected, qty=12_000):
        po = owner.post("/api/orders", {
            "store_id": sid, "supplier_id": sup["id"], "origin": origin,
            "expected_date": expected.isoformat() if expected else None,
            "lines": [{"product_id": pid["D"], "qty_milli": qty, "unit_cost_paise": 5_000}]}).json()
        assert owner.post(f"/api/orders/{po['id']}/approve").status_code == 200
        with dbmod.SessionLocal() as db:
            db.get(PurchaseOrder, po["id"]).approved_at = _at(approved_on, 10)
            db.commit()
        return po["id"]

    def deliver(po_id, good, damaged, on):
        r = owner.post("/api/deliveries", {"store_id": sid, "po_id": po_id, "lines": [
            {"product_id": pid["D"], "received_milli": good, "damaged_milli": damaged}]})
        assert r.status_code == 201, r.text
        with dbmod.SessionLocal() as db:
            for d in db.scalars(select(Delivery).where(Delivery.po_id == po_id)):
                d.created_at = _at(on, 16)
            db.commit()

    days = lambda n: timedelta(days=n)
    # 1. From suggestions, approved on a Monday, wanted in 2 days: normal. 120 ordered;
    #    100 good and 5 damaged arrive on the day: on time.
    p1 = order("suggested", m, m + days(2), 120_000)
    deliver(p1, 100_000, 5_000, m + days(2))
    # 2. By hand on a Tuesday, not an order day: an emergency. All 50 arrive a day late.
    p2 = order("manual", m + days(1), m + days(4), 50_000)
    deliver(p2, 50_000, 0, m + days(5))
    # 3. By hand on a Thursday, an order day, but wanted the next day against a 2-day
    #    lead time: an emergency. Nothing has come and the date has passed: late.
    order("manual", m + days(3), m + days(4))
    # 4. From suggestions on a Monday with no expected date: not judged on time.
    order("suggested", m, None)
    # A draft is not an order.
    owner.post("/api/orders", {"store_id": sid, "supplier_id": sup["id"], "lines": [
        {"product_id": pid["D"], "qty_milli": 12_000}]})
    rep = owner.get(f"/api/reports/supplier_performance?store_id={sid}"
                    f"&from={m - days(1)}&to={t}").json()
    assert rep["title"] == "Order performance by supplier"
    row = rep["rows"][0]
    assert row["supplier"] == "Gupta Traders" and row["orders"] == 4
    assert row["fill_rate"] == round(150 / 170, 4)         # (100 + 50) / (120 + 50), arrived
    assert row["on_time"] == round(1 / 3, 4)               # order 1 of orders 1, 2 and 3
    assert row["damaged_share"] == round(5 / 155, 4)       # 5 refused of 155 arrived
    assert (row["unplanned_orders"], row["emergency_orders"]) == (2, 2)
    assert rep["totals"]["emergency_orders"] == 2 and rep["totals"]["orders"] == 4


# ===================================================================== weekly summary

def _alert_settings(make_client, owner, s1):
    """An Admin who has not ticked the summaries, one who has (email and WhatsApp), and
    a store manager who emails everything she can."""
    adil, uma, mona = make_client(), make_client(), make_client()
    invite_and_join(owner, adil, "Adil Admin", "admin", email="adil@acme.example")
    invite_and_join(owner, uma, "Uma Admin", "admin", email="uma@acme.example")
    invite_and_join(owner, mona, "Mona Manager", "store_manager", [s1], email="mona@acme.example")
    # Adil saves the settings screen as it shows them: the summaries unticked.
    shown = adil.get("/api/alerts/settings").json()
    assert {"owner_summary", "owner_monthly"} <= set(shown["muted"])
    assert adil.put("/api/alerts/settings", {"email": True,
                                             "muted": shown["muted"]}).status_code == 200
    s = uma.put("/api/alerts/settings", {"email": True, "whatsapp": True, "muted": []}).json()
    assert "owner_summary" not in s["muted"] and "owner_monthly" not in s["muted"]
    assert mona.put("/api/alerts/settings", {"email": True}).status_code == 200
    with dbmod.SessionLocal() as db:
        db.scalar(select(Person).where(Person.email == "uma@acme.example")).phone = "98200 33333"
        db.commit()
    return adil, uma, mona


def test_weekly_owner_summary_figures_words_and_once_per_week(make_client, monkeypatch):
    owner, cid, (sid,), pid = _company(make_client)
    adil, uma, mona = _alert_settings(make_client, owner, sid)
    # The settings screen: the summaries are listed, ticked for the Owner, unticked for an
    # Admin until she ticks them; a store manager does not see them at all.
    s = adil.get("/api/alerts/settings").json()
    assert {"owner_summary", "owner_monthly"} <= {k["kind"] for k in s["kinds"]}
    assert {"owner_summary", "owner_monthly"} <= set(s["muted"])
    assert "Weekly owner summary" in {k["label"] for k in s["kinds"]}
    assert not {"owner_summary", "owner_monthly"} & set(owner.get("/api/alerts/settings").json()["muted"])
    assert "owner_summary" not in {k["kind"] for k in mona.get("/api/alerts/settings").json()["kinds"]}

    monkeypatch.setattr(whatsapp, "configured", lambda: True)
    monkeypatch.setattr(whatsapp, "_post", lambda number, payload: {"messages": [{"id": "x"}]})
    monkeypatch.delenv("DQ_WHATSAPP_TEMPLATE_ALERT", raising=False)
    whatsapp.outbox.clear()
    _classes(cid, rest="C")
    t = _today()
    monday = t - timedelta(days=t.weekday())
    now = datetime.combine(monday, time(7, 30), tzinfo=IST)          # Monday 07:30
    m = monday - timedelta(days=7)                                    # last week: m to m + 6
    d = lambda n: m + timedelta(days=n)
    with dbmod.SessionLocal() as db:
        # Sales Rs 3,00,000 + Rs 1,20,000 = Rs 4,20,000; the week before Rs 3,50,000: up 20%.
        for day, sku, rupees, oos in ((d(1), "A", 300_000, False), (d(2), "B", 120_000, False),
                                      (d(3), "B", 0, True), (d(-5), "A", 350_000, False)):
            db.add(DailySales(company_id=cid, store_id=sid, product_id=pid[sku], day=day,
                              units_milli=1000 if rupees else 0, revenue_paise=rupees * 100,
                              out_of_stock=oos, source="upload"))
        # The forecast: sold 100 + 100, forecast 110 + 100, missed by 20 + 10: off by
        # 30 / 200 = 15%, running (210 - 200) / 200 = 5% high.
        run = ForecastRun(company_id=cid, kind="nightly", status="succeeded")
        db.add(run)
        db.flush()
        for day, cat, f, e in ((d(1), "Staples", 110_000, 20_000), (d(2), "Bakery", 100_000, 10_000)):
            db.add(ForecastScore(company_id=cid, run_id=run.id, day=day, store_id=sid, category=cat,
                                 n=1, actual_milli=100_000, forecast_milli=f, abs_error_milli=e,
                                 inside_range=1))
        db.commit()
    # Stock now: 8 Atta at Rs 80 and 2 Curd at Rs 20 = Rs 680. Bread known at zero: out.
    _post(cid, sid, pid["A"], 10_000, at=_at(t - timedelta(days=30)))
    _post(cid, sid, pid["A"], -2_000, "count_adjustment", at=_at(d(2)))    # Rs 160 shrinkage
    _post(cid, sid, pid["C"], 5_000, at=_at(t - timedelta(days=30)))
    _post(cid, sid, pid["C"], -3_000, "wastage", reason="expired", at=_at(d(3)))  # Rs 60 expired
    _post(cid, sid, pid["B"], 0, "count_adjustment", makes_known=True, at=_at(t - timedelta(days=30)))
    # Counts: all four products are class C and were never counted before the week, so 4
    # are due; Atta was counted: 1 done.
    _count(cid, sid, pid["A"], d(2))
    # One order sent last week, one the week before.
    for sent_on in (d(1), d(-3)):
        po = owner.post("/api/orders", {"store_id": sid, "lines": [
            {"product_id": pid["D"], "qty_milli": 12_000}]}).json()
        owner.post(f"/api/orders/{po['id']}/approve")
        with dbmod.SessionLocal() as db:
            p = db.get(PurchaseOrder, po["id"])
            p.status, p.sent_at = "sent", _at(sent_on)
            db.commit()

    with dbmod.SessionLocal() as db:
        # A company that did not exist last week gets no summary of it.
        assert summaries.run(db, cid, now, kinds=("owner_summary",)) == []
        db.get(Company, cid).created_at = _at(m - timedelta(days=1))
        db.commit()
        f = summaries.weekly_figures(db, cid, [sid], m, d(6))
    assert f == {"sales_paise": 42_000_000, "previous_sales_paise": 35_000_000,
                 "stock_value_paise": 68_000, "out_of_stock_now": 1, "empty_shelf_days": 1,
                 "shrinkage_paise": 16_000, "wastage_paise": 6_000, "expiry_paise": 6_000,
                 "forecast_error": 0.15, "forecast_bias": 0.05, "orders_sent": 1,
                 "counts_due": 4, "counts_done": 1}

    outbox.clear()
    label = f"{m.isocalendar()[0]}-W{m.isocalendar()[1]:02d}"
    with dbmod.SessionLocal() as db:
        assert summaries.run(db, cid, now, kinds=("owner_summary",)) == [f"owner_summary:{label}"]
    # The Owner (who has never saved settings) and the Admin who ticked it; not the other
    # Admin, nor the store manager.
    assert sorted(mail.to for mail in outbox) == ["asha@acme.example", "uma@acme.example"]
    mail = next(x for x in outbox if x.to == "asha@acme.example")
    assert mail.attachments == []
    for words in (f"week from {d(0):%d %b %Y} to {d(6):%d %b %Y}",
                  "Sales: ₹4.20 L, up 20% on the week before (₹3.50 L).",
                  "Stock at cost: ₹680 now.",
                  "Out of stock: 1 product now; 1 product-day on an empty shelf last week.",
                  "Losses at cost: ₹220. Shrinkage (stock missing at counts) ₹160; "
                  "wastage ₹60, of which ₹60 expired.",
                  "Forecast: off by 15% of what sold, 5% high on balance.",
                  "Orders sent to suppliers: 1.",
                  "Stock counts: 1 of 4 due counts done (25%).",
                  "You get this as an Owner of Acme Retail"):
        assert words in mail.body, words
    # WhatsApp: the short text, to the Admin who chose it.
    assert [w.to for w in whatsapp.outbox] == ["+919820033333"]
    assert "sales ₹4.20 L (up 20%)" in whatsapp.outbox[0].text
    # In the app: one alert for the week, opening Reports on that week.
    listed = [a for a in owner.get("/api/alerts").json() if a["kind"] == "owner_summary"]
    assert len(listed) == 1 and listed[0]["link"] == f"#/reports?from={d(0)}&to={d(6)}"
    assert "week of" in listed[0]["message"] and "15%" in listed[0]["message"]

    # Once a week, whatever the worker does: the same morning, later in the week, and
    # alerts.deliver (which leaves summaries alone) send nothing more.
    with dbmod.SessionLocal() as db:
        assert summaries.run(db, cid, now + timedelta(hours=1), kinds=("owner_summary",)) == []
        assert summaries.run(db, cid, now + timedelta(days=3), kinds=("owner_summary",)) == []
        assert A.deliver(db, cid) == 0
    assert len(outbox) == 2 and len(whatsapp.outbox) == 1

    # The next week: the worker dies after claiming the week but before sending. The next
    # pass sends that week, once.
    class Crash(BaseException):
        pass

    def die(*a, **kw):
        raise Crash()
    nxt = now + timedelta(days=7)
    with monkeypatch.context() as mp, dbmod.SessionLocal() as db:
        mp.setattr(summaries, "_deliver", die)
        with pytest.raises(Crash):
            summaries.run(db, cid, nxt, kinds=("owner_summary",))
    nlabel = f"{monday.isocalendar()[0]}-W{monday.isocalendar()[1]:02d}"
    with dbmod.SessionLocal() as db:
        claimed = db.scalar(select(Alert).where(Alert.dedupe_key == f"owner_summary:{nlabel}"))
        assert claimed is not None and claimed.notified_at is None
        assert summaries.run(db, cid, nxt + timedelta(minutes=1), kinds=("owner_summary",)) == \
            [f"owner_summary:{nlabel}"]
        assert summaries.run(db, cid, nxt + timedelta(minutes=2), kinds=("owner_summary",)) == []
        # The new week's alert replaces last week's on the bell.
        open_ = [a.dedupe_key for a in db.scalars(select(Alert).where(
            Alert.kind == "owner_summary", Alert.resolved_at.is_(None)))]
        assert open_ == [f"owner_summary:{nlabel}"]
    assert len(outbox) == 4


def test_monthly_owner_report_with_workbook(make_client):
    owner, cid, (s1, s2), pid = _company(make_client, (("S1", "Andheri"), ("S2", "Bandra")))
    _classes(cid, rest="C")
    t = _today()
    first = t.replace(day=1)
    now = datetime.combine(first, time(7, 30), tzinfo=IST)            # the 1st, 07:30
    me = first - timedelta(days=1)
    ms = me.replace(day=1)                                             # last month: ms to me
    d = lambda n: ms + timedelta(days=n)
    with dbmod.SessionLocal() as db:
        db.get(Company, cid).created_at = _at(ms - timedelta(days=30))
        run = ForecastRun(company_id=cid, kind="nightly", status="succeeded")
        db.add(run)
        db.flush()
        # Andheri: sold 100, forecast 120, missed by 32: off by 32%, 20% high.
        # Bandra: sold 300, forecast 280, missed by 40: off by 13%, 7% low.
        # Both: sold 400, forecast 400, missed by 72: off by 18%, neither high nor low.
        for sid, a_, f_, e_ in ((s1, 100_000, 120_000, 32_000), (s2, 300_000, 280_000, 40_000)):
            db.add(ForecastScore(company_id=cid, run_id=run.id, day=d(3), store_id=sid,
                                 category="Staples", n=1, actual_milli=a_, forecast_milli=f_,
                                 abs_error_milli=e_, inside_range=1))
        db.commit()
    for sid in (s1, s2):
        for sku in ("A", "B", "C"):
            _post(cid, sid, pid[sku], 10_000, at=_at(ms - timedelta(days=10)))
    _post(cid, s1, pid["A"], -2_000, "count_adjustment", at=_at(d(5)))   # Rs 160 missing
    _post(cid, s2, pid["A"], 1_000, "count_adjustment", at=_at(d(6)))    # Rs 80 found
    _post(cid, s1, pid["C"], -3_000, "wastage", reason="expired", at=_at(d(7)))   # Rs 60
    _post(cid, s2, pid["B"], -1_000, "damage", reason="damaged", at=_at(d(8)))     # Rs 30
    # Counts: 4 class-C products in 2 stores, none counted before: 8 due; Atta counted in
    # each store: 2 done.
    _count(cid, s1, pid["A"], d(5))
    _count(cid, s2, pid["A"], d(6))
    outbox.clear()
    with dbmod.SessionLocal() as db:
        assert summaries.run(db, cid, now, kinds=("owner_monthly",)) == \
            [f"owner_monthly:{ms:%Y-%m}"]
        assert summaries.run(db, cid, now + timedelta(days=5), kinds=("owner_monthly",)) == []
    assert [m.to for m in outbox] == ["asha@acme.example"]
    mail = outbox[0]
    assert mail.subject == f"DQ-Stack monthly report: Acme Retail, {ms:%B %Y}"
    for words in ("Andheri: off by 32% of what sold, 20% high on balance.",
                  "Bandra: off by 13% of what sold, 7% low on balance.",
                  "All stores: off by 18% of what sold, neither high nor low on balance.",
                  "C: 2 of 8 due counts done (25%).",
                  "Shrinkage: ₹160 of stock missing at counts, ₹80 found: net ₹80 lost.",
                  "Wastage: ₹90 written off at cost, of which ₹60 expired."):
        assert words in mail.body, words
    # The workbook: a summary sheet, then every report for the month.
    from openpyxl import load_workbook
    (name, data, mime), = mail.attachments
    assert name == f"dqstack-report-{ms:%Y-%m}.xlsx" and mime.endswith("spreadsheetml.sheet")
    wb = load_workbook(io.BytesIO(data))
    assert wb.sheetnames[:2] == ["Contents", "Owner summary"] and len(wb.sheetnames) == 16
    assert {"Stock turn and days of cover", "Cycle count compliance",
            "Order performance by supplier"} <= set(wb.sheetnames)
    lines = [r[0] for r in wb["Owner summary"].iter_rows(min_row=4, values_only=True)]
    assert "C: 2 of 8 due counts done (25%)." in lines


# ===================================================================== SFTP file drop

PUB = ("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGq0a6S3v7Tqk0l1n1y8xFv2bX3b7m9cWzv0d3Yh0Q1z "
       "pos@acme")
SALES = ("type,store_code,bill_number,occurred_at,sku,qty,price,payment_method\n"
         "sale,PUN1,N1,{day}T10:00:00+05:30,RICE,2,450,cash\n"
         "sale,PUN1,N1,{day}T10:00:00+05:30,DAL,1,180,cash\n"
         "sale,PUN1,N2,{day}T11:00:00+05:30,RICE,1,450,card\n")


def _pos_company(make_client, company="Acme Retail", email=None):
    c = make_client()
    c.signup(company=company, email=email)
    s = c.post("/api/stores", {"code": "PUN1", "name": "Pune", "mode": "connected"}).json()
    c.import_file("products", "SKU,Name,Unit,MRP,Selling price,Cost\n"
                              "RICE,Rice 5 kg,each,500,450,380\nDAL,Toor dal 1 kg,each,200,180,150\n")
    cid = c.me["company"]["id"]
    with dbmod.SessionLocal() as db:
        pid = {p.sku: p.id for p in db.scalars(select(Product).where(Product.company_id == cid))}
    _post(cid, s["id"], pid["RICE"], 100_000, at=_at(_today() - timedelta(days=30)))
    key = c.post("/api/integrations/api-keys", {"name": "Store POS"}).json()
    return c, cid, s["id"], pid, key


def _rice(sid, pid):
    with dbmod.SessionLocal() as db:
        return ledger.on_hand(db, sid, pid["RICE"])


def _drop(root, folder, name, text):
    (root / folder / "upload" / name).write_text(text, encoding="utf-8")


@pytest.fixture
def drop(tmp_path, monkeypatch):
    monkeypatch.setenv("DQ_SFTP_ROOT", str(tmp_path))
    monkeypatch.setenv("DQ_SFTP_SETTLE_SECONDS", "0")
    return tmp_path


def test_sftp_folder_end_to_end_through_the_api_code(make_client, drop):
    owner, cid, sid, pid, key = _pos_company(make_client)
    # The operator's command: refuses what is unsafe, then writes the mapping, the SFTP
    # account (key login only) and the account's public key.
    add = lambda *a: manage.main(["sftp-add-folder", *a])
    assert add("--folder", "Acme_POS", "--api-key-id", key["id"], "--public-key", PUB) == 1
    assert add("--folder", "root", "--api-key-id", key["id"], "--public-key", PUB) == 1
    assert add("--folder", "acme-pos", "--api-key-id", key["id"], "--public-key", "hunter2") == 1
    assert add("--folder", "acme-pos", "--api-key-id", "nope", "--public-key", PUB) == 1
    assert add("--folder", "acme-pos", "--api-key-id", key["id"], "--public-key", PUB) == 0
    assert add("--folder", "acme-pos", "--api-key-id", key["id"], "--public-key", PUB) == 1
    folders = json.loads((drop / ".dqstack" / "folders.json").read_text())["folders"]
    assert folders["acme-pos"]["api_key_id"] == key["id"]
    assert folders["acme-pos"]["company_id"] == cid
    assert "acme-pos::10002:10001:upload" in (drop / ".dqstack" / "users.conf").read_text()
    assert (drop / "acme-pos" / ".ssh" / "keys" / "dqstack.pub").read_text() == PUB + "\n"

    day = (_today() - timedelta(days=1)).isoformat()
    _drop(drop, "acme-pos", "sales.csv", SALES.format(day=day))
    _drop(drop, "acme-pos", "next.csv.part", SALES.format(day=day))   # still uploading
    res = filedrop.pickup()
    assert [(r["file"], r["outcome"], r["bills"], r["accepted"], r["refused"]) for r in res] == \
        [("sales.csv", "processed", 2, 2, 0)]
    assert _rice(sid, pid) == 97_000
    home, upload = drop / "acme-pos", drop / "acme-pos" / "upload"
    done = sorted(p.name for p in (home / "processed").iterdir())
    assert len(done) == 2 and done[0].endswith("-sales.csv") and done[1].endswith(".report.txt")
    assert "2 in the file: 2 recorded now" in (home / "processed" / done[1]).read_text()
    assert (upload / "next.csv.part").exists() and not (upload / "sales.csv").exists()
    # The same audit as the API: each bill under the key, and the file itself.
    with dbmod.SessionLocal() as db:
        acts = list(db.scalars(select(ActivityLog).where(ActivityLog.company_id == cid)))
    bills = [a for a in acts if a.action == "bill.paid"]
    assert len(bills) == 2 and {a.device for a in bills} == {f"sftp:{key['prefix']}"}
    got = [a.after for a in acts if a.action == "pos.file_received"]
    assert got[0]["folder"] == "acme-pos" and got[0]["accepted"] == 2
    # Sent again, by SFTP or by the API: recognised, stock moves once.
    _drop(drop, "acme-pos", "sales.csv", SALES.format(day=day))
    again = filedrop.pickup()
    assert [(r["outcome"], r["accepted"], r["duplicates"]) for r in again] == [("processed", 0, 2)]
    api = owner.http.post("/api/pos/v1/files", headers={"Authorization": f"Bearer {key['key']}"},
                          files={"file": ("sales.csv", SALES.format(day=day).encode(),
                                          "text/csv")}).json()
    assert (api["accepted"], api["duplicates"]) == (0, 2)
    assert _rice(sid, pid) == 97_000
    # A file still being written is left until it has been quiet for the settle time.
    os.environ["DQ_SFTP_SETTLE_SECONDS"] = "60"
    _drop(drop, "acme-pos", "fresh.csv", SALES.format(day=day))
    assert filedrop.pickup() == [] and (upload / "fresh.csv").exists()
    # The scheduler picks files up too.
    os.environ["DQ_SFTP_SETTLE_SECONDS"] = "0"
    worker.tick()
    assert not (upload / "fresh.csv").exists()
    # The operator can take the folder away again; its files stay.
    assert manage.main(["sftp-remove-folder", "--folder", "acme-pos"]) == 0
    assert filedrop.load_folders(drop) == {} and (home / "processed").exists()
    assert "acme-pos" not in (drop / ".dqstack" / "users.conf").read_text()


def test_sftp_failures_are_set_aside_reported_and_alerted(make_client, drop):
    owner, cid, sid, pid, key = _pos_company(make_client)
    assert owner.put("/api/alerts/settings", {"email": True}).status_code == 200
    filedrop.add_folder(drop, "acme-pos", key["id"], PUB)
    home = drop / "acme-pos"
    day = (_today() - timedelta(days=1)).isoformat()
    # One good bill and one for a store this key may not send for.
    _drop(drop, "acme-pos", "mixed.csv",
          "type,store_code,bill_number,occurred_at,sku,qty,price,payment_method\n"
          f"sale,PUN1,F1,{day}T10:00:00+05:30,RICE,1,450,cash\n"
          f"sale,BLR9,F2,{day}T10:05:00+05:30,RICE,1,450,cash\n")
    # And a file whose columns are not the file drop's.
    _drop(drop, "acme-pos", "wrong.csv", "store,bill,sku\nPUN1,X1,RICE\n")
    res = {r["file"]: r for r in filedrop.pickup()}
    assert (res["mixed.csv"]["outcome"], res["mixed.csv"]["accepted"],
            res["mixed.csv"]["refused"]) == ("failed", 1, 1)
    assert res["wrong.csv"]["outcome"] == "failed" and res["wrong.csv"]["bills"] == 0
    assert _rice(sid, pid) == 99_000                     # only the good bill
    failed = home / "failed"
    report = next(p for p in failed.iterdir() if p.name.endswith("-mixed.csv.errors.txt"))
    text = report.read_text()
    assert "1 refused" in text and "sale BLR9 bill F2: That store code is not one this key" in text
    assert "not counted twice" in text
    wrong = next(p for p in failed.iterdir() if p.name.endswith("-wrong.csv.errors.txt"))
    assert "no store_code or bill_number column" in wrong.read_text()
    # Admins hear about it: an alert in the app, emailed to those who chose email.
    with dbmod.SessionLocal() as db:
        raised = list(db.scalars(select(Alert).where(Alert.kind == "file_drop_failed")))
        assert len(raised) == 2 and all(a.company_id == cid for a in raised)
        outbox.clear()
        A.deliver(db, cid)
        db.commit()
    assert {m.to for m in outbox} == {"asha@acme.example"}
    assert all("Nightly file not recorded" in m.subject for m in outbox)
    # The corrected file goes in whole: the good bill is not counted twice.
    _drop(drop, "acme-pos", "mixed.csv",
          "type,store_code,bill_number,occurred_at,sku,qty,price,payment_method\n"
          f"sale,PUN1,F1,{day}T10:00:00+05:30,RICE,1,450,cash\n"
          f"sale,PUN1,F2,{day}T10:05:00+05:30,RICE,1,450,cash\n")
    fixed = filedrop.pickup()
    assert [(r["outcome"], r["accepted"], r["duplicates"]) for r in fixed] == [("processed", 1, 1)]
    assert _rice(sid, pid) == 98_000
    # A file left claimed by a pass that stopped half way is recorded on the next pass.
    (home / "processing" / "20261111-010203-left.csv").write_text(
        "type,store_code,bill_number,occurred_at,sku,qty,price,payment_method\n"
        f"sale,PUN1,L1,{day}T12:00:00+05:30,RICE,1,450,cash\n", encoding="utf-8")
    left = filedrop.pickup()
    assert [(r["file"], r["outcome"], r["accepted"]) for r in left] == [("left.csv", "processed", 1)]
    assert _rice(sid, pid) == 97_000 and not any((home / "processing").iterdir())
    # A revoked key stops the folder: files are set aside, nothing is recorded.
    owner.post(f"/api/integrations/api-keys/{key['id']}/revoke")
    _drop(drop, "acme-pos", "late.csv", SALES.format(day=day))
    late = filedrop.pickup()
    assert late[0]["outcome"] == "failed" and "revoked" in late[0]["error"]
    assert _rice(sid, pid) == 97_000
    assert any(p.name.endswith("-late.csv.errors.txt") for p in (home / "failed").iterdir())


def test_sftp_folders_never_cross_companies(make_client, drop):
    a, a_cid, a_sid, a_pid, a_key = _pos_company(make_client)
    b, b_cid, b_sid, b_pid, b_key = _pos_company(make_client, "Other Stores", "o@other.example")
    filedrop.add_folder(drop, "acme-pos", a_key["id"], PUB)
    # The beginning the Integrations screen shows ("dqk_ab12cd34…") names a key too.
    filedrop.add_folder(drop, "other-pos", b_key["prefix"] + "…", PUB)
    assert filedrop.load_folders(drop)["other-pos"]["api_key_id"] == b_key["id"]
    day = (_today() - timedelta(days=1)).isoformat()
    # Both companies have a store PUN1: a file in Acme's folder only ever reaches Acme's.
    _drop(drop, "acme-pos", "sales.csv", SALES.format(day=day))
    assert filedrop.pickup()[0]["accepted"] == 2
    assert (_rice(a_sid, a_pid), _rice(b_sid, b_pid)) == (97_000, 100_000)
    # A folder on disk that the operator never added is not read.
    (drop / "rogue" / "upload").mkdir(parents=True)
    _drop(drop, "rogue", "sales.csv", SALES.format(day=day))
    assert filedrop.pickup() == [] and (drop / "rogue" / "upload" / "sales.csv").exists()
    # A mapping edited to pair Acme's key with the other company is refused outright.
    cfg = drop / ".dqstack" / "folders.json"
    data = json.loads(cfg.read_text())
    data["folders"]["other-pos"]["api_key_id"] = a_key["id"]
    cfg.write_text(json.dumps(data))
    _drop(drop, "other-pos", "sales.csv", SALES.format(day=day))
    res = filedrop.pickup()
    assert res[0]["outcome"] == "failed" and res[0]["bills"] == 0
    assert (_rice(a_sid, a_pid), _rice(b_sid, b_pid)) == (97_000, 100_000)
    # A link planted in Acme's folder, pointing at another folder's file, is never followed.
    target = drop / "other-pos" / "upload" / "theirs.csv"
    target.write_text(SALES.format(day=day))
    try:
        os.symlink(target, drop / "acme-pos" / "upload" / "link.csv")
        linked = True
    except (OSError, NotImplementedError):     # Windows without the privilege to make one
        linked = False
    data["folders"]["other-pos"]["api_key_id"] = b_key["id"]
    cfg.write_text(json.dumps(data))
    res = filedrop.pickup()
    assert [r["folder"] for r in res] == ["other-pos"]       # its own file, by its own key
    assert (_rice(a_sid, a_pid), _rice(b_sid, b_pid)) == (97_000, 97_000)
    if linked:
        assert (drop / "acme-pos" / "upload" / "link.csv").is_symlink()
    # The worker's own folders sit beside upload/, where an account cannot write. Should
    # one ever be something other than a real folder, the worker stops using that SFTP
    # folder rather than move files through it.
    processed = drop / "acme-pos" / "processed"
    for p in processed.iterdir():
        p.unlink()
    processed.rmdir()
    processed.write_text("not a folder")
    _drop(drop, "acme-pos", "more.csv", SALES.format(day=day))
    assert filedrop.pickup() == [] and (drop / "acme-pos" / "upload" / "more.csv").exists()
    assert _rice(a_sid, a_pid) == 97_000
