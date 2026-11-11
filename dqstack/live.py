"""Forecasting from a saved model, without retraining: the nightly run.

The weekly run (`pipeline.run`) trains every candidate, fits the combination and
the calibration, and backtests: minutes to an hour. The nightly run only pushes
today's data through the models already trained, which takes seconds. Section 10
of the specification asks for exactly this split ("save every trained model with a
version number; overnight runs only forecast; retrain weekly").

    saved = saved_model(state, cfg)          # bytes, kept with the model version
    model = load_model(saved)
    out = forecast(model, tables)            # today's 28-day distribution
    tq, target = targets(out.quantiles, cost, risk_days, cfg)

Weights for a series the model has not seen (a product launched since training)
come from the saved combination rule applied to today's diagnostics, so new
products are forecast too -- by the cold-start candidates the ensemble already
contains.

For histories too short to train on (2 weeks to about 10 months), `starter` uses
only the engine's statistical candidates, which need no training, lifted to a
distribution by a Negative Binomial. It is labelled as an early forecast.

A model trained in shards (dqstack/app/forecast/shards.py) is used through the same
`forecast_panel`, which hands it the panel to forecast shard by shard.

Saved models are pickles. Load only what this system wrote itself (the model
store in the company's own database); never a file from anywhere else.
"""

from __future__ import annotations

import gzip
import io
import pickle
from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from . import combine as comb
from . import decide as dec
from . import diagnose, ingest
from .config import CFG, HORIZON, Q50_INDEX, QUANTILES
from .features import Panel, build_panel, horizon_group_of
from .models import statistical
from .models.dist import nb_quantiles, sanitize

KEEP = ("trained", "meta", "conformal", "conformal_orders", "weights_method", "stack_tbl",
        "meta_extra")
# Kept when the state has them: the single best method and its own calibrator, which
# let the app issue that method instead of the blend (`issued_as`).
OPTIONAL = ("best_single", "conformal_single")
ISSUES = ("blend", "single")


def saved_model(state: dict, cfg=CFG) -> bytes:
    """What the nightly run needs, compressed: the trained candidates (without the
    ablation arm), the combination rule, the conformal calibrators and the config."""
    keep = {k: state[k] for k in KEEP}
    keep.update({k: state[k] for k in OPTIONAL if state.get(k) is not None})
    keep["trained"] = replace(state["trained"], gbm_nomask=None, P_nomask=None)
    keep["series"] = state["P"].series["series_id"].astype(str).tolist()
    keep["cfg"] = cfg
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6) as f:
        pickle.dump(keep, f, protocol=pickle.HIGHEST_PROTOCOL)
    return buf.getvalue()


def load_model(blob: bytes) -> dict:
    with gzip.GzipFile(fileobj=io.BytesIO(blob)) as f:
        return pickle.load(f)  # noqa: S301 -- only blobs this system wrote (see module doc)


def orders_calibrator(model: dict):
    """Stage 6 for the forecast orders read, fitted on that forecast. A model saved
    before it had its own has only the calibrator fitted on the reconciled forecast,
    and keeps using it until it is retrained."""
    cal = model.get("conformal_orders")
    return model["conformal"] if cal is None else cal


def issued_as(model, issue: str):
    """The same trained model, issuing `issue`:

    * "blend": Stage 4's combination of every method (what a model issues by default);
    * "single": the one method with the lowest loss on the meta origins, corrected by
      the calibrator fitted on that method's own forecasts.

    The app's training scores both on the same recent weeks and issues whichever
    would have earned more (spec, Stage 4: "allow issuing the single best model when
    it earns more"). The choice is recorded with the model version, not in the saved
    bytes, so one saved model serves both. A model saved before it kept its single
    best method can only issue the blend. The early forecast ("starter") is returned
    as it is."""
    if issue not in ISSUES:
        raise ValueError(f"unknown issue {issue!r}")
    if isinstance(model, str):
        return model
    if hasattr(model, "issued_as"):            # trained in shards
        return model.issued_as(issue)
    out = dict(model)
    out["issue"] = issue
    return out


def can_issue_single(model) -> bool:
    """Whether this model kept a single best method to issue on its own."""
    if isinstance(model, str):
        return False
    if hasattr(model, "can_issue_single"):
        return model.can_issue_single()
    return bool(model.get("best_single")) and model.get("conformal_single") is not None


@dataclass
class LiveResult:
    P: Panel
    origin: int
    quantiles: np.ndarray          # (S, H, Q) calibrated, unreconciled: what orders read
    planning_class: np.ndarray     # (S,)
    history_days: np.ndarray       # (S,) days since the series' first sale
    feed_runs: list
    tier: str                      # "engine" or "starter"

    @property
    def series_ids(self) -> list[str]:
        return self.P.series["series_id"].astype(str).tolist()


def _prepare(tables: dict, cfg) -> tuple[Panel, pd.DataFrame, list]:
    ing = ingest.run(tables=tables)            # Stage 0: raises on a blocking failure
    hist = ing.panel[~ing.panel["is_future"]].reset_index(drop=True)
    diag, masks = diagnose.run(hist, cfg)
    P = build_panel(ing.panel, diag, masks, apply_mask=cfg.enable_censor_masking)
    return P, diag, ing.feed_runs


def _history_days(P: Panel, O: int) -> np.ndarray:
    sold = np.isfinite(P.y[:, : O + 1]) & (np.nan_to_num(P.y[:, : O + 1]) > 0)
    first = np.where(sold.any(axis=1), sold.argmax(axis=1), O + 1)
    return (O + 1 - first).astype(int)


def forecast(model: dict, tables: dict, cfg=None) -> LiveResult:
    cfg = cfg or model["cfg"]
    P, diag, feed_runs = _prepare(tables, cfg)
    O = P.n_history - 1
    q = forecast_panel(model, P, diag, O, cfg)
    pc = P.series["planning_class"].astype(str).to_numpy(dtype="U20")
    return LiveResult(P=P, origin=O, quantiles=q, planning_class=pc,
                      history_days=_history_days(P, O), feed_runs=feed_runs, tier="engine")


def cost_params(P: Panel, items: pd.DataFrame, cfg=CFG) -> dec.CostParams:
    """Per-series costs, built the way the pipeline builds them for Stage 7."""
    it = items.set_index("item_id")
    ids = P.series["item_id"].astype(str).to_numpy()
    price = np.nan_to_num(P.series["avg_price"].to_numpy(dtype="float64"), nan=0.0)
    unit_cost = it.loc[ids, "unit_cost"].to_numpy(dtype="float64")
    margin = it.loc[ids, "margin_rate"].to_numpy(dtype="float64")
    price = np.where(price <= 0.0, unit_cost / np.maximum(1.0 - margin, 0.1), price)
    return dec.CostParams(price=price, unit_cost=unit_cost, margin_rate=margin,
                          shelf_life=it.loc[ids, "shelf_life_days"].to_numpy().astype("float64"),
                          holding_rate=cfg.decision.holding_cost_rate,
                          markdown_risk=cfg.decision.markdown_risk)


def recent_scores(models: dict[str, dict | str], tables: dict, cfg=CFG, n_origins: int = 4
                  ) -> dict[str, dict[str, float]]:
    """Each model scored on the same recent weekly origins against what actually
    sold: simulated margin (the promotion rule's evidence), WAPE, how often sales
    fell inside the 5-95% range, and fill rate. Comparing two models' own backtests
    would compare different weeks; this compares like with like. A model is a saved
    model, or the string "starter" for the early forecast."""
    return finish_scores(score_sums(models, tables, cfg, n_origins))


def score_sums(models: dict[str, dict | str], tables: dict, cfg=CFG, n_origins: int = 4
               ) -> dict[str, dict]:
    """The sums behind `recent_scores`, per model, with each origin's figures kept
    apart, so that scores made on separate parts of a company's series (the shards of
    app/forecast/shards.py) add up to the score of the whole."""
    P, diag, _ = _prepare(tables, cfg)
    cp = cost_params(P, tables["items"], cfg)
    live_o = P.n_history - 1
    origins = [o for o in (live_o - HORIZON - 7 * k for k in range(n_origins)) if o > 28]
    sums = {name: {"dates": [], "margin": [], "fill": [], "sales": [], "demand": [],
                   "abs_err": 0.0, "actual": 0.0, "inside": 0.0, "cells": 0.0}
            for name in models}
    sids = P.series["series_id"].astype(str).to_numpy()
    lo, hi = QUANTILES.index(0.05), QUANTILES.index(0.95)
    for O in origins:
        H = range(1, HORIZON + 1)
        actual = np.stack([P.y[:, O + h] for h in H], axis=1)        # masked: demand signal
        valid = np.isfinite(actual)
        demand = np.nan_to_num(np.stack([P.y_raw[:, O + h] for h in H], axis=1).astype("float64"))
        dates = pd.DatetimeIndex([P.dates[O + h] for h in H])
        shared: dict = {}       # one trained model issued two ways forecasts its methods once
        for name, model in models.items():
            q = starter_panel(P, O) if model == "starter" else \
                forecast_panel(model, P, diag, O, cfg, shared)
            res, _ = dec.decide(q, demand, cp, sids, dates, cfg)
            s = sums[name]
            s["dates"].append(str(P.dates[O].date()))
            s["margin"].append(float(res.totals["realized_margin"]))
            s["fill"].append(float(res.totals.get("fill_rate", 0.0)))
            s["sales"].append(float(res.totals.get("sales", 0.0)))
            s["demand"].append(float(res.totals.get("demand", 0.0)))
            s["abs_err"] += float(np.abs(np.where(valid, actual - q[..., Q50_INDEX], 0.0)).sum())
            s["actual"] += float(np.where(valid, actual, 0.0).sum())
            s["inside"] += float(((q[..., lo] <= actual) & (actual <= q[..., hi]))[valid].sum())
            s["cells"] += float(valid.sum())
    return sums


def finish_scores(sums: dict[str, dict]) -> dict[str, dict[str, float]]:
    """`score_sums` (or several added together) as the scores `recent_scores` gives:
    margin and fill rate averaged over the origins."""
    out = {}
    for name, s in sums.items():
        n = max(len(s["dates"]), 1)
        margin = fill = 0.0
        for m, f in zip(s["margin"], s["fill"]):
            margin += m
            fill += f
        out[name] = {"margin": margin / n, "fill_rate": fill / n,
                     "wape": s["abs_err"] / s["actual"] if s["actual"] else float("nan"),
                     "inside_range": s["inside"] / s["cells"] if s["cells"] else float("nan"),
                     "origins": len(s["dates"])}
    return out


def forecast_panel(model: dict, P: Panel, diag: pd.DataFrame, O: int, cfg,
                   shared: dict | None = None) -> np.ndarray:
    """The calibrated, unreconciled distribution at origin O: what orders read. That
    is the blend, or the single best method alone when the model issues it
    (`issued_as`). `shared`, when given, keeps each trained model's method forecasts
    for this panel and origin, so the same model issued both ways forecasts once."""
    if hasattr(model, "panel_quantiles"):      # trained in shards: app/forecast/shards.py
        return model.panel_quantiles(P, diag, O, cfg)
    from .pipeline import candidate_quantiles
    horizons = tuple(range(1, HORIZON + 1))
    tr, meta = model["trained"], model["meta"]
    key = (id(tr), O)
    if shared is not None and key in shared:
        qs = shared[key]
    else:
        _, qs = candidate_quantiles(P, O, horizons, tr)
        if shared is not None:
            shared[key] = qs
    pc = P.series["planning_class"].astype(str).to_numpy(dtype="U20")
    hg = np.array([horizon_group_of(h) for h in horizons], dtype="U12")
    pc2 = np.repeat(pc[:, None], len(horizons), axis=1)
    hg2 = np.broadcast_to(hg, pc2.shape)
    single = model.get("best_single") if model.get("issue") == "single" else None
    if single is not None and single in qs and model.get("conformal_single") is not None:
        if cfg.enable_calibration:
            return model["conformal_single"].apply(qs[single], pc2, hg2)
        return qs[single]
    if model["weights_method"] == "stack":
        W = comb.weights_from_strata(pc, model["stack_tbl"], len(meta.candidates))
    else:
        d = P.series.copy()
        extra = [c for c in (model.get("meta_extra") or []) if c not in d.columns
                 and c in diag.columns]
        if extra:
            d = d.merge(diag[["series_id", *extra]].assign(
                series_id=lambda x: x["series_id"].astype(str)), on="series_id", how="left")
        W = meta.weights(d, cfg)        # "select" (an ablation) also falls back here
    combined = comb.combine_quantiles(qs, W, meta.candidates)
    if cfg.enable_calibration:
        return orders_calibrator(model).apply(combined, pc2, hg2)
    return combined


def starter(tables: dict, cfg=CFG, dispersion: float = 4.0) -> LiveResult:
    """An early forecast for 2 weeks to ~10 months of history: the statistical
    candidates (seasonal naive, ETS, Croston-SBA, TSB, ADIDA) averaged, with a
    Negative Binomial range. No training, so nothing to save."""
    P, diag, feed_runs = _prepare(tables, cfg)
    O = P.n_history - 1
    q = starter_panel(P, O, dispersion)
    pc = P.series["planning_class"].astype(str).to_numpy(dtype="U20")
    return LiveResult(P=P, origin=O, quantiles=q, planning_class=pc,
                      history_days=_history_days(P, O), feed_runs=feed_runs, tier="starter")


def starter_panel(P: Panel, O: int, dispersion: float = 4.0) -> np.ndarray:
    """The statistical candidates averaged at origin O, with a Negative Binomial
    range. (S, H, Q)"""
    pts = statistical.forecast_all(P, O, np.arange(1, HORIZON + 1))
    mu = np.nanmean(np.stack(list(pts.values())), axis=0)
    mu = np.nan_to_num(np.clip(mu, 0.0, None))
    return sanitize(nb_quantiles(mu, np.full(mu.shape, dispersion)))


def targets(q: np.ndarray, cp: dec.CostParams, risk_days: np.ndarray, cfg=CFG
            ) -> tuple[np.ndarray, np.ndarray, dict[str, np.ndarray]]:
    """Stock target per series from its own risk period (supplier lead time plus
    review period), not one global rule: the critical fractile and the
    risk-period quantile, computed for each distinct risk period in turn."""
    S = q.shape[0]
    risk = np.clip(np.asarray(risk_days, dtype=int), 1, q.shape[1])
    tq, target = np.zeros(S), np.zeros(S)
    parts = {k: np.zeros(S) for k in ("underage_cost", "overage_cost")}
    for r in np.unique(risk):
        idx = risk == r
        sub = dec.CostParams(price=cp.price[idx], unit_cost=cp.unit_cost[idx],
                             margin_rate=cp.margin_rate[idx], shelf_life=cp.shelf_life[idx],
                             holding_rate=cp.holding_rate, markdown_risk=cp.markdown_risk)
        fr = dec.critical_fractile(sub, int(r), cfg)
        tq[idx] = fr["target_quantile"]
        target[idx] = dec.risk_period_quantile(q[idx, : int(r), :], fr["target_quantile"])
        for k in parts:
            parts[k][idx] = fr[k]
    return tq, target, parts


def week_range(q: np.ndarray, days: int = 7) -> np.ndarray:
    """Total demand over the next `days` at the 5%, 50% and 95% levels. (S, 3)"""
    S = q.shape[0]
    return np.stack([dec.risk_period_quantile(q[:, :days, :], np.full(S, level))
                     for level in (0.05, 0.50, 0.95)], axis=1)


def daily_bands(q: np.ndarray) -> np.ndarray:
    """(S, H, 3): each day's 5%, 50% and 95% quantiles, for charts."""
    idx = [QUANTILES.index(0.05), Q50_INDEX, QUANTILES.index(0.95)]
    return q[..., idx]
