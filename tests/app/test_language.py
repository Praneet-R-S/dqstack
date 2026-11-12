"""A person's own language for the store app (webapp/js/i18n.js): kept on their
account, so it follows them from one till or phone to another, settable by everyone,
cashiers included, and limited to the languages the app has a dictionary for."""

from __future__ import annotations

from .conftest import invite_and_join


def test_language_is_saved_on_the_account_and_returned_at_sign_in(make_client):
    owner = make_client()
    me = owner.signup()
    assert me["person"]["language"] == "en"
    r = owner.put("/api/auth/language", {"language": "hi"})
    assert r.status_code == 200, r.text
    assert r.json() == {"language": "hi"}
    assert owner.get("/api/auth/me").json()["person"]["language"] == "hi"

    again = make_client()                    # another device: the choice comes along
    r = again.login(me["person"]["email"])
    assert r.status_code == 200, r.text
    assert r.json()["person"]["language"] == "hi"


def test_a_cashier_may_set_their_own_language(make_client):
    owner = make_client()
    owner.signup()
    store = owner.post("/api/stores", {"code": "S1", "name": "Main Road", "state": "Karnataka"})
    assert store.status_code in (200, 201), store.text
    cashier = make_client()
    invite_and_join(owner, cashier, "Ravi Till", "cashier", store_ids=[store.json()["id"]])
    r = cashier.put("/api/auth/language", {"language": "hi"})
    assert r.status_code == 200, r.text
    assert cashier.get("/api/auth/me").json()["person"]["language"] == "hi"


def test_only_languages_the_app_has_are_accepted(make_client):
    owner = make_client()
    owner.signup()
    r = owner.put("/api/auth/language", {"language": "fr"})
    assert r.status_code == 422
    assert "not available" in r.json()["error"]
    assert owner.get("/api/auth/me").json()["person"]["language"] == "en"


def test_changing_language_needs_the_csrf_token(make_client):
    owner = make_client()
    owner.signup()
    owner.csrf = None
    r = owner.put("/api/auth/language", {"language": "hi"})
    assert r.status_code == 403
