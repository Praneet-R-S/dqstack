"""The Connectors screen: Tally files out and in, and the Zoho Inventory connection.
Owners and Admins only (`integrations.manage`).

    GET  /api/connectors                          both connectors, their settings, last runs
    GET  /api/connectors/runs                     the sync log

    PUT  /api/connectors/tally/settings           ledger names and choices
    GET  /api/connectors/tally/export             ?start&end&stores=a,b&part=zip|masters|vouchers
    POST /api/connectors/tally/import             multipart: kind, file, store_id -> an import job

    POST /api/connectors/zoho/connect             client id and secret -> Zoho's consent page
    GET  /api/connectors/zoho/callback            where Zoho sends the browser back
    POST /api/connectors/zoho/code                a self client's code, pasted
    PUT  /api/connectors/zoho/settings            organisation, warehouse map, choices
    POST /api/connectors/zoho/warehouses          read the warehouse list again
    POST /api/connectors/zoho/sync                "Sync now" (or everything again)
    POST /api/connectors/zoho/disconnect
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..config import settings as app_settings
from ..connectors import secrets as vault
from ..connectors import tally as T
from ..connectors import zoho as Z
from ..connectors.importing import make_job
from ..db import get_db, utcnow
from ..deps import ctx, optional_ctx
from ..imports import parsing as P
from ..models import Connector, ConnectorRun, Person, Store
from ..ratelimit import limiter
from ..scope import Ctx, store_or_404
from ..security import _aware
from .imports import job_dict

router = APIRouter(prefix="/api/connectors", tags=["connectors"])


def _admin(c: Ctx) -> None:
    c.require("integrations.manage")


def _run_dict(r: ConnectorRun, names: dict | None = None) -> dict:
    return {"id": r.id, "kind": r.kind, "trigger": r.trigger, "status": r.status,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "message": r.message, "summary": r.summary, "import_job_ids": r.import_job_ids,
            "person": (names or {}).get(r.person_id)}


def _runs(db: Session, company_id: str, kind: str | None, limit: int = 20) -> list[dict]:
    q = select(ConnectorRun).where(ConnectorRun.company_id == company_id)
    if kind:
        q = q.where(ConnectorRun.kind == kind)
    names = {p.id: p.name for p in db.scalars(select(Person).where(
        Person.company_id == company_id))}
    return [_run_dict(r, names) for r in db.scalars(
        q.order_by(ConnectorRun.started_at.desc()).limit(limit))]


def _zoho_public(conn: Connector) -> dict:
    s = Z.settings_of(conn)
    creds = vault.unseal(conn.credentials) or {}
    return {"status": conn.status, "last_sync_at": conn.last_sync_at.isoformat()
            if conn.last_sync_at else None, "last_error": conn.last_error,
            "client_id": creds.get("client_id") or "",
            "has_secret": bool(creds.get("client_secret")),
            "settings": {k: s[k] for k in ("data_centre", "organization_id", "organization_name",
                                           "organizations", "sales_source", "sales_into",
                                           "warehouse_map", "default_store_id", "warehouses",
                                           "start_date", "sync_time")},
            "sync_requested": bool(s.get("sync_requested")),
            "data_centres": [{"value": k, "label": v[2]} for k, v in Z.DATA_CENTRES.items()],
            "scopes": list(Z.SCOPES), "redirect_uri": Z.redirect_uri()}


@router.get("")
def overview(c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    _admin(c)
    tally = T.get_connector(db, c.company_id, "tally")
    zoho = Z.connector(db, c.company_id)
    db.commit()
    return {"tally": {"settings": T.settings_of(tally), "defaults": T.DEFAULTS,
                      "runs": _runs(db, c.company_id, "tally", 10)},
            "zoho": {**_zoho_public(zoho), "runs": _runs(db, c.company_id, "zoho", 20)},
            "inline": app_settings.inline_jobs}


@router.get("/runs")
def runs(kind: str | None = None, c: Ctx = Depends(ctx), db: Session = Depends(get_db),
         limit: int = Query(50, ge=1, le=200)):
    _admin(c)
    return _runs(db, c.company_id, kind, limit)


# =================================================================== Tally

@router.put("/tally/settings")
def tally_settings(body: dict, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    _admin(c)
    conn = T.get_connector(db, c.company_id, "tally")
    clean = T.clean_settings(body)
    before = dict(conn.settings or {})
    conn.settings = {**before, **clean}
    conn.updated_at = utcnow()
    audit.log(db, c, "connector.tally_settings", "connector", conn.id, before=before,
              after=conn.settings)
    db.commit()
    return T.settings_of(conn)


def _stores_param(db: Session, c: Ctx, raw: str | None) -> list[str]:
    ids = [x for x in re.split(r"[,\s]+", raw or "") if x]
    if not ids:
        return c.visible_store_ids(db)
    return [store_or_404(db, c, sid).id for sid in ids]


def _period(start: str, end: str) -> tuple[date, date]:
    try:
        a, b = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        raise HTTPException(422, "Dates must look like 2026-11-01.") from None
    if b < a:
        raise HTTPException(422, "The period ends before it starts.")
    if (b - a).days > 366:
        raise HTTPException(422, "Export at most a year at a time.")
    return a, b


@router.get("/tally/export")
def tally_export(start: str, end: str, stores: str | None = None,
                 part: str = Query("zip", pattern="^(zip|masters|vouchers)$"),
                 c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    _admin(c)
    a, b = _period(start, end)
    store_ids = _stores_param(db, c, stores)
    if not store_ids:
        raise HTTPException(422, "Add a store before exporting to Tally.")
    conn = T.get_connector(db, c.company_id, "tally")
    stem = f"dqstack-tally-{a:%Y%m%d}-{b:%Y%m%d}"
    if part == "zip":
        body, summary = T.export_zip(db, c.company_id, store_ids, a, b, conn)
        media, name = "application/zip", f"{stem}.zip"
    else:
        out = T.build_export(db, c.company_id, store_ids, a, b, conn)
        body, summary = out[part], out["summary"]
        media, name = "application/xml", f"{stem}-{part}.xml"
    run = ConnectorRun(company_id=c.company_id, connector_id=conn.id, kind="tally",
                       trigger="export", status="succeeded", person_id=c.person.id,
                       summary=summary, import_job_ids=[], finished_at=utcnow(),
                       message=(f"Exported {summary['sales_vouchers']} sales, "
                                f"{summary['credit_notes']} credit notes and "
                                f"{summary['purchase_vouchers']} purchases for "
                                f"{a:%d %b %Y} to {b:%d %b %Y}."))
    db.add(run)
    conn.last_sync_at = run.finished_at
    audit.log(db, c, "connector.tally_export", "connector", conn.id,
              after={"start": a, "end": b, "stores": summary["stores"], "part": part,
                     "sales_vouchers": summary["sales_vouchers"],
                     "purchase_vouchers": summary["purchase_vouchers"]})
    db.commit()
    return Response(body, media_type=media, headers={
        "Content-Disposition": f'attachment; filename="{name}"',
        "X-DQ-Summary-Sales-Paise": str(summary["sales_paise"])})


TALLY_KINDS = {"products": "Stock items", "sales_history": "Sales history",
               "daily_sales": "Daily sales"}


@router.post("/tally/import", status_code=201)
async def tally_import(kind: str = Form(...), file: UploadFile = File(...),
                       store_id: str | None = Form(None), c: Ctx = Depends(ctx),
                       db: Session = Depends(get_db)):
    _admin(c)
    if kind not in TALLY_KINDS:
        raise HTTPException(422, "Choose stock items, sales history or daily sales.")
    content = await file.read(P.MAX_BYTES * 2 + 1)
    default_store = store_or_404(db, c, store_id) if store_id else None
    conn = T.get_connector(db, c.company_id, "tally")
    headers, rows, notes = T.convert(db, c.company_id, content, kind, conn, default_store)
    name = (file.filename or "tally.xml")[:250]
    job = make_job(db, c, kind, name, headers, rows, person_id=c.person.id)
    run = ConnectorRun(company_id=c.company_id, connector_id=conn.id, kind="tally",
                       trigger="import", status="succeeded", person_id=c.person.id,
                       summary={"file": name, "kind": kind, "rows": len(rows),
                                **{k: v for k, v in notes.items() if k != "unknown_units"},
                                "unknown_units": notes.get("unknown_units", [])},
                       import_job_ids=[job.id], finished_at=utcnow(),
                       message=f"Read {name}: {len(rows)} rows for {TALLY_KINDS[kind].lower()}, "
                               "waiting to be imported.")
    db.add(run)
    audit.log(db, c, "import.uploaded", "import", job.id,
              after={"kind": kind, "filename": name, "rows": len(rows), "source": "tally"})
    db.commit()
    out = job_dict(job)
    out["tally"] = {k: v for k, v in notes.items()}
    return out


# =================================================================== Zoho

class ConnectIn(BaseModel):
    client_id: str = Field(min_length=5, max_length=200)
    client_secret: str | None = Field(None, max_length=300)
    data_centre: str = "in"


def _check_dc(dc: str) -> str:
    if dc not in Z.DATA_CENTRES:
        raise HTTPException(422, "Choose the Zoho data centre your account is in.")
    return dc


@router.post("/zoho/connect")
def zoho_connect(body: ConnectIn, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    """Keep the client id and secret (encrypted) and send the Admin to Zoho's consent
    page. A secret left blank keeps the one already stored."""
    _admin(c)
    conn = Z.connector(db, c.company_id)
    creds = vault.unseal(conn.credentials) or {}
    secret = (body.client_secret or "").strip() or creds.get("client_secret")
    if not secret:
        raise HTTPException(422, "Type the client secret from the Zoho API Console.")
    dc = _check_dc(body.data_centre)
    raw, digest = Z.new_state()
    st = dict(conn.settings or {})
    st.update(data_centre=dc, oauth_state=digest,
              oauth_state_expires=(utcnow() + timedelta(minutes=15)).isoformat())
    conn.settings = st
    keep = {k: v for k, v in creds.items() if k in ("refresh_token", "access_token",
                                                   "access_expires", "accounts_host", "api_host")}
    conn.credentials = vault.seal({**keep, "client_id": body.client_id.strip(),
                                   "client_secret": secret})
    if conn.status in ("not_connected",):
        conn.status = "connecting"
    conn.updated_at = utcnow()
    audit.log(db, c, "connector.zoho_connect_started", "connector", conn.id,
              after={"data_centre": dc, "client_id": body.client_id.strip()[:12] + "…"})
    db.commit()
    return {"authorize_url": Z.authorize_url(Z.settings_of(conn), body.client_id.strip(), raw),
            "redirect_uri": Z.redirect_uri()}


def _after_tokens(db: Session, c: Ctx, conn: Connector, creds: dict) -> None:
    """Store the tokens, then read the organisations and warehouses (best effort)."""
    conn.credentials = vault.seal(creds)
    conn.status = "connected"
    conn.last_error = None
    st = dict(conn.settings or {})
    st.pop("oauth_state", None)
    st.pop("oauth_state_expires", None)
    st["full_next"] = True
    conn.settings = st
    s = Z.settings_of(conn)
    try:
        with Z.Api(creds, s) as api:
            orgs = Z.organizations(api)
            st["organizations"] = orgs
            if not s["organization_id"] and len(orgs) >= 1:
                st["organization_id"], st["organization_name"] = orgs[0]["id"], orgs[0]["name"]
            elif s["organization_id"]:
                st["organization_name"] = next((o["name"] for o in orgs
                                                if o["id"] == s["organization_id"]), "")
            api.org = st.get("organization_id") or ""
            if api.org:
                st["warehouses"] = Z.warehouses(api)
            if api.changed:
                conn.credentials = vault.seal(api.creds)
    except Z.ZohoError as e:
        conn.last_error = e.message
    conn.settings = st
    conn.updated_at = utcnow()
    audit.log(db, c, "connector.zoho_connected", "connector", conn.id,
              after={"organization": st.get("organization_name"), "data_centre": s["data_centre"]})


def _back(reason: str) -> RedirectResponse:
    return RedirectResponse(f"/app/#/connectors?zoho={reason}", status_code=303)


@router.get("/zoho/callback")
def zoho_callback(request: Request, code: str | None = None, state: str | None = None,
                  error: str | None = None, c: Ctx | None = Depends(optional_ctx),
                  db: Session = Depends(get_db)):
    if c is None:
        return RedirectResponse("/app/#/signin", status_code=303)
    if not c.can("integrations.manage"):
        return _back("not-allowed")
    from ..deps import client_ip
    limiter.check(f"zoho-cb:{client_ip(request)}", limit=20, window_s=300)
    conn = Z.connector(db, c.company_id)
    st = conn.settings or {}
    expected, expires = st.get("oauth_state"), st.get("oauth_state_expires")
    if error:
        return _back("denied")
    if not state or not expected or hashlib.sha256(state.encode()).hexdigest() != expected:
        return _back("failed")
    if not expires or _aware(datetime.fromisoformat(expires)) < utcnow():
        return _back("expired")
    creds = vault.unseal(conn.credentials) or {}
    if not code or not creds.get("client_id"):
        return _back("failed")
    try:
        tokens = Z.exchange_code(Z.settings_of(conn), creds, code,
                                 request.query_params.get("accounts-server"))
    except Z.ZohoError as e:
        conn.last_error = e.message
        db.commit()
        return _back("failed")
    _after_tokens(db, c, conn, tokens)
    db.commit()
    return _back("connected")


class CodeIn(BaseModel):
    code: str = Field(min_length=6, max_length=500)


@router.post("/zoho/code")
def zoho_code(body: CodeIn, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    """A self client: the Admin made a code in the Zoho API Console (with the scopes
    shown) and pastes it here, after saving the client id and secret."""
    _admin(c)
    conn = Z.connector(db, c.company_id)
    creds = vault.unseal(conn.credentials) or {}
    if not creds.get("client_id") or not creds.get("client_secret"):
        raise HTTPException(422, "Save the client id and secret first.")
    try:
        tokens = Z.exchange_code(Z.settings_of(conn), creds, body.code, with_redirect=False)
    except Z.ZohoError as e:
        raise HTTPException(422, e.message) from None
    _after_tokens(db, c, conn, tokens)
    db.commit()
    return _zoho_public(conn)


class ZohoSettingsIn(BaseModel):
    organization_id: str | None = Field(None, max_length=40)
    sales_source: str | None = None
    sales_into: str | None = None
    warehouse_map: dict[str, str] | None = None
    default_store_id: str | None = None
    start_date: str | None = None
    sync_time: str | None = None
    data_centre: str | None = None


@router.put("/zoho/settings")
def zoho_settings(body: ZohoSettingsIn, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    _admin(c)
    conn = Z.connector(db, c.company_id)
    st = dict(conn.settings or {})
    before = {k: st.get(k) for k in ("organization_id", "sales_source", "sales_into",
                                     "warehouse_map", "default_store_id", "start_date",
                                     "sync_time", "data_centre")}
    resync = False
    if body.organization_id is not None:
        orgs = {o["id"]: o["name"] for o in st.get("organizations") or []}
        if body.organization_id and orgs and body.organization_id not in orgs:
            raise HTTPException(422, "That organisation is not one this Zoho account can read.")
        if body.organization_id != st.get("organization_id"):
            resync = True
            conn.cursor = {}
        st["organization_id"] = body.organization_id
        st["organization_name"] = orgs.get(body.organization_id, "")
    if body.sales_source is not None:
        if body.sales_source not in Z.SOURCES:
            raise HTTPException(422, "Sales come from invoices or sales orders.")
        resync = resync or body.sales_source != st.get("sales_source", "invoices")
        st["sales_source"] = body.sales_source
    if body.sales_into is not None:
        if body.sales_into not in ("daily_sales", "sales_history"):
            raise HTTPException(422, "Sales go in as daily sales or as sales history.")
        st["sales_into"] = body.sales_into
    if body.warehouse_map is not None:
        clean = {}
        for wid, sid in body.warehouse_map.items():
            if sid:
                clean[str(wid)[:64]] = store_or_404(db, c, sid).id
        resync = resync or clean != (st.get("warehouse_map") or {})
        st["warehouse_map"] = clean
    if body.default_store_id is not None:
        sid = store_or_404(db, c, body.default_store_id).id if body.default_store_id else ""
        resync = resync or sid != (st.get("default_store_id") or "")
        st["default_store_id"] = sid
    if body.start_date is not None:
        if body.start_date:
            try:
                d = date.fromisoformat(body.start_date)
            except ValueError:
                raise HTTPException(422, "The start date must look like 2026-08-01.") from None
            if d > date.today():
                raise HTTPException(422, "The start date cannot be in the future.")
        resync = resync or body.start_date != (st.get("start_date") or "")
        st["start_date"] = body.start_date
    if body.sync_time is not None:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", body.sync_time):
            raise HTTPException(422, "The nightly time must look like 00:30.")
        st["sync_time"] = body.sync_time
    if body.data_centre is not None:
        if conn.status == "connected" and body.data_centre != st.get("data_centre", "in"):
            raise HTTPException(409, "Disconnect before changing the data centre.")
        st["data_centre"] = _check_dc(body.data_centre)
    if resync:
        st["full_next"] = True
    conn.settings = st
    conn.updated_at = utcnow()
    audit.log(db, c, "connector.zoho_settings", "connector", conn.id, before=before,
              after={k: st.get(k) for k in before})
    db.commit()
    return _zoho_public(conn)


def _api_for(conn: Connector) -> Z.Api:
    creds = vault.unseal(conn.credentials)
    if conn.status != "connected" or not creds or not creds.get("refresh_token"):
        raise HTTPException(409, "Connect Zoho Inventory first.")
    return Z.Api(creds, Z.settings_of(conn))


@router.post("/zoho/warehouses")
def zoho_warehouses(c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    _admin(c)
    conn = Z.connector(db, c.company_id)
    st = dict(conn.settings or {})
    try:
        with _api_for(conn) as api:
            st["organizations"] = Z.organizations(api)
            if api.org:
                st["warehouses"] = Z.warehouses(api)
            if api.changed:
                conn.credentials = vault.seal(api.creds)
    except Z.ZohoError as e:
        if e.reconnect:
            conn.status = "needs_reconnect"
        conn.last_error = e.message
        db.commit()
        raise HTTPException(502, e.message) from None
    conn.settings = st
    db.commit()
    return _zoho_public(conn)


class SyncIn(BaseModel):
    full: bool = False


@router.post("/zoho/sync")
def zoho_sync(body: SyncIn, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    """Sync now. In development and tests it runs in this request; on a server the
    worker picks it up within a minute (a sync can take minutes at Zoho's rate limit)."""
    _admin(c)
    conn = Z.connector(db, c.company_id)
    if conn.status != "connected":
        raise HTTPException(409, "Connect Zoho Inventory first." if conn.status != "needs_reconnect"
                            else "Zoho no longer accepts the stored access. Connect again.")
    st = dict(conn.settings or {})
    if body.full:
        st["full_next"] = True
    audit.log(db, c, "connector.zoho_sync_requested", "connector", conn.id,
              after={"full": body.full})
    if app_settings.inline_jobs:
        conn.settings = st
        db.commit()
        run = Z.sync(db, conn, "manual", person_id=c.person.id)
        return {"queued": False, "run": _run_dict(run)}
    st["sync_requested"] = utcnow().isoformat()
    st["sync_requested_by"] = c.person.id
    conn.settings = st
    db.commit()
    return {"queued": True, "message": "The sync starts within a minute. This page shows it "
                                       "when it is done."}


@router.post("/zoho/disconnect")
def zoho_disconnect(c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    _admin(c)
    conn = Z.connector(db, c.company_id)
    Z.revoke(Z.settings_of(conn), vault.unseal(conn.credentials))
    Z.forget(db, conn)
    audit.log(db, c, "connector.zoho_disconnected", "connector", conn.id)
    db.commit()
    return _zoho_public(conn)
