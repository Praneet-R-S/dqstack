"""The vendor's platform portal, and a client's data when they leave (sections 2 and 13).

Platform (the vendor; sign in at /platform):
    POST /api/platform/login                    platform administrators only
    GET  /api/platform/me
    GET  /api/platform/companies                metadata only: no client records
    POST /api/platform/companies                create a company for a sale, invite its Owner
    PATCH /api/platform/companies/{id}          plan, pause or resume
    GET  /api/platform/health                   database, scheduler, failed nights
    POST /api/platform/companies/{id}/support   start a support session, only while the
                                                client's Owner grants it; read-only
    POST /api/platform/companies/{id}/purge     delete a departing client after the
                                                confirmed period

The client (Owner):
    GET  /api/company/export                    every record, CSV in a zip, with an Excel
                                                workbook of the main lists
    POST /api/company/deletion                  ask for the company to be deleted after
                                                30 days (type the company name)
    POST /api/company/deletion/cancel
"""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.orm import Session

from .. import audit, festivals
from ..config import settings
from ..db import Base, engine, get_db, utcnow
from ..deps import client_ip, ctx, device_of
from ..mailer import MailError, send
from ..models import (ActivityLog, AuthSession, Company, DailySales, ForecastRun, Invitation,
                      Person, PlatformStatus, Product, PurgePermit, Store, SupportGrant)
from ..ratelimit import limiter
from ..scope import Ctx
from ..security import (AuthError, _aware, authenticate, new_token, session_for, start_session,
                        token_hash)

router = APIRouter(prefix="/api", tags=["platform"])
DELETION_DAYS = 30
SECRET_COLUMNS = {"password_hash", "manager_pin_hash", "token_hash", "csrf_token", "key_hash",
                  "content", "credentials"}


# ===================================================================== platform auth

def platform_admin(request: Request, db: Session = Depends(get_db)) -> Person:
    found = session_for(db, request.cookies.get(settings.cookie_name))
    if found is None:
        raise HTTPException(401, "Please sign in to continue.")
    sess, person = found
    if not person.is_platform_admin or sess.company_id is not None:
        raise HTTPException(403, "This is for DQ-Stack platform administrators.")
    if request.method != "GET" and request.headers.get("X-CSRF-Token") != sess.csrf_token:
        raise HTTPException(403, "This request was refused because it did not come from the app.")
    return person


def _cookie(resp: Response, token: str) -> None:
    resp.set_cookie(settings.cookie_name, token, httponly=True, samesite="lax",
                    secure=settings.cookie_secure, path="/",
                    max_age=settings.session_hours * 3600)


class LoginIn(BaseModel):
    login: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=200)


@router.post("/platform/login")
def platform_login(body: LoginIn, request: Request, response: Response,
                   db: Session = Depends(get_db)):
    limiter.check(f"plogin:{client_ip(request)}", limit=10, window_s=300)
    try:
        person = authenticate(db, body.login, body.password)
    except AuthError as e:
        raise HTTPException(401, str(e)) from None
    if not person.is_platform_admin:
        raise HTTPException(401, "That email or phone and password do not match.")
    token, sess = start_session(db, person, ip=client_ip(request), device=device_of(request))
    _cookie(response, token)
    db.commit()
    return {"name": person.name, "email": person.email, "csrf": sess.csrf_token}


@router.get("/platform/me")
def platform_me(request: Request, person: Person = Depends(platform_admin),
                db: Session = Depends(get_db)):
    sess, _ = session_for(db, request.cookies.get(settings.cookie_name))
    return {"name": person.name, "email": person.email, "csrf": sess.csrf_token}


# ===================================================================== companies

def _company_row(db: Session, co: Company) -> dict:
    n = lambda model, *w: db.scalar(select(func.count()).select_from(model).where(
        model.company_id == co.id, *w)) or 0
    last_run = db.scalar(select(ForecastRun).where(ForecastRun.company_id == co.id)
                         .order_by(ForecastRun.created_at.desc()).limit(1))
    last_act = db.scalar(select(func.max(ActivityLog.at)).where(ActivityLog.company_id == co.id))
    grant = db.scalar(select(SupportGrant).where(
        SupportGrant.company_id == co.id, SupportGrant.revoked_at.is_(None))
        .order_by(SupportGrant.expires_at.desc()).limit(1))
    granted = grant is not None and _aware(grant.expires_at) > utcnow()
    return {"id": co.id, "name": co.name, "plan": co.plan, "status": co.status,
            "is_demo": co.is_demo, "created_at": co.created_at.isoformat() if co.created_at else None,
            "stores": n(Store), "people": n(Person), "products": n(Product),
            "sales_days": db.scalar(select(func.count(func.distinct(DailySales.day))).where(
                DailySales.company_id == co.id)) or 0,
            "last_forecast": None if last_run is None else {
                "kind": last_run.kind, "status": last_run.status,
                "at": last_run.created_at.isoformat() if last_run.created_at else None},
            "last_activity": last_act.isoformat() if last_act else None,
            "support_granted_until": grant.expires_at.isoformat() if granted else None,
            "support_reason": grant.reason if granted else None,
            "deletion_due": (co.setup or {}).get("deletion_due")}


@router.get("/platform/companies")
def companies(person: Person = Depends(platform_admin), db: Session = Depends(get_db)):
    return [_company_row(db, co) for co in db.scalars(select(Company).order_by(Company.name))]


class NewCompanyIn(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    owner_name: str = Field(min_length=1, max_length=200)
    owner_email: str = Field(min_length=3, max_length=254)
    plan: str = Field(default="trial", max_length=32)


@router.post("/platform/companies", status_code=201)
def create_company(body: NewCompanyIn, person: Person = Depends(platform_admin),
                   db: Session = Depends(get_db)):
    """Created by the vendor for a sale: the company and an invitation for its Owner,
    who sets their own password. The vendor never knows it."""
    from .auth import EMAIL_RE
    email = body.owner_email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(422, "Enter a valid email address.")
    if db.scalar(select(Person.id).where(Person.email == email)):
        raise HTTPException(409, "Someone with that email already has an account.")
    co = Company(name=body.name.strip(), plan=body.plan, timezone=settings.default_timezone,
                 setup={"company": utcnow().isoformat()})
    db.add(co)
    db.flush()
    festivals.seed(db, co.id)
    owner = Person(company_id=co.id, name=body.owner_name.strip(), email=email, role="owner",
                   status="invited")
    db.add(owner)
    token = new_token()
    db.add(Invitation(company_id=co.id, token_hash=token_hash(token), name=owner.name,
                      email=email, role="owner", store_ids=[], invited_by=person.id,
                      expires_at=utcnow() + timedelta(days=14)))
    db.add(ActivityLog(company_id=co.id, person_id=None, action="company.created_by_platform",
                       entity="company", entity_id=co.id, after={"by": person.name}))
    db.commit()
    link = f"{settings.public_url}/app/#/invite/{token}"
    delivered = None
    try:
        send(email, f"Your DQ-Stack company {co.name} is ready",
             f"Hello {owner.name},\n\n{co.name} has been set up on DQ-Stack. Choose your "
             f"password here within 14 days to start:\n{link}\n")
        delivered = "email"
    except MailError:
        pass
    return {**_company_row(db, co), "invite_link": link, "delivered_by": delivered}


class CompanyPatchIn(BaseModel):
    plan: str | None = Field(default=None, max_length=32)
    status: str | None = None


@router.patch("/platform/companies/{company_id}")
def update_company(company_id: str, body: CompanyPatchIn, person: Person = Depends(platform_admin),
                   db: Session = Depends(get_db)):
    co = db.get(Company, company_id)
    if co is None:
        raise HTTPException(404, "That company was not found.")
    if body.status is not None:
        if body.status not in ("active", "suspended"):
            raise HTTPException(422, "Status must be active or suspended.")
        co.status = body.status
    if body.plan is not None:
        co.plan = body.plan
    db.add(ActivityLog(company_id=co.id, action="company.changed_by_platform", entity="company",
                       entity_id=co.id, after={"by": person.name, **body.model_dump(exclude_none=True)}))
    db.commit()
    return _company_row(db, co)


@router.get("/platform/health")
def health(person: Person = Depends(platform_admin), db: Session = Depends(get_db)):
    since = utcnow() - timedelta(hours=24)
    beat = db.get(PlatformStatus, "worker")
    failed = db.scalars(select(ForecastRun).where(ForecastRun.status == "failed",
                                                  ForecastRun.created_at >= since)).all()
    names = {c.id: c.name for c in db.scalars(select(Company))}
    worker_age = None
    if beat is not None:
        worker_age = round((utcnow() - _aware(beat.updated_at)).total_seconds() / 60, 1)
    with engine().connect() as conn:
        conn.execute(text("SELECT 1"))
    return {"database": "ok", "version": __import__("dqstack.app.main", fromlist=["VERSION"]).VERSION,
            "worker_last_pass_minutes": worker_age,
            "worker_ok": worker_age is not None and worker_age < 10,
            "failed_runs_24h": [{"company": names.get(r.company_id), "kind": r.kind,
                                 "message": r.message,
                                 "at": r.created_at.isoformat() if r.created_at else None}
                                for r in failed],
            "queued_runs": db.scalar(select(func.count()).select_from(ForecastRun).where(
                ForecastRun.status.in_(("queued", "running")))) or 0,
            "companies": db.scalar(select(func.count()).select_from(Company)) or 0}


@router.post("/platform/companies/{company_id}/support")
def start_support(company_id: str, request: Request, response: Response,
                  person: Person = Depends(platform_admin), db: Session = Depends(get_db)):
    """Only while the client's Owner has granted it; read-only; logged in the client's
    activity log. Ends when the grant ends or the session is signed out."""
    grant = db.scalar(select(SupportGrant).where(
        SupportGrant.company_id == company_id, SupportGrant.revoked_at.is_(None))
        .order_by(SupportGrant.expires_at.desc()).limit(1))
    if grant is None or _aware(grant.expires_at) <= utcnow():
        raise HTTPException(403, "This company has not granted support access. Ask its Owner "
                                 "to grant it under Settings > Support access.")
    token, sess = start_session(db, person, ip=client_ip(request), device=device_of(request))
    sess.company_id = company_id
    sess.expires_at = min(_aware(sess.expires_at), _aware(grant.expires_at))
    db.add(ActivityLog(company_id=company_id, person_id=person.id, action="support.started",
                       entity="support_grant", entity_id=grant.id,
                       after={"by": person.name, "reason": grant.reason}, ip=client_ip(request)))
    db.commit()
    _cookie(response, token)
    return {"ok": True, "go_to": "/app/#/home", "until": grant.expires_at.isoformat(),
            "read_only": True}


# ===================================================================== leaving

def _company_tables():
    """Every table holding company records, children before parents."""
    return [t for t in reversed(Base.metadata.sorted_tables)
            if "company_id" in t.c and t.name != "purge_permits"]


def purge_company(db: Session, company_id: str) -> dict[str, int]:
    """Delete everything a company owns. The append-only triggers allow it only while
    this company's purge permit exists, inside this transaction."""
    counts = {}
    db.execute(insert(PurgePermit.__table__).values(company_id=company_id, reason="client left",
                                                    created_at=utcnow()))
    for t in _company_tables():
        res = db.execute(delete(t).where(t.c.company_id == company_id))
        if res.rowcount:
            counts[t.name] = res.rowcount
    db.execute(delete(Company.__table__).where(Company.__table__.c.id == company_id))
    db.execute(delete(PurgePermit.__table__).where(PurgePermit.company_id == company_id))
    return counts


@router.post("/platform/companies/{company_id}/purge")
def purge(company_id: str, person: Person = Depends(platform_admin), db: Session = Depends(get_db)):
    co = db.get(Company, company_id)
    if co is None:
        raise HTTPException(404, "That company was not found.")
    due = (co.setup or {}).get("deletion_due")
    if co.status != "closing" or not due:
        raise HTTPException(409, "This company has not asked to be deleted.")
    from datetime import datetime
    if datetime.fromisoformat(due) > utcnow():
        raise HTTPException(409, f"The confirmed period runs until {due[:10]}.")
    counts = purge_company(db, company_id)
    db.commit()
    return {"deleted": counts}


@router.get("/company/export")
def export(c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    """Every record the company owns, as CSV files in one zip, with an Excel workbook of
    the main lists. Secrets (password and PIN hashes, tokens, file contents) are left out."""
    if c.role != "owner":
        raise HTTPException(403, "Only an Owner can export all of the company's data.")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for t in _company_tables() + [Company.__table__]:
            cols = [col for col in t.c if col.name not in SECRET_COLUMNS]
            key = t.c.id if t.name == "companies" else t.c.company_id
            out = io.StringIO()
            w = csv.writer(out)
            w.writerow([col.name for col in cols])
            for row in db.execute(select(*cols).where(key == c.company_id)):
                w.writerow(["" if v is None else v for v in row])
            z.writestr(f"{t.name}.csv", "﻿" + out.getvalue())
        z.writestr("main-lists.xlsx", _workbook(db, c.company_id))
        z.writestr("README.txt", "Every record of your company on DQ-Stack, one CSV file per "
                                 "table. Quantities ending _milli are thousandths of a unit; "
                                 "amounts ending _paise are paise (divide by 100 for rupees).\n")
    audit.log(db, c, "company.exported", "company", c.company_id)
    db.commit()
    return Response(buf.getvalue(), media_type="application/zip", headers={
        "Content-Disposition": 'attachment; filename="dqstack-export.zip"'})


def _workbook(db: Session, company_id: str) -> bytes:
    from openpyxl import Workbook
    from ..models import StockOnHand, Supplier
    wb = Workbook()
    wb.remove(wb.active)
    sheets = (("Stores", Store, ["code", "name", "address", "state", "mode"]),
              ("Products", Product, ["sku", "name", "category", "unit", "mrp_paise",
                                     "price_paise", "cost_paise", "gst_rate_bp", "status"]),
              ("Suppliers", Supplier, ["name", "gstin", "contact", "email", "phone",
                                       "lead_time_days", "min_order_paise"]),
              ("People", Person, ["name", "email", "phone", "role", "status"]),
              ("Stock on hand", StockOnHand, ["store_id", "product_id", "on_hand_milli",
                                              "avg_cost_paise", "known"]))
    for title, model, cols in sheets:
        ws = wb.create_sheet(title)
        ws.append(cols)
        for row in db.execute(select(*[getattr(model, c) for c in cols]).where(
                model.company_id == company_id)):
            ws.append(list(row))
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


class DeletionIn(BaseModel):
    confirm_name: str


@router.post("/company/deletion")
def request_deletion(body: DeletionIn, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    if c.role != "owner":
        raise HTTPException(403, "Only an Owner can ask for the company to be deleted.")
    co = db.get(Company, c.company_id)
    if body.confirm_name.strip() != co.name:
        raise HTTPException(422, "Type the company's name exactly to confirm.")
    due = utcnow() + timedelta(days=DELETION_DAYS)
    co.status = "closing"
    co.setup = {**(co.setup or {}), "deletion_due": due.isoformat()}
    audit.log(db, c, "company.deletion_requested", "company", co.id,
              after={"deletion_due": due.isoformat()})
    db.commit()
    return {"status": co.status, "deletion_due": due.isoformat(),
            "message": f"Everything will be deleted on {due:%d %b %Y}. Export your data before "
                       "then. You can cancel until that day."}


@router.post("/company/deletion/cancel")
def cancel_deletion(c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    if c.role != "owner":
        raise HTTPException(403, "Only an Owner can cancel the deletion.")
    co = db.get(Company, c.company_id)
    co.status = "active"
    co.setup = {k: v for k, v in (co.setup or {}).items() if k != "deletion_due"}
    audit.log(db, c, "company.deletion_cancelled", "company", co.id)
    db.commit()
    return {"status": co.status}
