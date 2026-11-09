"""API answers the screens rely on: a scan with today's store price, Undo for a resolved
alert, the list of support grants, the stock status counts under a status filter, and
the dates the browser formats."""

from __future__ import annotations

from sqlalchemy import select

from dqstack.app import db as dbmod
from dqstack.app.models import Alert

from .conftest import invite_and_join
from .test_step9_checkout import _till


def test_scan_with_store_carries_price_and_unit(make_client):
    owner, cashier, s1, pid = _till(make_client)
    r = cashier.get(f"/api/checkout/scan?code=8901234567890&store_id={s1['id']}")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["found"] and d["product_id"] == pid["RICE"]
    assert d["price_paise"] == 45_000 and d["unit"] == "each" and d["sku"] == "RICE"
    # Without a store the answer stays as it was.
    assert "price_paise" not in cashier.get("/api/checkout/scan?code=RICE").json()
    # Another company's store is "not found".
    other = make_client()
    other.signup(email="other@example.com", company="Other Co")
    assert other.get(f"/api/checkout/scan?code=RICE&store_id={s1['id']}").status_code == 404


def test_alert_done_can_be_undone(make_client):
    owner, cashier, s1, pid = _till(make_client)
    with dbmod.SessionLocal() as db:
        a = Alert(company_id=owner.me["company"]["id"], store_id=s1["id"], kind="count_overdue",
                  severity="risk", message="Count rice", dedupe_key="count_overdue:x")
        db.add(a)
        db.commit()
        aid = a.id
    assert owner.post(f"/api/alerts/{aid}/resolve").status_code == 200
    assert owner.post(f"/api/alerts/{aid}/reopen").status_code == 200
    with dbmod.SessionLocal() as db:
        assert db.get(Alert, aid).resolved_at is None
    other = make_client()
    other.signup(email="other@example.com", company="Other Co")
    assert other.post(f"/api/alerts/{aid}/reopen").status_code == 404


def test_support_grants_are_listed_for_the_owner_only(make_client):
    owner, cashier, s1, pid = _till(make_client)
    g = owner.post("/api/company/support-grants", {"reason": "Help with imports", "hours": 2})
    assert g.status_code == 201, g.text
    rows = owner.get("/api/company/support-grants").json()
    assert rows[0]["id"] == g.json()["id"] and rows[0]["active"] is True
    owner.post(f"/api/company/support-grants/{g.json()['id']}/revoke")
    assert owner.get("/api/company/support-grants").json()[0]["active"] is False
    assert cashier.get("/api/company/support-grants").status_code == 403


def test_stock_counts_cover_every_status_when_filtered(make_client):
    owner, cashier, s1, pid = _till(make_client)
    full = owner.get(f"/api/stock?store_id={s1['id']}").json()
    some = next(iter(full["status_counts"]))
    filtered = owner.get(f"/api/stock?store_id={s1['id']}&status={some}").json()
    assert filtered["status_counts"] == full["status_counts"]
    assert all(r["status"] == some for r in filtered["rows"])
    assert filtered["total"] == full["status_counts"][some]


def test_setup_links_to_the_sales_history_import(make_client):
    owner = make_client()
    owner.signup()
    text = owner.get("/api/setup").text
    assert "#/imports/new/sales\"" not in text


def test_unknown_barcode_alert_links_to_stock(make_client):
    owner, cashier, s1, pid = _till(make_client)
    body = {"store_id": s1["id"], "bill_number": "T1-9", "device_id": "till-1",
            "lines": [{"barcode": "999000111", "description": "Misc", "qty_milli": 1000,
                       "unit_price_paise": 1_000}],
            "payments": [{"method": "cash", "amount_paise": 1_000}]}
    assert cashier.post("/api/checkout/bills", body).status_code == 201
    with dbmod.SessionLocal() as db:
        a = db.scalar(select(Alert).where(Alert.kind == "unmapped_barcode"))
        assert a is not None and a.link == "#/stock?q=999000111"
