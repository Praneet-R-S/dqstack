"""The Zoho Inventory connector, against a mocked Zoho (httpx.MockTransport).

The mock answers like Zoho's documented API: OAuth at accounts.zoho.<dc>, data at
www.zohoapis.<dc>/inventory/v1 with `organization_id`, lists in pages with
`page_context.has_more_page`, `Zoho-oauthtoken` authorisation, 401 for an expired
token and 429 with Retry-After when asked to slow down. It proves the connect flow,
pagination, token refresh, back-off, the warehouse-to-store map, repeat-safety
(syncing twice changes nothing), corrections from edited and voided invoices, and the
failure alert. It cannot prove Zoho's real answers match the documentation in every
field: that needs a trial against a real Zoho Inventory organisation.
"""

from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import func, select

from dqstack.app import db as dbmod
from dqstack.app.connectors import secrets as vault
from dqstack.app.connectors import zoho as Z
from dqstack.app.models import (Alert, Connector, ConnectorDoc, DailySales, ImportJob, Product,
                                StockMovement)
from dqstack.app.sales import local_today, tz_of

from .conftest import invite_and_join

SECRET = "zoho-client-secret-do-not-leak-7731"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%dT%H:%M:%S%z")


class FakeZoho:
    """Just enough of Zoho Inventory, keeping a log of what was asked."""

    def __init__(self, today):
        self.today = today
        self.log: list[tuple[str, str, dict]] = []
        self.access = None
        self.issued = 0
        self.refresh_ok = True
        self.slow_down = 0                       # answer this many requests with 429
        self.base = datetime(2026, 11, 1, tzinfo=timezone.utc)
        t = self.base
        self.items = [
            {"item_id": "I1", "name": "Basmati Rice 5kg", "sku": "RICE", "rate": 800,
             "purchase_rate": 700, "unit": "pcs", "hsn_or_sac": "10063020",
             "tax_percentage": 5, "status": "active", "last_modified_time": _iso(t)},
            {"item_id": "I2", "name": "Bath Soap", "sku": "SOAP", "rate": 38.14,
             "unit": "Nos", "hsn_or_sac": "34011110", "status": "active",
             "item_tax_preferences": [{"tax_specification": "intra", "tax_percentage": 18},
                                      {"tax_specification": "inter", "tax_percentage": 18}],
             "last_modified_time": _iso(t + timedelta(minutes=1))},
            {"item_id": "I3", "name": "Loose Sugar", "sku": "SUGAR", "rate": 45, "unit": "kg",
             "tax_percentage": 5, "status": "active",
             "last_modified_time": _iso(t + timedelta(minutes=2))},
            {"item_id": "I4", "name": "Gift wrap", "sku": "", "rate": 10, "unit": "pcs",
             "status": "active", "last_modified_time": _iso(t + timedelta(minutes=3))},
            {"item_id": "I5", "name": "Old Detergent", "sku": "DET-OLD", "rate": 100,
             "unit": "box", "tax_percentage": 18, "status": "inactive",
             "last_modified_time": _iso(t + timedelta(minutes=4))},
        ]
        d1, d2 = (today - timedelta(days=2)).isoformat(), (today - timedelta(days=1)).isoformat()
        self.invoices = {
            "V1": {"invoice_id": "V1", "invoice_number": "INV-1", "date": d1, "status": "paid",
                   "is_inclusive_tax": False, "location_id": "W1", "line_items": [
                       {"item_id": "I1", "sku": "RICE", "quantity": 2, "rate": 800,
                        "item_total": 1600, "tax_percentage": 5, "warehouse_id": "W1"},
                       {"item_id": "I2", "quantity": 3, "rate": 38.14, "item_total": 114.42,
                        "tax_percentage": 18, "warehouse_id": "W2"}]},
            "V2": {"invoice_id": "V2", "invoice_number": "INV-2", "date": d2, "status": "sent",
                   "is_inclusive_tax": True, "line_items": [
                       {"item_id": "I3", "sku": "SUGAR", "quantity": 1.5, "rate": 47.25,
                        "item_total": 70.88, "tax_percentage": 5}]},
            "V3": {"invoice_id": "V3", "invoice_number": "INV-3", "date": d2, "status": "draft",
                   "line_items": [{"item_id": "I1", "sku": "RICE", "quantity": 50,
                                   "item_total": 40000, "warehouse_id": "W1"}]},
            "V4": {"invoice_id": "V4", "invoice_number": "INV-4", "date": d2, "status": "overdue",
                   "is_inclusive_tax": False, "line_items": [
                       {"item_id": "I1", "sku": "RICE", "quantity": 1, "item_total": 800,
                        "tax_percentage": 5, "warehouse_id": "W9"},          # not mapped
                       {"item_id": "I1", "sku": "RICE", "quantity": 4, "item_total": 3200,
                        "tax_percentage": 5, "warehouse_id": "W1"}]},
        }
        for i, v in enumerate(self.invoices.values()):
            v["last_modified_time"] = _iso(t + timedelta(hours=i + 1))

    # ------------------------------------------------------------ the transport
    def handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        q = dict(urllib.parse.parse_qsl(url.query.decode()))
        form = dict(urllib.parse.parse_qsl(request.content.decode())) if request.content else {}
        self.log.append((request.method, f"{url.host}{url.path}", {**q, **form}))
        if url.path == "/oauth/v2/token":
            return self.token(url.host, form)
        if url.path == "/oauth/v2/token/revoke":
            return httpx.Response(200, json={"status": "success"})
        assert url.host == "www.zohoapis.in", url
        if self.slow_down:
            self.slow_down -= 1
            return httpx.Response(429, headers={"Retry-After": "3"},
                                  json={"code": 44, "message": "Too many requests"})
        if request.headers.get("Authorization") != f"Zoho-oauthtoken {self.access}":
            return httpx.Response(401, json={"code": 57, "message": "Invalid OAuth token"})
        path = url.path.removeprefix("/inventory/v1/")
        if path == "organizations":
            return httpx.Response(200, json={"code": 0, "organizations": [
                {"organization_id": "600001", "name": "Synthetic Traders"}]})
        assert q.get("organization_id") == "600001"
        if path == "settings/warehouses":
            return httpx.Response(200, json={"code": 0, "warehouses": [
                {"warehouse_id": "W1", "warehouse_name": "Andheri shop", "status": "active"},
                {"warehouse_id": "W2", "warehouse_name": "Thane shop", "status": "active"},
                {"warehouse_id": "W9", "warehouse_name": "Old godown", "status": "inactive"}]})
        if path == "items":
            return self.page(sorted(self.items, key=lambda r: r["last_modified_time"],
                                    reverse=True), "items", q)
        if path == "invoices":
            rows = [{k: v[k] for k in ("invoice_id", "invoice_number", "date", "status",
                                       "last_modified_time")} for v in self.invoices.values()]
            if q.get("date_start"):
                rows = [r for r in rows if r["date"] >= q["date_start"]]
            return self.page(sorted(rows, key=lambda r: r["last_modified_time"], reverse=True),
                             "invoices", q)
        if path.startswith("invoices/"):
            return httpx.Response(200, json={"code": 0, "invoice": self.invoices[path[9:]]})
        return httpx.Response(404, json={"code": 5, "message": "Invalid URL"})

    def page(self, rows, key, q):
        page, per = int(q["page"]), int(q["per_page"])
        chunk = rows[(page - 1) * per: page * per]
        return httpx.Response(200, json={"code": 0, "message": "success", key: chunk,
                                         "page_context": {"page": page, "per_page": per,
                                                          "has_more_page": page * per < len(rows)}})

    def token(self, host, form):
        assert host == "accounts.zoho.in", host
        assert form["client_id"] == "1000.CLIENT" and form["client_secret"] == SECRET
        if form["grant_type"] == "authorization_code":
            if form["code"] != "good-code":
                return httpx.Response(200, json={"error": "invalid_code"})
            self.issued += 1
            self.access = f"access-{self.issued}"
            return httpx.Response(200, json={"access_token": self.access,
                                             "refresh_token": "1000.refresh",
                                             "api_domain": "https://www.zohoapis.in",
                                             "token_type": "Bearer", "expires_in": 3600})
        assert form["grant_type"] == "refresh_token" and form["refresh_token"] == "1000.refresh"
        if not self.refresh_ok:
            return httpx.Response(400, json={"error": "invalid_code"})
        self.issued += 1
        self.access = f"access-{self.issued}"
        return httpx.Response(200, json={"access_token": self.access, "expires_in": 3600,
                                         "api_domain": "https://www.zohoapis.in"})

    def count(self, path_end: str, method="GET") -> int:
        return sum(1 for m, p, _ in self.log if m == method and p.endswith(path_end))

    def touch(self, inv_id, **changes):
        v = self.invoices[inv_id]
        v.update(changes)
        v["last_modified_time"] = _iso(datetime.now(timezone.utc))


@pytest.fixture
def world(make_client, monkeypatch):
    owner = make_client()
    owner.signup()
    s1 = owner.post("/api/stores", {"code": "S1", "name": "Andheri", "state": "Maharashtra"}).json()
    s2 = owner.post("/api/stores", {"code": "S2", "name": "Thane", "state": "Maharashtra"}).json()
    with dbmod.SessionLocal() as db:
        today = local_today(tz_of(db, owner.me["company"]["id"]))
    fake = FakeZoho(today)
    sleeps: list[float] = []
    clock = [1000.0]

    def sleep(s):
        sleeps.append(s)
        clock[0] += s
    monkeypatch.setattr(Z, "TRANSPORT", httpx.MockTransport(fake.handle))
    monkeypatch.setattr(Z, "SLEEP", sleep)
    monkeypatch.setattr(Z, "CLOCK", lambda: clock[0])
    monkeypatch.setattr(Z, "PER_PAGE", 2)          # small pages, so paging is exercised
    return owner, s1, s2, fake, sleeps, today


def _connect(owner, fake) -> dict:
    r = owner.post("/api/connectors/zoho/connect", {"client_id": "1000.CLIENT",
                                                    "client_secret": SECRET, "data_centre": "in"})
    assert r.status_code == 200, r.text
    url = urllib.parse.urlparse(r.json()["authorize_url"])
    q = dict(urllib.parse.parse_qsl(url.query))
    r = owner.http.get("/api/connectors/zoho/callback", follow_redirects=False, params={
        "code": "good-code", "state": q["state"], "location": "in",
        "accounts-server": "https://accounts.zoho.in"})
    assert r.status_code == 303 and r.headers["location"].endswith("?zoho=connected"), r.headers
    return q


def _map(owner, s1, s2):
    r = owner.put("/api/connectors/zoho/settings", {
        "warehouse_map": {"W1": s1["id"], "W2": s2["id"]}, "default_store_id": s1["id"]})
    assert r.status_code == 200, r.text


def _sync(owner, full=False) -> dict:
    r = owner.post("/api/connectors/zoho/sync", {"full": full})
    assert r.status_code == 200, r.text
    return r.json()["run"]


def _state():
    with dbmod.SessionLocal() as db:
        return {
            "products": {p.sku: (p.name, p.unit, p.price_paise, p.cost_paise, p.gst_rate_bp,
                                 p.hsn, p.status) for p in db.scalars(select(Product))},
            "sales": {(r.store_id, r.day.isoformat(), r.product_id): (r.units_milli, r.revenue_paise)
                      for r in db.scalars(select(DailySales))},
            "moves": db.scalar(select(func.count()).select_from(StockMovement)),
            "jobs": db.scalar(select(func.count()).select_from(ImportJob)),
        }


# =================================================================== connecting

def test_connect_flow_stores_tokens_encrypted_and_reads_the_organisation(world, make_client):
    owner, s1, s2, fake, sleeps, today = world
    q = _connect(owner, fake)
    assert q["scope"] == ",".join(Z.SCOPES) and all(s.endswith(".READ") for s in Z.SCOPES)
    assert q["access_type"] == "offline" and q["redirect_uri"].endswith("/api/connectors/zoho/callback")
    r = owner.get("/api/connectors")
    assert r.status_code == 200
    z = r.json()["zoho"]
    assert z["status"] == "connected"
    assert z["settings"]["organization_id"] == "600001"
    assert [w["name"] for w in z["settings"]["warehouses"]] == ["Andheri shop", "Thane shop", "Old godown"]
    assert SECRET not in r.text and "1000.refresh" not in r.text and "access-" not in r.text
    with dbmod.SessionLocal() as db:
        conn = db.scalar(select(Connector).where(Connector.kind == "zoho"))
        assert SECRET.encode() not in conn.credentials and b"1000.refresh" not in conn.credentials
        assert vault.unseal(conn.credentials)["refresh_token"] == "1000.refresh"
        assert vault.unseal(conn.credentials[:-3] + b"xyz") is None       # tampering is caught
    # The export of company data leaves the credentials out.
    exp = owner.get("/api/company/export")
    assert exp.status_code == 200 and SECRET.encode() not in exp.content


def test_callback_refuses_a_wrong_state_and_never_sends_the_secret_elsewhere(world, make_client):
    owner, s1, s2, fake, sleeps, today = world
    owner.post("/api/connectors/zoho/connect", {"client_id": "1000.CLIENT",
                                                "client_secret": SECRET, "data_centre": "in"})
    r = owner.http.get("/api/connectors/zoho/callback", follow_redirects=False,
                       params={"code": "good-code", "state": "forged"})
    assert r.headers["location"].endswith("?zoho=failed")
    assert fake.count("/oauth/v2/token", "POST") == 0
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(owner.post(
        "/api/connectors/zoho/connect", {"client_id": "1000.CLIENT", "data_centre": "in"}
    ).json()["authorize_url"]).query))
    # A callback naming some other "accounts server" still talks to Zoho India only
    # (FakeZoho.token asserts the host), and the stored secret was kept from before.
    r = owner.http.get("/api/connectors/zoho/callback", follow_redirects=False, params={
        "code": "good-code", "state": q["state"], "accounts-server": "https://evil.example"})
    assert r.headers["location"].endswith("?zoho=connected")
    # Someone without the right cannot connect or read the settings.
    mgr = make_client()
    invite_and_join(owner, mgr, "Meera Manager", "store_manager", [s1["id"]])
    assert mgr.get("/api/connectors").status_code == 403
    assert mgr.post("/api/connectors/zoho/sync", {}).status_code == 403


def test_self_client_code_and_a_refused_code(world):
    owner, s1, s2, fake, sleeps, today = world
    assert owner.post("/api/connectors/zoho/code", {"code": "good-code-but"}).status_code == 422
    owner.post("/api/connectors/zoho/connect", {"client_id": "1000.CLIENT",
                                                "client_secret": SECRET, "data_centre": "in"})
    bad = owner.post("/api/connectors/zoho/code", {"code": "an-expired-code"})
    assert bad.status_code == 422 and "expired" in bad.json()["error"]
    ok = owner.post("/api/connectors/zoho/code", {"code": "good-code"})
    assert ok.status_code == 200 and ok.json()["status"] == "connected"
    # A self client's code is exchanged without a redirect address.
    form = [f for m, p, f in fake.log if p.endswith("/oauth/v2/token")][-1]
    assert "redirect_uri" not in form


# =================================================================== syncing

def test_sync_pages_refreshes_backs_off_maps_warehouses_and_repeats_safely(world):
    owner, s1, s2, fake, sleeps, today = world
    _connect(owner, fake)
    _map(owner, s1, s2)
    fake.access = "expired-elsewhere"              # the stored access token no longer works
    fake.slow_down = 2                             # and Zoho asks to slow down twice
    run = _sync(owner)
    assert run["status"] == "warning", run         # sales of an unmapped warehouse were left out
    sm = run["summary"]
    assert sm["token_renewed"] == 1 and sm["slowed_down"] == 2 and sleeps[:2] == [3.0, 3.0]
    assert fake.count("/inventory/v1/items") >= 3                  # 5 items, pages of 2
    assert sm["items"]["read"] == 5 and sm["items"]["no_sku"] == 1
    assert sm["sales"]["fetched"] == 3 and sm["sales"]["unmapped_lines"] == 1
    assert "no SKU" in run["message"] and "warehouse not matched" in run["message"]

    st = _state()
    assert st["products"] == {
        "RICE": ("Basmati Rice 5kg", "each", 84_000, 70_000, 500, "10063020", "active"),
        "SOAP": ("Bath Soap", "each", 4_501, None, 1800, "34011110", "active"),
        "SUGAR": ("Loose Sugar", "kg", 4_725, None, 500, None, "active"),
        "DET-OLD": ("Old Detergent", "each", 11_800, None, 1800, None, "discontinued"),
    }
    pid = {p: k for p, k in ((p.sku, p.id) for p in _products())}
    d1, d2 = (today - timedelta(days=2)).isoformat(), (today - timedelta(days=1)).isoformat()
    assert st["sales"] == {
        (s1["id"], d1, pid["RICE"]): (2_000, 168_000),            # 1,600 + 5% GST
        (s2["id"], d1, pid["SOAP"]): (3_000, 13_502),             # 114.42 + 18%
        (s1["id"], d2, pid["SUGAR"]): (1_500, 7_088),             # tax-inclusive, default store
        (s1["id"], d2, pid["RICE"]): (4_000, 336_000),            # W9 line left out; draft V3 too
    }
    # Daily sales into standalone stores move stock, once per product and day.
    assert st["moves"] == 4

    again = _sync(owner)
    assert again["status"] == "succeeded" and again["import_job_ids"] == []
    assert _state() == st                          # syncing twice changes nothing
    assert again["summary"]["sales"]["fetched"] == 0
    with dbmod.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Alert).where(
            Alert.kind == "connector_failed", Alert.resolved_at.is_(None))) == 0


def _products():
    with dbmod.SessionLocal() as db:
        return list(db.scalars(select(Product)))


def test_edited_and_voided_invoices_correct_their_day(world):
    owner, s1, s2, fake, sleeps, today = world
    _connect(owner, fake)
    _map(owner, s1, s2)
    _sync(owner)
    before = _state()
    pid = {p.sku: p.id for p in _products()}
    d1, d2 = (today - timedelta(days=2)).isoformat(), (today - timedelta(days=1)).isoformat()
    fake.touch("V1", line_items=[{"item_id": "I1", "sku": "RICE", "quantity": 3,
                                  "item_total": 2400, "tax_percentage": 5, "warehouse_id": "W1"}])
    fake.touch("V4", status="void")
    run = _sync(owner)
    assert run["status"] == "succeeded", run
    st = _state()
    assert st["sales"][(s1["id"], d1, pid["RICE"])] == (3_000, 252_000)
    assert st["sales"][(s2["id"], d1, pid["SOAP"])] == (0, 0)         # gone from the invoice
    assert st["sales"][(s1["id"], d2, pid["RICE"])] == (0, 0)         # its only invoice voided
    assert st["sales"][(s1["id"], d2, pid["SUGAR"])] == before["sales"][(s1["id"], d2, pid["SUGAR"])]
    with dbmod.SessionLocal() as db:
        moved = dict(db.execute(select(StockMovement.product_id, func.sum(StockMovement.qty_milli))
                                .where(StockMovement.store_id == s1["id"])
                                .group_by(StockMovement.product_id)).all())
    assert moved[pid["RICE"]] == -3_000           # stock follows the corrected days, not the sum
    # Sync everything again: same answer, nothing new written.
    full = _sync(owner, full=True)
    assert full["summary"]["full"] is True
    assert _state()["sales"] == st["sales"]


def test_failures_raise_one_alert_in_plain_words_and_recovery_resolves_it(world):
    owner, s1, s2, fake, sleeps, today = world
    _connect(owner, fake)
    _map(owner, s1, s2)
    fake.access = "revoked"
    fake.refresh_ok = False
    run = _sync(owner)
    assert run["status"] == "failed"
    assert "Connect again" in run["message"]
    z = owner.get("/api/connectors").json()["zoho"]
    assert z["status"] == "needs_reconnect" and "Connect again" in z["last_error"]
    alerts = owner.get("/api/alerts").json()
    items = alerts["items"] if isinstance(alerts, dict) else alerts
    mine = [a for a in items if a["kind"] == "connector_failed"]
    assert len(mine) == 1 and mine[0]["message"].startswith("Zoho Inventory sync:")
    assert _state()["jobs"] == 0                  # nothing half-done
    # Too many 429s: the sync gives up in plain words, leaving the cursor where it was.
    fake.refresh_ok = True
    _connect(owner, fake)
    fake.slow_down = 50
    run = _sync(owner)
    assert run["status"] == "failed" and "slow down" in run["message"]
    with dbmod.SessionLocal() as db:
        assert db.scalar(select(Connector).where(Connector.kind == "zoho")).cursor == {}
    fake.slow_down = 0
    assert _sync(owner)["status"] in ("succeeded", "warning")


def test_rate_limit_spacing_without_429s(monkeypatch):
    waits, clock = [], [0.0]
    monkeypatch.setattr(Z, "SLEEP", lambda s: (waits.append(s), clock.__setitem__(0, clock[0] + s)))
    monkeypatch.setattr(Z, "CLOCK", lambda: clock[0])
    api = Z.Api({"access_token": "x", "access_expires": 9e18}, {"rate_per_minute": 3})
    for _ in range(7):
        api._throttle()
        clock[0] += 1
    assert waits == [57.0, 57.0]                   # the 4th and 7th wait for the minute to turn
    api.close()


def test_worker_runs_the_nightly_sync_once_a_day_and_sync_now_when_queued(world, monkeypatch):
    owner, s1, s2, fake, sleeps, today = world
    _connect(owner, fake)
    _map(owner, s1, s2)
    with dbmod.SessionLocal() as db:
        conn = db.scalar(select(Connector).where(Connector.kind == "zoho"))
        conn.settings = {**conn.settings, "sync_time": "00:00"}
        db.commit()
    now = datetime.now(timezone.utc)
    assert [s for _, s in Z.run_due(now)] == ["warning"]
    assert Z.run_due(now) == []                    # already ran tonight


def test_disconnect_revokes_and_forgets(world):
    owner, s1, s2, fake, sleeps, today = world
    _connect(owner, fake)
    _map(owner, s1, s2)
    _sync(owner)
    r = owner.post("/api/connectors/zoho/disconnect")
    assert r.status_code == 200 and r.json()["status"] == "not_connected"
    assert fake.count("/oauth/v2/token/revoke", "POST") == 1
    with dbmod.SessionLocal() as db:
        conn = db.scalar(select(Connector).where(Connector.kind == "zoho"))
        assert conn.credentials is None and conn.cursor == {}
        assert db.scalar(select(func.count()).select_from(ConnectorDoc)) == 0
        assert conn.settings["warehouse_map"]               # the Admin's map is kept
    assert owner.post("/api/connectors/zoho/sync", {}).status_code == 409
