"""Sign-in extras: password reset by a code sent to the phone, the optional second
step at sign-in, and Google / Microsoft sign-in (OpenID Connect).

The identity-provider tests do not mock the token check. They generate a real RSA key,
publish its public half as the provider's JWKS, and sign real RS256 ID tokens with it,
so a token signed by any other key, for another audience, with another nonce or from
another issuer is refused by the same code that runs in production. Only the two
network calls (the code exchange and fetching the JWKS) are replaced.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import re
import time
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select

from dqstack.app import config, oidc, signin, sms
from dqstack.app import db as dbmod
from dqstack.app.db import utcnow
from dqstack.app.mailer import outbox as mail_outbox
from dqstack.app.models import ActivityLog, OneTimeCode, Person

from .conftest import invite_and_join

PW = "correct horse battery"
NEW_PW = "a brand new password"
PHONE = "98765 43210"            # how it is stored on the account
PHONE_TYPED = "+91 98765-43210"  # how the person types it


@pytest.fixture(autouse=True)
def _clean_sms():
    sms.outbox.clear()
    yield
    sms.outbox.clear()


# ------------------------------------------------------------------ helpers

def update_person(email: str, **fields) -> None:
    with dbmod.SessionLocal() as db:
        p = db.scalar(select(Person).where(Person.email == email))
        for k, v in fields.items():
            setattr(p, k, v)
        db.commit()


def get_person(email: str) -> Person:
    with dbmod.SessionLocal() as db:
        return db.scalar(select(Person).where(Person.email == email))


def sms_code(i: int = -1) -> str:
    return re.search(r"code is (\d{6})", sms.outbox[i].text).group(1)


def mail_code(i: int = -1) -> str:
    return re.search(r"code is (\d{6})", mail_outbox[i].body).group(1)


def wrong(code: str) -> str:
    return f"{(int(code) + 1) % 10 ** 6:06d}"


def log_entries(action: str | None = None) -> list[ActivityLog]:
    with dbmod.SessionLocal() as db:
        q = select(ActivityLog).order_by(ActivityLog.at)
        if action:
            q = q.where(ActivityLog.action == action)
        return list(db.scalars(q))


def assert_no_secrets_logged(*secrets_: str) -> None:
    text = json.dumps([[e.action, e.before, e.after, e.entity_id] for e in log_entries()])
    for s in secrets_:
        assert s not in text


def owner_with_phone(make_client, company="Acme Retail", name="Asha Owner", phone=PHONE):
    owner = make_client()
    owner.signup(company=company, name=name)
    email = owner.me["person"]["email"]
    update_person(email, phone=phone)
    return owner, email


def set_two_step(owner, mode: str):
    r = owner.put("/api/auth/security", {"two_step": mode})
    assert r.status_code == 200, r.text
    assert r.json()["two_step"] == mode
    return r.json()


# ================================================================== reset by phone

def test_phone_reset_happy_path_signs_out_everywhere(make_client):
    owner, email = owner_with_phone(make_client)
    assert owner.get("/api/auth/me").status_code == 200

    anon = make_client()
    r = anon.post("/api/auth/password/phone", {"phone": PHONE_TYPED})
    assert r.status_code == 200 and "6-digit code" in r.json()["message"]
    assert len(sms.outbox) == 1 and sms.outbox[0].to == "+919876543210"
    code = sms_code()

    # Password rules are checked first, and a refused password does not use up a try.
    r = anon.post("/api/auth/password/phone/reset", {"phone": PHONE_TYPED, "code": code,
                                                     "password": "short"})
    assert r.status_code == 422
    r = anon.post("/api/auth/password/phone/reset", {"phone": "9876543210", "code": code[:3] + " " + code[3:],
                                                     "password": NEW_PW})
    assert r.status_code == 200, r.text

    assert owner.get("/api/auth/me").status_code == 401        # signed out everywhere
    assert make_client().login(email, PW).status_code == 401
    assert make_client().login(email, NEW_PW).status_code == 200
    # The code is spent.
    r = anon.post("/api/auth/password/phone/reset", {"phone": PHONE_TYPED, "code": code,
                                                     "password": "yet another password"})
    assert r.status_code == 400

    sent = log_entries("auth.code_sent")
    assert [e.after for e in sent] == [{"purpose": "reset", "channel": "sms"}]
    done = log_entries("person.password_reset")
    assert done[-1].after["method"] == "phone code" and done[-1].after["sessions_ended"] >= 1
    assert_no_secrets_logged(code)
    # Only a keyed hash of the code is stored.
    with dbmod.SessionLocal() as db:
        row = db.scalar(select(OneTimeCode))
        assert code not in row.code_hash and row.code_hash != hashlib.sha256(code.encode()).hexdigest()


def test_phone_login_accepts_other_spellings(make_client):
    _, email = owner_with_phone(make_client)
    assert make_client().login("+91 98765-43210", PW).status_code == 200
    assert make_client().login("09876543210", PW).status_code == 200


def test_five_wrong_codes_kill_the_reset_code(make_client):
    _, email = owner_with_phone(make_client)
    anon = make_client()
    anon.post("/api/auth/password/phone", {"phone": PHONE})
    code = sms_code()
    for _ in range(5):
        r = anon.post("/api/auth/password/phone/reset", {"phone": PHONE, "code": wrong(code),
                                                         "password": NEW_PW})
        assert r.status_code == 400
    r = anon.post("/api/auth/password/phone/reset", {"phone": PHONE, "code": code,
                                                     "password": NEW_PW})
    assert r.status_code == 400                                 # dead even with the right code
    assert make_client().login(email, PW).status_code == 200    # password unchanged
    failed = log_entries("auth.code_failed")
    assert len(failed) == 5 and failed[-1].after == {"purpose": "reset", "tries": 5, "dead": True}
    assert_no_secrets_logged(code, wrong(code))


def test_reset_code_expires_after_ten_minutes(make_client):
    _, email = owner_with_phone(make_client)
    anon = make_client()
    anon.post("/api/auth/password/phone", {"phone": PHONE})
    code = sms_code()
    with dbmod.SessionLocal() as db:
        row = db.scalar(select(OneTimeCode))
        assert abs((signin._aware(row.expires_at) - utcnow()) - timedelta(minutes=10)) < timedelta(seconds=30)
        row.expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    r = anon.post("/api/auth/password/phone/reset", {"phone": PHONE, "code": code, "password": NEW_PW})
    assert r.status_code == 400


def test_a_new_reset_code_replaces_the_old_one(make_client):
    owner_with_phone(make_client)
    anon = make_client()
    anon.post("/api/auth/password/phone", {"phone": PHONE})
    first = sms_code()
    anon.post("/api/auth/password/phone", {"phone": PHONE})
    second = sms_code()
    if first != second:
        r = anon.post("/api/auth/password/phone/reset", {"phone": PHONE, "code": first, "password": NEW_PW})
        assert r.status_code == 400
    r = anon.post("/api/auth/password/phone/reset", {"phone": PHONE, "code": second, "password": NEW_PW})
    assert r.status_code == 200


def test_phone_reset_does_not_reveal_who_has_an_account(make_client):
    owner_with_phone(make_client)
    anon = make_client()
    known = anon.post("/api/auth/password/phone", {"phone": PHONE})
    unknown = anon.post("/api/auth/password/phone", {"phone": "91234 56789"})
    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()
    assert [m.to for m in sms.outbox] == ["+919876543210"]       # nothing sent to the stranger
    bad_known = anon.post("/api/auth/password/phone/reset", {"phone": PHONE, "code": "000000", "password": NEW_PW})
    bad_unknown = anon.post("/api/auth/password/phone/reset", {"phone": "91234 56789", "code": "000000", "password": NEW_PW})
    assert bad_known.status_code == bad_unknown.status_code == 400
    assert bad_known.json() == bad_unknown.json()


def test_disabled_person_gets_no_reset_code(make_client):
    _, email = owner_with_phone(make_client)
    update_person(email, status="disabled")
    r = make_client().post("/api/auth/password/phone", {"phone": PHONE})
    assert r.status_code == 200
    assert sms.outbox == []


def test_phone_reset_is_rate_limited_per_number_and_per_address(make_client):
    owner_with_phone(make_client)
    anon = make_client()
    for _ in range(3):
        assert anon.post("/api/auth/password/phone", {"phone": PHONE}).status_code == 200
    assert anon.post("/api/auth/password/phone", {"phone": PHONE}).status_code == 429   # per number
    assert anon.post("/api/auth/password/phone", {"phone": "91234 56789"}).status_code == 200
    assert anon.post("/api/auth/password/phone", {"phone": "91234 56780"}).status_code == 429  # per address


def test_a_reset_code_only_works_for_its_own_account(make_client):
    owner_with_phone(make_client)
    owner_with_phone(make_client, company="Beta Mart", name="Bina Owner", phone="91234 56789")
    anon = make_client()
    anon.post("/api/auth/password/phone", {"phone": PHONE})
    code_a = sms_code()
    r = anon.post("/api/auth/password/phone/reset", {"phone": "91234 56789", "code": code_a, "password": NEW_PW})
    assert r.status_code == 400
    assert make_client().login("bina@beta.example", PW).status_code == 200


def test_two_accounts_with_the_same_number_get_no_code(make_client):
    owner_with_phone(make_client)
    owner_with_phone(make_client, company="Beta Mart", name="Bina Owner", phone="+919876543210")
    r = make_client().post("/api/auth/password/phone", {"phone": PHONE})
    assert r.status_code == 200 and sms.outbox == []


def test_providers_endpoint_needs_no_sign_in(make_client):
    r = make_client().get("/api/auth/providers")
    assert r.status_code == 200
    assert r.json() == {"google": False, "microsoft": False, "phone_reset": True}


# ================================================================== two-step sign-in

def _company_with_people(make_client):
    owner = make_client()
    owner.signup()
    s1 = owner.post("/api/stores", {"code": "S1", "name": "Andheri"}).json()
    admin, viewer = make_client(), make_client()
    invite_and_join(owner, admin, "Anil Admin", "admin")
    invite_and_join(owner, viewer, "Vera Viewer", "viewer", [s1["id"]])
    return owner, "asha@acme.example", "anil.admin@test.example", "vera.viewer@test.example"


def test_two_step_is_off_by_default_and_only_owner_or_admin_may_change_it(make_client):
    owner, owner_email, admin_email, viewer_email = _company_with_people(make_client)
    r = owner.get("/api/auth/security")
    assert r.status_code == 200 and r.json()["two_step"] == "off" and r.json()["can_edit"]
    r = make_client().login(owner_email, PW)
    assert r.status_code == 200 and r.json().get("csrf")

    viewer = make_client()
    viewer.login(viewer_email, "a long enough password")
    assert viewer.get("/api/auth/security").json()["can_edit"] is False
    assert viewer.put("/api/auth/security", {"two_step": "everyone"}).status_code == 403
    assert owner.put("/api/auth/security", {"two_step": "sometimes"}).status_code == 422

    admin = make_client()
    admin.login(admin_email, "a long enough password")
    assert admin.put("/api/auth/security", {"two_step": "admins"}).status_code == 200
    entry = log_entries("company.signin_security")[-1]
    assert entry.before["two_step"] == "off" and entry.after["two_step"] == "admins"


def test_two_step_for_admins_asks_owner_for_a_code_but_not_a_viewer(make_client):
    owner, owner_email, admin_email, viewer_email = _company_with_people(make_client)
    set_two_step(owner, "admins")
    mail_outbox.clear()

    c = make_client()
    r = c.login(owner_email, PW)
    assert r.status_code == 200
    body = r.json()
    assert body["two_step"] is True and "csrf" not in body and "person" not in body
    assert "dq_session" not in r.headers.get("set-cookie", "")
    assert body["channel"] == "email"                            # the Owner has no phone
    assert body["message"] == "We sent a 6-digit code to your email a***@acme.example. It works for 10 minutes."
    assert c.get("/api/auth/me").status_code == 401             # no session yet
    code = mail_code()
    r = c.post("/api/auth/login/code", {"ticket": body["ticket"], "code": code})
    assert r.status_code == 200 and r.json()["person"]["email"] == owner_email
    assert c.get("/api/auth/me").status_code == 200
    assert log_entries("person.signed_in")[-1].after == {"method": "password and code", "channel": "email"}
    # The ticket is spent.
    assert c.post("/api/auth/login/code", {"ticket": body["ticket"], "code": code}).status_code == 400

    v = make_client()
    r = v.login(viewer_email, "a long enough password")
    assert r.status_code == 200 and r.json().get("csrf")         # straight in
    assert_no_secrets_logged(code, body["ticket"])


def test_two_step_for_everyone_sends_a_text_when_the_person_has_a_phone(make_client):
    owner, owner_email, admin_email, viewer_email = _company_with_people(make_client)
    update_person(viewer_email, phone="91234 54321")
    set_two_step(owner, "everyone")
    c = make_client()
    r = c.login(viewer_email, "a long enough password")
    body = r.json()
    assert body["two_step"] and body["channel"] == "sms"
    assert "your phone ending 4321" in body["message"]
    assert sms.outbox[-1].to == "+919123454321"
    r = c.post("/api/auth/login/code", {"ticket": body["ticket"], "code": sms_code()})
    assert r.status_code == 200


def test_wrong_password_with_two_step_on_sends_nothing(make_client):
    owner, owner_email, *_ = _company_with_people(make_client)
    update_person(owner_email, phone=PHONE)
    set_two_step(owner, "everyone")
    mail_outbox.clear()
    r = make_client().login(owner_email, "not the password")
    assert r.status_code == 401
    assert r.json() == make_client().login("nobody@nowhere.example", "not the password").json()
    assert sms.outbox == [] and mail_outbox == []


def test_five_wrong_sign_in_codes_end_the_attempt(make_client):
    owner, owner_email, *_ = _company_with_people(make_client)
    set_two_step(owner, "admins")
    c = make_client()
    body = c.login(owner_email, PW).json()
    code = mail_code()
    r = c.post("/api/auth/login/code", {"ticket": body["ticket"], "code": wrong(code)})
    assert r.status_code == 400 and r.json()["error"] == "That code is not right. 4 tries left."
    for _ in range(4):
        r = c.post("/api/auth/login/code", {"ticket": body["ticket"], "code": wrong(code)})
    assert "last try" in r.json()["error"]
    r = c.post("/api/auth/login/code", {"ticket": body["ticket"], "code": code})
    assert r.status_code == 400 and "Too many wrong codes" in r.json()["error"]
    assert c.post("/api/auth/login/code/resend", {"ticket": body["ticket"]}).status_code == 400
    assert c.get("/api/auth/me").status_code == 401
    assert get_person(owner_email).failed_attempts == 1         # counts towards the lockout
    assert log_entries("auth.code_failed")[-1].after == {"purpose": "signin", "tries": 5, "dead": True}


def test_sign_in_code_expires(make_client):
    owner, owner_email, *_ = _company_with_people(make_client)
    set_two_step(owner, "admins")
    c = make_client()
    body = c.login(owner_email, PW).json()
    with dbmod.SessionLocal() as db:
        db.scalar(select(OneTimeCode)).expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    r = c.post("/api/auth/login/code", {"ticket": body["ticket"], "code": mail_code()})
    assert r.status_code == 400 and "expired" in r.json()["error"]


def test_send_a_new_code(make_client):
    owner, owner_email, *_ = _company_with_people(make_client)
    set_two_step(owner, "admins")
    c = make_client()
    body = c.login(owner_email, PW).json()
    first = mail_code()
    c.post("/api/auth/login/code", {"ticket": body["ticket"], "code": wrong(first)})
    r = c.post("/api/auth/login/code/resend", {"ticket": body["ticket"]})
    assert r.status_code == 200 and "new 6-digit code" in r.json()["message"]
    second = mail_code()
    r = c.post("/api/auth/login/code/resend", {"ticket": body["ticket"]})
    assert r.status_code == 200
    third = mail_code()
    r = c.post("/api/auth/login/code/resend", {"ticket": body["ticket"]})
    assert r.status_code == 429                                  # three codes in all
    for old in {first, second} - {third}:
        r = c.post("/api/auth/login/code", {"ticket": body["ticket"], "code": old})
        assert r.status_code == 400
    r = c.post("/api/auth/login/code", {"ticket": body["ticket"], "code": third})
    assert r.status_code == 200
    sent = log_entries("auth.code_sent")
    assert [e.after.get("resend", False) for e in sent] == [False, True, True]
    assert_no_secrets_logged(first, second, third)


def test_a_ticket_only_signs_in_its_own_person(make_client):
    owner_a, email_a, *_ = _company_with_people(make_client)
    set_two_step(owner_a, "admins")
    owner_b = make_client()
    owner_b.signup(company="Beta Mart", name="Bina Owner")
    set_two_step(owner_b, "admins")
    ca, cb = make_client(), make_client()
    ticket_a = ca.login(email_a, PW).json()["ticket"]
    code_a = mail_code()
    ticket_b = cb.login("bina@beta.example", PW).json()["ticket"]
    r = cb.post("/api/auth/login/code", {"ticket": ticket_b, "code": code_a})
    assert r.status_code == 400
    r = ca.post("/api/auth/login/code", {"ticket": ticket_a, "code": code_a})
    assert r.status_code == 200 and r.json()["company"]["name"] == "Acme Retail"


# ================================================================== Google and Microsoft

GOOGLE_ID = "google-client-123.apps.googleusercontent.com"
MS_ID = "11111111-2222-3333-4444-555555555555"
MS_TENANT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class FakeProvider:
    """Stands in for Google's and Microsoft's servers: a real RSA key, its JWKS, and a
    code exchange that answers with an RS256 ID token signed by that key."""

    def __init__(self, monkeypatch):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.signing_key = self.key
        self.kid = "test-key-1"
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        jwk.update(kid=self.kid, use="sig", alg="RS256")
        self.jwks = {"keys": [jwk]}
        self.email = None
        self.nonce = None
        self.overrides: dict = {}
        self.exchanges: list[tuple[str, str, str]] = []
        monkeypatch.setattr(config, "settings", dataclasses.replace(
            config.settings, google_client_id=GOOGLE_ID, google_client_secret="g-secret",
            microsoft_client_id=MS_ID, microsoft_client_secret="m-secret",
            microsoft_tenant="organizations"))
        monkeypatch.setattr(oidc, "fetch_jwks", lambda provider, refresh=False: self.jwks)
        monkeypatch.setattr(oidc, "exchange_code", self.exchange)

    def exchange(self, provider, code, verifier):
        self.exchanges.append((provider, code, verifier))
        return {"access_token": "not-used", "id_token": self.id_token(provider)}

    def id_token(self, provider):
        now = int(time.time())
        if provider == "google":
            claims = {"iss": "https://accounts.google.com", "aud": GOOGLE_ID, "sub": "g-1",
                      "email": self.email, "email_verified": True}
        else:
            claims = {"iss": f"https://login.microsoftonline.com/{MS_TENANT}/v2.0",
                      "tid": MS_TENANT, "aud": MS_ID, "sub": "m-1",
                      "preferred_username": self.email}
        claims.update(iat=now, exp=now + 600, nonce=self.nonce)
        claims.update(self.overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(claims, self.signing_key, algorithm="RS256", headers={"kid": self.kid})

    def sign_in(self, client, provider, email, state=None, **overrides):
        """Start, then come back from the provider. Returns the callback's Location."""
        r = client.http.get(f"/api/auth/oidc/{provider}/start", follow_redirects=False)
        assert r.status_code == 302, r.text
        url = urlparse(r.headers["location"])
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        assert q["code_challenge_method"] == "S256" and q["redirect_uri"].endswith(
            f"/api/auth/oidc/{provider}/callback")
        self.email, self.nonce, self.overrides = email, q["nonce"], overrides
        self.challenge = q["code_challenge"]
        r = client.http.get(f"/api/auth/oidc/{provider}/callback",
                            params={"code": "auth-code-1", "state": state or q["state"]},
                            follow_redirects=False)
        assert r.status_code == 303, r.text
        return r.headers["location"]


@pytest.fixture
def idp(monkeypatch):
    return FakeProvider(monkeypatch)


def test_providers_show_only_when_configured(make_client, idp):
    r = make_client().get("/api/auth/providers")
    assert r.json() == {"google": True, "microsoft": True, "phone_reset": True}


def test_start_is_refused_when_a_provider_has_no_keys(make_client):
    r = make_client().http.get("/api/auth/oidc/google/start", follow_redirects=False)
    assert r.status_code == 404
    r = make_client().http.get("/api/auth/oidc/yahoo/start", follow_redirects=False)
    assert r.status_code == 404


def test_google_sign_in_existing_person_with_pkce(make_client, idp):
    owner = make_client()
    owner.signup()
    c = make_client()
    where = idp.sign_in(c, "google", "Asha@Acme.example")
    assert where == "/app/#/home"
    me = c.get("/api/auth/me")
    assert me.status_code == 200 and me.json()["person"]["email"] == "asha@acme.example"
    provider, code, verifier = idp.exchanges[-1]
    assert (provider, code) == ("google", "auth-code-1")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == idp.challenge
    entry = log_entries("person.signed_in")[-1]
    assert entry.after == {"method": "google"}
    assert_no_secrets_logged(verifier, idp.nonce)


def test_microsoft_sign_in_needs_the_companys_own_tenant(make_client, idp):
    """Any organisation's tenant can put any address in the email claim, so with a
    multi-tenant setup a company must name its own tenant first."""
    owner = make_client()
    owner.signup()
    c = make_client()
    assert idp.sign_in(c, "microsoft", "asha@acme.example") == "/app/#/signin?sso=ms-tenant"
    assert c.get("/api/auth/me").status_code == 401
    assert owner.put("/api/auth/security", {"microsoft_tenant": "not-a-guid"}).status_code == 422
    other = "11111111-2222-3333-4444-555555555555"
    owner.put("/api/auth/security", {"microsoft_tenant": other})
    assert idp.sign_in(c, "microsoft", "asha@acme.example") == "/app/#/signin?sso=ms-tenant"
    r = owner.put("/api/auth/security", {"microsoft_tenant": MS_TENANT.upper()})
    assert r.status_code == 200 and r.json()["microsoft_tenant"] == MS_TENANT


def test_microsoft_sign_in_uses_preferred_username(make_client, idp):
    owner = make_client()
    owner.signup()
    owner.put("/api/auth/security", {"microsoft_tenant": MS_TENANT})
    c = make_client()
    assert idp.sign_in(c, "microsoft", "asha@acme.example") == "/app/#/home"
    assert c.get("/api/auth/me").json()["person"]["email"] == "asha@acme.example"
    assert log_entries("person.signed_in")[-1].after == {"method": "microsoft"}


def test_identity_provider_sign_in_skips_the_two_step_code(make_client, idp):
    owner = make_client()
    owner.signup()
    set_two_step(owner, "everyone")
    mail_outbox.clear()
    c = make_client()
    assert idp.sign_in(c, "google", "asha@acme.example") == "/app/#/home"
    assert c.get("/api/auth/me").status_code == 200
    assert mail_outbox == [] and sms.outbox == []


@pytest.mark.parametrize("provider,overrides,reason", [
    ("google", {"nonce": "someone-elses-nonce"}, "failed"),
    ("google", {"aud": "another-client"}, "failed"),
    ("google", {"iss": "https://accounts.evil.example"}, "failed"),
    ("google", {"exp": int(time.time()) - 3600, "iat": int(time.time()) - 7200}, "failed"),
    ("google", {"email_verified": False}, "unverified"),
    ("microsoft", {"aud": "another-client"}, "failed"),
    ("microsoft", {"iss": "https://login.microsoftonline.com/ffffffff-0000-0000-0000-000000000000/v2.0"}, "failed"),
    ("microsoft", {"tid": oidc.MS_CONSUMER_TENANT,
                   "iss": f"https://login.microsoftonline.com/{oidc.MS_CONSUMER_TENANT}/v2.0"}, "failed"),
])
def test_bad_id_tokens_are_refused(make_client, idp, provider, overrides, reason):
    make_client().signup()
    c = make_client()
    where = idp.sign_in(c, provider, "asha@acme.example", **overrides)
    assert where == f"/app/#/signin?sso={reason}"
    assert c.get("/api/auth/me").status_code == 401


def test_token_signed_by_another_key_is_refused(make_client, idp):
    make_client().signup()
    idp.signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    c = make_client()
    assert idp.sign_in(c, "google", "asha@acme.example") == "/app/#/signin?sso=failed"
    assert c.get("/api/auth/me").status_code == 401


def test_wrong_state_is_refused(make_client, idp):
    make_client().signup()
    c = make_client()
    assert idp.sign_in(c, "google", "asha@acme.example", state="forged-state") == "/app/#/signin?sso=failed"
    assert idp.exchanges == []                                   # never got as far as the code
    assert c.get("/api/auth/me").status_code == 401


def test_callback_without_the_start_cookie_is_refused(make_client, idp):
    make_client().signup()
    c = make_client()
    r = c.http.get("/api/auth/oidc/google/callback", params={"code": "x", "state": "y"},
                   follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app/#/signin?sso=expired"
    r = c.http.get("/api/auth/oidc/google/callback", params={"error": "access_denied"},
                   follow_redirects=False)
    assert r.headers["location"] == "/app/#/signin?sso=cancelled"


def test_unknown_email_is_refused_and_nothing_is_created(make_client, idp):
    make_client().signup()
    with dbmod.SessionLocal() as db:
        before = len(db.scalars(select(Person)).all())
    c = make_client()
    assert idp.sign_in(c, "google", "stranger@gmail.example") == "/app/#/signin?sso=no-account"
    with dbmod.SessionLocal() as db:
        assert len(db.scalars(select(Person)).all()) == before


def test_disabled_and_locked_people_are_refused(make_client, idp):
    owner = make_client()
    owner.signup()
    other = make_client()
    invite_and_join(owner, other, "Anil Admin", "admin")
    update_person("anil.admin@test.example", status="disabled")
    c = make_client()
    assert idp.sign_in(c, "google", "anil.admin@test.example") == "/app/#/signin?sso=disabled"
    update_person("asha@acme.example", locked_until=utcnow() + timedelta(minutes=10))
    assert idp.sign_in(c, "microsoft", "asha@acme.example") == "/app/#/signin?sso=locked"
    assert c.get("/api/auth/me").status_code == 401
    refused = log_entries("auth.idp_refused")
    assert [e.after["reason"] for e in refused] == ["disabled", "locked"]


def test_company_can_turn_identity_providers_off_for_itself_only(make_client, idp):
    owner_a = make_client()
    owner_a.signup()
    r = owner_a.put("/api/auth/security", {"idp": False})
    assert r.status_code == 200 and r.json()["idp"] is False
    owner_b = make_client()
    owner_b.signup(company="Beta Mart", name="Bina Owner")
    ca, cb = make_client(), make_client()
    assert idp.sign_in(ca, "google", "asha@acme.example") == "/app/#/signin?sso=not-allowed"
    assert idp.sign_in(cb, "google", "bina@beta.example") == "/app/#/home"
    assert cb.get("/api/auth/me").json()["company"]["name"] == "Beta Mart"
    assert ca.get("/api/auth/me").status_code == 401
