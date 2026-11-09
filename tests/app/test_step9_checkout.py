"""Build step 9 -- Checkout.

Done when: all Checkout scenarios in section 17 pass, including double press,
offline and refund. Those are acceptance scenarios 3 to 8:

3. Paid bill: 3 items, cash; within 2 seconds stock drops by exactly those, and the
   activity log shows one bill.
4. Unpaid bill: 3 items, voided; stock does not change.
5. Double press: the payment is sent twice; stock drops once.
6. Offline till: 5 bills made offline post once, in order, and the waiting count
   returns to zero.
7. Refund: one resaleable item back adds 1 to stock; a damaged return goes to
   wastage, not stock.
8. Sell below zero: an item at 0 sells, stock shows -1, and a "count needed" alert
   appears.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select

from dqstack.app import db as dbmod
from dqstack.app import ledger
from dqstack.app.models import Alert, Bill, DailySales, Product, StockMovement

from .conftest import invite_and_join


def _till(make_client):
    owner = make_client()
    owner.signup()
    s1 = owner.post("/api/stores", {"code": "S1", "name": "Andheri", "mode": "connected"}).json()
    owner.import_file("products", "SKU,Name,Unit,MRP,Selling price,Cost,GST rate,Barcodes\n"
                                  "RICE,Rice 5 kg,each,500,450,380,5,8901234567890\n"
                                  "DAL,Toor dal 1 kg,each,200,180,150,5,\n"
                                  "SOAP,Bath soap,each,45,45,32,18,\n"
                                  "ONION,Onions,kg,40,40,30,0,\n")
    with dbmod.SessionLocal() as db:
        pid = {p.sku: p.id for p in db.scalars(select(Product))}
        cid = owner.me["company"]["id"]
        for sku, q in (("RICE", 20), ("DAL", 10), ("SOAP", 5), ("ONION", 50)):
            ledger.post(db, company_id=cid, store_id=s1["id"], product_id=pid[sku],
                        qty_milli=q * 1000, type="opening")
        db.commit()
    owner.post("/api/people/me/manager-pin", {"pin": "4321"})
    cashier = make_client()
    invite_and_join(owner, cashier, "Chirag Cashier", "cashier", [s1["id"]])
    return owner, cashier, s1, pid


def _hand(store_id, product_id):
    with dbmod.SessionLocal() as db:
        return ledger.on_hand(db, store_id, product_id)


def _bill(s1, number, lines, method="cash", tendered=None, **kw):
    total = sum(q * p // 1000 for _, q, p in lines)
    pay = {"method": method, "amount_paise": total}
    if tendered is not None:
        pay["tendered_paise"] = tendered
    return {"store_id": s1["id"], "bill_number": number, "device_id": "till-1",
            "lines": [{"product_id": pid, "qty_milli": q, "unit_price_paise": p}
                      for pid, q, p in lines], "payments": [pay], **kw}


def test_scenario_3_paid_bill(make_client):
    owner, cashier, s1, pid = _till(make_client)
    body = _bill(s1, "T1-0001", [(pid["RICE"], 1000, 45_000), (pid["DAL"], 2000, 18_000),
                                 (pid["SOAP"], 3000, 4_500)], tendered=100_000)
    t0 = time.perf_counter()
    r = cashier.post("/api/checkout/bills", body)
    took = time.perf_counter() - t0
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["state"] == "paid" and b["total_paise"] == 94_500 and b["change_paise"] == 5_500
    assert took < 2.0
    assert (_hand(s1["id"], pid["RICE"]), _hand(s1["id"], pid["DAL"]),
            _hand(s1["id"], pid["SOAP"])) == (19_000, 8_000, 2_000)
    log = owner.get("/api/activity?entity=bill").json()
    assert [e["action"] for e in log] == ["bill.paid"]
    # GST inside the price: 5% on rice and dal, 18% on soap.
    assert b["tax_paise"] == round(45_000 * 500 / 10_500) + round(36_000 * 500 / 10_500) + \
        round(13_500 * 1800 / 11_800)


def test_scenario_4_voided_before_payment(make_client):
    owner, cashier, s1, pid = _till(make_client)
    lines = [{"product_id": pid[s], "qty_milli": 1000} for s in ("RICE", "DAL", "SOAP")]
    held = cashier.post("/api/checkout/hold", {"store_id": s1["id"], "bill_number": "T1-0002",
                                               "lines": lines}).json()
    assert held["state"] == "held"
    stock = {r["sku"]: r for r in owner.get(f"/api/stock?store_id={s1['id']}").json()["rows"]}
    assert stock["RICE"]["in_open_bills_milli"] == 1000 and stock["RICE"]["on_hand_milli"] == 20_000
    v = cashier.post("/api/checkout/void", {"store_id": s1["id"], "bill_number": "T1-0002",
                                            "reason": "Customer left"}).json()
    assert v["state"] == "voided" and v["void_reason"] == "Customer left"
    assert _hand(s1["id"], pid["RICE"]) == 20_000 and _hand(s1["id"], pid["SOAP"]) == 5_000
    assert cashier.post("/api/checkout/bills", _bill(s1, "T1-0002", [(pid["RICE"], 1000, 45_000)])
                        ).status_code == 409


def test_scenario_5_double_press(make_client):
    owner, cashier, s1, pid = _till(make_client)
    body = _bill(s1, "T1-0003", [(pid["RICE"], 2000, 45_000)], method="upi")
    first = cashier.post("/api/checkout/bills", body).json()
    second = cashier.post("/api/checkout/bills", body).json()
    assert second["duplicate"] is True and second["id"] == first["id"]
    assert _hand(s1["id"], pid["RICE"]) == 18_000
    with dbmod.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Bill)) == 1


def test_scenario_6_offline_till(make_client):
    owner, cashier, s1, pid = _till(make_client)
    cashier.post("/api/checkout/heartbeat", {"store_id": s1["id"], "device_id": "till-1",
                                             "pending": 5})
    z = owner.get(f"/api/checkout/z-report?store_id={s1['id']}").json()
    assert z["devices_waiting"] == 5
    t0 = datetime.now(timezone.utc) - timedelta(minutes=40)
    bills = [_bill(s1, f"T1-OFF-{i}", [(pid["DAL"], 1000, 18_000)],
                   paid_at=(t0 + timedelta(minutes=i)).isoformat()) for i in range(5)]
    queue = {"store_id": s1["id"], "device_id": "till-1", "bills": bills, "pending_after": 0}
    r = cashier.post("/api/checkout/sync", queue).json()
    assert (r["posted"], r["duplicates"], r["refused"]) == (5, 0, 0)
    again = cashier.post("/api/checkout/sync", queue).json()
    assert (again["posted"], again["duplicates"]) == (0, 5)
    assert _hand(s1["id"], pid["DAL"]) == 5_000
    with dbmod.SessionLocal() as db:
        times = [m.at for m in db.scalars(select(StockMovement).where(
            StockMovement.type == "sale").order_by(StockMovement.at))]
        assert len(times) == 5 and all(a < b for a, b in zip(times, times[1:]))
        assert all(b.made_offline for b in db.scalars(select(Bill)))
    z = owner.get(f"/api/checkout/z-report?store_id={s1['id']}").json()
    assert z["devices_waiting"] == 0 and z["made_offline"] == 5


def test_scenario_7_refund_resaleable_and_damaged(make_client):
    owner, cashier, s1, pid = _till(make_client)
    cashier.post("/api/checkout/bills", _bill(s1, "T1-0010", [(pid["SOAP"], 3000, 4_500)]))
    assert _hand(s1["id"], pid["SOAP"]) == 2_000
    base = {"store_id": s1["id"], "original_bill_number": "T1-0010", "reason": "Wrong fragrance",
            "lines": [{"product_id": pid["SOAP"], "qty_milli": 1000, "resaleable": True}]}
    no_pin = cashier.post("/api/checkout/refunds", {**base, "refund_number": "R-1"})
    assert no_pin.status_code == 403 and "manager's PIN" in no_pin.json()["error"]
    wrong = cashier.post("/api/checkout/refunds", {**base, "refund_number": "R-1",
                                                   "manager_pin": "0000"})
    assert wrong.status_code == 403
    ok = cashier.post("/api/checkout/refunds", {**base, "refund_number": "R-1",
                                                "manager_pin": "4321"}).json()
    assert ok["state"] == "refund" and ok["total_paise"] == -4_500
    assert _hand(s1["id"], pid["SOAP"]) == 3_000                     # +1, back on the shelf
    damaged = {**base, "refund_number": "R-2", "manager_pin": "4321", "reason": "Box crushed",
               "lines": [{"product_id": pid["SOAP"], "qty_milli": 1000, "resaleable": False}]}
    cashier.post("/api/checkout/refunds", damaged)
    assert _hand(s1["id"], pid["SOAP"]) == 3_000                     # not back in stock
    waste = owner.get(f"/api/wastage?store_id={s1['id']}").json()
    assert waste[0]["type"] == "damage" and waste[0]["qty_milli"] == 1_000
    too_many = {**base, "refund_number": "R-3", "manager_pin": "4321",
                "lines": [{"product_id": pid["SOAP"], "qty_milli": 2000}]}
    assert cashier.post("/api/checkout/refunds", too_many).status_code == 422
    assert cashier.post("/api/checkout/refunds", {**damaged}).json()["duplicate"] is True


def test_scenario_8_sell_below_zero(make_client):
    owner, cashier, s1, pid = _till(make_client)
    cashier.post("/api/checkout/bills", _bill(s1, "T1-0020", [(pid["SOAP"], 5000, 4_500)]))
    assert _hand(s1["id"], pid["SOAP"]) == 0
    r = cashier.post("/api/checkout/bills", _bill(s1, "T1-0021", [(pid["SOAP"], 1000, 4_500)]))
    assert r.status_code == 201                                      # never block a customer
    assert _hand(s1["id"], pid["SOAP"]) == -1_000
    with dbmod.SessionLocal() as db:
        a = db.scalar(select(Alert).where(Alert.kind == "negative_stock"))
        assert a is not None and "Count it" in a.message
    stock = {x["sku"]: x for x in owner.get(f"/api/stock?store_id={s1['id']}").json()["rows"]}
    assert stock["SOAP"]["status"] == "Count needed"


def test_discounts_need_a_manager_and_are_logged(make_client):
    owner, cashier, s1, pid = _till(make_client)
    cheap = _bill(s1, "T1-0030", [(pid["RICE"], 1000, 40_000)])
    cheap["lines"][0]["override_reason"] = "Damaged pack"
    assert cashier.post("/api/checkout/bills", cheap).status_code == 403
    cheap["manager_pin"] = "4321"
    r = cashier.post("/api/checkout/bills", cheap).json()
    assert r["lines"][0]["original_price_paise"] == 45_000 and r["discount_paise"] == 5_000
    no_reason = _bill(s1, "T1-0031", [(pid["RICE"], 1000, 40_000)], manager_pin="4321")
    assert cashier.post("/api/checkout/bills", no_reason).status_code == 422


def test_unknown_barcodes_case_and_scale_labels(make_client):
    owner, cashier, s1, pid = _till(make_client)
    owner.post(f"/api/products/{pid['ONION']}/barcodes", {"code": "2001234"})
    assert cashier.get("/api/checkout/scan?code=8901234567890").json()["product_id"] == pid["RICE"]
    scale = cashier.get("/api/checkout/scan?code=2001234012508").json()   # 1.250 kg of onions
    assert scale["product_id"] == pid["ONION"] and scale["qty_milli"] == 1250
    miss = cashier.get("/api/checkout/scan?code=4006381333931").json()
    assert miss["found"] is False and "misc item" in miss["message"]
    body = {"store_id": s1["id"], "bill_number": "T1-0040", "lines": [
        {"barcode": "4006381333931", "description": "Pen", "qty_milli": 1000,
         "unit_price_paise": 1_000},
        {"product_id": pid["ONION"], "qty_milli": 1250, "unit_price_paise": 4_000}],
        "payments": [{"method": "cash", "amount_paise": 6_000}]}
    assert cashier.post("/api/checkout/bills", body).status_code == 201
    assert _hand(s1["id"], pid["ONION"]) == 48_750
    with dbmod.SessionLocal() as db:
        assert db.scalar(select(Alert).where(Alert.kind == "unmapped_barcode")) is not None


def test_payments_must_add_up(make_client):
    owner, cashier, s1, pid = _till(make_client)
    body = _bill(s1, "T1-0050", [(pid["RICE"], 1000, 45_000)])
    body["payments"] = [{"method": "cash", "amount_paise": 20_000},
                        {"method": "upi", "amount_paise": 20_000}]
    assert cashier.post("/api/checkout/bills", body).status_code == 422
    body["payments"][1]["amount_paise"] = 25_000                     # split cash + UPI
    assert cashier.post("/api/checkout/bills", body).status_code == 201


def test_z_report_receipt_and_day_close(make_client):
    owner, cashier, s1, pid = _till(make_client)
    cashier.post("/api/checkout/bills", _bill(s1, "T1-0060", [(pid["RICE"], 1000, 45_000)],
                                              tendered=50_000))
    cashier.post("/api/checkout/bills", _bill(s1, "T1-0061", [(pid["DAL"], 1000, 18_000)],
                                              method="card"))
    cashier.post("/api/checkout/hold", {"store_id": s1["id"], "bill_number": "T1-0062",
                                        "lines": [{"product_id": pid["SOAP"], "qty_milli": 1000}]})
    z = owner.get(f"/api/checkout/z-report?store_id={s1['id']}").json()
    assert (z["bills"], z["sales_paise"], z["by_method_paise"]["cash"],
            z["by_method_paise"]["card"]) == (2, 63_000, 45_000, 18_000)
    found = owner.get(f"/api/checkout/bills?store_id={s1['id']}&number=0060").json()
    detail = owner.get(f"/api/checkout/bills/{found[0]['id']}?phone=9820012345").json()
    assert "Rice 5 kg" in detail["receipt"] and "Change" in detail["receipt"]
    assert detail["whatsapp_link"].startswith("https://wa.me/919820012345?text=")
    # Day close: the held bill must be confirmed for voiding; cash counted is kept.
    st = owner.get(f"/api/day-close?store_id={s1['id']}").json()
    assert st["cash_expected_paise"] == 45_000 and len(st["open_bills"]) == 1
    assert owner.post("/api/day-close", {"store_id": s1["id"]}).status_code == 409
    r = owner.post("/api/day-close", {"store_id": s1["id"], "void_open_bills": True,
                                      "cash_counted_paise": 45_000})
    assert r.status_code == 201, r.text
    with dbmod.SessionLocal() as db:
        rows = {d.product_id: d for d in db.scalars(select(DailySales))}
    assert rows[pid["RICE"]].units_milli == 1000 and rows[pid["RICE"]].source == "checkout"
    assert rows[pid["RICE"]].posted_milli == 1000


def test_standalone_stores_and_other_roles(make_client):
    owner, cashier, s1, pid = _till(make_client)
    s2 = owner.post("/api/stores", {"code": "S2", "name": "Bandra"}).json()
    assert owner.post("/api/checkout/bills", _bill(s2, "X1", [(pid["RICE"], 1000, 45_000)])
                      ).status_code == 409
    clerk = make_client()
    invite_and_join(owner, clerk, "Kavya Clerk", "stock_clerk", [s1["id"]])
    assert clerk.post("/api/checkout/bills", _bill(s1, "X2", [(pid["RICE"], 1000, 45_000)])
                      ).status_code == 403
