"""Company settings, stores, people and the activity log (sections 2, 5 and 12)."""

from __future__ import annotations

import re
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit, rules
from ..config import settings
from ..db import get_db, utcnow
from ..deps import ctx
from ..mailer import MailError, send
from ..models import (ROLES, ActivityLog, Company, Invitation, Person, PersonStore, Store,
                      assign_stores,
                      SupportGrant)
from ..scope import Ctx, get_or_404, store_or_404
from ..security import new_token, revoke_all_sessions, token_hash

router = APIRouter(prefix="/api", tags=["organisation"])

STATE_NAMES = (
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh", "Goa", "Gujarat",
    "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka", "Kerala", "Madhya Pradesh",
    "Maharashtra", "Manipur", "Meghalaya", "Mizoram", "Nagaland", "Odisha", "Punjab",
    "Rajasthan", "Sikkim", "Tamil Nadu", "Telangana", "Tripura", "Uttar Pradesh",
    "Uttarakhand", "West Bengal", "Delhi", "Jammu and Kashmir", "Ladakh", "Puducherry",
    "Chandigarh", "Andaman and Nicobar Islands", "Dadra and Nagar Haveli and Daman and Diu",
    "Lakshadweep")
GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


# ------------------------------------------------------------------ company

@router.get("/company")
def get_company(c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    co = db.get(Company, c.company_id)
    return {"id": co.id, "name": co.name, "gstin": co.gstin, "timezone": co.timezone,
            "currency": co.currency, "fy_start_month": co.fy_start_month, "plan": co.plan,
            "rules": rules.effective(co.rules), "rule_defaults": rules.DEFAULTS,
            "is_demo": co.is_demo}


class CompanyPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=200)
    gstin: str | None = Field(default=None, max_length=15)
    timezone: str | None = Field(default=None, max_length=64)
    fy_start_month: int | None = Field(default=None, ge=1, le=12)
    rules: dict | None = None


@router.patch("/company")
def patch_company(body: CompanyPatch, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    c.require("company.settings")
    co = db.get(Company, c.company_id)
    before = audit.snapshot(co)
    if body.gstin is not None:
        g = body.gstin.strip().upper()
        if g and not GSTIN_RE.match(g):
            raise HTTPException(422, "That GSTIN is not in the right format (15 characters, "
                                     "for example 27AAPFU0939F1ZV).")
        co.gstin = g or None
    if body.rules is not None:
        problems = rules.validate(body.rules)
        if problems:
            raise HTTPException(422, " ".join(problems))
        co.rules = {**(co.rules or {}), **body.rules}
        co.setup = {**(co.setup or {}), "rules": utcnow().isoformat()}
    for f in ("name", "timezone", "fy_start_month"):
        v = getattr(body, f)
        if v is not None:
            setattr(co, f, v)
    audit.log(db, c, "company.updated", "company", co.id, before, audit.snapshot(co))
    db.commit()
    return get_company(c, db)


# ------------------------------------------------------------------ stores

def store_dict(s: Store) -> dict:
    return {"id": s.id, "code": s.code, "name": s.name, "address": s.address, "state": s.state,
            "opening_hours": s.opening_hours, "day_close_time": s.day_close_time,
            "weekly_off": s.weekly_off, "size": s.size, "mode": s.mode, "active": s.active}


@router.get("/stores")
def list_stores(c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    q = select(Store).where(Store.company_id == c.company_id).order_by(Store.code)
    if not c.all_stores:
        q = q.where(Store.id.in_(c.store_ids or {"__none__"}))
    return [store_dict(s) for s in db.scalars(q)]


@router.get("/stores/{store_id}")
def get_store(store_id: str, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    return store_dict(store_or_404(db, c, store_id))


class StoreIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=200)
    address: str | None = Field(default=None, max_length=2000)
    state: str | None = Field(default=None, max_length=64)
    opening_hours: str | None = Field(default=None, max_length=64)
    day_close_time: str = "22:00"
    weekly_off: list[int] = Field(default_factory=list)
    size: str | None = Field(default=None, max_length=16)
    mode: str = "standalone"


def _check_store(body) -> None:
    if body.state and body.state not in STATE_NAMES:
        raise HTTPException(422, f"'{body.state}' is not an Indian state or union territory.")
    if getattr(body, "day_close_time", None) and not TIME_RE.match(body.day_close_time):
        raise HTTPException(422, "Day-close time must look like 22:00.")
    if getattr(body, "mode", None) and body.mode not in ("standalone", "connected"):
        raise HTTPException(422, "Mode must be standalone or connected.")
    if any(d not in range(7) for d in (getattr(body, "weekly_off", None) or [])):
        raise HTTPException(422, "Weekly off days are 0 (Monday) to 6 (Sunday).")


@router.post("/stores", status_code=201)
def create_store(body: StoreIn, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    c.require("company.settings")
    _check_store(body)
    code = body.code.strip().upper()
    if db.scalar(select(Store.id).where(Store.company_id == c.company_id, Store.code == code)):
        raise HTTPException(409, f"A store with code {code} already exists.")
    s = Store(company_id=c.company_id, **{**body.model_dump(), "code": code})
    db.add(s)
    db.flush()
    co = db.get(Company, c.company_id)
    co.setup = {**(co.setup or {}), "stores": co.setup.get("stores") or utcnow().isoformat()}
    audit.log(db, c, "store.created", "store", s.id, after=store_dict(s), store_id=s.id)
    db.commit()
    return store_dict(s)


class StorePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    address: str | None = None
    state: str | None = None
    opening_hours: str | None = None
    day_close_time: str | None = None
    weekly_off: list[int] | None = None
    size: str | None = None
    mode: str | None = None
    active: bool | None = None


@router.patch("/stores/{store_id}")
def patch_store(store_id: str, body: StorePatch, c: Ctx = Depends(ctx),
                db: Session = Depends(get_db)):
    c.require("company.settings")
    s = store_or_404(db, c, store_id)
    _check_store(body)
    before = store_dict(s)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(s, k, v)
    audit.log(db, c, "store.updated", "store", s.id, before, store_dict(s), store_id=s.id)
    db.commit()
    return store_dict(s)


# ------------------------------------------------------------------ people

def person_dict(p: Person, now=None) -> dict:
    from ..security import _aware
    locked = _aware(p.locked_until)
    return {"id": p.id, "name": p.name, "email": p.email, "phone": p.phone, "role": p.role,
            "status": p.status, "language": p.language,
            "locked": bool(locked and locked > (now or utcnow())),
            "store_ids": [s.id for s in p.stores],
            "last_sign_in": p.last_sign_in.isoformat() if p.last_sign_in else None}


@router.get("/people")
def list_people(c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    c.require("people.manage")
    q = select(Person).where(Person.company_id == c.company_id).order_by(Person.name)
    return [person_dict(p) for p in db.scalars(q)]


@router.get("/people/{person_id}")
def get_person(person_id: str, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    if person_id != c.person.id:
        c.require("people.manage")
    p = get_or_404(db, c, Person, person_id, store_attr=None, what="That person")
    return person_dict(p)


class InviteIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=20)
    role: str
    store_ids: list[str] = Field(default_factory=list)


def _valid_stores(db: Session, c: Ctx, ids: list[str]) -> list[Store]:
    out = []
    for sid in ids:
        out.append(store_or_404(db, c, sid))
    return out


@router.post("/people/invite", status_code=201)
def invite(body: InviteIn, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    c.require("people.manage")
    from .auth import EMAIL_RE
    if body.role not in ROLES:
        raise HTTPException(422, "That role does not exist.")
    if body.role == "owner" and c.role != "owner":
        raise HTTPException(403, "Only an Owner can invite another Owner.")
    email = (body.email or "").strip().lower() or None
    phone = (body.phone or "").strip() or None
    if not email and not phone:
        raise HTTPException(422, "Give an email address or a phone number.")
    if email and not EMAIL_RE.match(email):
        raise HTTPException(422, "Enter a valid email address.")
    if body.role not in ("owner", "admin") and not body.store_ids:
        raise HTTPException(422, "Choose at least one store for this person.")
    stores = _valid_stores(db, c, body.store_ids)
    if email and db.scalar(select(Person.id).where(Person.email == email)):
        raise HTTPException(409, "Someone with that email already has an account.")
    if phone and db.scalar(select(Person.id).where(Person.phone == phone)):
        raise HTTPException(409, "Someone with that phone number already has an account.")
    p = Person(company_id=c.company_id, name=body.name.strip(), email=email, phone=phone,
               role=body.role, status="invited")
    db.add(p)
    db.flush()
    assign_stores(db, p, stores)
    token = new_token()
    inv = Invitation(company_id=c.company_id, token_hash=token_hash(token), name=p.name,
                     email=email, phone=phone, role=body.role, store_ids=body.store_ids,
                     invited_by=c.person.id, expires_at=utcnow() + timedelta(days=7))
    db.add(inv)
    db.flush()
    co = db.get(Company, c.company_id)
    co.setup = {**(co.setup or {}), "people": co.setup.get("people") or utcnow().isoformat()}
    audit.log(db, c, "person.invited", "person", p.id, after=person_dict(p))
    db.commit()
    link = f"{settings.public_url}/app/#/invite/{token}"
    delivered = None
    if email:
        try:
            send(email, f"You're invited to {co.name} on DQ-Stack",
                 f"Hello {p.name},\n\n{c.person.name} has invited you to join {co.name} on "
                 f"DQ-Stack as {body.role.replace('_', ' ')}.\n\nSet your password here within "
                 f"7 days:\n{link}\n")
            delivered = "email"
        except MailError:
            delivered = None
    # The link is returned so an administrator can pass it on by phone or WhatsApp
    # when the person has no email, or when email is not configured.
    return {**person_dict(p), "invite_link": link, "delivered_by": delivered}


class PersonPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    role: str | None = None
    store_ids: list[str] | None = None
    language: str | None = Field(default=None, max_length=8)


@router.patch("/people/{person_id}")
def patch_person(person_id: str, body: PersonPatch, c: Ctx = Depends(ctx),
                 db: Session = Depends(get_db)):
    c.require("people.manage")
    p = get_or_404(db, c, Person, person_id, store_attr=None, what="That person")
    before = person_dict(p)
    if body.role is not None:
        if body.role not in ROLES:
            raise HTTPException(422, "That role does not exist.")
        if (body.role == "owner" or p.role == "owner") and c.role != "owner":
            raise HTTPException(403, "Only an Owner can change an Owner's role.")
        if p.role == "owner" and body.role != "owner":
            owners = db.scalar(select(func.count()).select_from(Person).where(
                Person.company_id == c.company_id, Person.role == "owner",
                Person.status == "active"))
            if owners <= 1:
                raise HTTPException(409, "A company must keep at least one Owner.")
        p.role = body.role
    if body.store_ids is not None:
        assign_stores(db, p, _valid_stores(db, c, body.store_ids))
    if body.name is not None:
        p.name = body.name.strip()
    if body.language is not None:
        p.language = body.language
    audit.log(db, c, "person.updated", "person", p.id, before, person_dict(p))
    db.commit()
    return person_dict(p)


@router.post("/people/{person_id}/disable")
def disable_person(person_id: str, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    """Signed out everywhere immediately; history kept (section 13)."""
    c.require("people.manage")
    p = get_or_404(db, c, Person, person_id, store_attr=None, what="That person")
    if p.id == c.person.id:
        raise HTTPException(409, "You cannot disable your own account.")
    if p.role == "owner" and c.role != "owner":
        raise HTTPException(403, "Only an Owner can disable an Owner.")
    before = person_dict(p)
    p.status = "disabled"
    n = revoke_all_sessions(db, p.id)
    audit.log(db, c, "person.disabled", "person", p.id, before,
              {**person_dict(p), "sessions_ended": n})
    db.commit()
    return person_dict(p)


@router.post("/people/{person_id}/enable")
def enable_person(person_id: str, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    c.require("people.manage")
    p = get_or_404(db, c, Person, person_id, store_attr=None, what="That person")
    before = person_dict(p)
    p.status = "active" if p.password_hash else "invited"
    audit.log(db, c, "person.enabled", "person", p.id, before, person_dict(p))
    db.commit()
    return person_dict(p)


@router.post("/people/{person_id}/unlock")
def unlock_person(person_id: str, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    c.require("people.manage")
    p = get_or_404(db, c, Person, person_id, store_attr=None, what="That person")
    p.locked_until = None
    p.failed_attempts = 0
    audit.log(db, c, "person.unlocked", "person", p.id)
    db.commit()
    return person_dict(p)


@router.post("/people/{person_id}/reset-password")
def admin_reset_password(person_id: str, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    """'Admin can also reset' (section 13). The person gets a link to choose a new
    password; nobody, the administrator included, ever learns it. The link is also
    returned so it can be passed on by phone or WhatsApp to someone without email.
    Their sessions end now, in case the reason is a lost or shared device."""
    c.require("people.manage")
    from ..models import PasswordReset
    p = get_or_404(db, c, Person, person_id, store_attr=None, what="That person")
    if p.role == "owner" and c.role != "owner":
        raise HTTPException(403, "Only an Owner can reset an Owner's password.")
    if p.status == "invited":
        raise HTTPException(409, "This person has not joined yet. Send the invitation again "
                                 "instead.")
    if p.status == "disabled":
        raise HTTPException(409, "This account is disabled. Enable it first.")
    token = new_token()
    db.add(PasswordReset(token_hash=token_hash(token), person_id=p.id, company_id=p.company_id,
                         expires_at=utcnow() + timedelta(hours=24)))
    p.failed_attempts = 0
    p.locked_until = None
    n = revoke_all_sessions(db, p.id)
    audit.log(db, c, "person.reset_by_admin", "person", p.id, after={"sessions_ended": n})
    db.commit()
    link = f"{settings.public_url}/app/#/reset/{token}"
    delivered = None
    if p.email:
        try:
            co = db.get(Company, c.company_id)
            send(p.email, "Choose a new DQ-Stack password",
                 f"Hello {p.name},\n\n{c.person.name} at {co.name} has asked DQ-Stack to "
                 f"let you choose a new password. Use this link within 24 hours:\n{link}\n")
            delivered = "email"
        except MailError:
            delivered = None
    return {**person_dict(p), "reset_link": link, "delivered_by": delivered}


@router.post("/people/{person_id}/resend-invite")
def resend_invite(person_id: str, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    """A fresh link for someone who has not joined yet. Earlier links stop working."""
    c.require("people.manage")
    p = get_or_404(db, c, Person, person_id, store_attr=None, what="That person")
    if p.status != "invited":
        raise HTTPException(409, "This person has already joined." if p.status == "active"
                            else "This account is disabled. Enable it first.")
    now = utcnow()
    match = (Invitation.email == p.email) if p.email else (Invitation.phone == p.phone)
    for old in db.scalars(select(Invitation).where(Invitation.company_id == c.company_id,
                                                   Invitation.accepted_at.is_(None), match)):
        old.expires_at = now
    token = new_token()
    db.add(Invitation(company_id=c.company_id, token_hash=token_hash(token), name=p.name,
                      email=p.email, phone=p.phone, role=p.role,
                      store_ids=[st.id for st in p.stores], invited_by=c.person.id,
                      expires_at=now + timedelta(days=7)))
    audit.log(db, c, "person.reinvited", "person", p.id)
    db.commit()
    link = f"{settings.public_url}/app/#/invite/{token}"
    delivered = None
    if p.email:
        try:
            co = db.get(Company, c.company_id)
            send(p.email, f"Your invitation to {co.name} on DQ-Stack",
                 f"Hello {p.name},\n\nHere is a new link to join {co.name} on DQ-Stack. "
                 f"Set your password within 7 days:\n{link}\n")
            delivered = "email"
        except MailError:
            delivered = None
    return {**person_dict(p), "invite_link": link, "delivered_by": delivered}


class PinIn(BaseModel):
    pin: str = Field(min_length=4, max_length=8, pattern=r"^\d+$")


@router.post("/people/me/manager-pin")
def set_manager_pin(body: PinIn, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    """A manager PIN authorises refunds and price overrides at the till (section 7)."""
    if not c.can("sales.refund"):
        raise HTTPException(403, "Only managers set a manager PIN.")
    from ..security import hash_password
    p = db.get(Person, c.person.id)
    p.manager_pin_hash = hash_password(body.pin)
    audit.log(db, c, "person.pin_set", "person", p.id)
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ activity log

@router.get("/activity")
def activity(c: Ctx = Depends(ctx), db: Session = Depends(get_db),
             limit: int = Query(100, ge=1, le=1000), entity: str | None = None,
             person_id: str | None = None):
    c.require("activity.view")
    q = select(ActivityLog).where(ActivityLog.company_id == c.company_id)
    if not c.all_stores:
        q = q.where(ActivityLog.store_id.in_(c.store_ids or {"__none__"}))
    if entity:
        q = q.where(ActivityLog.entity == entity)
    if person_id:
        q = q.where(ActivityLog.person_id == person_id)
    rows = db.scalars(q.order_by(ActivityLog.at.desc()).limit(limit)).all()
    names = {p.id: p.name for p in db.scalars(select(Person).where(
        Person.company_id == c.company_id))}
    return [{"id": r.id, "at": r.at.isoformat(), "action": r.action, "entity": r.entity,
             "entity_id": r.entity_id, "person": names.get(r.person_id), "device": r.device,
             "store_id": r.store_id, "before": r.before, "after": r.after} for r in rows]


# ------------------------------------------------------------------ support access

class GrantIn(BaseModel):
    reason: str = Field(min_length=5, max_length=500)
    hours: int = Field(default=24, ge=1, le=168)


@router.get("/company/support-grants")
def list_support(c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    """Every grant this company has made, newest first, with whether it is open now."""
    if c.role != "owner":
        raise HTTPException(403, "Only an Owner can see support access.")
    names = {p.id: p.name for p in db.scalars(select(Person).where(
        Person.company_id == c.company_id))}
    now = utcnow()
    out = []
    for g in db.scalars(select(SupportGrant).where(SupportGrant.company_id == c.company_id)
                        .order_by(SupportGrant.created_at.desc()).limit(100)):
        exp = g.expires_at if g.expires_at.tzinfo else g.expires_at.replace(tzinfo=now.tzinfo)
        out.append({"id": g.id, "reason": g.reason, "granted_by": names.get(g.granted_by),
                    "created_at": g.created_at.isoformat() if g.created_at else None,
                    "expires_at": g.expires_at.isoformat(),
                    "revoked_at": g.revoked_at.isoformat() if g.revoked_at else None,
                    "active": g.revoked_at is None and exp > now})
    return out


@router.post("/company/support-grants", status_code=201)
def grant_support(body: GrantIn, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    """The client, not the vendor, opens the door, for a limited time, on the record."""
    if c.role != "owner":
        raise HTTPException(403, "Only an Owner can grant support access.")
    g = SupportGrant(company_id=c.company_id, granted_by=c.person.id, reason=body.reason,
                     expires_at=utcnow() + timedelta(hours=body.hours))
    db.add(g)
    db.flush()
    audit.log(db, c, "support.granted", "support_grant", g.id,
              after={"reason": body.reason, "hours": body.hours})
    db.commit()
    return {"id": g.id, "expires_at": g.expires_at.isoformat()}


@router.post("/company/support-grants/{grant_id}/revoke")
def revoke_support(grant_id: str, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    if c.role != "owner":
        raise HTTPException(403, "Only an Owner can revoke support access.")
    g = get_or_404(db, c, SupportGrant, grant_id, store_attr=None, what="That grant")
    g.revoked_at = utcnow()
    audit.log(db, c, "support.revoked", "support_grant", g.id)
    db.commit()
    return {"ok": True}
