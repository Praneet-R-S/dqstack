"""Build step 10 -- the POS connector API.

Done when: 1,000 sales sent twice reduce stock once.
"""

from __future__ import annotations

from sqlalchemy import func, select

from dqstack.app import db as dbmod
from dqstack.app import ledger
from dqstack.app.models import Bill, Product

from .conftest import invite_and_join


def _setup(client):
    client.signup()
    s1 = client.post("/api/stores", {"code": "PUN1", "name": "Pune", "mode": "connected"}).json()
    client.post("/api/stores", {"code": "BLR1", "name": "Bengaluru", "mode": "standalone"})
    client.import_file("products", "SKU,Name,Unit,MRP,Selling price,Cost,Barcodes\n"
                                   "RICE,Rice 5 kg,each,500,450,380,8901234567890\n"
                                   "DAL,Toor dal 1 kg,each,200,180,150,\n")
    with dbmod.SessionLocal() as db:
        pid = {p.sku: p.id for p in db.scalars(select(Product))}
        ledger.post(db, company_id=client.me["company"]["id"], store_id=s1["id"],
                    product_id=pid["RICE"], qty_milli=5_000_000, type="opening")
        db.commit()
    key = client.post("/api/integrations/api-keys", {"name": "Store POS"}).json()["key"]
    return s1, pid, {"Authorization": f"Bearer {key}"}


def _hand(store_id, pid):
    with dbmod.SessionLocal() as db:
        return ledger.on_hand(db, store_id, pid)


def _sale(i):
    return {"id": f"pos-{i:05d}", "type": "sale", "store_code": "PUN1",
            "bill_number": f"B{i:05d}", "occurred_at": "2026-11-08T18:00:00+05:30",
            "lines": [{"sku": "RICE", "qty": 1, "price": 450}], "payment_method": "upi"}


def test_done_when_1000_sales_sent_twice_reduce_stock_once(client):
    s1, pid, auth = _setup(client)
    sales = [_sale(i) for i in range(1000)]
    for send in (1, 2):
        for k in range(0, 1000, 500):
            r = client.http.post("/api/pos/v1/messages", json={"messages": sales[k:k + 500]},
                                 headers=auth)
            assert r.status_code == 200, r.text
            body = r.json()
            if send == 1:
                assert (body["accepted"], body["duplicates"]) == (500, 0)
            else:
                assert (body["accepted"], body["duplicates"]) == (0, 500)
    assert _hand(s1["id"], pid["RICE"]) == 5_000_000 - 1_000_000
    with dbmod.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Bill)) == 1000
        assert ledger.verify(db, client.me["company"]["id"]) == []


def test_refunds_and_voids_as_their_own_messages(client):
    s1, pid, auth = _setup(client)
    send = lambda *m: client.http.post("/api/pos/v1/messages", json={"messages": list(m)},
                                       headers=auth).json()
    send(_sale(1), _sale(2))
    assert _hand(s1["id"], pid["RICE"]) == 4_998_000
    r = send({"id": "pos-r1", "type": "refund", "store_code": "PUN1",
              "original_bill_number": "B00001", "lines": [{"sku": "RICE", "qty": 1}]})
    assert r["accepted"] == 1 and _hand(s1["id"], pid["RICE"]) == 4_999_000
    v = send({"id": "pos-v2", "type": "void", "store_code": "PUN1",
              "original_bill_number": "B00002"})
    assert v["accepted"] == 1 and v["results"][0]["result"]["reversed"] is True
    assert _hand(s1["id"], pid["RICE"]) == 5_000_000
    never = send({"id": "pos-v3", "type": "void", "store_code": "PUN1", "bill_number": "B99999"})
    assert never["results"][0]["result"]["reversed"] is False


def test_messages_are_refused_plainly(client):
    s1, pid, auth = _setup(client)
    r = client.http.post("/api/pos/v1/messages", headers=auth, json={"messages": [
        {"id": "x1", "type": "sale", "store_code": "BLR1", "lines": [{"sku": "RICE", "qty": 1,
                                                                    "price": 450}]},
        {"id": "x2", "type": "sale", "store_code": "NOPE", "lines": []},
        {"type": "sale"}]}).json()
    assert r["refused"] == 3 and "standalone" in r["results"][0]["error"]
    assert client.http.get("/api/pos/v1/ping").status_code == 401
    assert client.http.get("/api/pos/v1/ping", headers={"Authorization": "Bearer dqk_nope"}
                           ).status_code == 401


def test_unknown_items_sell_as_misc_and_barcodes_work(client):
    s1, pid, auth = _setup(client)
    r = client.http.post("/api/pos/v1/messages", headers=auth, json={"messages": [
        {"id": "m1", "type": "sale", "store_code": "PUN1", "bill_number": "M1",
         "lines": [{"barcode": "8901234567890", "qty": 2, "price": 450},
                   {"sku": "UNKNOWN-1", "name": "Gift wrap", "qty": 1, "price": 20}]}]}).json()
    assert r["accepted"] == 1 and _hand(s1["id"], pid["RICE"]) == 4_998_000


def test_the_nightly_file_drop(client):
    s1, pid, auth = _setup(client)
    f = ("type,store_code,bill_number,occurred_at,sku,qty,price,payment_method\n"
         "sale,PUN1,N1,2026-11-08T10:00:00+05:30,RICE,2,450,cash\n"
         "sale,PUN1,N1,2026-11-08T10:00:00+05:30,DAL,1,180,cash\n"
         "sale,PUN1,N2,2026-11-08T11:00:00+05:30,RICE,1,450,card\n")
    up = lambda: client.http.post("/api/pos/v1/files", headers=auth,
                                  files={"file": ("sales.csv", f.encode(), "text/csv")}).json()
    first = up()
    assert (first["bills"], first["accepted"]) == (2, 2)
    second = up()
    assert (second["accepted"], second["duplicates"]) == (0, 2)
    assert _hand(s1["id"], pid["RICE"]) == 4_997_000


def test_keys_belong_to_owners_and_admins_and_can_be_revoked(make_client):
    owner = make_client()
    s1, pid, auth = _setup(owner)
    mgr = make_client()
    invite_and_join(owner, mgr, "Mona Manager", "store_manager", [s1["id"]])
    assert mgr.post("/api/integrations/api-keys", {"name": "Mine"}).status_code == 403
    keys = owner.get("/api/integrations/api-keys").json()
    assert "key" not in keys[0] and keys[0]["prefix"].startswith("dqk_")
    owner.post(f"/api/integrations/api-keys/{keys[0]['id']}/revoke")
    assert owner.http.get("/api/pos/v1/ping", headers=auth).status_code == 401
    store_key = owner.post("/api/integrations/api-keys",
                           {"name": "BLR only", "store_id": s1["id"]}).json()["key"]
    other = make_client()
    other.signup(company="Other", email="o@other.example")
    other.post("/api/stores", {"code": "PUN1", "name": "Their Pune", "mode": "connected"})
    # A key only ever reaches its own company's stores.
    r = owner.http.post("/api/pos/v1/messages", headers={"Authorization": f"Bearer {store_key}"},
                        json={"messages": [_sale(9)]}).json()
    assert r["accepted"] == 1
    with dbmod.SessionLocal() as db:
        b = db.scalar(select(Bill))
        assert b.company_id == owner.me["company"]["id"] and b.source == "pos"


def test_key_use_is_noted_without_holding_up_the_bill(client):
    """last_used_at is written in its own short transaction, at most once a minute, so
    requests on one key do not queue behind each other's row lock."""
    from dqstack.app.models import ApiKey
    s1, pid, auth = _setup(client)
    with dbmod.SessionLocal() as db:
        assert db.scalar(select(ApiKey.last_used_at)) is None
    assert client.http.post("/api/pos/v1/messages", json={"messages": [_sale(1)]},
                            headers=auth).status_code == 200
    with dbmod.SessionLocal() as db:
        first = db.scalar(select(ApiKey.last_used_at))
    assert first is not None
    client.http.post("/api/pos/v1/messages", json={"messages": [_sale(2)]}, headers=auth)
    with dbmod.SessionLocal() as db:
        assert db.scalar(select(ApiKey.last_used_at)) == first      # within the minute
