"""The forecast's state, history and outputs (build step 7).

    GET  /api/forecast/status            what every screen's freshness label reads
    GET  /api/forecast/runs              recent runs, failures included
    POST /api/forecast/run               queue a forecast or a retrain now (Owner, Admin)
    GET  /api/forecast/models            model versions, backtests, promotion decisions
    GET  /api/forecast/monitoring        last night's forecasts scored against sales
    GET  /api/recommendations            the current forecast per product and store
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import audit
from ..config import settings
from ..db import get_db, utcnow
from ..deps import ctx
from ..forecast import jobs
from ..models import (DailySales, ForecastRun, ForecastScore, ModelVersion, Product,
                      Recommendation, Store, Supplier)
from ..scope import Ctx, store_or_404
from ..security import _aware

router = APIRouter(prefix="/api", tags=["forecast"])


def run_dict(r: ForecastRun | None) -> dict | None:
    if r is None:
        return None
    return {"id": r.id, "kind": r.kind, "trigger": r.trigger, "status": r.status, "tier": r.tier,
            "data_cutoff": r.data_cutoff.isoformat() if r.data_cutoff else None,
            "model_version": r.model_version, "message": r.message,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "accuracy": r.accuracy, "timings": r.timings}


@router.get("/forecast/status")
def status(c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    """"forecast from data to 7 Nov" on every screen (interface rule 2)."""
    c.require("data.read")
    cur = jobs.current_run(db, c.company_id)
    latest = db.scalar(select(ForecastRun).where(ForecastRun.company_id == c.company_id)
                       .order_by(ForecastRun.created_at.desc()).limit(1))
    mv = jobs.active_version(db, c.company_id)
    lo, hi = db.execute(select(func.min(DailySales.day), func.max(DailySales.day)).where(
        DailySales.company_id == c.company_id)).one()
    days = (hi - lo).days + 1 if lo and hi else 0
    age = None
    if cur is not None and cur.finished_at is not None:
        age = round((utcnow() - _aware(cur.finished_at)).total_seconds() / 3600, 1)
    return {"current": run_dict(cur), "latest": run_dict(latest), "age_hours": age,
            "stale": age is not None and age > 36,
            "model": None if mv is None else {"version": mv.version, "tier": mv.tier,
                                              "data_cutoff": mv.data_cutoff.isoformat()
                                              if mv.data_cutoff else None,
                                              "backtest": mv.backtest, "promotion": mv.promotion},
            # days_needed: until the first forecast (an estimate, 2 weeks);
            # days_to_full: until full forecasting (8 weeks); needs: both in words.
            "history_days": days, "days_needed": max(0, jobs.MIN_DAYS - days),
            "days_to_full": max(0, jobs.OWN_DAYS - days), "needs": jobs.needs_text(days)}


@router.get("/forecast/runs")
def runs(c: Ctx = Depends(ctx), db: Session = Depends(get_db),
         limit: int = Query(30, ge=1, le=200)):
    c.require("reports.view")
    return [run_dict(r) for r in db.scalars(select(ForecastRun).where(
        ForecastRun.company_id == c.company_id).order_by(ForecastRun.created_at.desc())
        .limit(limit))]


class RunIn(BaseModel):
    kind: str = "nightly"


@router.post("/forecast/run", status_code=202)
def queue(body: RunIn, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    c.require("company.settings")
    if body.kind not in ("nightly", "train"):
        raise HTTPException(422, "Kind must be nightly or train.")
    busy = db.scalar(select(func.count()).select_from(ForecastRun).where(
        ForecastRun.company_id == c.company_id, ForecastRun.status.in_(("queued", "running"))))
    if busy:
        raise HTTPException(409, "A forecast is already queued or running.")
    r = ForecastRun(company_id=c.company_id, kind=body.kind, trigger="manual", status="queued")
    db.add(r)
    db.flush()
    audit.log(db, c, "forecast.queued", "forecast_run", r.id, after={"kind": body.kind})
    db.commit()
    if settings.inline_jobs:
        fn = jobs.train if body.kind == "train" else jobs.nightly
        r = fn(db, c.company_id, "manual", r.id)
    return run_dict(r)


@router.get("/forecast/models")
def models(c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    c.require("reports.view")
    return [{"id": m.id, "version": m.version, "tier": m.tier, "status": m.status,
             "data_cutoff": m.data_cutoff.isoformat() if m.data_cutoff else None,
             "n_series": m.n_series, "train_seconds": m.train_seconds, "backtest": m.backtest,
             "promotion": m.promotion,
             "created_at": m.created_at.isoformat() if m.created_at else None}
            for m in db.scalars(select(ModelVersion).where(
                ModelVersion.company_id == c.company_id).order_by(ModelVersion.created_at.desc()))]


@router.get("/forecast/monitoring")
def monitoring(c: Ctx = Depends(ctx), db: Session = Depends(get_db),
               days: int = Query(28, ge=1, le=366), store_id: str | None = None):
    c.require("reports.view")
    ids = [store_or_404(db, c, store_id).id] if store_id else c.visible_store_ids(db)
    since = date.today() - timedelta(days=days)
    rows = list(db.scalars(select(ForecastScore).where(
        ForecastScore.company_id == c.company_id, ForecastScore.store_id.in_(ids),
        ForecastScore.day >= since)))
    by_day: dict = defaultdict(lambda: [0, 0, 0, 0, 0])
    by_cat: dict = defaultdict(lambda: [0, 0, 0, 0, 0])
    for r in rows:
        for agg, k in ((by_day, r.day.isoformat()), (by_cat, r.category)):
            v = agg[k]
            v[0] += r.n
            v[1] += r.actual_milli
            v[2] += r.forecast_milli
            v[3] += r.abs_error_milli
            v[4] += r.inside_range

    def fmt(v):
        n, a, f, e, inside = v
        return {"n": n, "actual_milli": a, "forecast_milli": f,
                "wape": round(e / a, 4) if a else None,
                "bias": round((f - a) / a, 4) if a else None,
                "inside_range": round(inside / n, 4) if n else None}
    return {"by_day": [{"day": d, **fmt(v)} for d, v in sorted(by_day.items())],
            "by_category": [{"category": k, **fmt(v)} for k, v in sorted(by_cat.items())]}


@router.get("/recommendations")
def recommendations(c: Ctx = Depends(ctx), db: Session = Depends(get_db),
                    store_id: str | None = None, flagged: bool | None = None,
                    q: str | None = Query(None, max_length=100), supplier_id: str | None = None,
                    limit: int = Query(200, ge=1, le=20000), offset: int = Query(0, ge=0)):
    c.require("data.read")
    # The store is checked before anything else, so another company's store id is
    # "not found" whether or not a forecast exists.
    ids = [store_or_404(db, c, store_id).id] if store_id else c.visible_store_ids(db)
    cur = jobs.current_run(db, c.company_id)
    if cur is None:
        return {"run": None, "total": 0, "rows": []}
    qy = select(Recommendation, Product).join(Product, Product.id == Recommendation.product_id).where(
        Recommendation.run_id == cur.id, Recommendation.store_id.in_(ids))
    if flagged is not None:
        qy = qy.where(Recommendation.flagged.is_(flagged))
    if supplier_id:
        qy = qy.where(Recommendation.supplier_id == supplier_id)
    if q:
        like = f"%{q.strip()}%"
        qy = qy.where(or_(Product.name.ilike(like), Product.sku.ilike(like)))
    total = db.scalar(select(func.count()).select_from(qy.subquery()))
    rows = db.execute(qy.order_by(Recommendation.money_at_risk_paise.desc(), Product.name)
                      .offset(offset).limit(limit)).all()
    stores = {s.id: s.name for s in db.scalars(select(Store).where(Store.id.in_(ids)))}
    sups = {s.id: s.name for s in db.scalars(select(Supplier).where(
        Supplier.company_id == c.company_id))}
    return {"run": run_dict(cur), "total": total, "rows": [rec_dict(r, p, stores, sups)
                                                           for r, p in rows]}


def rec_dict(r: Recommendation, p: Product, stores: dict, sups: dict) -> dict:
    return {"id": r.id, "store_id": r.store_id, "store": stores.get(r.store_id),
            "product_id": p.id, "sku": p.sku, "name": p.name, "unit": p.unit,
            "category": p.category, "case_pack": p.case_pack,
            "week": {"lo_milli": r.week1_lo_milli, "mid_milli": r.week1_mid_milli,
                     "hi_milli": r.week1_hi_milli},
            "daily": r.daily, "risk_q": r.risk_q, "risk_days": r.risk_days,
            "lead_time_days": r.lead_time_days, "target_milli": r.target_milli,
            "service_level": r.service_level, "on_hand_milli": r.on_hand_milli,
            "on_order_milli": r.on_order_milli, "stock_known": r.stock_known,
            "suggested_milli": r.suggested_milli, "underage_paise": r.underage_paise,
            "overage_paise": r.overage_paise, "supplier_id": r.supplier_id,
            "supplier": sups.get(r.supplier_id), "reasons": r.reasons,
            "money_at_risk_paise": r.money_at_risk_paise, "flagged": r.flagged,
            "estimate": r.estimate, "history_days": r.history_days,
            "planning_class": r.planning_class}
