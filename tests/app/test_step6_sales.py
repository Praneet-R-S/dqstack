"""Build step 6 -- Sales in (standalone).

Done when: uploading a day's file updates stock and the daily sales table; a late
file replaces that day. Also the parts of acceptance scenario 9 this step owns (the
same file twice changes nothing; a late file for the day before is flagged for
tonight's re-run), bill-level exports, history that never touches stock, the Sales
entry grid, out-of-stock flags and Day close.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select

from dqstack.app import db as dbmod
from dqstack.app import ledger
from dqstack.app import sales as S
from dqstack.app.models import CalendarEvent, DailySales, DayClose, Product, StockMovement

from .conftest import invite_and_join


def _today() -> date:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Asia/Kolkata")).date()


def _world(client, mode="standalone"):
    client.signup()
    s1 = client.post("/api/stores", {"code": "S1", "name": "Andheri", "mode": mode,
                                     "day_close_time": "22:00"}).json()
    client.import_file("products", "SKU,Name,Unit,MRP,Selling price,Cost\n"
                                   "RICE,Rice 5 kg,each,500,450,400\n"
                                   "DAL,Toor dal 1 kg,each,200,180,150\n"
                                   "MILK,Milk 1 L,each,70,66,55\n")
    with dbmod.SessionLocal() as db:
        prods = {p.sku: p.id for p in db.scalars(select(Product))}
    return s1, prods


def _open(client, store_id, pid, qty_milli, days_ago=3):
    """Opening stock dated a few days back. A figure set "now" would, after the
    store's 22:00 close, rightly absorb today's sales -- and make tests depend on the
    time of day they run."""
    from dqstack.app.models import Store
    cid = client.me["company"]["id"]
    with dbmod.SessionLocal() as db:
        at = S.close_instant(db.get(Store, store_id), S.tz_of(db, cid),
                             _today() - timedelta(days=days_ago))
        ledger.post(db, company_id=cid, store_id=store_id, product_id=pid, qty_milli=qty_milli,
                    type="opening", at=at)
        db.commit()


def _hand(store_id, pid) -> int:
    with dbmod.SessionLocal() as db:
        return ledger.on_hand(db, store_id, pid)


def _rows(store_id, day) -> dict:
    with dbmod.SessionLocal() as db:
        return {r.product_id: (r.units_milli, r.revenue_paise) for r in db.scalars(
            select(DailySales).where(DailySales.store_id == store_id, DailySales.day == day,
                                     DailySales.source != "ledger"))}


def _moves() -> int:
    with dbmod.SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(StockMovement))


def test_done_when_a_days_file_updates_stock_and_a_late_file_replaces_the_day(client):
    s1, prods = _world(client)
    day = _today() - timedelta(days=1)
    d = day.strftime("%d-%m-%Y")
    # Stock was set before that day's trading.
    for sku in prods:
        _open(client, s1["id"], prods[sku], 100_000, days_ago=6)
    first = (f"Date,Store code,SKU,Units,Revenue\n{d},S1,RICE,6,2700\n{d},S1,DAL,10,1800\n"
             f"{d},S1,MILK,24,1584\n")
    job = client.import_file("daily_sales", first, "sales-day1.csv")
    assert (job["rows_new"], job["rows_failed"]) == (3, 0)
    assert _hand(s1["id"], prods["RICE"]) == 94_000
    assert _hand(s1["id"], prods["MILK"]) == 76_000
    assert _rows(s1["id"], day)[prods["DAL"]] == (10_000, 180_000)

    # The same file again changes nothing.
    moves = _moves()
    again = client.import_file("daily_sales", first, "sales-day1.csv")
    assert (again["rows_new"], again["rows_updated"], again["rows_unchanged"]) == (0, 0, 3)
    assert _moves() == moves and _hand(s1["id"], prods["RICE"]) == 94_000

    # The day is closed; then a corrected file arrives: rice 8 (2 more), milk 20 (4
    # fewer), and dal missing (the earlier file was wrong: no dal sold).
    r = client.post("/api/day-close", {"store_id": s1["id"], "day": day.isoformat()})
    assert r.status_code == 201, r.text
    late = f"Date,Store code,SKU,Units,Revenue\n{d},S1,RICE,8,3600\n{d},S1,MILK,20,1320\n"
    job = client.import_file("daily_sales", late, "sales-day1-corrected.csv")
    assert (job["rows_updated"], job["rows_new"]) == (3, 0)        # rice, milk, and dal to zero
    assert _hand(s1["id"], prods["RICE"]) == 92_000
    assert _hand(s1["id"], prods["MILK"]) == 80_000
    assert _hand(s1["id"], prods["DAL"]) == 100_000
    assert _rows(s1["id"], day) == {prods["RICE"]: (8000, 360_000), prods["MILK"]: (20_000, 132_000),
                                    prods["DAL"]: (0, 0)}
    days = client.get(f"/api/sales/days?store_id={s1['id']}&days=3").json()
    assert next(x for x in days if x["day"] == day.isoformat())["late_data"] is True
    with dbmod.SessionLocal() as db:
        assert ledger.verify(db, client.me["company"]["id"]) == []


def test_bill_level_exports_add_up_by_day(client):
    s1, prods = _world(client)
    d = (_today() - timedelta(days=1)).strftime("%d-%m-%Y")
    bills = (f"Bill date,Branch,Item code,Qty,Net amount\n"
             f"{d},S1,RICE,2,900\n{d},S1,RICE,1,450\n{d},S1,RICE,-1,-450\n{d},S1,DAL,3,540\n")
    job = client.import_file("daily_sales", bills)
    assert job["rows_read"] == 4 and job["rows_new"] == 2 and job["rows_accepted"] == 4
    day = _today() - timedelta(days=1)
    assert _rows(s1["id"], day)[prods["RICE"]] == (2000, 90_000)


def test_history_fills_the_table_but_never_moves_stock(client):
    s1, prods = _world(client)
    start = _today() - timedelta(days=70)
    lines = ["Date,Store code,SKU,Units"]
    for i in range(63):
        lines.append(f"{(start + timedelta(days=i)).isoformat()},S1,RICE,{(i % 5) + 1}")
    job = client.import_file("sales_history", "\n".join(lines) + "\n")
    assert job["rows_new"] == 63
    assert _moves() == 0
    steps = {s["key"]: s for s in client.get("/api/setup").json()["steps"]}
    assert steps["sales_history"]["state"] == "done" and "9 weeks" in steps["sales_history"]["detail"]
    readiness = client.get("/api/setup/readiness").json()
    assert readiness["stores"][0]["weeks_of_sales"] == 9.0


def test_connected_stores_cannot_upload_daily_sales(client):
    s1, prods = _world(client, mode="connected")
    d = (_today() - timedelta(days=1)).isoformat()
    job = client.upload("daily_sales", f"Date,Store code,SKU,Units\n{d},S1,RICE,1\n").json()
    assert "connected store" in job["errors"][0]["problems"][0]
    hist = client.upload("sales_history", f"Date,Store code,SKU,Units\n{d},S1,RICE,1\n").json()
    assert hist["rows_new"] == 1                       # history is fine for any store


def test_future_dates_and_unknown_products_are_refused(client):
    s1, prods = _world(client)
    tomorrow = (_today() + timedelta(days=1)).isoformat()
    job = client.upload("daily_sales", f"Date,Store code,SKU,Units\n{tomorrow},S1,RICE,1\n"
                                       f"{_today().isoformat()},S1,NOPE,1\n").json()
    probs = [e["problems"][0] for e in job["errors"]]
    assert any("in the future" in p for p in probs) and any("NOPE is not" in p for p in probs)


def test_sales_before_the_last_count_do_not_move_stock(client):
    s1, prods = _world(client)
    cid = client.me["company"]["id"]
    two_days_ago = _today() - timedelta(days=2)
    # The rice was set at 30 today, after the sales below happened.
    client.post("/api/stock/opening", {"store_id": s1["id"], "product_id": prods["RICE"],
                                       "qty_milli": 30_000})
    job = client.import_file("daily_sales", f"Date,Store code,SKU,Units\n"
                                            f"{two_days_ago.isoformat()},S1,RICE,4\n")
    assert job["rows_new"] == 1
    assert _hand(s1["id"], prods["RICE"]) == 30_000           # the count already saw it
    assert _rows(s1["id"], two_days_ago)[prods["RICE"]][0] == 4000
    again = client.import_file("daily_sales", f"Date,Store code,SKU,Units\n"
                                              f"{two_days_ago.isoformat()},S1,RICE,4\n")
    assert again["rows_unchanged"] == 1


def test_out_of_stock_days_are_flagged_from_the_ledger(client):
    s1, prods = _world(client)
    cid = client.me["company"]["id"]
    day = _today() - timedelta(days=1)
    _open(client, s1["id"], prods["DAL"], 5_000)
    _open(client, s1["id"], prods["MILK"], 2_000)
    with dbmod.SessionLocal() as db:
        ledger.post(db, company_id=cid, store_id=s1["id"], product_id=prods["MILK"],
                    qty_milli=-2_000, type="sale", at=S.close_instant(
                        db.get(__import__("dqstack.app.models", fromlist=["Store"]).Store,
                               s1["id"]), S.tz_of(db, cid), day) - timedelta(hours=3))
        db.commit()
    # Rice was never set, so its zero says nothing; milk sold out yesterday; dal did not.
    with dbmod.SessionLocal() as db:
        from dqstack.app.models import Store
        n = S.mark_out_of_stock(db, cid, db.get(Store, s1["id"]), day)
        db.commit()
    assert n == 1
    with dbmod.SessionLocal() as db:
        oos = {r.product_id: (r.out_of_stock, r.oos_source) for r in db.scalars(
            select(DailySales).where(DailySales.day == day))}
    assert oos[prods["MILK"]] == (True, "ledger") and prods["DAL"] not in oos
    assert prods["RICE"] not in oos


def test_sales_entry_grid(make_client):
    owner = make_client()
    s1, prods = _world(owner)
    _open(owner, s1["id"], prods["RICE"], 50_000)
    mgr, clerk = make_client(), make_client()
    invite_and_join(owner, mgr, "Mohan Manager", "store_manager", [s1["id"]])
    invite_and_join(owner, clerk, "Kavya Clerk", "stock_clerk", [s1["id"]])
    day = _today().isoformat()
    body = {"store_id": s1["id"], "day": day,
            "lines": [{"product_id": prods["RICE"], "units_milli": 3000}]}
    assert clerk.put("/api/sales/entry", body).status_code == 403
    r = mgr.put("/api/sales/entry", body).json()
    assert r["new"] == 1 and _hand(s1["id"], prods["RICE"]) == 47_000
    # Typing a different total later replaces just that product's figure.
    body["lines"][0]["units_milli"] = 5000
    mgr.put("/api/sales/entry", body)
    assert _hand(s1["id"], prods["RICE"]) == 45_000
    grid = mgr.get(f"/api/sales?store_id={s1['id']}").json()
    rice = next(x for x in grid["rows"] if x["sku"] == "RICE")
    assert rice["days"][day]["units_milli"] == 5000 and rice["days"][day]["revenue_paise"] == 225_000
    bad = {**body, "lines": [{"product_id": prods["RICE"], "units_milli": 1500}]}
    assert mgr.put("/api/sales/entry", bad).status_code == 422


def test_day_close_rules(make_client):
    owner = make_client()
    s1, prods = _world(owner)
    day = _today().isoformat()
    status = owner.get(f"/api/day-close?store_id={s1['id']}&day={day}").json()
    assert status["closed"] is False and status["checks"][0]["ok"] is False
    r = owner.post("/api/day-close", {"store_id": s1["id"], "day": day})
    assert r.status_code == 409 and "sales" in r.json()["error"]
    r = owner.post("/api/day-close", {"store_id": s1["id"], "day": day, "store_was_closed": True})
    assert r.status_code == 201 and r.json()["closed"] is True
    assert owner.post("/api/day-close", {"store_id": s1["id"], "day": day,
                                         "no_sales": True}).status_code == 409
    with dbmod.SessionLocal() as db:
        ev = db.scalar(select(CalendarEvent).where(CalendarEvent.kind == "closure"))
        assert ev is not None and ev.store_ids == [s1["id"]]
    clerk = make_client()
    invite_and_join(owner, clerk, "Kavya Clerk", "stock_clerk", [s1["id"]])
    yesterday = (_today() - timedelta(days=1)).isoformat()
    assert clerk.post("/api/day-close", {"store_id": s1["id"], "day": yesterday,
                                         "no_sales": True}).status_code == 403
    hist = owner.get(f"/api/day-close/history?store_id={s1['id']}").json()
    assert hist[0]["store_was_closed"] is True and hist[0]["closed_by"] == "Asha Owner"


def test_automatic_close_at_the_set_time(client):
    s1, prods = _world(client)
    from zoneinfo import ZoneInfo
    ist = ZoneInfo("Asia/Kolkata")
    tomorrow = _today() + timedelta(days=1)
    before = datetime.combine(tomorrow, datetime.min.time(), tzinfo=ist).replace(hour=0, minute=30)
    with dbmod.SessionLocal() as db:
        made = S.auto_close(db, now=before.astimezone(timezone.utc))
        db.commit()
        assert [m.day for m in made] == [_today()] and made[0].automatic
        # Run again: nothing new.
        assert S.auto_close(db, now=before.astimezone(timezone.utc)) == []
    days = client.get(f"/api/sales/days?store_id={s1['id']}&days=1").json()
    assert days[0]["closed_automatically"] is True


def test_undoing_a_daily_sales_import_gives_the_stock_back(client):
    s1, prods = _world(client)
    _open(client, s1["id"], prods["DAL"], 40_000)
    day = _today().isoformat()
    job = client.import_file("daily_sales", f"Date,Store code,SKU,Units\n{day},S1,DAL,7\n")
    assert _hand(s1["id"], prods["DAL"]) == 33_000
    rep = client.post(f"/api/imports/{job['id']}/undo").json()["undo_report"]
    assert rep == {"reverted": 1, "kept": 0, "reasons": []}
    assert _hand(s1["id"], prods["DAL"]) == 40_000
    assert prods["DAL"] not in _rows(s1["id"], _today())


def test_another_companys_sales_are_not_found(make_client):
    a, b = make_client(), make_client()
    s1, prods = _world(a)
    b.signup(company="Beta", email="b@beta.example")
    assert b.get(f"/api/sales?store_id={s1['id']}").status_code == 404
    assert b.get(f"/api/day-close?store_id={s1['id']}").status_code == 404
    assert b.put("/api/sales/entry", {"store_id": s1["id"], "day": _today().isoformat(),
                                      "lines": [{"product_id": prods["RICE"],
                                                 "units_milli": 1000}]}).status_code == 404
