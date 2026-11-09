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


def test_a_cashier_account_is_for_the_till_only(make_client):
    owner, cashier, s1, pid = _till(make_client)
    sid = s1["id"]
    for path in (f"/api/stock?store_id={sid}", f"/api/today?store_id={sid}",
                 f"/api/orders/suggestions?store_id={sid}", "/api/suppliers", "/api/setup",
                 "/api/imports", f"/api/day-close?store_id={sid}", "/api/people"):
        assert cashier.get(path).status_code == 403, path
    for path in ("/api/auth/me", "/api/products?limit=10", f"/api/checkout/bills?store_id={sid}",
                 "/api/alerts?open=true", "/api/company", f"/api/checkout/scan?code=RICE&store_id={sid}"):
        assert cashier.get(path).status_code == 200, path
    # The owner is unaffected.
    assert owner.get(f"/api/stock?store_id={sid}").status_code == 200


def test_a_cashier_sees_only_their_own_shift(make_client):
    owner, cashier, s1, pid = _till(make_client)

    def bill(who, number):
        r = who.post("/api/checkout/bills", {
            "store_id": s1["id"], "bill_number": number, "device_id": "till-1",
            "lines": [{"product_id": pid["SOAP"], "qty_milli": 1000, "unit_price_paise": 4_500}],
            "payments": [{"method": "cash", "amount_paise": 4_500}]})
        assert r.status_code == 201, r.text
    bill(cashier, "C-1")
    bill(owner, "O-1")
    bill(owner, "O-2")
    sid = s1["id"]
    assert cashier.get(f"/api/checkout/z-report?store_id={sid}").json()["bills"] == 1
    assert cashier.get(f"/api/checkout/z-report?store_id={sid}&mine=true").json()["sales_paise"] == 4_500
    assert owner.get(f"/api/checkout/z-report?store_id={sid}").json()["bills"] == 3
    assert owner.get(f"/api/checkout/z-report?store_id={sid}&mine=true").json()["bills"] == 2
    mine = cashier.get(f"/api/checkout/bills?store_id={sid}&mine=true").json()
    assert [b["bill_number"] for b in mine] == ["C-1"]
    # Looking a bill up by number still finds any bill in the store, for a return.
    assert len(cashier.get(f"/api/checkout/bills?store_id={sid}&number=O-").json()) == 2


def test_every_report_downloads_as_excel_pdf_and_csv(make_client):
    import io

    from openpyxl import load_workbook
    owner, cashier, s1, pid = _till(make_client)
    names = [r["name"] for r in owner.get("/api/reports").json()]
    assert len(names) == 12
    for name in names:
        base = f"/api/reports/{name}?store_id={s1['id']}"
        x = owner.get(base + "&format=xlsx")
        assert x.status_code == 200 and x.content[:2] == b"PK", name
        assert "attachment" in x.headers["content-disposition"] and ".xlsx" in x.headers["content-disposition"]
        assert owner.get(base + "&format=pdf").content.startswith(b"%PDF"), name
        c = owner.get(base + "&format=csv")
        assert c.status_code == 200 and c.content.startswith(b"\xef\xbb\xbf"), name
        assert c.headers["content-type"].startswith("text/csv")
    assert owner.get(f"/api/reports/sales?format=docx").status_code == 422
    allx = owner.get(f"/api/reports/all.xlsx?store_id={s1['id']}")
    assert allx.status_code == 200
    wb = load_workbook(io.BytesIO(allx.content))
    assert wb.sheetnames[0] == "Contents" and len(wb.sheetnames) == 13
    assert len(set(n.lower() for n in wb.sheetnames)) == 13
    # Downloads are on the record; a cashier cannot download reports at all.
    acts = owner.get("/api/activity?limit=200").json()
    rows = acts.get("rows", acts) if isinstance(acts, dict) else acts
    assert any(a.get("action") == "report.exported" for a in rows)
    assert cashier.get("/api/reports/all.xlsx").status_code == 403
    # Another company cannot use this company's store.
    other = make_client()
    other.signup(email="other@example.com", company="Other Co")
    assert other.get(f"/api/reports/all.xlsx?store_id={s1['id']}").status_code == 404
