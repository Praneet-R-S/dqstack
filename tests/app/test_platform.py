"""The vendor's platform portal, support access, and a client leaving (sections 2, 13).

The platform administrator sees companies' metadata, never their records, unless the
client's Owner grants time-limited support access; a support session is read-only
and logged in the client's own activity log. A client that leaves gets a full export
and, after a confirmed period, everything is deleted -- including the append-only
ledger and log, which only a purge holding that company's permit may delete.
"""

from __future__ import annotations

import io
import zipfile
from datetime import timedelta

from sqlalchemy import func, select

from dqstack.app import db as dbmod
from dqstack.app import ledger, worker
from dqstack.app.db import utcnow
from dqstack.app.models import ActivityLog, Company, Person, Product, StockMovement
from dqstack.app.security import hash_password


def _vendor(make_client):
    with dbmod.SessionLocal() as db:
        db.add(Person(company_id=None, name="Ops Desk", email="ops@vendor.example", role="admin",
                      status="active", is_platform_admin=True,
                      password_hash=hash_password("vendor password 1")))
        db.commit()
    v = make_client()
    r = v.post("/api/platform/login", {"login": "ops@vendor.example", "password": "vendor password 1"})
    assert r.status_code == 200, r.text
    v.csrf = r.json()["csrf"]
    return v


def _client_company(make_client, name="Alpha Mart", email="a@alpha.example"):
    owner = make_client()
    owner.signup(company=name, email=email)
    s = owner.post("/api/stores", {"code": "S1", "name": "One"}).json()
    owner.import_file("products", "SKU,Name,Unit,Cost\nP1,Rice,each,50\n")
    with dbmod.SessionLocal() as db:
        pid = db.scalar(select(Product.id).where(Product.company_id == owner.me["company"]["id"]))
        ledger.post(db, company_id=owner.me["company"]["id"], store_id=s["id"], product_id=pid,
                    qty_milli=5000, type="opening")
        db.commit()
    return owner, s


def test_platform_sees_metadata_and_creates_companies(make_client):
    owner, s = _client_company(make_client)
    v = _vendor(make_client)
    assert make_client().login("ops@vendor.example", "vendor password 1").status_code == 403
    rows = v.get("/api/platform/companies").json()
    alpha = next(r for r in rows if r["name"] == "Alpha Mart")
    assert (alpha["stores"], alpha["products"]) == (1, 1) and alpha["support_granted_until"] is None
    # No company records through the vendor's own session.
    assert v.get("/api/stores").status_code == 403
    made = v.post("/api/platform/companies", {"name": "Beta Stores", "owner_name": "Bina",
                                              "owner_email": "bina@beta.example"}).json()
    assert made["invite_link"].startswith("http") and made["delivered_by"] == "email"
    assert owner.get("/api/stores").status_code == 200
    v.patch(f"/api/platform/companies/{alpha['id']}", {"status": "suspended"})
    r = owner.get("/api/stores")
    assert r.status_code == 403 and "paused" in r.json()["error"]
    v.patch(f"/api/platform/companies/{alpha['id']}", {"status": "active"})
    assert owner.get("/api/stores").status_code == 200
    assert owner.get("/api/platform/companies").status_code == 403
    health = v.get("/api/platform/health").json()
    assert health["database"] == "ok" and health["companies"] == 2


def test_support_access_only_when_granted_and_read_only(make_client):
    owner, s = _client_company(make_client)
    cid = owner.me["company"]["id"]
    v = _vendor(make_client)
    assert v.post(f"/api/platform/companies/{cid}/support").status_code == 403
    g = owner.post("/api/company/support-grants", {"reason": "Imports failing", "hours": 2}).json()
    started = v.post(f"/api/platform/companies/{cid}/support")
    assert started.status_code == 200 and started.json()["read_only"] is True
    me = v.get("/api/auth/me").json()
    v.csrf = me["csrf"]
    assert me["person"]["role"] == "viewer" and [x["code"] for x in me["stores"]] == ["S1"]
    assert v.get("/api/stock?store_id=" + s["id"]).status_code == 200
    assert v.post("/api/stores", {"code": "X", "name": "X"}).status_code == 403
    log = owner.get("/api/activity?limit=10").json()
    assert any(e["action"] == "support.started" for e in log)
    owner.post(f"/api/company/support-grants/{g['id']}/revoke")
    r = v.get("/api/stores")
    assert r.status_code == 403 and "ended" in r.json()["error"]


def test_a_departing_client_exports_everything(make_client):
    owner, s = _client_company(make_client)
    r = owner.get("/api/company/export")
    assert r.status_code == 200
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(z.namelist())
    assert {"products.csv", "stock_movements.csv", "people.csv", "main-lists.xlsx"} <= names
    people = z.read("people.csv").decode("utf-8-sig").splitlines()[0]
    assert "password_hash" not in people and "email" in people
    assert "Rice" in z.read("products.csv").decode("utf-8-sig")


def test_deletion_after_the_confirmed_period(make_client):
    owner, s = _client_company(make_client)
    other, _ = _client_company(make_client, "Beta Stores", "b@beta.example")
    cid = owner.me["company"]["id"]
    assert owner.post("/api/company/deletion", {"confirm_name": "wrong"}).status_code == 422
    r = owner.post("/api/company/deletion", {"confirm_name": "Alpha Mart"}).json()
    assert r["status"] == "closing" and "Export your data" in r["message"]
    v = _vendor(make_client)
    assert v.post(f"/api/platform/companies/{cid}/purge").status_code == 409   # not due yet
    with dbmod.SessionLocal() as db:
        co = db.get(Company, cid)
        co.setup = {**co.setup, "deletion_due": (utcnow() - timedelta(days=1)).isoformat()}
        db.commit()
        worker._purge_due(db, utcnow())
    with dbmod.SessionLocal() as db:
        assert db.get(Company, cid) is None
        for model in (StockMovement, ActivityLog, Product, Person):
            assert db.scalar(select(func.count()).select_from(model).where(
                model.company_id == cid)) == 0, model.__name__
        assert db.scalar(select(func.count()).select_from(StockMovement)) == 1   # Beta's stays
    assert other.get("/api/stores").status_code == 200
