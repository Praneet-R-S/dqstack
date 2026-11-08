"""Build step 4 -- Inventory ledger.

Done when: stock on hand always equals the sum of movements in a randomised test
of 10,000 movements. Plus opening stock (file and single entry), product history
with a running balance, adjustments, valuation at average cost, the stock list's
statuses, and -- on PostgreSQL -- concurrent sales of the same product.
"""

from __future__ import annotations

import random
import threading
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from dqstack.app import db as dbmod
from dqstack.app import ledger
from dqstack.app.models import (DailySales, ForecastRun, Product, Recommendation, StockMovement,
                                StockOnHand, Store)

from .conftest import ON_PG, invite_and_join


def _setup(client, n_products=3, stores=("S1",)):
    client.signup()
    st = [client.post("/api/stores", {"code": s, "name": s}).json() for s in stores]
    rows = "\n".join(f"P{i},Product {i},each,100,90,60,{'8901234567890' if i == 0 else ''}"
                     for i in range(n_products))
    client.import_file("products", "SKU,Name,Unit,MRP,Selling price,Cost,Barcodes\n" + rows)
    with dbmod.SessionLocal() as db:
        prods = {p.sku: p.id for p in db.scalars(select(Product))}
    return st, prods


def test_done_when_on_hand_equals_sum_of_10000_random_movements(client):
    stores, prods = _setup(client, n_products=25, stores=("S1", "S2", "S3"))
    company_id = client.me["company"]["id"]
    rng = random.Random(20261108)
    types = [t for t in ledger.TYPES if t != "opening"]
    keys_used: list[str] = []
    expected: dict = {}
    posted = duplicates = 0
    with dbmod.SessionLocal() as db:
        for i in range(10_000):
            sid = rng.choice(stores)["id"]
            pid = rng.choice(list(prods.values()))
            # One movement in twenty repeats an earlier key: a resend or double press.
            if keys_used and rng.random() < 0.05:
                key = rng.choice(keys_used)
                res = ledger.post(db, company_id=company_id, store_id=sid, product_id=pid,
                                  qty_milli=1000, type="delivery", idempotency_key=key)
                assert res.duplicate
                duplicates += 1
                continue
            t = rng.choice(types)
            sign = ledger.TYPES[t] or rng.choice((-1, 1))
            q = sign * rng.randint(1, 50_000)           # up to 50 units, in thousandths
            key = f"k{i}" if rng.random() < 0.5 else None
            ledger.post(db, company_id=company_id, store_id=sid, product_id=pid, qty_milli=q,
                        type=t, idempotency_key=key,
                        unit_cost_paise=rng.randint(1000, 9000) if sign > 0 else None)
            if key:
                keys_used.append(key)
            expected[(sid, pid)] = expected.get((sid, pid), 0) + q
            posted += 1
            if i % 1000 == 999:
                db.commit()
        db.commit()
        assert posted + duplicates == 10_000 and duplicates > 300
        assert db.scalar(select(func.count()).select_from(StockMovement)) == posted
        assert ledger.verify(db, company_id) == []
        for row in db.scalars(select(StockOnHand)):
            assert row.on_hand_milli == expected.get((row.store_id, row.product_id), 0)
    # The same numbers through the API, and the history's running balance agrees.
    sid = stores[0]["id"]
    listing = {r["product_id"]: r["on_hand_milli"]
               for r in client.get(f"/api/stock?store_id={sid}").json()["rows"]}
    for pid in prods.values():
        assert listing[pid] == expected.get((sid, pid), 0)
    pid = next(iter(prods.values()))
    hist = client.get(f"/api/stock/history?store_id={sid}&product_id={pid}&limit=5000").json()
    assert hist["movements"][0]["balance_milli"] == hist["on_hand_milli"]
    assert sum(m["qty_milli"] for m in hist["movements"]) == hist["on_hand_milli"]
    assert client.get("/api/stock/verify").json()["consistent"] is True


def test_ledger_rules(client):
    stores, prods = _setup(client)
    cid, sid, pid = client.me["company"]["id"], stores[0]["id"], prods["P0"]
    with dbmod.SessionLocal() as db:
        for kw in ({"type": "sale", "qty_milli": 1000}, {"type": "delivery", "qty_milli": -1000},
                   {"type": "wastage", "qty_milli": 0}, {"type": "teleport", "qty_milli": 5},
                   {"type": "opening", "qty_milli": -1000}):
            with pytest.raises(ledger.LedgerError):
                ledger.post(db, company_id=cid, store_id=sid, product_id=pid, **kw)
        first = ledger.post(db, company_id=cid, store_id=sid, product_id=pid, qty_milli=3000,
                            type="delivery", idempotency_key="po-1-line-1")
        again = ledger.post(db, company_id=cid, store_id=sid, product_id=pid, qty_milli=3000,
                            type="delivery", idempotency_key="po-1-line-1")
        assert again.duplicate and again.movement.id == first.movement.id
        assert again.on_hand_milli == 3000
        db.commit()


def test_average_cost_and_valuation(client):
    stores, prods = _setup(client)
    cid, sid, pid = client.me["company"]["id"], stores[0]["id"], prods["P1"]
    with dbmod.SessionLocal() as db:
        ledger.post(db, company_id=cid, store_id=sid, product_id=pid, qty_milli=10_000,
                    type="opening", unit_cost_paise=5000)
        ledger.post(db, company_id=cid, store_id=sid, product_id=pid, qty_milli=10_000,
                    type="delivery", unit_cost_paise=7000)
        sale = ledger.post(db, company_id=cid, store_id=sid, product_id=pid, qty_milli=-5000,
                           type="sale")
        assert sale.movement.unit_cost_paise == 6000          # outbound valued at average
        db.commit()
    v = client.get("/api/stock/valuation").json()
    assert v["total_paise"] == 15 * 6000                    # 15 units at Rs 60


def test_opening_stock_file_set_once_and_undone_by_reversal(make_client):
    owner = make_client()
    stores, prods = _setup(owner, stores=("S1", "S2"))
    f = "Store code,SKU,Quantity,Unit cost,Expiry date\nS1,P0,24,55,30-06-2027\nS1,P1,0,,\nS2,P0,7.5,,\n"
    job = owner.upload("opening_stock", f).json()
    assert job["rows_new"] == 2 and "must be a whole number" in job["errors"][0]["problems"][0]
    job = owner.post(f"/api/imports/{job['id']}/commit").json()
    rows = {r["sku"]: r for r in owner.get(f"/api/stock?store_id={stores[0]['id']}").json()["rows"]}
    assert rows["P0"]["on_hand_milli"] == 24_000 and rows["P0"]["known"]
    assert rows["P1"]["on_hand_milli"] == 0 and rows["P1"]["status"] == "Out"
    assert rows["P2"]["status"] == "Count needed"
    # The same file again changes nothing; a different number is refused.
    again = owner.import_file("opening_stock", "Store code,SKU,Quantity\nS1,P0,24\n")
    assert again["rows_unchanged"] == 1
    clash = owner.upload("opening_stock", "Store code,SKU,Quantity\nS1,P0,30\n").json()
    assert "already 24" in clash["errors"][0]["problems"][0]
    # Undo posts reversing movements; nothing is deleted from the ledger.
    with dbmod.SessionLocal() as db:
        before = db.scalar(select(func.count()).select_from(StockMovement))
    rep = owner.post(f"/api/imports/{job['id']}/undo").json()["undo_report"]
    assert rep["kept"] == 0
    with dbmod.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(StockMovement)) == before + 1
    rows = {r["sku"]: r for r in owner.get(f"/api/stock?store_id={stores[0]['id']}").json()["rows"]}
    assert rows["P0"]["on_hand_milli"] == 0 and rows["P0"]["status"] == "Count needed"


def test_opening_stock_only_for_your_own_stores(make_client):
    owner = make_client()
    stores, prods = _setup(owner, stores=("S1", "S2"))
    mgr = make_client()
    invite_and_join(owner, mgr, "Mira Manager", "store_manager", [stores[0]["id"]])
    job = mgr.upload("opening_stock", "Store code,SKU,Quantity\nS1,P0,5\nS2,P0,5\n").json()
    assert job["rows_new"] == 1
    assert "S2 is not one of your stores" in job["errors"][0]["problems"][0]
    clerk = make_client()
    invite_and_join(owner, clerk, "Kamal Clerk", "stock_clerk", [stores[0]["id"]])
    assert clerk.upload("opening_stock", "Store code,SKU,Quantity\nS1,P0,5\n").status_code == 403


def test_single_opening_entry_and_adjustments(make_client):
    owner = make_client()
    stores, prods = _setup(owner)
    sid = stores[0]["id"]
    body = {"store_id": sid, "product_id": prods["P0"], "qty_milli": 12_000}
    assert owner.post("/api/stock/opening", body).json()["on_hand_milli"] == 12_000
    assert owner.post("/api/stock/opening", body).json()["duplicate"] is True
    assert owner.post("/api/stock/opening", {**body, "qty_milli": 9000}).status_code == 409

    clerk = make_client()
    invite_and_join(owner, clerk, "Kamal Clerk", "stock_clerk", [sid])
    adj = {"store_id": sid, "product_id": prods["P0"], "qty_milli": -2000,
           "reason": "Found broken in the back room", "key": "adj-1"}
    assert clerk.post("/api/stock/adjust", adj).status_code == 403
    r = owner.post("/api/stock/adjust", adj).json()
    assert r["on_hand_milli"] == 10_000
    assert owner.post("/api/stock/adjust", adj).json()["duplicate"] is True
    assert owner.post("/api/stock/adjust", {**adj, "key": None, "qty_milli": 500}).status_code == 422
    hist = owner.get(f"/api/stock/history?store_id={sid}&product_id={prods['P0']}").json()
    assert [m["label"] for m in hist["movements"]] == ["Count adjustment", "Opening stock"]
    assert hist["movements"][0]["reason"] == "Found broken in the back room"
    assert hist["movements"][0]["person"] == "Asha Owner"
    log = owner.get("/api/activity?entity=product").json()
    assert any(e["action"] == "stock.adjusted" for e in log)


def test_stock_list_statuses(client):
    stores, prods = _setup(client, n_products=6)
    cid, sid = client.me["company"]["id"], stores[0]["id"]
    today = date.today()
    with dbmod.SessionLocal() as db:
        def put(sku, q, **kw):
            ledger.post(db, company_id=cid, store_id=sid, product_id=prods[sku], qty_milli=q,
                        type=kw.pop("type", "opening"), **kw)
        put("P1", 5000)                                            # sells 1/day: OK
        put("P2", 400_000)                                         # sells 1/day: overstocked
        put("P3", 3000)
        put("P3", 1000, type="delivery", expiry=today + timedelta(days=2))   # expiring
        put("P4", 2000)                                            # below target, no order
        put("P5", 1000)
        put("P5", -3000, type="sale")                              # negative: count needed
        for sku in ("P1", "P2"):
            for d in range(1, 15):
                db.add(DailySales(company_id=cid, store_id=sid, product_id=prods[sku],
                                  day=today - timedelta(days=d), units_milli=1000))
        run = ForecastRun(company_id=cid, status="succeeded", data_cutoff=today)
        db.add(run)
        db.flush()
        db.add(Recommendation(company_id=cid, run_id=run.id, store_id=sid,
                              product_id=prods["P4"], target_milli=10_000))
        db.commit()
    data = client.get(f"/api/stock?store_id={sid}").json()
    status = {r["sku"]: r["status"] for r in data["rows"]}
    assert status == {"P0": "Count needed", "P1": "OK", "P2": "Overstocked", "P3": "Expiring",
                      "P4": "Low", "P5": "Count needed"}
    rows = {r["sku"]: r for r in data["rows"]}
    assert rows["P3"]["expiring_milli"] == 1000
    assert rows["P1"]["days_of_cover"] == 5.0
    assert data["status_counts"]["Count needed"] == 2
    only_low = client.get(f"/api/stock?store_id={sid}&status=Low").json()
    assert [r["sku"] for r in only_low["rows"]] == ["P4"]
    csv_text = client.get(f"/api/stock/export.csv?store_id={sid}").content.decode("utf-8-sig")
    assert csv_text.splitlines()[0].startswith("SKU,Name,Category")


def test_lookup_by_barcode_case_barcode_and_sku(client):
    stores, prods = _setup(client)
    r = client.post(f"/api/products/{prods['P1']}/barcodes",
                    {"code": "18901234567897", "units_per_scan": 12})
    assert r.status_code == 201, r.text
    assert client.get("/api/products/lookup?code=8901234567890").json()["product"]["sku"] == "P0"
    case = client.get("/api/products/lookup?code=18901234567897").json()
    assert case["product"]["sku"] == "P1" and case["units_per_scan"] == 12
    assert client.get("/api/products/lookup?code=p2").json()["product"]["sku"] == "P2"
    miss = client.get("/api/products/lookup?code=4006381333931").json()
    assert miss["found"] is False and "not in your catalogue" in miss["message"]
    clash = client.post(f"/api/products/{prods['P2']}/barcodes", {"code": "8901234567890"})
    assert clash.status_code == 409 and "SKU P0" in clash.json()["error"]


def test_another_companys_stock_is_not_found(make_client):
    a, b = make_client(), make_client()
    stores, prods = _setup(a)
    b.signup(company="Beta", email="b@beta.example")
    sid, pid = stores[0]["id"], prods["P0"]
    assert b.get(f"/api/stock/history?store_id={sid}&product_id={pid}").status_code == 404
    assert b.get(f"/api/products/{pid}").status_code == 404
    assert b.get(f"/api/products/{pid}/overview").status_code == 404
    assert b.post("/api/stock/adjust", {"store_id": sid, "product_id": pid, "qty_milli": 1000,
                                        "reason": "theft"}).status_code == 404


@pytest.mark.skipif(not ON_PG, reason="row locks are a PostgreSQL feature")
def test_concurrent_sales_of_one_product_lose_nothing(client):
    stores, prods = _setup(client)
    cid, sid, pid = client.me["company"]["id"], stores[0]["id"], prods["P0"]
    with dbmod.SessionLocal() as db:
        ledger.post(db, company_id=cid, store_id=sid, product_id=pid, qty_milli=1_000_000,
                    type="opening")
        db.commit()
    errors = []

    def till(n: int):
        try:
            for i in range(50):
                with dbmod.SessionLocal() as db:
                    ledger.post(db, company_id=cid, store_id=sid, product_id=pid,
                                qty_milli=-1000, type="sale", idempotency_key=f"t{n}-b{i}")
                    db.commit()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=till, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    with dbmod.SessionLocal() as db:
        assert ledger.on_hand(db, sid, pid) == 1_000_000 - 300_000
        assert ledger.verify(db, cid) == []
