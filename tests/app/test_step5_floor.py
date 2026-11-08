"""Build step 5 -- Floor work.

Done when: a count done offline syncs once; a difference over the limit waits for
approval; a partial delivery leaves the order open.

Also acceptance scenarios 10 (partial delivery: 120 ordered, 100 good and 5
damaged arrive) and 11 (a 40-unit shortfall needs approval, a 2-unit one posts by
itself, and a sale during the count is not a difference), blind counts, two people
disagreeing, transfers with a loss, wastage with a photo, and who may do what.
"""

from __future__ import annotations

import base64
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select

from dqstack.app import db as dbmod
from dqstack.app import ledger
from dqstack.app.models import Product, StockMovement

from .conftest import invite_and_join


def _world(make_client):
    """An owner, two stores, four products with known stock, a manager and a clerk
    for store S1."""
    owner = make_client()
    owner.signup()
    s1 = owner.post("/api/stores", {"code": "S1", "name": "Andheri"}).json()
    s2 = owner.post("/api/stores", {"code": "S2", "name": "Bandra"}).json()
    owner.import_file("products", "SKU,Name,Unit,MRP,Cost,Case pack,Perishable\n"
                                  "RICE,Rice 5 kg,each,500,400,12,no\n"
                                  "DAL,Toor dal 1 kg,each,200,150,10,no\n"
                                  "MILK,Milk 1 L,each,70,55,12,yes\n"
                                  "ATTA,Atta 10 kg,each,600,480,4,no\n")
    owner.import_file("opening_stock", "Store code,SKU,Quantity\nS1,RICE,100\nS1,DAL,50\n"
                                       "S1,MILK,20\nS2,RICE,30\n")
    with dbmod.SessionLocal() as db:
        prods = {p.sku: p.id for p in db.scalars(select(Product))}
    mgr, clerk = make_client(), make_client()
    invite_and_join(owner, mgr, "Mala Manager", "store_manager", [s1["id"]])
    invite_and_join(owner, clerk, "Kabir Clerk", "stock_clerk", [s1["id"]])
    return owner, mgr, clerk, s1, s2, prods


def _on_hand(store_id, product_id) -> int:
    with dbmod.SessionLocal() as db:
        return ledger.on_hand(db, store_id, product_id)


def _movements() -> int:
    with dbmod.SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(StockMovement))


def test_done_when_an_offline_count_syncs_once(make_client):
    owner, mgr, clerk, s1, s2, prods = _world(make_client)
    # The phone made the session and four counts while offline, each with its own key.
    session = clerk.post("/api/counts", {"store_id": s1["id"], "kind": "full",
                                         "key": "phone7-session-1"}).json()
    again = clerk.post("/api/counts", {"store_id": s1["id"], "kind": "full",
                                       "key": "phone7-session-1"}).json()
    assert again["id"] == session["id"]
    # Counted after the opening stock went in (setup, just now), sent later.
    t0 = datetime.now(timezone.utc)
    queue = {"lines": [
        {"product_id": prods["RICE"], "counted_milli": 98_000, "key": "phone7-l1",
         "counted_at": t0.isoformat()},
        {"product_id": prods["DAL"], "counted_milli": 50_000, "key": "phone7-l2",
         "counted_at": (t0 + timedelta(milliseconds=1)).isoformat()},
        {"product_id": prods["MILK"], "counted_milli": 21_000, "key": "phone7-l3",
         "counted_at": (t0 + timedelta(milliseconds=2)).isoformat()},
        {"product_id": prods["ATTA"], "counted_milli": 6_000, "key": "phone7-l4",
         "counted_at": (t0 + timedelta(milliseconds=3)).isoformat()},
    ]}
    first = clerk.post(f"/api/counts/{session['id']}/lines/batch", queue).json()
    assert (first["applied"], first["repeats"]) == (4, 0)
    after_first = _movements()
    second = clerk.post(f"/api/counts/{session['id']}/lines/batch", queue).json()
    assert (second["applied"], second["repeats"]) == (0, 4)
    assert _movements() == after_first
    # Rice -2 is within 5 units but worth Rs 800, over the Rs 500 limit: it waits.
    assert _on_hand(s1["id"], prods["RICE"]) == 100_000
    assert _on_hand(s1["id"], prods["MILK"]) == 21_000      # +1, Rs 55: posted by itself
    assert _on_hand(s1["id"], prods["ATTA"]) == 6_000       # first count sets the stock
    statuses = [r["status"] for r in first["results"]]
    assert statuses == ["waiting", "confirmed", "posted", "posted"]


def test_done_when_a_large_difference_waits_for_approval(make_client):
    """Acceptance scenario 11."""
    owner, mgr, clerk, s1, s2, prods = _world(make_client)
    s = clerk.post("/api/counts", {"store_id": s1["id"], "kind": "cycle",
                                   "product_ids": [prods["RICE"], prods["DAL"], prods["MILK"]]}).json()
    big = clerk.post(f"/api/counts/{s['id']}/lines",
                     {"product_id": prods["RICE"], "counted_milli": 60_000, "key": "c1"}).json()
    assert big["status"] == "waiting" and "manager will review" in big["message"]
    assert _on_hand(s1["id"], prods["RICE"]) == 100_000      # unchanged until approved
    small = clerk.post(f"/api/counts/{s['id']}/lines",
                       {"product_id": prods["DAL"], "counted_milli": 48_000, "key": "c2"}).json()
    assert small["status"] == "posted"
    assert _on_hand(s1["id"], prods["DAL"]) == 48_000

    # A sale during the count: MILK is counted (20 on the shelf), a customer buys 3 a
    # moment later, and the count reaches the server only after the sale.
    counted_at = datetime.now(timezone.utc)
    cid = owner.me["company"]["id"]
    with dbmod.SessionLocal() as db:
        ledger.post(db, company_id=cid, store_id=s1["id"], product_id=prods["MILK"],
                    qty_milli=-3000, type="sale", at=counted_at + timedelta(seconds=2))
        db.commit()
    milk = clerk.post(f"/api/counts/{s['id']}/lines",
                      {"product_id": prods["MILK"], "counted_milli": 20_000,
                       "counted_at": counted_at.isoformat(), "key": "c3"}).json()
    assert milk["status"] == "confirmed"
    assert _on_hand(s1["id"], prods["MILK"]) == 17_000

    clerk.post(f"/api/counts/{s['id']}/submit")
    assert clerk.get(f"/api/counts/{s['id']}").json()["status"] == "submitted"
    queue = mgr.get(f"/api/counts/approvals?store_id={s1['id']}").json()
    assert [(q["sku"], q["difference_milli"]) for q in queue] == [("RICE", -40_000)]
    assert clerk.post(f"/api/counts/lines/{big['line_id']}/decide",
                      {"action": "approve", "reason": "Checked the back room"}).status_code == 403
    r = mgr.post(f"/api/counts/lines/{big['line_id']}/decide",
                 {"action": "approve", "reason": "Recounted with Kabir; 40 missing"})
    assert r.status_code == 200 and r.json()["status"] == "posted"
    assert _on_hand(s1["id"], prods["RICE"]) == 60_000
    assert clerk.get(f"/api/counts/{s['id']}").json()["status"] == "closed"


def test_done_when_a_partial_delivery_leaves_the_order_open(make_client):
    """Acceptance scenario 10: 120 ordered; 100 good and 5 damaged arrive."""
    owner, mgr, clerk, s1, s2, prods = _world(make_client)
    sup = owner.post("/api/suppliers", {"name": "Gupta Traders", "lead_time_days": 2}).json()
    po = owner.post("/api/orders", {"store_id": s1["id"], "supplier_id": sup["id"],
                                    "lines": [{"product_id": prods["ATTA"], "qty_milli": 120_000,
                                               "unit_cost_paise": 47_000}]}).json()
    owner.post(f"/api/orders/{po['id']}/approve")
    owner.post(f"/api/orders/{po['id']}/mark-sent")
    d = clerk.post("/api/deliveries", {
        "store_id": s1["id"], "po_id": po["id"], "invoice_number": "GT/2611/88",
        "key": "recv-1", "lines": [{"product_id": prods["ATTA"], "received_milli": 100_000,
                                    "damaged_milli": 5_000}]}).json()
    assert d["order"]["status"] == "partly_received"
    assert d["order"]["lines"][0]["due_milli"] == 20_000
    assert _on_hand(s1["id"], prods["ATTA"]) == 100_000
    credits = owner.get(f"/api/deliveries/credits?store_id={s1['id']}").json()
    assert [(c["sku"], c["units_milli"], c["reason"]) for c in credits] == [("ATTA", 5_000, "damaged")]
    assert credits[0]["value_paise"] == 5 * 47_000
    stock = {r["sku"]: r for r in owner.get(f"/api/stock?store_id={s1['id']}").json()["rows"]}
    assert stock["ATTA"]["on_order_milli"] == 20_000
    # Sent again by a flaky connection: nothing changes.
    again = clerk.post("/api/deliveries", {
        "store_id": s1["id"], "po_id": po["id"], "key": "recv-1",
        "lines": [{"product_id": prods["ATTA"], "received_milli": 100_000,
                   "damaged_milli": 5_000}]}).json()
    assert again["duplicate"] is True and _on_hand(s1["id"], prods["ATTA"]) == 100_000
    # The rest arrives; the order closes.
    clerk.post("/api/deliveries", {"store_id": s1["id"], "po_id": po["id"], "key": "recv-2",
                                   "lines": [{"product_id": prods["ATTA"], "received_milli": 20_000}]})
    assert owner.get(f"/api/orders/{po['id']}").json()["status"] == "received"


def test_close_short_and_cancel_need_reasons(make_client):
    owner, mgr, clerk, s1, s2, prods = _world(make_client)
    po = owner.post("/api/orders", {"store_id": s1["id"], "lines": [
        {"product_id": prods["DAL"], "qty_milli": 30_000}]}).json()
    owner.post(f"/api/orders/{po['id']}/approve")
    clerk.post("/api/deliveries", {"store_id": s1["id"], "po_id": po["id"],
                                   "lines": [{"product_id": prods["DAL"], "received_milli": 10_000}]})
    assert owner.post(f"/api/orders/{po['id']}/cancel",
                      {"reason": "Not needed now"}).status_code == 409
    r = owner.post(f"/api/orders/{po['id']}/close-short", {"reason": "Supplier out of stock"})
    assert r.json()["status"] == "received" and r.json()["closed_short"] is True
    stock = {x["sku"]: x for x in owner.get(f"/api/stock?store_id={s1['id']}").json()["rows"]}
    assert stock["DAL"]["on_order_milli"] == 0


def test_purchaser_creates_manager_approves(make_client):
    owner, mgr, clerk, s1, s2, prods = _world(make_client)
    buyer = make_client()
    invite_and_join(owner, buyer, "Pari Purchaser", "purchaser", [s1["id"]])
    po = buyer.post("/api/orders", {"store_id": s1["id"], "lines": [
        {"product_id": prods["RICE"], "qty_milli": 24_000}]})
    assert po.status_code == 201
    assert buyer.post(f"/api/orders/{po.json()['id']}/approve").status_code == 403
    assert mgr.post(f"/api/orders/{po.json()['id']}/approve").json()["status"] == "approved"
    assert clerk.post("/api/orders", {"store_id": s1["id"], "lines": [
        {"product_id": prods["RICE"], "qty_milli": 1000}]}).status_code == 403
    assert buyer.post("/api/orders", {"store_id": s2["id"], "lines": [
        {"product_id": prods["RICE"], "qty_milli": 1000}]}).status_code == 404


def test_blind_counts_hide_the_expected_number_from_the_counter(make_client):
    owner, mgr, clerk, s1, s2, prods = _world(make_client)
    s = clerk.post("/api/counts", {"store_id": s1["id"], "kind": "cycle", "blind": True,
                                   "product_ids": [prods["RICE"]]}).json()
    assert s["shows_expected"] is False
    res = clerk.post(f"/api/counts/{s['id']}/lines",
                     {"product_id": prods["RICE"], "counted_milli": 99_000}).json()
    assert "system_milli" not in res and "difference_milli" not in res
    view = clerk.get(f"/api/counts/{s['id']}").json()
    assert "system_milli" not in view["lines"][0]
    assert mgr.get(f"/api/counts/{s['id']}").json()["lines"][0]["system_milli"] == 100_000


def test_two_people_disagree_and_the_manager_picks(make_client):
    owner, mgr, clerk, s1, s2, prods = _world(make_client)
    other = make_client()
    invite_and_join(owner, other, "Om Other", "stock_clerk", [s1["id"]])
    s = clerk.post("/api/counts", {"store_id": s1["id"], "kind": "full"}).json()
    a = clerk.post(f"/api/counts/{s['id']}/lines",
                   {"product_id": prods["DAL"], "counted_milli": 40_000}).json()
    b = other.post(f"/api/counts/{s['id']}/lines",
                   {"product_id": prods["DAL"], "counted_milli": 47_000}).json()
    assert a["status"] == "waiting" and b["status"] == "conflict"
    view = mgr.get(f"/api/counts/{s['id']}").json()
    assert sorted(l["status"] for l in view["lines"]) == ["conflict", "conflict"]
    assert mgr.post(f"/api/counts/lines/{a['line_id']}/decide",
                    {"action": "approve", "reason": "Looks right"}).status_code == 409
    mgr.post(f"/api/counts/lines/{b['line_id']}/decide",
             {"action": "pick", "reason": "Om counted the back shelf too"})
    assert _on_hand(s1["id"], prods["DAL"]) == 47_000
    statuses = sorted(l["status"] for l in mgr.get(f"/api/counts/{s['id']}").json()["lines"])
    assert statuses == ["posted", "superseded"]


def test_the_due_list_puts_valuable_products_first(make_client):
    owner, mgr, clerk, s1, s2, prods = _world(make_client)
    due = clerk.get(f"/api/counts/due?store_id={s1['id']}").json()
    assert due["total"] == 4
    names = [r["sku"] for r in due["rows"]]
    assert names.index("RICE") < names.index("DAL")       # Rs 40,000 of rice beats Rs 7,500 of dal
    s = clerk.post("/api/counts", {"store_id": s1["id"], "kind": "cycle"}).json()
    assert len(s["still_to_count"]) == 4
    clerk.post(f"/api/counts/{s['id']}/lines", {"product_id": prods["RICE"], "counted_milli": 100_000})
    due = clerk.get(f"/api/counts/due?store_id={s1['id']}").json()
    assert "RICE" not in [r["sku"] for r in due["rows"]]


def test_transfer_with_a_loss_in_transit(make_client):
    owner, mgr, clerk, s1, s2, prods = _world(make_client)
    t = clerk.post("/api/transfers", {"from_store_id": s1["id"], "to_store_id": s2["id"],
                                      "key": "tr-1", "lines": [{"product_id": prods["RICE"],
                                                                "qty_milli": 10_000}]}).json()
    assert t["status"] == "in_transit"
    assert clerk.post("/api/transfers", {"from_store_id": s1["id"], "to_store_id": s2["id"],
                                         "key": "tr-1", "lines": [{"product_id": prods["RICE"],
                                                                   "qty_milli": 10_000}]}).json()["duplicate"]
    assert _on_hand(s1["id"], prods["RICE"]) == 90_000
    s2_stock = {r["sku"]: r for r in owner.get(f"/api/stock?store_id={s2['id']}").json()["rows"]}
    assert s2_stock["RICE"]["in_transit_milli"] == 10_000
    # The clerk works in S1 only, so receiving in S2 is someone else's job.
    assert clerk.post(f"/api/transfers/{t['id']}/receive", {"lines": []}).status_code == 403
    got = owner.post(f"/api/transfers/{t['id']}/receive",
                     {"lines": [{"product_id": prods["RICE"], "qty_milli": 8_000}],
                      "key": "tr-1-in"}).json()
    assert got["status"] == "received"
    assert got["losses"][0]["lost_milli"] == 2_000 and got["losses"][0]["value_paise"] == 80_000
    assert _on_hand(s2["id"], prods["RICE"]) == 38_000
    log = owner.get("/api/activity?entity=transfer").json()
    assert any(e["action"] == "transfer.loss" for e in log)


def test_wastage_with_a_photo_valued_at_cost(make_client):
    owner, mgr, clerk, s1, s2, prods = _world(make_client)
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 64).decode()
    r = clerk.post("/api/wastage", {"store_id": s1["id"], "product_id": prods["MILK"],
                                    "qty_milli": 4_000, "reason": "expired", "key": "w1",
                                    "photo_base64": png, "photo_type": "image/png"}).json()
    assert r["on_hand_milli"] == 16_000 and r["value_paise"] == 4 * 5_500 and r["photo_id"]
    assert clerk.post("/api/wastage", {"store_id": s1["id"], "product_id": prods["MILK"],
                                       "qty_milli": 4_000, "reason": "expired",
                                       "key": "w1"}).json()["duplicate"] is True
    listing = mgr.get(f"/api/wastage?store_id={s1['id']}").json()
    assert listing[0]["reason"] == "expired" and listing[0]["photo_id"] == r["photo_id"]
    assert owner.get(f"/api/attachments/{r['photo_id']}").content.startswith(b"\x89PNG")
    assert clerk.post("/api/wastage", {"store_id": s1["id"], "product_id": prods["MILK"],
                                       "qty_milli": 1_000, "reason": "other"}).status_code == 422
    stranger = make_client()
    stranger.signup(company="Other Co", email="x@other.example")
    assert stranger.get(f"/api/attachments/{r['photo_id']}").status_code == 404


def test_receiving_without_an_order_and_the_perishable_rule(make_client):
    owner, mgr, clerk, s1, s2, prods = _world(make_client)
    r = clerk.post("/api/deliveries", {"store_id": s1["id"], "lines": [
        {"product_id": prods["MILK"], "received_milli": 12_000}]})
    assert r.status_code == 422 and "expiry" in r.json()["error"]
    expiry = (date.today() + timedelta(days=5)).isoformat()
    r = clerk.post("/api/deliveries", {"store_id": s1["id"], "invoice_number": "M-11", "lines": [
        {"product_id": prods["MILK"], "received_milli": 12_000, "unit_cost_paise": 6_000,
         "batch": "B7", "expiry": expiry}]})
    assert r.status_code == 201
    assert _on_hand(s1["id"], prods["MILK"]) == 32_000
    hist = owner.get(f"/api/stock/history?store_id={s1['id']}&product_id={prods['MILK']}").json()
    assert hist["movements"][0]["label"] == "Delivery received"
    assert hist["movements"][0]["expiry"] == expiry


def test_viewer_and_cashier_cannot_do_floor_work(make_client):
    owner, mgr, clerk, s1, s2, prods = _world(make_client)
    viewer, cashier = make_client(), make_client()
    invite_and_join(owner, viewer, "Vani Viewer", "viewer", [s1["id"]])
    invite_and_join(owner, cashier, "Chirag Cashier", "cashier", [s1["id"]])
    for who in (viewer, cashier):
        assert who.post("/api/counts", {"store_id": s1["id"]}).status_code == 403
        assert who.post("/api/wastage", {"store_id": s1["id"], "product_id": prods["RICE"],
                                         "qty_milli": 1000, "reason": "damaged"}).status_code == 403
        assert who.post("/api/deliveries", {"store_id": s1["id"], "lines": [
            {"product_id": prods["RICE"], "received_milli": 1000}]}).status_code == 403
