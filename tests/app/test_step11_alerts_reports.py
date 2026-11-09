"""Build step 11 -- Alerts and reports.

Done when: each alert fires once in a scripted day; each report matches a
hand-checked figure.
"""

from __future__ import annotations

import io
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from dqstack.app import alerts as A
from dqstack.app import db as dbmod
from dqstack.app import ledger
from dqstack.app.mailer import outbox
from dqstack.app.models import (Alert, CountLine, CountSession, DailySales, ForecastRun, Product,
                                Recommendation)

from .conftest import invite_and_join


def _today():
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Asia/Kolkata")).date()


def _world(make_client):
    owner = make_client()
    owner.signup()
    s1 = owner.post("/api/stores", {"code": "S1", "name": "Andheri"}).json()
    owner.import_file("products", "SKU,Name,Category,Unit,MRP,Selling price,Cost,Perishable\n"
                                  "A,Atta,Staples,each,100,100,80,no\n"
                                  "B,Bread,Bakery,each,40,40,30,no\n"
                                  "C,Curd,Dairy,each,30,30,20,yes\n"
                                  "D,Detergent,Home,each,200,200,150,no\n"
                                  "E,Eggs,Dairy,each,80,80,60,no\n"
                                  "F,Flour,Staples,each,60,60,50,no\n")
    with dbmod.SessionLocal() as db:
        pid = {p.sku: p.id for p in db.scalars(select(Product))}
    cid = owner.me["company"]["id"]
    return owner, cid, s1, pid


def _post(cid, sid, pid, q, typ="opening", **kw):
    with dbmod.SessionLocal() as db:
        r = ledger.post(db, company_id=cid, store_id=sid, product_id=pid, qty_milli=q, type=typ,
                        **kw)
        db.commit()
        return r


def test_done_when_each_alert_fires_once_in_a_scripted_day(make_client):
    owner, cid, s1, pid = _world(make_client)
    sid = s1["id"]
    t = _today()
    past = datetime.now(timezone.utc) - timedelta(days=10)
    _post(cid, sid, pid["A"], 0, "count_adjustment", makes_known=True, at=past)      # known, 0: out
    _post(cid, sid, pid["B"], 2000, at=past)
    _post(cid, sid, pid["B"], -5000, "sale")                                        # negative
    _post(cid, sid, pid["C"], 0, "count_adjustment", makes_known=True,
          at=past - timedelta(days=1))                                       # counted at 0
    _post(cid, sid, pid["C"], 6000, "delivery", expiry=t + timedelta(days=2), at=past)  # expiring
    _post(cid, sid, pid["D"], 500_000, at=past)                                     # overstocked
    _post(cid, sid, pid["E"], 4000, at=past)                                        # below target
    _post(cid, sid, pid["F"], 300_000, at=past)
    _post(cid, sid, pid["F"], -150_000, "count_adjustment")                         # Rs 7,500 lost
    with dbmod.SessionLocal() as db:
        for d in range(1, 15):
            db.add(DailySales(company_id=cid, store_id=sid, product_id=pid["D"],
                              day=t - timedelta(days=d + 1), units_milli=1000, source="upload"))
        run = ForecastRun(company_id=cid, kind="nightly", status="succeeded",
                          data_cutoff=t - timedelta(days=1))
        db.add(run)
        db.flush()
        db.add(Recommendation(company_id=cid, run_id=run.id, store_id=sid, product_id=pid["E"],
                              target_milli=20_000, flagged=True, money_at_risk_paise=1))
        s = CountSession(company_id=cid, store_id=sid, kind="cycle")
        db.add(s)
        db.flush()
        db.add(CountLine(company_id=cid, session_id=s.id, product_id=pid["A"], counted_milli=0,
                         system_milli=40_000, difference_milli=-40_000, status="waiting"))
        db.commit()
    draft = owner.post("/api/orders", {"store_id": sid, "lines": [
        {"product_id": pid["E"], "qty_milli": 12_000}]}).json()
    late = owner.post("/api/orders", {"store_id": sid, "expected_date":
                                      (t - timedelta(days=1)).isoformat(), "lines": [
        {"product_id": pid["A"], "qty_milli": 12_000}]}).json()
    owner.post(f"/api/orders/{late['id']}/approve")

    # The scheduler looks every few minutes; over a scripted day, eight looks.
    with dbmod.SessionLocal() as db:
        for _ in range(8):
            A.evaluate(db, cid)
            db.commit()
        kinds = Counter(a.kind for a in db.scalars(select(Alert)))
    expected = {"out_of_stock", "negative_stock", "expiring", "overstocked", "low_stock",
                "count_waiting", "shrinkage", "today_ready", "orders_waiting",
                "delivery_overdue", "sales_missing"}
    assert set(kinds) >= expected, set(kinds) ^ expected
    assert all(kinds[k] == 1 for k in expected), kinds

    # A condition that clears resolves itself; the bell counts what is open.
    _post(cid, sid, pid["A"], 10_000, "delivery")
    with dbmod.SessionLocal() as db:
        A.evaluate(db, cid)
        db.commit()
        out = db.scalar(select(Alert).where(Alert.kind == "out_of_stock"))
        assert out.resolved_at is not None
    listed = owner.get("/api/alerts").json()
    assert "out_of_stock" not in {a["kind"] for a in listed}
    assert listed[0]["severity"] == "risk"                       # most urgent first
    assert owner.get("/api/alerts/count").json()["open"] == len(listed)


def test_alerts_reach_the_right_people(make_client):
    owner, cid, s1, pid = _world(make_client)
    mgr, clerk, buyer = make_client(), make_client(), make_client()
    invite_and_join(owner, mgr, "Mona Manager", "store_manager", [s1["id"]],
                    email="mona@test.example")
    invite_and_join(owner, clerk, "Kiran Clerk", "stock_clerk", [s1["id"]])
    invite_and_join(owner, buyer, "Puja Purchaser", "purchaser", [s1["id"]])
    _post(cid, s1["id"], pid["C"], 0, "count_adjustment", makes_known=True,
          at=datetime.now(timezone.utc) - timedelta(days=4))
    _post(cid, s1["id"], pid["C"], 6000, "delivery", expiry=_today() + timedelta(days=1),
          at=datetime.now(timezone.utc) - timedelta(days=3))
    _post(cid, s1["id"], pid["B"], 0, "count_adjustment", makes_known=True)
    mgr.put("/api/alerts/settings", {"email": True})
    outbox.clear()
    with dbmod.SessionLocal() as db:
        A.evaluate(db, cid)
        A.deliver(db, cid)
        db.commit()
    assert {m.to for m in outbox} == {"mona@test.example"}       # only she chose email
    assert {a["kind"] for a in clerk.get("/api/alerts").json()} >= {"expiring"}
    assert "out_of_stock" not in {a["kind"] for a in clerk.get("/api/alerts").json()}
    assert "expiring" not in {a["kind"] for a in buyer.get("/api/alerts").json()}
    with dbmod.SessionLocal() as db:                             # sent once, not every pass
        A.evaluate(db, cid)
        assert A.deliver(db, cid) == 0


def test_reports_match_hand_checked_figures(make_client):
    owner, cid, s1, pid = _world(make_client)
    sid = s1["id"]
    t = _today()
    d1, d2 = t - timedelta(days=3), t - timedelta(days=2)
    with dbmod.SessionLocal() as db:
        for day, sku, units, rev in ((d1, "A", 3, 300), (d1, "B", 2, 80), (d2, "A", 1, 100),
                                     (d2, "E", 5, 400)):
            db.add(DailySales(company_id=cid, store_id=sid, product_id=pid[sku], day=day,
                              units_milli=units * 1000, revenue_paise=rev * 100, source="upload"))
        # Two selling days of B before an empty-shelf day: 2 units a day, Rs 10 margin.
        for k in (5, 6):
            db.add(DailySales(company_id=cid, store_id=sid, product_id=pid["B"],
                              day=t - timedelta(days=k), units_milli=2000, source="upload"))
        db.add(DailySales(company_id=cid, store_id=sid, product_id=pid["B"], day=t - timedelta(days=4),
                          units_milli=0, out_of_stock=True, oos_source="ledger", source="ledger"))
        db.commit()
    q = f"store_id={sid}&from={(t - timedelta(days=7)).isoformat()}&to={t.isoformat()}"
    rep = lambda name: owner.get(f"/api/reports/{name}?{q}").json()

    sales = rep("sales")
    assert sales["totals"] == {"units_milli": 15_000, "revenue_paise": 88_000}
    by_day = {r["day"]: r["revenue_paise"] for r in sales["rows"]}
    assert by_day[d1.isoformat()] == 38_000 and by_day[d2.isoformat()] == 50_000
    cats = {r["category"]: r for r in rep("sales_by_category")["rows"]}
    assert cats["Staples"]["revenue_paise"] == 40_000 and cats["Staples"]["share"] == round(400 / 880, 4)
    assert rep("best_worst")["rows"][0]["sku"] == "A"

    # Stock and its value: 10 Atta at Rs 80 and 4 Eggs at Rs 60 = Rs 1,040.
    _post(cid, sid, pid["A"], 10_000)
    _post(cid, sid, pid["E"], 4_000)
    assert rep("stock_value")["totals"]["value_paise"] == 104_000

    # Shrinkage: 2 Atta missing at Rs 80 = Rs 160 lost. Wastage: 3 Curd expired at Rs 20.
    _post(cid, sid, pid["A"], -2_000, "count_adjustment")
    _post(cid, sid, pid["C"], 5_000)
    owner.post("/api/wastage", {"store_id": sid, "product_id": pid["C"], "qty_milli": 3_000,
                                "reason": "expired"})
    assert rep("shrinkage")["totals"]["lost_paise"] == 16_000
    w = rep("wastage")
    assert w["totals"] == {"value_paise": 6_000, "expiry_paise": 6_000}

    # Supplier: 120 ordered; 100 good and 5 damaged arrive. Fill 100/120; damaged 5/105.
    sup = owner.post("/api/suppliers", {"name": "Gupta Traders"}).json()
    po = owner.post("/api/orders", {"store_id": sid, "supplier_id": sup["id"], "lines": [
        {"product_id": pid["F"], "qty_milli": 120_000, "unit_cost_paise": 5_000}]}).json()
    owner.post(f"/api/orders/{po['id']}/approve")
    owner.post("/api/deliveries", {"store_id": sid, "po_id": po["id"], "lines": [
        {"product_id": pid["F"], "received_milli": 100_000, "damaged_milli": 5_000}]})
    perf = rep("supplier_performance")["rows"][0]
    assert perf["fill_rate"] == round(100 / 120, 4) and perf["damaged_share"] == round(5 / 105, 4)
    assert perf["unplanned_orders"] == 1

    # Lost sales: 1 empty day x 2 units a day x Rs 10 margin = Rs 20.
    ls = rep("lost_sales")
    assert ls["totals"]["margin_paise"] == 2_000 and ls["rows"][0]["units_milli"] == 2_000

    # Accounting: uploaded sales Rs 880 and purchases 100 x Rs 50 = Rs 5,000.
    acc = rep("accounting")
    assert acc["totals"] == {"sales_paise": 88_000, "purchases_paise": 500_000}

    # Every report exports to Excel and PDF.
    from openpyxl import load_workbook
    x = owner.get(f"/api/reports/sales?{q}&format=xlsx")
    ws = load_workbook(io.BytesIO(x.content)).active
    assert ws.cell(1, 4).value == "Revenue" and ws.cell(ws.max_row, 4).value == 880
    for name in (r["name"] for r in owner.get("/api/reports").json()):
        assert owner.get(f"/api/reports/{name}?{q}&format=pdf").content.startswith(b"%PDF"), name


def test_reports_respect_roles_and_companies(make_client):
    owner, cid, s1, pid = _world(make_client)
    clerk = make_client()
    invite_and_join(owner, clerk, "Kiran Clerk", "stock_clerk", [s1["id"]])
    assert clerk.get("/api/reports/sales").status_code == 403
    other = make_client()
    other.signup(company="Other", email="o@other.example")
    assert other.get(f"/api/reports/sales?store_id={s1['id']}").status_code == 404
    assert other.get("/api/reports/sales").json()["rows"] == []
