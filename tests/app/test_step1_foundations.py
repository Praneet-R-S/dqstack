"""Build step 1 -- Foundations.

Done when: two test companies exist; a user of one cannot read the other by changing
any id; all existing tests pass. Plus the section 15 sign-in targets that belong to
this step: hashed passwords, lockout after 5 failures, expiring sessions, roles
checked on the server, and an append-only activity log.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from dqstack.app import db as dbmod
from dqstack.app.models import ActivityLog, AuthSession, ImmutableRecordError, Person

from .conftest import invite_and_join


def _two_companies(make_client):
    a, b = make_client(), make_client()
    a.signup(company="Alpha Mart", name="Anil Alpha", email="anil@alpha.example")
    b.signup(company="Beta Stores", name="Bina Beta", email="bina@beta.example")
    sa = a.post("/api/stores", {"code": "A1", "name": "Alpha Andheri", "state": "Maharashtra"}).json()
    sb = b.post("/api/stores", {"code": "B1", "name": "Beta Banjara", "state": "Telangana"}).json()
    return a, b, sa, sb


def test_two_companies_exist_and_each_sees_only_its_own(make_client):
    a, b, sa, sb = _two_companies(make_client)
    assert [s["code"] for s in a.get("/api/stores").json()] == ["A1"]
    assert [s["code"] for s in b.get("/api/stores").json()] == ["B1"]
    assert a.get("/api/company").json()["name"] == "Alpha Mart"
    assert b.get("/api/company").json()["name"] == "Beta Stores"


def test_a_user_of_one_company_cannot_read_the_other_by_changing_ids(make_client):
    """Acceptance scenario 17: company B's records answer 'not found' to company A --
    the same answer as for an id that does not exist, so nothing is confirmed."""
    a, b, sa, sb = _two_companies(make_client)
    b_owner_id = b.me["person"]["id"]
    # Read attempts
    for url in (f"/api/stores/{sb['id']}", f"/api/people/{b_owner_id}"):
        r = a.get(url)
        assert r.status_code == 404, (url, r.status_code, r.text)
        assert "not found" in r.json()["error"]
    # Write attempts by id must fail the same way and change nothing
    assert a.patch(f"/api/stores/{sb['id']}", {"name": "hijacked"}).status_code == 404
    assert a.post(f"/api/people/{b_owner_id}/disable").status_code == 404
    assert b.get(f"/api/stores/{sb['id']}").json()["name"] == "Beta Banjara"
    assert b.get("/api/auth/me").status_code == 200
    # Inviting someone into a store of the other company is refused as unknown
    r = a.post("/api/people/invite", {"name": "Spy", "email": "spy@x.example",
                                      "role": "store_manager", "store_ids": [sb["id"]]})
    assert r.status_code == 404


def test_store_level_access_for_assigned_roles(make_client):
    owner = make_client()
    owner.signup()
    s1 = owner.post("/api/stores", {"code": "S1", "name": "One"}).json()
    s2 = owner.post("/api/stores", {"code": "S2", "name": "Two"}).json()
    mgr = make_client()
    invite_and_join(owner, mgr, "Meera Manager", "store_manager", [s1["id"]])
    assert [s["code"] for s in mgr.get("/api/stores").json()] == ["S1"]
    assert mgr.get(f"/api/stores/{s2['id']}").status_code == 404
    assert mgr.get(f"/api/stores/{s1['id']}").status_code == 200


def test_roles_are_checked_on_the_server_not_only_in_the_browser(make_client):
    """Acceptance scenario 16, the part this step can prove: a Viewer sending requests
    straight to the API is refused."""
    owner = make_client()
    owner.signup()
    s1 = owner.post("/api/stores", {"code": "S1", "name": "One"}).json()
    viewer = make_client()
    invite_and_join(owner, viewer, "Vikram Viewer", "viewer", [s1["id"]])
    assert viewer.get("/api/stores").status_code == 200            # can read
    assert viewer.post("/api/stores", {"code": "X", "name": "X"}).status_code == 403
    assert viewer.patch(f"/api/stores/{s1['id']}", {"name": "x"}).status_code == 403
    assert viewer.get("/api/people").status_code == 403
    assert viewer.patch("/api/company", {"name": "taken over"}).status_code == 403
    assert owner.get("/api/company").json()["name"] == "Acme Retail"


def test_state_changes_need_the_csrf_token(client):
    client.signup()
    token = client.csrf
    client.csrf = None
    r = client.post("/api/stores", {"code": "S1", "name": "One"})
    assert r.status_code == 403
    client.csrf = "wrong-token"
    assert client.post("/api/stores", {"code": "S1", "name": "One"}).status_code == 403
    client.csrf = token
    assert client.post("/api/stores", {"code": "S1", "name": "One"}).status_code == 201


def test_passwords_are_hashed_never_stored_plain(client):
    client.signup(email="plain@test.example", password="correct horse battery")
    with dbmod.SessionLocal() as db:
        p = db.scalar(select(Person).where(Person.email == "plain@test.example"))
        assert p.password_hash and "correct horse" not in p.password_hash
        assert p.password_hash.startswith("$argon2")


def test_lockout_after_five_failures_and_unlock(make_client):
    owner = make_client()
    owner.signup(email="lock@test.example")
    c = make_client()
    for _ in range(5):
        r = c.login("lock@test.example", "wrong password!!")
        assert r.status_code == 401
    r = c.login("lock@test.example")                       # right password, still locked
    assert r.status_code == 401 and "locked" in r.json()["error"]
    # An unknown account gets the same message as a wrong password.
    assert c.login("nobody@test.example", "whatever pw").json()["error"] == \
        "That email or phone and password do not match."


def test_lock_expires_after_fifteen_minutes(make_client):
    owner = make_client()
    owner.signup(email="expire@test.example")
    c = make_client()
    for _ in range(5):
        c.login("expire@test.example", "wrong password!!")
    with dbmod.SessionLocal() as db:
        p = db.scalar(select(Person).where(Person.email == "expire@test.example"))
        p.locked_until = p.locked_until - timedelta(minutes=16)
        db.commit()
    assert c.login("expire@test.example").status_code == 200


def test_sessions_expire(client):
    client.signup()
    with dbmod.SessionLocal() as db:
        for s in db.scalars(select(AuthSession)):
            s.expires_at = s.expires_at - timedelta(days=2)
        db.commit()
    assert client.get("/api/auth/me").status_code == 401


def test_disabled_person_is_signed_out_immediately(make_client):
    """Acceptance scenario 18: a disabled employee is signed out on every device, and
    their past actions stay in the log."""
    owner = make_client()
    owner.signup()
    s1 = owner.post("/api/stores", {"code": "S1", "name": "One"}).json()
    clerk_phone, clerk_laptop = make_client(), make_client()
    info = invite_and_join(owner, clerk_phone, "Chetan Clerk", "stock_clerk", [s1["id"]],
                           email="chetan@test.example")
    clerk_laptop.login("chetan@test.example", "a long enough password")
    assert clerk_phone.get("/api/auth/me").status_code == 200
    assert clerk_laptop.get("/api/auth/me").status_code == 200
    assert owner.post(f"/api/people/{info['id']}/disable").status_code == 200
    assert clerk_phone.get("/api/auth/me").status_code == 401
    assert clerk_laptop.get("/api/auth/me").status_code == 401
    log = owner.get("/api/activity?limit=50").json()
    assert any(e["action"] == "person.joined" and e["person"] == "Chetan Clerk" for e in log)


def test_activity_log_is_append_only(client):
    client.signup()
    client.post("/api/stores", {"code": "S1", "name": "One"})
    with dbmod.SessionLocal() as db:
        entry = db.scalars(select(ActivityLog)).first()
        entry.action = "rewritten"
        with pytest.raises(ImmutableRecordError):
            db.commit()
        db.rollback()
        entry = db.scalars(select(ActivityLog)).first()
        db.delete(entry)
        with pytest.raises(ImmutableRecordError):
            db.commit()


def test_errors_are_plain_language_never_a_stack_trace(client):
    r = client.get("/api/stores")
    assert r.status_code == 401 and r.json() == {"error": "Please sign in to continue."}
    r = client.post("/api/auth/signup", {"company_name": "x"})
    assert r.status_code == 422 and "problems" in r.json() and "Traceback" not in r.text


def test_production_refuses_unsafe_settings(monkeypatch):
    from dqstack.app import config
    monkeypatch.setenv("DQ_ENV", "production")
    monkeypatch.delenv("DQ_SECRET_KEY", raising=False)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///x.db")
    with pytest.raises(RuntimeError) as e:
        config.load()
    msg = str(e.value)
    assert "DQ_SECRET_KEY" in msg and "PostgreSQL" in msg
    monkeypatch.setenv("DQ_SECRET_KEY", "x" * 48)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db/dq")
    monkeypatch.setenv("DQ_PUBLIC_URL", "https://dq.example.in")
    s = config.load()
    assert s.cookie_secure and s.env == "production"


def test_security_headers_present(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["ok"] is True
    for h in ("X-Content-Type-Options", "X-Frame-Options", "Referrer-Policy"):
        assert h in r.headers
