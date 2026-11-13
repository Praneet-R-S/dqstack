"""One-click sign-in to the demonstration company from the sign-in page."""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import select

from dqstack.app import config as config_mod
from dqstack.app import db as dbmod
from dqstack.app.models import Company, Person
from dqstack.app.routes import auth as auth_routes

from .conftest import invite_and_join


def _set(monkeypatch, **kw):
    s = replace(config_mod.settings, **kw)
    monkeypatch.setattr(config_mod, "settings", s)
    monkeypatch.setattr(auth_routes, "settings", s)


def _make_demo(client, make_client, name="Demo Mart"):
    client.signup(company=name, name="Ravi Owner", email="ravi@real-owner.example")
    with dbmod.SessionLocal() as db:
        co = db.scalar(select(Company).where(Company.name == name))
        co.plan = "demo"
        db.commit()
    store = client.post("/api/stores", {"code": "PUN1", "name": "Pune", "state": "Maharashtra"})
    assert store.status_code == 201, store.text
    invite_and_join(client, make_client(), "Meera Manager", "store_manager",
                    store_ids=[store.json()["id"]])
    return store.json()


def test_off_unless_turned_on(client, make_client):
    _make_demo(client, make_client)
    assert config_mod.settings.demo_logins is False          # DQ_ENV=test
    r = make_client().get("/api/auth/demo")
    assert r.status_code == 200 and r.json()["accounts"] == []
    with dbmod.SessionLocal() as db:
        pid = db.scalar(select(Person.id).where(Person.name == "Ravi Owner"))
    assert make_client().post("/api/auth/demo", {"person_id": pid}).status_code == 404


def test_lists_people_without_emails_and_signs_in(monkeypatch, client, make_client):
    _set(monkeypatch, demo_logins=True, env="development")
    _make_demo(client, make_client)
    visitor = make_client()
    d = visitor.get("/api/auth/demo").json()
    assert d["company"] == "Demo Mart"
    assert [a["role"] for a in d["accounts"]] == ["owner", "store_manager"]
    assert d["accounts"][1]["stores"] == ["Pune"]
    assert "@" not in str(d), "the sign-in page must not show anyone's email"

    r = visitor.post("/api/auth/demo", {"person_id": d["accounts"][1]["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["person"]["name"] == "Meera Manager"
    assert visitor.get("/api/auth/me").status_code == 200
    # In development nothing is locked.
    assert r.json()["can"]["people.manage"] is False      # a store manager never could
    owner = make_client()
    r = owner.post("/api/auth/demo", {"person_id": d["accounts"][0]["id"]})
    assert r.json()["can"]["people.manage"] is True


def test_only_the_demo_company(monkeypatch, client, make_client):
    _set(monkeypatch, demo_logins=True, env="development")
    _make_demo(client, make_client)
    real = make_client()
    real.signup(company="Real Shop", name="Priya Real", email="priya@realshop.example")
    with dbmod.SessionLocal() as db:
        pid = db.scalar(select(Person.id).where(Person.name == "Priya Real"))
    r = make_client().post("/api/auth/demo", {"person_id": pid})
    assert r.status_code == 404
    assert "Priya Real" not in str(make_client().get("/api/auth/demo").json())


def test_public_demo_cannot_reach_outside(monkeypatch, client, make_client):
    """On a public server (not development) the demo's people cannot invite, change
    roles, company settings or connections: those reach real addresses and systems."""
    _make_demo(client, make_client)
    _set(monkeypatch, demo_logins=True, env="production")
    d = make_client().get("/api/auth/demo").json()
    visitor = make_client()
    me = visitor.post("/api/auth/demo", {"person_id": d["accounts"][0]["id"]}).json()
    assert me["person"]["role"] == "owner"
    for action in ("people.manage", "company.settings", "company.billing",
                   "integrations.manage"):
        assert me["can"][action] is False, action
    assert me["can"]["orders.approve"] is True
    r = visitor.post("/api/people/invite", {"name": "Spam", "email": "victim@example.com",
                                            "role": "viewer", "store_ids": []})
    assert r.status_code == 403


@pytest.mark.parametrize("pid", ["nope", "x" * 64])
def test_unknown_person(monkeypatch, client, make_client, pid):
    _set(monkeypatch, demo_logins=True, env="development")
    _make_demo(client, make_client)
    assert make_client().post("/api/auth/demo", {"person_id": pid}).status_code == 404
