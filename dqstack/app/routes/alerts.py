"""The Alerts screen and the bell, and the Reports screen (build step 11).

    GET  /api/alerts                     open alerts this person should see
    GET  /api/alerts/count               for the bell in the top bar
    POST /api/alerts/{id}/resolve        mark one done
    GET  /api/alerts/settings            my channels
    PUT  /api/alerts/settings
    GET  /api/reports                    the report list
    GET  /api/reports/{name}?store_id&from&to&format=json|xlsx|pdf
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import alerts as A
from .. import audit
from .. import reports as R
from ..db import get_db, utcnow
from ..deps import ctx
from ..models import Alert, Person
from ..scope import Ctx, get_or_404, store_or_404

router = APIRouter(prefix="/api", tags=["alerts", "reports"])


def _alert_query(c: Ctx, open_only: bool = True):
    q = select(Alert).where(Alert.company_id == c.company_id,
                            Alert.kind.in_(A.visible_kinds(c.role)))
    if open_only:
        q = q.where(Alert.resolved_at.is_(None))
    if not c.all_stores:
        q = q.where((Alert.store_id.is_(None)) | (Alert.store_id.in_(c.store_ids or {"-"})))
    return q


@router.get("/alerts")
def list_alerts(c: Ctx = Depends(ctx), db: Session = Depends(get_db), open: bool = True,
                store_id: str | None = None, kind: str | None = None,
                limit: int = Query(200, ge=1, le=1000)):
    q = _alert_query(c, open)
    if store_id:
        q = q.where(Alert.store_id == store_or_404(db, c, store_id).id)
    if kind:
        q = q.where(Alert.kind == kind)
    order = {"risk": 0, "attention": 1, "info": 2}
    rows = list(db.scalars(q.order_by(Alert.created_at.desc()).limit(limit)))
    rows.sort(key=lambda a: order.get(a.severity, 3))
    return [{"id": a.id, "kind": a.kind, "label": A.LABELS.get(a.kind, a.kind),
             "severity": a.severity, "message": a.message, "store_id": a.store_id,
             "product_id": a.product_id, "link": a.link,
             "created_at": a.created_at.isoformat() if a.created_at else None,
             "resolved": a.resolved_at is not None} for a in rows]


@router.get("/alerts/count")
def count(c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    q = _alert_query(c).subquery()
    n = db.scalar(select(func.count()).select_from(q)) or 0
    risk = db.scalar(select(func.count()).select_from(q).where(q.c.severity == "risk")) or 0
    return {"open": n, "risk": risk}


@router.post("/alerts/{alert_id}/resolve")
def resolve(alert_id: str, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    a = get_or_404(db, c, Alert, alert_id, what="That alert")
    if a.kind not in A.visible_kinds(c.role):
        raise HTTPException(404, "That alert was not found.")
    a.resolved_at = a.resolved_at or utcnow()
    audit.log(db, c, "alert.resolved", "alert", a.id, store_id=a.store_id)
    db.commit()
    return {"ok": True}


@router.post("/alerts/{alert_id}/reopen")
def reopen(alert_id: str, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    """Undo for Done. A condition that has cleared will be resolved again by the next check."""
    a = get_or_404(db, c, Alert, alert_id, what="That alert")
    if a.kind not in A.visible_kinds(c.role):
        raise HTTPException(404, "That alert was not found.")
    if a.resolved_at is not None:
        a.resolved_at = None
        audit.log(db, c, "alert.reopened", "alert", a.id, store_id=a.store_id)
        db.commit()
    return {"ok": True}


class SettingsIn(BaseModel):
    email: bool = False
    whatsapp: bool = False
    muted: list[str] = Field(default_factory=list)


@router.get("/alerts/settings")
def get_settings(c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    p = db.get(Person, c.person.id)
    kinds = sorted(A.visible_kinds(c.role))
    from .. import whatsapp
    from ..sms import normalise
    available = whatsapp.configured()
    if not available:
        note = ("WhatsApp alerts are not available on this server. They arrive in the app, "
                "and by email if you choose.")
    elif not normalise(p.phone):
        note = ("Your account has no mobile number, so WhatsApp alerts cannot reach you. "
                "Ask an administrator to add one.")
    else:
        note = f"Sent to {p.phone} on WhatsApp."
    return {"email": bool((p.notify or {}).get("email")),
            "whatsapp": bool((p.notify or {}).get("whatsapp")),
            "muted": _muted(p),
            "kinds": [{"kind": k, "label": A.LABELS[k]} for k in kinds],
            "whatsapp_available": available,
            "whatsapp_note": note}


def _muted(p: Person) -> list[str]:
    """What this person does not receive. The Owner's summaries reach an Admin only once
    they tick them, so for an Admin they count as muted until then (the settings screen
    shows a ticked box for anything not muted)."""
    n = p.notify or {}
    muted = list(n.get("muted", []))
    if p.role == "admin":
        muted += [k for k in A.OPT_IN if k not in n.get("opted_in", []) and k not in muted]
    return muted


@router.put("/alerts/settings")
def put_settings(body: SettingsIn, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    p = db.get(Person, c.person.id)
    bad = [k for k in body.muted if k not in A.KINDS]
    if bad:
        raise HTTPException(422, f"'{bad[0]}' is not a kind of alert.")
    data = body.model_dump()
    if p.role == "admin":
        data["opted_in"] = [k for k in A.OPT_IN if k not in body.muted]
        data["muted"] = [k for k in body.muted if k not in A.OPT_IN]
    p.notify = data
    db.commit()
    return get_settings(c, db)


TITLES = {"sales": "Sales by day and store", "sales_by_category": "Sales by category",
          "best_worst": "Best and worst sellers", "stock_value": "Stock value at cost",
          "stock_turn": "Stock turn and days of cover",
          "slow_dead_stock": "Slow and dead stock", "shrinkage": "Shrinkage",
          "wastage": "Wastage, damage and expiry", "count_compliance": "Cycle count compliance",
          "forecast_accuracy": "Forecast accuracy",
          "supplier_performance": "Order performance by supplier",
          "lost_sales": "Lost sales estimate",
          "profit_comparison": "Profit comparison", "accounting": "Accounting export"}


@router.get("/reports")
def report_list(c: Ctx = Depends(ctx)):
    c.require("reports.view")
    return [{"name": k, "title": v} for k, v in TITLES.items()]


XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _period(db: Session, c: Ctx, store_id: str | None, start: date | None, end: date | None):
    ids = [store_or_404(db, c, store_id).id] if store_id else c.visible_store_ids(db)
    if end is None:
        from ..sales import local_today, tz_of
        end = local_today(tz_of(db, c.company_id))
    start = start or end - timedelta(days=27)
    if start > end or (end - start).days > 1100:
        raise HTTPException(422, "Choose a period of up to three years, start before end.")
    return ids, start, end


def _attachment(data: bytes, media: str, filename: str) -> Response:
    return Response(data, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/reports/all.xlsx")
def all_reports(c: Ctx = Depends(ctx), db: Session = Depends(get_db),
                store_id: str | None = None, start: date | None = Query(None, alias="from"),
                end: date | None = Query(None, alias="to")):
    """Every report for the period in one Excel workbook, a sheet each."""
    c.require("reports.view")
    ids, start, end = _period(db, c, store_id, start, end)
    reps = [R.REPORTS[k](db, c.company_id, ids, start, end) for k in TITLES]
    period = f"{start:%d %b %Y} to {end:%d %b %Y}"
    audit.log(db, c, "report.exported", "report", "all", after={"format": "xlsx",
              "from": start.isoformat(), "to": end.isoformat(), "store_id": store_id})
    db.commit()
    return _attachment(R.to_xlsx_many(reps, period), XLSX,
                       f"dqstack-reports-{start:%Y%m%d}-{end:%Y%m%d}.xlsx")


@router.get("/reports/{name}")
def report(name: str, c: Ctx = Depends(ctx), db: Session = Depends(get_db),
           store_id: str | None = None, start: date | None = Query(None, alias="from"),
           end: date | None = Query(None, alias="to"), format: str = "json"):
    c.require("reports.view")
    fn = R.REPORTS.get(name)
    if fn is None:
        raise HTTPException(404, "That report does not exist.")
    if format not in ("json", "xlsx", "pdf", "csv"):
        raise HTTPException(422, "Format must be xlsx, pdf or csv.")
    ids, start, end = _period(db, c, store_id, start, end)
    rep = fn(db, c.company_id, ids, start, end)
    rep["from"], rep["to"] = start.isoformat(), end.isoformat()
    period = f"{start:%d %b %Y} to {end:%d %b %Y}"
    if format == "json":
        return rep
    stem = f"{name}-{start:%Y%m%d}-{end:%Y%m%d}"
    audit.log(db, c, "report.exported", "report", name, after={"format": format,
              "from": start.isoformat(), "to": end.isoformat(), "store_id": store_id})
    db.commit()
    if format == "xlsx":
        return _attachment(R.to_xlsx(rep), XLSX, stem + ".xlsx")
    if format == "csv":
        return _attachment(R.to_csv(rep), "text/csv; charset=utf-8", stem + ".csv")
    return _attachment(R.to_pdf(rep, period), "application/pdf", stem + ".pdf")
