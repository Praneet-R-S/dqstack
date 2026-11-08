"""Sign-in, sign-up, sign-out, password reset and invitations (sections 5 and 13)."""

from __future__ import annotations

import re
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit, permissions
from ..config import settings
from ..db import get_db, utcnow
from ..deps import client_ip, ctx, device_of
from ..mailer import send
from ..models import Company, Invitation, PasswordReset, Person, Store
from ..ratelimit import limiter
from ..scope import Ctx, build_ctx
from ..security import (AuthError, authenticate, hash_password, new_token, password_problem,
                        revoke_all_sessions, start_session, token_hash, _aware)

router = APIRouter(prefix="/api/auth", tags=["auth"])

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
    token, sess = start_session(db, person, ip=client_ip(request), device=device_of(request))
    _set_cookie(response, token)
    c = build_ctx(db, person, sess, ip=client_ip(request), device=device_of(request))
    audit.log(db, c, "person.signed_in", "person", person.id)
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


def _system_ctx(person: Person) -> _SysCtx:
    return _SysCtx(person)
