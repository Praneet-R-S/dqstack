"""The forecasting jobs for one company (build step 7).

    train    onboarding and the weekly retrain: the engine trains on the company's
             own history and backtests itself; the new model becomes the active
             one only if it would have earned at least as much (the promotion
             rule), then tonight's forecast is made with whichever model is active.
    nightly  today's data through the active model: the 28-day forecast, the stock
             target from each product's own supplier terms, and the Today list.
             A failure anywhere -- including the Stage 0 quality checks -- leaves
             yesterday's forecast in place, its age shown, and alerts the Admin.

Production runs each job in its own process (`python -m dqstack.app.forecast.jobs
train --company ID`) with its own scratch folders, so a crash cannot take the
worker down and companies never share files. Tests call the functions directly.

Which engine a company gets depends on its history (see `plan`):
  >= 667 days     the full pipeline, demo settings
  >= 358 days     the full pipeline, origin blocks scaled to the history
  >= 302 days     the full pipeline, minimal origin blocks
  >= 56 days      the early forecast (statistical candidates, no training)
  less            no forecast yet; the screens say so
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import math
import shutil
import sys
import tempfile
import time
import traceback
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import numpy as np
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ... import decide as dec
from ... import live
from ...config import CFG, HORIZON, NHITS, QUANTILES
from .. import rules as rules_mod
from .. import stock as stock_mod
from ..db import utcnow
from ..models import (Alert, CalendarEvent, Company, DailySales, Decision, ForecastRun,
                      ForecastScore, ModelArtifact, ModelVersion, Product, Recommendation,
                      StockOnHand, Store, Supplier, SupplierProduct)
from . import source

MIN_DAYS = 56
FLAG_PER_STORE = 20


# ===================================================================== plumbing

@contextlib.contextmanager
def scratch():
    """Point the engine's output folders at a private temporary folder for the length
    of one in-process run, so a company's run never writes over the demo's files."""
    import dqstack.backtest
    import dqstack.diagnose
    import dqstack.features
    import dqstack.ingest
    import dqstack.pipeline
    import dqstack.sensitivity
    tmp = Path(tempfile.mkdtemp(prefix="dq-engine-"))
    paths = {name: tmp / name.lower() for name in ("ARTIFACTS", "INTERIM", "PROCESSED")}
    for p in paths.values():
        p.mkdir()
    saved = []
    for m in (dqstack.backtest, dqstack.diagnose, dqstack.features, dqstack.ingest,
              dqstack.pipeline, dqstack.sensitivity):
        for name, p in paths.items():
            if hasattr(m, name):
                saved.append((m, name, getattr(m, name)))
                setattr(m, name, p)
    try:
        yield tmp
    finally:
        for m, name, v in saved:
            setattr(m, name, v)
        shutil.rmtree(tmp, ignore_errors=True)


def company_cfg(db: Session, company_id: str):
    r = rules_mod.effective(db.get(Company, company_id).rules)
    d = replace(CFG.decision, min_quantile=float(r["service_level_floor"]),
                max_quantile=float(r["service_level_ceiling"]),
                holding_cost_rate=float(r["holding_rate_per_day"]),
                lead_time=int(r["default_lead_time_days"]),
                review_period=int(r["default_review_period_days"]))
    return replace(CFG, decision=d)


MIN_TRAIN_ORIGINS = 8     # the pipeline holds back 3 for early stopping and purges 4
# NHITS samples windows of input_size days of context plus the horizon, all before
# the last training origin (dqstack/models/neural.py).
NN_REGION = NHITS["input_size"] + HORIZON


def plan(history_days: int) -> tuple[str | None, dict | None, bool]:
    """(tier, make_splits overrides, quick) for this much history.

    With `live = history_days - 1` and weekly origins, make_splits places the blocks
    backwards from the live origin with a 28-day purge between them; the last
    training origin is therefore `live - 217` (8 evaluation, 5 meta, 5 conformal
    origins) or `live - 161` (4, 3, 3), and every training origin must be at least
    `min_origin`. The tiers below are where at least 8 training origins survive and
    the neural candidate (NHITS) has a full input window plus horizon before the
    last training origin."""
    live = history_days - 1
    if (live - 217 - 400) // 7 + 1 >= MIN_TRAIN_ORIGINS:        # 667 days or more
        return "engine", {}, False
    n = min(32, (live - 217 - 60) // 7 + 1)
    if n >= MIN_TRAIN_ORIGINS and live - 217 >= NN_REGION:        # 358 days or more
        return "engine", {"min_origin": 60, "n_train_origins": n, "n_eval_origins": 8}, True
    n = min(32, (live - 161 - 28) // 7 + 1)
    if n >= MIN_TRAIN_ORIGINS and live - 161 >= NN_REGION:        # 302 days or more
        return "engine", {"min_origin": 28, "n_train_origins": n, "n_meta_origins": 3,
                          "n_conformal_origins": 3, "n_eval_origins": 4}, True
    if history_days >= MIN_DAYS:
        return "starter", None, True
    return None, None, True


def active_version(db: Session, company_id: str) -> ModelVersion | None:
    return db.scalar(select(ModelVersion).where(ModelVersion.company_id == company_id,
                                                ModelVersion.status == "active")
                     .order_by(ModelVersion.created_at.desc()).limit(1))


def current_run(db: Session, company_id: str) -> ForecastRun | None:
    """The forecast the screens show: the latest night that succeeded."""
    return db.scalar(select(ForecastRun).where(ForecastRun.company_id == company_id,
                                               ForecastRun.kind == "nightly",
                                               ForecastRun.status == "succeeded")
                     .order_by(ForecastRun.created_at.desc()).limit(1))


def _next_version(db: Session, company_id: str) -> str:
    n = db.scalar(select(func.count()).select_from(ModelVersion).where(
        ModelVersion.company_id == company_id)) or 0
    return f"v{n + 1}"


def _alert(db: Session, company_id: str, kind: str, severity: str, message: str,
           key: str) -> None:
    if db.scalar(select(Alert.id).where(Alert.company_id == company_id,
                                        Alert.dedupe_key == key)):
        return
    db.add(Alert(company_id=company_id, kind=kind, severity=severity, message=message,
                 dedupe_key=key))


def _clean(x: float) -> float | None:
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else round(x, 4)


# ===================================================================== train

def engine_summary(state: dict) -> dict:
    """The backtest, in the terms a client reads: the engine's own rolling backtest
    of the forecast that orders use, against repeating last year's pattern."""
    from ... import backtest as bt
    by = {r["method"]: r for r in state["metrics"]["backtest"]["by_method"]}
    sysrow = by.get(bt.SYSTEM, {})
    naive = by.get("seasonal_naive", {})
    out = {"weeks": state["metrics"]["backtest"]["n_origins"],
           "wape": _clean(sysrow.get("wape_mean")),
           "inside_range": _clean(sysrow.get("inside_range_mean")),
           "fill_rate": _clean(sysrow.get("fill_rate_mean")),
           "bias": _clean(sysrow.get("bias_mean")),
           "margin": _clean(sysrow.get("realized_margin_mean")),
           "naive_wape": _clean(naive.get("wape_mean"))}
    if out["wape"] is not None and out["naive_wape"]:
        out["better_than_naive_pct"] = round(100 * (1 - out["wape"] / out["naive_wape"]), 1)
    return out


def _start(db: Session, company_id: str, kind: str, trigger: str,
           run_id: str | None) -> ForecastRun:
    """Use a run an Admin queued, or open a new one."""
    run = db.get(ForecastRun, run_id) if run_id else None
    if run is None or run.company_id != company_id:
        run = ForecastRun(company_id=company_id, kind=kind, trigger=trigger)
        db.add(run)
    run.status, run.started_at = "running", utcnow()
    db.commit()
    return run


def train(db: Session, company_id: str, trigger: str = "onboarding",
          run_id: str | None = None) -> ForecastRun:
    run = _start(db, company_id, "train", trigger, run_id)
    t0 = time.perf_counter()
    try:
        data = source.load(db, company_id)
        tier, splits, quick = plan(data.history_days)
        run.data_cutoff = data.cutoff
        if tier is None:
            run.status = "skipped"
            run.message = (f"Forecasts start after 8 weeks of sales. There are "
                           f"{data.history_days} days so far.")
            run.finished_at = utcnow()
            db.commit()
            return run
        cfg = company_cfg(db, company_id)
        mv = ModelVersion(company_id=company_id, version=_next_version(db, company_id),
                          tier=tier, status="candidate", data_cutoff=data.cutoff)
        new_model: dict | str
        current = active_version(db, company_id)
        # Every engine call writes scratch files; all of them stay in a private folder.
        with scratch():
            if tier == "starter":
                new_model = "starter"
                summary = {"early": True}
            else:
                state = live_pipeline(cfg, quick, data.tables, splits)
                blob = live.saved_model(state, cfg)
                new_model = live.load_model(blob)
                summary = engine_summary(state)
                mv.n_series = int(state["P"].n_series)
                del state
            mv.train_seconds = round(time.perf_counter() - t0, 1)
            models = {"new": new_model}
            if current is not None:
                models["current"] = _load(db, current)
            scores = live.recent_scores(models, data.tables, cfg)
        summary["recent"] = {k: {kk: _clean(vv) if isinstance(vv, float) else vv
                                 for kk, vv in v.items()} for k, v in scores.items()}
        if tier == "starter":
            summary.update({"weeks": scores["new"]["origins"],
                            "wape": _clean(scores["new"]["wape"]),
                            "inside_range": _clean(scores["new"]["inside_range"]),
                            "fill_rate": _clean(scores["new"]["fill_rate"]),
                            "margin": _clean(scores["new"]["margin"])})
        mv.backtest = summary
        mv.backtest_profit_paise = int(round(scores["new"]["margin"] * 100))
        db.add(mv)
        db.flush()
        if tier == "engine":
            db.add(ModelArtifact(version_id=mv.id, company_id=company_id, size=len(blob),
                                 sha256=hashlib.sha256(blob).hexdigest(), content=blob))
        promote = current is None or scores["new"]["margin"] >= scores["current"]["margin"]
        mv.promotion = {"decision": "promoted" if promote else "kept the current model",
                        "new_margin": _clean(scores["new"]["margin"]),
                        "current_margin": _clean(scores["current"]["margin"]) if current else None,
                        "current_version": current.version if current else None,
                        "origins": scores["new"]["origins"]}
        if promote:
            if current is not None:
                current.status = "retired"
            mv.status, mv.promoted, mv.promoted_at = "active", True, utcnow()
        else:
            mv.status = "rejected"
        run.status = "succeeded"
        run.tier = tier
        run.model_version, run.model_version_id = mv.version, mv.id
        run.accuracy = summary
        run.timings = {"seconds": round(time.perf_counter() - t0, 1)}
        run.message = (f"Model {mv.version} trained on {data.history_days} days of sales and "
                       + ("is now in use." if promote else
                          f"kept {current.version}, which would have earned more."))
        run.finished_at = utcnow()
        co = db.get(Company, company_id)
        co.setup = {k: v for k, v in (co.setup or {}).items() if k != "retrain_requested"}
        db.commit()
    except Exception as e:  # noqa: BLE001 -- recorded, alerted, never raised to the worker
        db.rollback()
        _fail(db, run, e, "train")
        return run
    nightly(db, company_id, trigger="after_training")
    return run


def live_pipeline(cfg, quick: bool, tables: dict, splits: dict | None) -> dict:
    from ... import pipeline
    return pipeline.run(cfg=cfg, quick=quick, ablate_censoring=False, light=True,
                        tables=tables, splits=splits)


def _load(db: Session, mv: ModelVersion) -> dict | str:
    if mv.tier == "starter":
        return "starter"
    art = db.get(ModelArtifact, mv.id)
    return live.load_model(art.content)


def _fail(db: Session, run: ForecastRun, e: Exception, what: str) -> None:
    run = db.merge(run)
    run.status = "failed"
    run.finished_at = utcnow()
    run.error = "".join(traceback.format_exception(type(e), e, e.__traceback__))[-8000:]
    if what == "nightly":
        run.message = ("Last night's forecast did not run. Yesterday's forecast is still "
                       "showing, with its age on every screen. It will try again tonight.")
        if "QualityGateFailure" in type(e).__name__:
            run.message = ("Last night's data did not pass its checks, so the forecast was "
                           "not refreshed. Yesterday's forecast is still showing.")
    else:
        run.message = "Training did not finish. The model in use is unchanged."
    _alert(db, run.company_id, f"forecast_{what}_failed", "risk", run.message,
           f"forecast_{what}_failed:{date.today().isoformat()}")
    db.commit()


# ===================================================================== nightly

def supply_terms(db: Session, company_id: str, rules: dict) -> dict:
    """Per product: (supplier, lead time days, review period days, case pack, MOQ).
    Without a supplier, the company defaults apply (section 5 step 5)."""
    out = {}
    rows = db.execute(select(SupplierProduct, Supplier).join(
        Supplier, Supplier.id == SupplierProduct.supplier_id).where(
        SupplierProduct.company_id == company_id).order_by(SupplierProduct.preferred.desc()))
    for link, sup in rows:
        if link.product_id in out:
            continue
        n = len(sup.order_days or [])
        review = max(1, round(7 / n)) if n else int(rules["default_review_period_days"])
        out[link.product_id] = (sup.id, int(sup.lead_time_days), review, link.case_pack,
                                link.min_order_qty_milli)
    return out


def _upcoming_events(db: Session, company_id: str, stores: dict, start: date,
                     days: int = 14) -> dict[str, list[tuple[date, str]]]:
    end = start + timedelta(days=days)
    out: dict[str, list] = {sid: [] for sid in stores}
    for e in db.scalars(select(CalendarEvent).where(
            CalendarEvent.company_id == company_id, CalendarEvent.day > start,
            CalendarEvent.day <= end, CalendarEvent.kind.in_(("festival", "local_event")))):
        for sid, s in stores.items():
            if (e.store_ids and sid in e.store_ids) or (not e.store_ids and (
                    not e.states or (s.state and s.state in e.states))):
                out[sid].append((e.day, e.name))
    return out


def nightly(db: Session, company_id: str, trigger: str = "schedule",
            run_id: str | None = None) -> ForecastRun:
    run = _start(db, company_id, "nightly", trigger, run_id)
    t0 = time.perf_counter()
    try:
        mv = active_version(db, company_id)
        if mv is None:
            run.status = "skipped"
            run.message = "There is no trained model yet. The first one trains once 8 weeks " \
                          "of sales are loaded."
            run.finished_at = utcnow()
            db.commit()
            return run
        data = source.load(db, company_id)
        cfg = company_cfg(db, company_id)
        with scratch():
            if mv.tier == "starter":
                res = live.starter(data.tables, cfg)
            else:
                res = live.forecast(_load(db, mv), data.tables, cfg)
        n = write_recommendations(db, company_id, run, res, data, cfg)
        run.tier = mv.tier
        run.model_version, run.model_version_id = mv.version, mv.id
        run.data_cutoff = data.cutoff
        run.status = "succeeded"
        run.timings = {"seconds": round(time.perf_counter() - t0, 1), "series": n}
        run.message = (f"Forecast for {n:,} products and stores, from sales to "
                       f"{data.cutoff:%d %b %Y}.")
        run.finished_at = utcnow()
        db.flush()
        score_previous(db, company_id, run, data)
        _check_accuracy(db, company_id, mv)
        _prune(db, company_id)
        db.commit()
    except Exception as e:  # noqa: BLE001
        db.rollback()
        _fail(db, run, e, "nightly")
    return run


def write_recommendations(db: Session, company_id: str, run: ForecastRun,
                          res: live.LiveResult, data: source.CompanyData, cfg) -> int:
    rules = rules_mod.effective(db.get(Company, company_id).rules)
    P = res.P
    item = P.series["item_id"].astype(str).to_numpy()
    store = P.series["store_id"].astype(str).to_numpy()
    S = len(item)
    keep = np.array([pid in data.products and data.products[pid].status != "discontinued"
                     and sid in data.stores for pid, sid in zip(item, store)])
    scale = np.array([data.scale(pid) if pid in data.products else 1000 for pid in item])
    terms = supply_terms(db, company_id, rules)
    lead = np.array([terms.get(pid, (None, int(rules["default_lead_time_days"])))[1]
                     for pid in item])
    review = np.array([terms[pid][2] if pid in terms else int(rules["default_review_period_days"])
                       for pid in item])
    risk = np.clip(lead + review, 1, res.quantiles.shape[1])
    cp = live.cost_params(P, data.tables["items"], cfg)
    q = res.quantiles
    tq, target, parts = live.targets(q, cp, risk, cfg)
    week = live.week_range(q)
    bands = live.daily_bands(q)
    risk_q = np.zeros((S, len(QUANTILES)))
    for r in np.unique(risk):
        idx = risk == r
        for j, level in enumerate(QUANTILES):
            risk_q[idx, j] = dec.risk_period_quantile(q[idx, : int(r), :],
                                                      np.full(int(idx.sum()), level))
    store_ids = list(data.stores)
    soh = {(r.store_id, r.product_id): r for r in db.scalars(select(StockOnHand).where(
        StockOnHand.company_id == company_id))}
    oo = stock_mod.on_order(db, company_id, store_ids)
    recent = stock_mod.daily_rate(db, company_id, store_ids, today=data.cutoff + timedelta(days=1))
    events = _upcoming_events(db, company_id, data.stores, data.cutoff)
    rows = []
    for s in range(S):
        if not keep[s]:
            continue
        pid, sid, sc = item[s], store[s], int(scale[s])
        p = data.products[pid]
        st = soh.get((sid, pid))
        on_hand = st.on_hand_milli if st else 0
        known = bool(st and st.known)
        on_order = oo.get((sid, pid), 0)
        tgt = int(round(target[s] * sc))
        term = terms.get(pid)
        case = (term[3] if term and term[3] else p.case_pack or 1) * 1000
        suggested = None
        if known:
            need = max(0, tgt - on_hand - on_order)
            suggested = int(math.ceil(need / case) * case) if need else 0
        under = int(round(parts["underage_cost"][s] * 100 * 1000 / sc))   # paise per unit
        over = int(round(parts["overage_cost"][s] * 100 * 1000 / sc))
        estimate = bool(res.history_days[s] < MIN_DAYS)
        reasons = []
        if estimate:
            reasons.append({"key": "estimate", "text": "Estimate from similar products: "
                                                        "no sales history of its own yet"})
        for d, name in events.get(sid, [])[:2]:
            reasons.append({"key": "festival", "text": f"{name} on {d:%d %b}"})
        mid7 = int(round(week[s, 1] * sc))
        rate = recent.get((sid, pid), 0.0) * 7
        if rate >= 3000 and mid7 > 1.4 * rate:
            reasons.append({"key": "rise", "text": f"Forecast is {round(100 * mid7 / rate - 100)}% "
                                                   "above the last four weeks"})
        elif rate >= 3000 and mid7 < 0.6 * rate:
            reasons.append({"key": "fall", "text": f"Forecast is {round(100 - 100 * mid7 / rate)}% "
                                                   "below the last four weeks"})
        money = 0
        if not known:
            reasons.append({"key": "unknown", "text": "Stock is not known: count it to get an "
                                                       "order suggestion"})
        else:
            short = tgt - on_hand - on_order
            if short > 0:
                reasons.append({"key": "below_target", "text": "Below its stock target"})
                money = short * under // 1000
            elif on_hand > 2 * max(tgt, 1000):
                reasons.append({"key": "overstock", "text": "More stock than it needs"})
                money = (on_hand - tgt) * over // 1000
        rows.append(Recommendation(
            company_id=company_id, run_id=run.id, store_id=sid, product_id=pid,
            daily=[[int(round(v * sc)) for v in day] for day in bands[s]],
            risk_q=[int(round(v * sc)) for v in risk_q[s]], risk_days=int(risk[s]),
            lead_time_days=int(lead[s]), supplier_id=term[0] if term else None,
            on_hand_milli=on_hand, on_order_milli=on_order, stock_known=known,
            suggested_milli=suggested, underage_paise=under, overage_paise=over,
            planning_class=str(res.planning_class[s]), history_days=int(res.history_days[s]),
            estimate=estimate, week1_lo_milli=int(round(week[s, 0] * sc)), week1_mid_milli=mid7,
            week1_hi_milli=int(round(week[s, 2] * sc)), target_milli=tgt,
            service_level=float(round(tq[s], 4)), reasons=reasons, money_at_risk_paise=int(money)))
    by_store: dict[str, list[Recommendation]] = {}
    for r in rows:
        by_store.setdefault(r.store_id, []).append(r)
    for recs in by_store.values():
        recs.sort(key=lambda r: -r.money_at_risk_paise)
        for r in recs[:FLAG_PER_STORE]:
            if r.money_at_risk_paise > 0 or any(x["key"] in ("festival", "rise", "fall")
                                                for x in r.reasons):
                r.flagged = True
    db.add_all(rows)
    db.flush()
    return len(rows)


def score_previous(db: Session, company_id: str, run: ForecastRun,
                   data: source.CompanyData) -> int:
    """Monitoring: the previous night's day-one forecast against what sold."""
    prev = db.scalar(select(ForecastRun).where(
        ForecastRun.company_id == company_id, ForecastRun.kind == "nightly",
        ForecastRun.status == "succeeded", ForecastRun.id != run.id,
        ForecastRun.data_cutoff < run.data_cutoff).order_by(ForecastRun.created_at.desc()).limit(1))
    if prev is None or prev.data_cutoff is None:
        return 0
    day = prev.data_cutoff + timedelta(days=1)
    if day > data.cutoff:
        return 0
    actual = {(r.store_id, r.product_id): r for r in db.scalars(select(DailySales).where(
        DailySales.company_id == company_id, DailySales.day == day))}
    reported = {sid for sid, _ in actual}
    cats = {pid: (p.category or "Uncategorised") for pid, p in data.products.items()}
    agg: dict[tuple[str, str], list[int]] = {}
    for rec in db.scalars(select(Recommendation).where(Recommendation.run_id == prev.id)):
        if rec.store_id not in reported or not rec.daily:
            continue
        a = actual.get((rec.store_id, rec.product_id))
        if a is not None and a.out_of_stock:
            continue
        units = a.units_milli if a is not None else 0
        lo, mid, hi = rec.daily[0]
        k = (rec.store_id, cats.get(rec.product_id, "Uncategorised"))
        v = agg.setdefault(k, [0, 0, 0, 0, 0])
        v[0] += 1
        v[1] += units
        v[2] += mid
        v[3] += abs(units - mid)
        v[4] += int(lo <= units <= hi)
    db.execute(delete(ForecastScore).where(ForecastScore.run_id == prev.id))
    for (sid, cat), (n, a, f, e, inside) in agg.items():
        db.add(ForecastScore(company_id=company_id, run_id=prev.id, day=day, store_id=sid,
                             category=cat, n=n, actual_milli=a, forecast_milli=f,
                             abs_error_milli=e, inside_range=inside))
    db.flush()
    return len(agg)


def _check_accuracy(db: Session, company_id: str, mv: ModelVersion) -> None:
    """A sustained drop raises an alert and asks for an early retrain (section 10)."""
    since = date.today() - timedelta(days=14)
    a, e, n = db.execute(select(func.coalesce(func.sum(ForecastScore.actual_milli), 0),
                                func.coalesce(func.sum(ForecastScore.abs_error_milli), 0),
                                func.count()).where(ForecastScore.company_id == company_id,
                                                    ForecastScore.day >= since)).one()
    expected = (mv.backtest or {}).get("wape")
    if not a or not expected or n < 10:
        return
    wape = e / a
    if wape > max(1.5 * expected, expected + 0.15):
        co = db.get(Company, company_id)
        co.setup = {**(co.setup or {}), "retrain_requested": utcnow().isoformat()}
        _alert(db, company_id, "accuracy_drop", "attention",
               f"Forecast error over the last two weeks is {round(100 * wape)}%, against "
               f"{round(100 * expected)}% in the backtest. An early retrain is booked for tonight.",
               f"accuracy_drop:{date.today().isocalendar()[1]}")


def _prune(db: Session, company_id: str, keep_days: int = 60) -> None:
    """Old forecasts are kept only where a person made a decision on them."""
    cutoff = utcnow() - timedelta(days=keep_days)
    old = select(ForecastRun.id).where(ForecastRun.company_id == company_id,
                                       ForecastRun.created_at < cutoff)
    decided = select(Decision.recommendation_id)
    db.execute(delete(Recommendation).where(Recommendation.run_id.in_(old),
                                            Recommendation.id.not_in(decided)))


# ===================================================================== command line

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m dqstack.app.forecast.jobs")
    ap.add_argument("job", choices=("train", "nightly"))
    ap.add_argument("--company", required=True)
    ap.add_argument("--trigger", default="schedule")
    ap.add_argument("--run", default=None)
    args = ap.parse_args(argv)
    from ..db import SessionLocal
    with SessionLocal() as db:
        run = (train(db, args.company, args.trigger, args.run) if args.job == "train"
               else nightly(db, args.company, args.trigger, args.run))
        print(f"{args.job} {run.status}: {run.message}")
        return 0 if run.status in ("succeeded", "skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
