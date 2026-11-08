"""Fixtures for the store-operations application.

Every test gets a brand-new in-memory database, so tests cannot leak state into one
another and never touch a developer's data. `Client` wraps FastAPI's test client and
carries the CSRF token the way the real browser code does, so tests exercise the
same protection production enforces rather than switching it off.
"""

from __future__ import annotations

import os

os.environ.setdefault("DQ_ENV", "test")

import pytest
from fastapi.testclient import TestClient

from dqstack.app import db as dbmod
from dqstack.app.mailer import outbox
from dqstack.app.ratelimit import limiter


class Client:
    def __init__(self, app):
        self.http = TestClient(app)
        self.csrf: str | None = None
        self.me: dict | None = None

    def _h(self, extra=None):
        h = {}
        if self.csrf:
            h["X-CSRF-Token"] = self.csrf
        h.update(extra or {})
        return h

    def _remember(self, r):
        if r.status_code < 300 and r.headers.get("content-type", "").startswith("application/json"):
            body = r.json()
            if isinstance(body, dict) and body.get("csrf"):
                self.csrf = body["csrf"]
                self.me = body
        return r

    def get(self, url, **kw):
        return self.http.get(url, headers=self._h(kw.pop("headers", None)), **kw)

    def post(self, url, json=None, **kw):
        return self._remember(self.http.post(url, json=json, headers=self._h(kw.pop("headers", None)), **kw))

    def patch(self, url, json=None, **kw):
        return self.http.patch(url, json=json, headers=self._h(kw.pop("headers", None)), **kw)

    def delete(self, url, **kw):
        return self.http.delete(url, headers=self._h(kw.pop("headers", None)), **kw)

    def put(self, url, json=None, **kw):
        return self.http.put(url, json=json, headers=self._h(kw.pop("headers", None)), **kw)

    def upload(self, kind: str, content: bytes | str, filename: str = "upload.csv"):
        if isinstance(content, str):
            content = content.encode("utf-8")
        return self.http.post("/api/imports", data={"kind": kind},
                              files={"file": (filename, content, "application/octet-stream")},
                              headers=self._h())

    def import_file(self, kind: str, content, filename: str = "upload.csv") -> dict:
        """Upload, expect a clean check, commit; return the committed job."""
        r = self.upload(kind, content, filename)
        assert r.status_code == 201, r.text
        job = r.json()
        assert job["status"] == "checked", job
        r = self.post(f"/api/imports/{job['id']}/commit")
        assert r.status_code == 200, r.text
        return r.json()

    # -------------------------------------------------------------- helpers
    def signup(self, company="Acme Retail", name="Asha Owner", email=None,
               password="correct horse battery"):
        email = email or f"{name.split()[0].lower()}@{company.split()[0].lower()}.example"
        r = self.post("/api/auth/signup", {"company_name": company, "name": name,
                                           "email": email, "password": password})
        assert r.status_code == 200, r.text
        return r.json()

    def login(self, login, password="correct horse battery"):
        return self.post("/api/auth/login", {"login": login, "password": password})


# DQ_APP_TESTS_ON_POSTGRES=1 with DQ_TEST_POSTGRES_URL runs every app test against
# PostgreSQL instead of in-memory SQLite (CI does both). The schema is wiped before
# each test, so the URL must name a database used for nothing else.
PG_URL = os.environ.get("DQ_TEST_POSTGRES_URL")
ON_PG = bool(PG_URL) and os.environ.get("DQ_APP_TESTS_ON_POSTGRES") == "1"


def reset_postgres(url: str) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    name = make_url(url).database or ""
    if "test" not in name:
        raise RuntimeError(f"Refusing to wipe database {name!r}: its name must contain 'test'.")
    eng = create_engine(url, future=True)
    with eng.begin() as conn:
        conn.exec_driver_sql("DROP SCHEMA public CASCADE")
        conn.exec_driver_sql("CREATE SCHEMA public")
    eng.dispose()


@pytest.fixture(autouse=True)
def fresh_db():
    if ON_PG:
        reset_postgres(PG_URL)
        dbmod.configure(PG_URL, pool_size=2, max_overflow=4)
    else:
        dbmod.configure("sqlite://")
    dbmod.create_all()
    outbox.clear()
    limiter.reset()
    yield
    dbmod.engine().dispose()


@pytest.fixture
def app():
    from dqstack.app.main import create_app
    return create_app()


@pytest.fixture
def client(app):
    return Client(app)


@pytest.fixture
def make_client(app):
    return lambda: Client(app)


def invite_and_join(owner: Client, new: Client, name: str, role: str, store_ids=(),
                    email=None, password="a long enough password"):
    """Invite someone as `owner`, then accept the invitation as `new`."""
    email = email or f"{name.split()[0].lower()}.{role}@test.example"
    r = owner.post("/api/people/invite", {"name": name, "email": email, "role": role,
                                          "store_ids": list(store_ids)})
    assert r.status_code == 201, r.text
    token = r.json()["invite_link"].rsplit("/", 1)[-1]
    r2 = new.post("/api/auth/invitation/accept", {"token": token, "password": password})
    assert r2.status_code == 200, r2.text
    return r.json()
