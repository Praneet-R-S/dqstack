"""Sign-in, sign-up, sign-out, password reset and invitations (sections 5 and 13)."""

from __future__ import annotations

import hmac
import logging
import re
from datetime import timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit, oidc, permissions, signin, sms
from ..config import settings
from ..db import get_db, utcnow
from ..deps import client_ip, ctx, device_of
from ..mailer import send
from ..models import Company, Invitation, OneTimeCode, PasswordReset, Person, Store
from ..ratelimit import limiter
from ..scope import Ctx, build_ctx
from ..security import (AuthError, authenticate, count_failure, hash_password, lock_message,
                        new_token, password_problem, revoke_all_sessions, start_session,
                        token_hash, _aware)

router = APIRouter(prefix="/api/auth", tags=["auth"])
log = logging.getLogger("dqstack.auth")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _set_cookie(resp: Response, token: str) -> None:
    resp.set_cookie(settings.cookie_name, token, httponly=True, secure=settings.cookie_secure,
                    samesite="lax", max_age=settings.session_hours * 3600, path="/")


def me_payload(db: Session, c: Ctx) -> dict:
    company = db.get(Company, c.company_id)
    stores = db.scalars(select(Store).where(Store.company_id == c.company_id)
                        .order_by(Store.code)).all()
    visible = [s for s in stores if c.all_stores or s.id in c.store_ids]
    return {
        "person": {"id": c.person.id, "name": c.person.name, "email": c.person.email,
                   "phone": c.person.phone, "role": c.role, "language": c.person.language},
        "company": {"id": company.id, "name": company.name, "currency": company.currency,
                    "timezone": company.timezone, "plan": company.plan,
                    "is_demo": company.is_demo},
        "stores": [{"id": s.id, "code": s.code, "name": s.name, "state": s.state,
                    "mode": s.mode} for s in visible],
        "can": {a: c.can(a) for a in permissions.ACTIONS},
        "csrf": c.session.csrf_token if c.session else None,
    }


# ------------------------------------------------------------------ sign-up

class SignupIn(BaseModel):
    company_name: str = Field(min_length=2, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=200)
    gstin: str | None = Field(default=None, max_length=15)


@router.post("/signup")
def signup(body: SignupIn, request: Request, response: Response, db: Session = Depends(get_db)):
    """Self sign-up for a trial. The person who signs up becomes the Owner."""
    limiter.check(f"signup:{client_ip(request)}", limit=5, window_s=3600)
    email = body.email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(422, "Enter a valid email address.")
    if (p := password_problem(body.password)):
        raise HTTPException(422, p)
    if db.scalar(select(Person.id).where(Person.email == email)):
        raise HTTPException(409, "An account with that email already exists. Sign in instead.")
    company = Company(name=body.company_name.strip(), gstin=(body.gstin or None),
                      timezone=settings.default_timezone)
    db.add(company)
    db.flush()
    owner = Person(company_id=company.id, name=body.name.strip(), email=email, role="owner",
                   status="active", password_hash=hash_password(body.password))
    db.add(owner)
    company.setup = {"company": utcnow().isoformat()}
    db.flush()
    from ..festivals import seed as seed_festivals
    seed_festivals(db, company.id)
    c = build_ctx(db, owner, None, ip=client_ip(request), device=device_of(request))
    audit.log(db, c, "company.created", "company", company.id, after=audit.snapshot(company))
    db.commit()
    token, sess = start_session(db, owner, ip=client_ip(request), device=device_of(request))
    _set_cookie(response, token)
    c.session = sess
    return me_payload(db, c)


# ------------------------------------------------------------------ sign-in

class LoginIn(BaseModel):
    login: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=200)


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    limiter.check(f"login:{client_ip(request)}", limit=30, window_s=300)
    try:
        person = authenticate(db, body.login, body.password)
    except AuthError as e:
        raise HTTPException(401, str(e)) from None
    if person.is_platform_admin and person.company_id is None:
        raise HTTPException(403, "Platform accounts sign in at /platform.")
    if signin.two_step_applies(db.get(Company, person.company_id), person):
        # The password was right; the session waits for the code (signin_extras below).
        return _start_two_step(db, person, request)
    token, sess = start_session(db, person, ip=client_ip(request), device=device_of(request))
    _set_cookie(response, token)
    c = build_ctx(db, person, sess, ip=client_ip(request), device=device_of(request))
    audit.log(db, c, "person.signed_in", "person", person.id, after={"method": "password"})
    db.commit()
    return me_payload(db, c)


@router.post("/logout")
def logout(response: Response, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    if c.session:
        s = db.merge(c.session)
        s.revoked_at = utcnow()
        db.commit()
    response.delete_cookie(settings.cookie_name, path="/")
    return {"ok": True}


@router.get("/me")
def me(c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    return me_payload(db, c)


# The languages the store app has a dictionary for (webapp/js/i18n-*.js). The person's
# choice is kept on their account, so it follows them from one till or phone to another.
SUPPORTED_LANGUAGES = ("en", "hi")


class LanguageIn(BaseModel):
    language: str = Field(min_length=2, max_length=8)


@router.put("/language")
def set_language(body: LanguageIn, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    """The signed-in person's own language. Anyone may set their own, cashiers too."""
    if body.language not in SUPPORTED_LANGUAGES:
        raise HTTPException(422, "That language is not available yet.")
    p = db.get(Person, c.person.id)
    if p.language != body.language:
        before = {"language": p.language}
        p.language = body.language
        audit.log(db, c, "person.language", "person", p.id, before, {"language": p.language})
        db.commit()
    return {"language": p.language}


# ------------------------------------------------------------------ password reset

class ForgotIn(BaseModel):
    login: str = Field(min_length=1, max_length=254)


@router.post("/password/forgot")
def forgot(body: ForgotIn, request: Request, db: Session = Depends(get_db)):
    """Always answers the same way, so it cannot be used to find out who has an account."""
    limiter.check(f"forgot:{client_ip(request)}", limit=5, window_s=900)
    login_ = body.login.strip().lower()
    person = db.scalar(select(Person).where((Person.email == login_) | (Person.phone == login_)))
    if person and person.status == "active" and person.email:
        token = new_token()
        db.add(PasswordReset(token_hash=token_hash(token), person_id=person.id,
                             company_id=person.company_id,
                             expires_at=utcnow() + timedelta(hours=1)))
        db.commit()
        send(person.email, "Reset your DQ-Stack password",
             f"Hello {person.name},\n\nUse this link within one hour to choose a new password:\n"
             f"{settings.public_url}/app/#/reset/{token}\n\n"
             "If you did not ask for this, you can ignore this email.")
    return {"ok": True, "message": "If that account exists, a reset link is on its way."}


class ResetIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    password: str = Field(min_length=1, max_length=200)


@router.post("/password/reset")
def reset(body: ResetIn, request: Request, db: Session = Depends(get_db)):
    limiter.check(f"reset:{client_ip(request)}", limit=10, window_s=900)
    pr = db.scalar(select(PasswordReset).where(PasswordReset.token_hash == token_hash(body.token)))
    if pr is None or pr.used_at is not None or _aware(pr.expires_at) <= utcnow():
        raise HTTPException(400, "This reset link has expired or was already used. Ask for a new one.")
    if (p := password_problem(body.password)):
        raise HTTPException(422, p)
    person = db.get(Person, pr.person_id)
    person.password_hash = hash_password(body.password)
    person.failed_attempts = 0
    person.locked_until = None
    pr.used_at = utcnow()
    revoke_all_sessions(db, person.id)
    audit.log(db, _system_ctx(person), "person.password_reset", "person", person.id)
    db.commit()
    return {"ok": True, "message": "Password changed. Sign in with the new one."}


# ------------------------------------------------------------------ invitations

@router.get("/invitation/{token}")
def invitation_info(token: str, db: Session = Depends(get_db)):
    inv = _live_invitation(db, token)
    company = db.get(Company, inv.company_id)
    return {"name": inv.name, "email": inv.email, "phone": inv.phone, "role": inv.role,
            "company": company.name}


class AcceptIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    password: str = Field(min_length=1, max_length=200)


@router.post("/invitation/accept")
def accept(body: AcceptIn, request: Request, response: Response, db: Session = Depends(get_db)):
    limiter.check(f"accept:{client_ip(request)}", limit=10, window_s=900)
    inv = _live_invitation(db, body.token)
    if (p := password_problem(body.password)):
        raise HTTPException(422, p)
    person = db.scalar(select(Person).where(Person.company_id == inv.company_id,
                                            (Person.email == inv.email) if inv.email
                                            else (Person.phone == inv.phone)))
    if person is None:
        raise HTTPException(400, "This invitation no longer matches an account. Ask for a new one.")
    if person.status == "disabled":
        # An administrator disabled this account after inviting it. Accepting the old
        # link must not quietly re-enable it.
        raise HTTPException(400, "This invitation has expired or was already used. "
                                 "Ask your administrator to send a new one.")
    person.password_hash = hash_password(body.password)
    person.status = "active"
    inv.accepted_at = utcnow()
    token, sess = start_session(db, person, ip=client_ip(request), device=device_of(request))
    c = build_ctx(db, person, sess, ip=client_ip(request), device=device_of(request))
    audit.log(db, c, "person.joined", "person", person.id)
    db.commit()
    _set_cookie(response, token)
    return me_payload(db, c)


def _live_invitation(db: Session, token: str) -> Invitation:
    inv = db.scalar(select(Invitation).where(Invitation.token_hash == token_hash(token)))
    if inv is None or inv.accepted_at is not None or _aware(inv.expires_at) <= utcnow():
        raise HTTPException(400, "This invitation has expired or was already used. "
                                 "Ask your administrator to send a new one.")
    return inv


class _SysCtx:
    def __init__(self, person: Person):
        self.person = person
        self.company_id = person.company_id
        self.device = None
        self.ip = None


def _system_ctx(person: Person, request: Request | None = None) -> _SysCtx:
    c = _SysCtx(person)
    if request is not None:
        c.ip, c.device = client_ip(request), device_of(request)
    return c


# ================================================================== sign-in extras
# Password reset by a code sent to the phone, the optional second step at sign-in,
# and Google / Microsoft sign-in. The rules for codes are in signin.py, the OpenID
# Connect checks in oidc.py. No code, ticket or token is ever written to the log.

# ------------------------------------------------------------------ demo sign-in

DEMO_ROLE_ORDER = ("owner", "admin", "store_manager", "purchaser", "stock_clerk", "cashier",
                   "viewer")


def _demo_company(db: Session) -> Company | None:
    """The demonstration company the sign-in page offers, when demo logins are on."""
    if not settings.demo_logins:
        return None
    from sqlalchemy import or_
    return db.scalar(select(Company).where(
        Company.status == "active", or_(Company.plan == "demo", Company.is_demo.is_(True)))
        .order_by(Company.created_at.desc()).limit(1))


@router.get("/demo")
def demo_accounts(db: Session = Depends(get_db)):
    """The demo company's people, for one-click sign-in. Needs no sign-in, so it gives
    names, roles and stores only: never an email (the Owner's is a real address)."""
    co = _demo_company(db)
    if co is None:
        return {"company": None, "accounts": []}
    people = list(db.scalars(select(Person).where(Person.company_id == co.id,
                                                  Person.status == "active")))
    order = {r: i for i, r in enumerate(DEMO_ROLE_ORDER)}
    people.sort(key=lambda p: (order.get(p.role, 99), p.name))
    return {"company": co.name, "accounts": [
        {"id": p.id, "name": p.name, "role": p.role,
         "stores": sorted(s.name for s in p.stores if s.company_id == co.id)}
        for p in people]}


class DemoIn(BaseModel):
    person_id: str = Field(min_length=1, max_length=64)


@router.post("/demo")
def demo_login(body: DemoIn, request: Request, response: Response,
               db: Session = Depends(get_db)):
    """Sign in as one of the demo company's people, without a password. Refused unless
    demo logins are on and the person belongs to the demonstration company."""
    limiter.check(f"demo:{client_ip(request)}", limit=30, window_s=300)
    co = _demo_company(db)
    person = db.get(Person, body.person_id) if co is not None else None
    if co is None or person is None or person.company_id != co.id or person.status != "active":
        raise HTTPException(404, "That demo account is not available.")
    token, sess = start_session(db, person, ip=client_ip(request), device=device_of(request))
    _set_cookie(response, token)
    c = build_ctx(db, person, sess, ip=client_ip(request), device=device_of(request))
    audit.log(db, c, "person.signed_in", "person", person.id, after={"method": "demo"})
    db.commit()
    return me_payload(db, c)


@router.get("/providers")
def providers():
    """What the sign-in screens may offer. Needs no sign-in."""
    return {**oidc.enabled(), "phone_reset": signin.texts_available()}


# ------------------------------------------------------------------ reset by phone

PHONE_RESET_NEUTRAL = ("If that number belongs to an account, we sent a 6-digit code to it. "
                       "It works for 10 minutes.")
PHONE_CODE_BAD = ("That code is not right, or it is no longer valid. Check it, or ask for a "
                  "new code.")


class PhoneIn(BaseModel):
    phone: str = Field(min_length=6, max_length=30)


def _phone_or_422(raw: str) -> str:
    n = sms.normalise(raw)
    if n is None:
        raise HTTPException(422, "Enter a mobile number, for example 98765 43210.")
    return n


@router.post("/password/phone")
def forgot_by_phone(body: PhoneIn, request: Request, background: BackgroundTasks,
                    db: Session = Depends(get_db)):
    """Send a reset code by text message. The same answer whether or not the number has
    an account, so it cannot be used to find out who is a customer."""
    if not signin.texts_available():
        raise HTTPException(404, "Resetting a password by text message is not set up here. "
                                 "Use your email instead.")
    limiter.check(f"forgot-phone:{client_ip(request)}", limit=5, window_s=900)
    number = _phone_or_422(body.phone)
    limiter.check(f"forgot-phone-to:{number}", limit=3, window_s=3600)
    person = signin.find_by_phone(db, number)
    if person is not None and person.status == "active":
        row, code = signin.issue(db, person, "reset", "sms")
        audit.log(db, _system_ctx(person, request), "auth.code_sent", "person", person.id,
                  after={"purpose": "reset", "channel": "sms"})
        db.commit()
        # Sent after the answer goes back, so the time taken does not show whether the
        # number has an account.
        background.add_task(_send_reset_text, row.id, person.phone, code)
    return {"ok": True, "message": PHONE_RESET_NEUTRAL}


def _send_reset_text(row_id: str, phone: str, code: str) -> None:
    try:
        signin.deliver("sms", phone, "reset", code)
    except signin.SendFailed:
        from .. import db as dbmod
        with dbmod.SessionLocal() as db:
            row = db.get(OneTimeCode, row_id)
            if row is not None and row.used_at is None:
                row.used_at = utcnow()      # never sent: do not leave a live code behind
                db.commit()
        log.warning("reset code text could not be sent")


class PhoneResetIn(BaseModel):
    phone: str = Field(min_length=6, max_length=30)
    code: str = Field(min_length=1, max_length=20)
    password: str = Field(min_length=1, max_length=200)


@router.post("/password/phone/reset")
def reset_by_phone(body: PhoneResetIn, request: Request, db: Session = Depends(get_db)):
    limiter.check(f"reset-phone:{client_ip(request)}", limit=20, window_s=900)
    number = _phone_or_422(body.phone)
    limiter.check(f"reset-phone-to:{number}", limit=10, window_s=900)
    # Password rules first, so a password that is too short does not use up a try.
    if (p := password_problem(body.password)):
        raise HTTPException(422, p)
    person = signin.find_by_phone(db, number)
    row = signin.latest_reset(db, person) if person is not None else None
    tries_before = row.attempts if row is not None else 0
    if row is None or person.status != "active" or not signin.check(row, body.code):
        if row is not None and row.attempts > tries_before:
            audit.log(db, _system_ctx(person, request), "auth.code_failed", "person", person.id,
                      after={"purpose": "reset", "tries": row.attempts,
                             "dead": row.attempts >= signin.MAX_TRIES})
            db.commit()
        raise HTTPException(400, PHONE_CODE_BAD)
    person.password_hash = hash_password(body.password)
    person.failed_attempts = 0
    person.locked_until = None
    n = revoke_all_sessions(db, person.id)
    audit.log(db, _system_ctx(person, request), "person.password_reset", "person", person.id,
              after={"method": "phone code", "sessions_ended": n})
    db.commit()
    return {"ok": True, "message": "Password changed. Sign in with the new one."}


# ------------------------------------------------------------------ two-step sign-in

SIGNIN_EXPIRED = "This sign-in has expired. Enter your password again."


def _start_two_step(db: Session, person: Person, request: Request) -> dict:
    """The password was right and the company wants a code too: send one and hand the
    browser a ticket for the half-finished sign-in. No session exists yet."""
    build_ctx(db, person, None)             # refuses a paused company before any code goes out
    way = signin.channel_for(person)
    if way is None:
        raise HTTPException(403, "Your company asks for a code at sign-in, but your account has "
                                 "no phone or email to send it to. Ask your administrator.")
    channel, to = way
    limiter.check(f"code-send:{person.id}", limit=10, window_s=3600)
    ticket = new_token()
    row, code = signin.issue(db, person, "signin", channel, ticket_hash=token_hash(ticket))
    try:
        signin.deliver(channel, to, "signin", code, person.name)
    except signin.SendFailed:
        row.used_at = utcnow()
        db.commit()
        raise HTTPException(503, "We could not send your sign-in code just now. "
                                 "Try again in a minute.") from None
    audit.log(db, _system_ctx(person, request), "auth.code_sent", "person", person.id,
              after={"purpose": "signin", "channel": channel})
    db.commit()
    where = signin.hint(channel, to)
    return {"two_step": True, "ticket": ticket, "channel": channel, "sent_to": where,
            "message": f"We sent a 6-digit code to {where}. It works for 10 minutes."}


def _pending(db: Session, ticket: str) -> tuple[OneTimeCode, Person]:
    row = db.scalar(select(OneTimeCode).where(OneTimeCode.ticket_hash == token_hash(ticket),
                                              OneTimeCode.purpose == "signin"))
    if row is None or row.used_at is not None or _aware(row.expires_at) <= utcnow():
        raise HTTPException(400, SIGNIN_EXPIRED)
    if row.attempts >= signin.MAX_TRIES:
        raise HTTPException(400, "Too many wrong codes. Enter your password again to get a new code.")
    person = db.get(Person, row.person_id)
    if person is None or person.status != "active":
        raise HTTPException(400, SIGNIN_EXPIRED)
    if (locked := lock_message(person)):
        raise HTTPException(401, locked)
    return row, person


class TicketIn(BaseModel):
    ticket: str = Field(min_length=10, max_length=200)


class CodeIn(TicketIn):
    code: str = Field(min_length=1, max_length=20)


@router.post("/login/code")
def login_code(body: CodeIn, request: Request, response: Response, db: Session = Depends(get_db)):
    limiter.check(f"login-code:{client_ip(request)}", limit=30, window_s=300)
    row, person = _pending(db, body.ticket)
    if not signin.check(row, body.code):
        dead = row.attempts >= signin.MAX_TRIES
        if dead:
            count_failure(person)           # a dead code counts towards the lockout
        audit.log(db, _system_ctx(person, request), "auth.code_failed", "person", person.id,
                  after={"purpose": "signin", "tries": row.attempts, "dead": dead})
        db.commit()
        if dead:
            raise HTTPException(400, "That code is not right, and that was the last try. "
                                     "Enter your password again to get a new code.")
        left = signin.tries_left(row)
        raise HTTPException(400, f"That code is not right. {left} "
                                 f"{'try' if left == 1 else 'tries'} left.")
    token, sess = start_session(db, person, ip=client_ip(request), device=device_of(request))
    _set_cookie(response, token)
    c = build_ctx(db, person, sess, ip=client_ip(request), device=device_of(request))
    audit.log(db, c, "person.signed_in", "person", person.id,
              after={"method": "password and code", "channel": row.channel})
    db.commit()
    return me_payload(db, c)


@router.post("/login/code/resend")
def login_code_resend(body: TicketIn, request: Request, db: Session = Depends(get_db)):
    limiter.check(f"code-resend:{client_ip(request)}", limit=10, window_s=900)
    row, person = _pending(db, body.ticket)
    if row.sends >= signin.MAX_SENDS:
        raise HTTPException(429, "You have asked for the most new codes allowed. "
                                 "Enter your password again to start over.")
    way = signin.channel_for(person)
    if way is None:
        raise HTTPException(400, SIGNIN_EXPIRED)
    channel, to = way
    code = signin.renew(row)
    row.channel = channel
    try:
        signin.deliver(channel, to, "signin", code, person.name)
    except signin.SendFailed:
        db.rollback()
        raise HTTPException(503, "We could not send a new code just now. "
                                 "Try again in a minute.") from None
    audit.log(db, _system_ctx(person, request), "auth.code_sent", "person", person.id,
              after={"purpose": "signin", "channel": channel, "resend": True})
    db.commit()
    where = signin.hint(channel, to)
    return {"ok": True, "channel": channel, "sent_to": where,
            "message": f"We sent a new 6-digit code to {where}. The old one no longer works."}


# ------------------------------------------------------------------ the company's setting

def _security_payload(db: Session, c: Ctx) -> dict:
    co = db.get(Company, c.company_id)
    return {**signin.company_settings(co), "providers": oidc.enabled(),
            "texts": signin.texts_available(), "can_edit": c.can("company.settings")}


@router.get("/security")
def get_security(c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    return _security_payload(db, c)


class SecurityIn(BaseModel):
    two_step: str | None = Field(default=None, pattern="^(off|admins|everyone)$")
    idp: bool | None = None
    # The company's own Microsoft Entra tenant id; "" clears it.
    microsoft_tenant: str | None = Field(default=None, max_length=36)


@router.put("/security")
def put_security(body: SecurityIn, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    c.require("company.settings")
    co = db.get(Company, c.company_id)
    before = signin.company_settings(co)
    after = dict(before)
    if body.two_step is not None:
        after["two_step"] = body.two_step
    if body.idp is not None:
        after["idp"] = body.idp
    if body.microsoft_tenant is not None:
        t = body.microsoft_tenant.strip().lower()
        if t and not signin.GUID.fullmatch(t):
            raise HTTPException(422, "The Microsoft tenant id looks like "
                                     "1b2c3d4e-0000-1111-2222-333344445555. Find it in Microsoft "
                                     "Entra admin centre, Overview.")
        after["microsoft_tenant"] = t or None
    co.signin = after                       # a new dict, so the change is saved
    audit.log(db, c, "company.signin_security", "company", co.id, before=before, after=after)
    db.commit()
    return _security_payload(db, c)


# ------------------------------------------------------------------ Google and Microsoft

def _oidc_done(location: str) -> Response:
    resp = RedirectResponse(location, status_code=303)
    resp.delete_cookie(oidc.COOKIE, path=oidc.COOKIE_PATH)
    return resp


def _to_signin(reason: str) -> Response:
    """Back to the sign-in screen with a short reason code (never personal data)."""
    return _oidc_done(f"/app/#/signin?sso={reason}")


@router.get("/oidc/{provider}/start")
def oidc_start(provider: str, request: Request):
    if provider not in oidc.PROVIDERS or not oidc.configured(provider):
        raise HTTPException(404, "That sign-in option is not set up here.")
    limiter.check(f"oidc:{client_ip(request)}", limit=20, window_s=300)
    url, cookie = oidc.begin(provider)
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie(oidc.COOKIE, cookie, httponly=True, secure=settings.cookie_secure,
                    samesite="lax", max_age=oidc.COOKIE_SECONDS, path=oidc.COOKIE_PATH)
    return resp


@router.get("/oidc/{provider}/callback")
def oidc_callback(provider: str, request: Request, code: str | None = None,
                  state: str | None = None, error: str | None = None,
                  db: Session = Depends(get_db)):
    if provider not in oidc.PROVIDERS or not oidc.configured(provider):
        return _to_signin("off")
    if error:
        return _to_signin("cancelled")
    limiter.check(f"oidc:{client_ip(request)}", limit=20, window_s=300)
    try:
        pending = oidc.read_cookie(request.cookies.get(oidc.COOKIE), provider)
        if not state or not hmac.compare_digest(state, pending["state"]):
            raise oidc.OidcError("failed", "state does not match")
        if not code:
            raise oidc.OidcError("failed", "no authorization code")
        tokens = oidc.exchange_code(provider, code, pending["verifier"])
        claims = oidc.verify_id_token(provider, tokens.get("id_token"), pending["nonce"])
        email = oidc.email_of(provider, claims)
    except oidc.OidcError as e:
        log.info("%s sign-in refused: %s", provider, e)
        return _to_signin(e.reason)
    person = db.scalar(select(Person).where(Person.email == email))
    if person is None or person.company_id is None or person.status == "invited":
        return _to_signin("no-account")

    def refuse(reason: str) -> Response:
        audit.log(db, _system_ctx(person, request), "auth.idp_refused", "person", person.id,
                  after={"provider": provider, "reason": reason})
        db.commit()
        return _to_signin(reason)

    if person.status != "active":
        return refuse("disabled")
    if lock_message(person):
        return refuse("locked")
    company_signin = signin.company_settings(db.get(Company, person.company_id))
    if not company_signin["idp"]:
        return refuse("not-allowed")
    if provider == "microsoft":
        # Under a multi-tenant app any organisation's tenant can put any address in the
        # email claim, so a Microsoft sign-in counts only from the tenant this company
        # named -- or from the one tenant the whole server is pinned to.
        tid = str(claims.get("tid") or "").lower()
        server_tenant = (settings.microsoft_tenant or "organizations").lower()
        pinned = bool(signin.GUID.fullmatch(server_tenant))
        mine = company_signin["microsoft_tenant"]
        if not ((pinned and tid == server_tenant and (mine is None or mine == tid))
                or (mine is not None and tid == mine)):
            return refuse("ms-tenant")
    try:
        build_ctx(db, person, None)
    except HTTPException:
        return refuse("paused")
    # A Google or Microsoft sign-in counts as strong: no extra code is asked for.
    person.last_sign_in = utcnow()
    token, sess = start_session(db, person, ip=client_ip(request), device=device_of(request))
    c = build_ctx(db, person, sess, ip=client_ip(request), device=device_of(request))
    audit.log(db, c, "person.signed_in", "person", person.id, after={"method": provider})
    db.commit()
    resp = _oidc_done("/app/#/home")
    _set_cookie(resp, token)
    return resp
