"""Build step 2 -- Company, stores, people.

Done when: a new company signs up, adds 3 stores and invites a manager who signs in.
Plus the rest of the step: the setup checklist, company and store settings, the
pre-filled calendar, invitations and password reset.
"""

from __future__ import annotations

import re
from datetime import date

from dqstack.app import festivals
from dqstack.app.mailer import outbox

from .conftest import invite_and_join


def _link(kind: str) -> str:
    """The token from the most recent email containing an /app/#/<kind>/ link."""
    for m in reversed(outbox):
        found = re.search(rf"/app/#/{kind}/(\S+)", m.body)
        if found:
            return found.group(1)
    raise AssertionError(f"no {kind} link was emailed")


def test_done_when_signup_three_stores_and_a_manager_who_signs_in(make_client):
    owner = make_client()
    owner.signup(company="Sharma Supermarkets", name="Ravi Sharma", email="ravi@sharma.example")
    stores = []
    for code, name, state in (("PUN1", "Pune Kothrud", "Maharashtra"),
                              ("PUN2", "Pune Baner", "Maharashtra"),
                              ("KOC1", "Kochi MG Road", "Kerala")):
        r = owner.post("/api/stores", {"code": code, "name": name, "state": state,
                                       "day_close_time": "22:00", "weekly_off": [6]})
        assert r.status_code == 201, r.text
        stores.append(r.json())
    assert len(owner.get("/api/stores").json()) == 3

    r = owner.post("/api/people/invite", {"name": "Meera Iyer", "email": "meera@sharma.example",
                                          "role": "store_manager", "store_ids": [stores[2]["id"]]})
    assert r.status_code == 201 and r.json()["delivered_by"] == "email"
    token = _link("invite")
    assert token == r.json()["invite_link"].rsplit("/", 1)[-1]

    manager = make_client()
    assert manager.get(f"/api/auth/invitation/{token}").json()["company"] == "Sharma Supermarkets"
    assert manager.post("/api/auth/invitation/accept",
                        {"token": token, "password": "kochi store 2026"}).status_code == 200
    manager.post("/api/auth/logout")
    # ...and signs in on their own, later, with the password they chose
    later = make_client()
    r = later.login("meera@sharma.example", "kochi store 2026")
    assert r.status_code == 200, r.text
    me = r.json()
    assert me["person"]["role"] == "store_manager"
    assert [s["code"] for s in me["stores"]] == ["KOC1"]


def test_checklist_follows_the_data(make_client):
    owner = make_client()
    owner.signup()
    first = owner.get("/api/setup").json()
    assert first["total"] == 13
    state = {s["key"]: s["state"] for s in first["steps"]}
    assert state["company"] == "done" and state["stores"] == "todo"
    assert not first["complete"]

    s1 = owner.post("/api/stores", {"code": "S1", "name": "One", "state": "Kerala"}).json()
    owner.post("/api/stores", {"code": "S2", "name": "Two", "state": "Goa"})
    owner.post("/api/people/invite", {"name": "Clerk", "email": "c@x.example",
                                      "role": "stock_clerk", "store_ids": [s1["id"]]})
    steps = {s["key"]: s for s in owner.get("/api/setup").json()["steps"]}
    assert steps["stores"]["state"] == "done" and steps["stores"]["detail"] == "2 stores"
    assert steps["people"]["state"] == "done"
    assert steps["products"]["route"] == "#/imports/new/products"


def test_manual_marks_only_where_the_spec_allows(make_client):
    owner = make_client()
    owner.signup()
    assert owner.post("/api/setup/suppliers", {"state": "later"}).status_code == 200
    assert owner.post("/api/setup/calendar", {"state": "done"}).status_code == 200
    assert owner.post("/api/setup/opening_stock", {"state": "unknown"}).status_code == 200
    steps = {s["key"]: s for s in owner.get("/api/setup").json()["steps"]}
    assert steps["suppliers"]["state"] == "later"
    assert steps["calendar"]["state"] == "done"
    assert steps["opening_stock"]["detail"] == "Starting with stock unknown"
    # A step that completes from its data cannot be ticked by hand...
    assert owner.post("/api/setup/products", {"state": "done"}).status_code == 409
    # ...nor marked in a way the spec does not allow...
    assert owner.post("/api/setup/calendar", {"state": "later"}).status_code == 422
    assert owner.post("/api/setup/nope", {"state": "done"}).status_code == 404
    # ...and reopening works.
    owner.post("/api/setup/calendar", {"state": "reopen"})
    steps = {s["key"]: s for s in owner.get("/api/setup").json()["steps"]}
    assert steps["calendar"]["state"] == "todo"


def test_only_owner_or_admin_mark_setup(make_client):
    owner = make_client()
    owner.signup()
    s1 = owner.post("/api/stores", {"code": "S1", "name": "One"}).json()
    mgr = make_client()
    invite_and_join(owner, mgr, "Mona Manager", "store_manager", [s1["id"]])
    assert mgr.get("/api/setup").status_code == 200
    assert mgr.post("/api/setup/calendar", {"state": "done"}).status_code == 403


def test_go_live_sets_each_stores_mode(make_client):
    owner = make_client()
    owner.signup()
    s1 = owner.post("/api/stores", {"code": "S1", "name": "One"}).json()
    s2 = owner.post("/api/stores", {"code": "S2", "name": "Two"}).json()
    r = owner.post("/api/setup/go_live", {"state": "done",
                                          "modes": {s1["id"]: "connected", s2["id"]: "standalone"}})
    assert r.status_code == 200
    modes = {s["code"]: s["mode"] for s in owner.get("/api/stores").json()}
    assert modes == {"S1": "connected", "S2": "standalone"}
    assert owner.post("/api/setup/go_live", {"state": "done", "modes": {"x" * 32: "connected"}}
                      ).status_code == 404


def test_calendar_is_prefilled_by_state(make_client):
    owner = make_client()
    owner.signup()
    kerala = owner.post("/api/stores", {"code": "K1", "name": "Kochi", "state": "Kerala"}).json()
    maha = owner.post("/api/stores", {"code": "M1", "name": "Pune", "state": "Maharashtra"}).json()
    span = "from=2026-01-01&to=2026-12-31"
    k = {e["name"]: e for e in owner.get(f"/api/calendar?{span}&store_id={kerala['id']}").json()}
    m = {e["name"]: e for e in owner.get(f"/api/calendar?{span}&store_id={maha['id']}").json()}
    assert k["Diwali"]["day"] == "2026-11-08" and k["Diwali"]["approximate"] is True
    assert "Onam" in k and "Onam" not in m
    assert "Ganesh Chaturthi" in m and "Ganesh Chaturthi" not in k
    assert k["Independence Day"]["approximate"] is False
    assert k["Good Friday"]["day"] == "2026-04-03"


def test_easter_is_exact():
    known = {2024: date(2024, 3, 31), 2025: date(2025, 4, 20), 2026: date(2026, 4, 5),
             2027: date(2027, 3, 28), 2028: date(2028, 4, 16), 2038: date(2038, 4, 25)}
    for y, d in known.items():
        assert festivals.easter(y) == d


def test_seeding_is_repeatable(make_client):
    owner = make_client()
    owner.signup()
    from sqlalchemy import func, select

    from dqstack.app import db as dbmod
    from dqstack.app.models import CalendarEvent
    with dbmod.SessionLocal() as db:
        cid = owner.me["company"]["id"]
        before = db.scalar(select(func.count()).select_from(CalendarEvent))
        assert festivals.seed(db, cid) == 0
        db.commit()
        assert db.scalar(select(func.count()).select_from(CalendarEvent)) == before


def test_manager_adds_a_local_event_for_their_store_only(make_client):
    owner = make_client()
    owner.signup()
    s1 = owner.post("/api/stores", {"code": "S1", "name": "One", "state": "Kerala"}).json()
    s2 = owner.post("/api/stores", {"code": "S2", "name": "Two", "state": "Kerala"}).json()
    mgr = make_client()
    invite_and_join(owner, mgr, "Mona Manager", "store_manager", [s1["id"]])
    ok = mgr.post("/api/calendar", {"name": "Temple fair", "day": "2026-12-05",
                                    "kind": "local_event", "store_ids": [s1["id"]]})
    assert ok.status_code == 201, ok.text
    assert mgr.post("/api/calendar", {"name": "Company sale", "day": "2026-12-05",
                                      "kind": "local_event"}).status_code == 403
    assert mgr.post("/api/calendar", {"name": "Not mine", "day": "2026-12-05",
                                      "kind": "closure", "store_ids": [s2["id"]]}).status_code == 404
    diwali = next(e for e in mgr.get("/api/calendar?from=2026-11-01&to=2026-11-30").json()
                  if e["name"] == "Diwali")
    assert mgr.patch(f"/api/calendar/{diwali['id']}", {"day": "2026-11-09"}).status_code == 403
    # The owner confirms a date: it stops being marked approximate
    fixed = owner.patch(f"/api/calendar/{diwali['id']}", {"day": "2026-11-08"}).json()
    assert fixed["approximate"] is False


def test_forgot_password_by_email(make_client):
    owner = make_client()
    owner.signup(email="asha@acme.example")
    c = make_client()
    r = c.post("/api/auth/password/forgot", {"login": "asha@acme.example"})
    same = c.post("/api/auth/password/forgot", {"login": "nobody@acme.example"})
    assert r.json()["message"] == same.json()["message"]       # never confirms an account
    token = _link("reset")
    assert c.post("/api/auth/password/reset", {"token": token, "password": "short"}).status_code == 422
    assert c.post("/api/auth/password/reset",
                  {"token": token, "password": "a brand new password"}).status_code == 200
    assert owner.get("/api/auth/me").status_code == 401            # old sessions ended
    assert c.post("/api/auth/password/reset",
                  {"token": token, "password": "another new password"}).status_code == 400
    assert make_client().login("asha@acme.example", "a brand new password").status_code == 200


def test_admin_resets_a_persons_password(make_client):
    owner = make_client()
    owner.signup()
    s1 = owner.post("/api/stores", {"code": "S1", "name": "One"}).json()
    clerk = make_client()
    info = invite_and_join(owner, clerk, "Kiran Clerk", "stock_clerk", [s1["id"]],
                           email="kiran@test.example")
    r = owner.post(f"/api/people/{info['id']}/reset-password")
    assert r.status_code == 200
    assert clerk.get("/api/auth/me").status_code == 401
    token = r.json()["reset_link"].rsplit("/", 1)[-1]
    assert make_client().post("/api/auth/password/reset",
                              {"token": token, "password": "kiran chose this"}).status_code == 200
    assert make_client().login("kiran@test.example", "kiran chose this").status_code == 200


def test_resend_invite_replaces_the_old_link(make_client):
    owner = make_client()
    owner.signup()
    s1 = owner.post("/api/stores", {"code": "S1", "name": "One"}).json()
    r = owner.post("/api/people/invite", {"name": "Late Joiner", "email": "late@x.example",
                                          "role": "viewer", "store_ids": [s1["id"]]})
    old = r.json()["invite_link"].rsplit("/", 1)[-1]
    new = owner.post(f"/api/people/{r.json()['id']}/resend-invite").json()["invite_link"]
    new = new.rsplit("/", 1)[-1]
    c = make_client()
    assert c.post("/api/auth/invitation/accept",
                  {"token": old, "password": "long password here"}).status_code == 400
    assert c.post("/api/auth/invitation/accept",
                  {"token": new, "password": "long password here"}).status_code == 200


def test_a_disabled_invitee_cannot_use_the_old_link(make_client):
    owner = make_client()
    owner.signup()
    s1 = owner.post("/api/stores", {"code": "S1", "name": "One"}).json()
    r = owner.post("/api/people/invite", {"name": "Gone", "email": "gone@x.example",
                                          "role": "viewer", "store_ids": [s1["id"]]})
    owner.post(f"/api/people/{r.json()['id']}/disable")
    token = r.json()["invite_link"].rsplit("/", 1)[-1]
    assert make_client().post("/api/auth/invitation/accept",
                              {"token": token, "password": "long password here"}).status_code == 400
    assert owner.get(f"/api/people/{r.json()['id']}").json()["status"] == "disabled"


def test_readiness_says_what_is_missing(make_client):
    owner = make_client()
    owner.signup()
    owner.post("/api/stores", {"code": "S1", "name": "One", "state": "Goa"})
    r = owner.get("/api/setup/readiness").json()
    assert r["stores"][0]["code"] == "S1" and 0 <= r["stores"][0]["score"] <= 100
    whats = " ".join(m["what"] for m in r["missing"])
    assert "No products yet" in whats and "No sales history" in whats
    assert r["first_forecast_minutes"] is None
