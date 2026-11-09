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

For histories too short to train on (8 weeks to about 6 months), `starter` uses
only the engine's statistical candidates, which need no training, lifted to a
distribution by a Negative Binomial. It is labelled as an early forecast.

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

KEEP = ("trained", "meta", "conformal", "weights_method", "stack_tbl", "meta_extra")


def saved_model(state: dict, cfg=CFG) -> bytes:
    """What the nightly run needs, compressed: the trained candidates (without the
    ablation arm), the combination rule, the conformal offsets and the config."""
    keep = {k: state[k] for k in KEEP}
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
    P, diag, _ = _prepare(tables, cfg)
    cp = cost_params(P, tables["items"], cfg)
    live_o = P.n_history - 1
    origins = [o for o in (live_o - HORIZON - 7 * k for k in range(n_origins)) if o > 28]
    sums = {name: {"margin": 0.0, "abs_err": 0.0, "actual": 0.0, "inside": 0.0, "cells": 0.0,
                   "fill": 0.0} for name in models}
    sids = P.series["series_id"].astype(str).to_numpy()
    lo, hi = QUANTILES.index(0.05), QUANTILES.index(0.95)
    for O in origins:
        H = range(1, HORIZON + 1)
        actual = np.stack([P.y[:, O + h] for h in H], axis=1)        # masked: demand signal
        valid = np.isfinite(actual)
        demand = np.nan_to_num(np.stack([P.y_raw[:, O + h] for h in H], axis=1).astype("float64"))
        dates = pd.DatetimeIndex([P.dates[O + h] for h in H])
        for name, model in models.items():
            q = starter_panel(P, O) if model == "starter" else \
                forecast_panel(model, P, diag, O, cfg)
            res, _ = dec.decide(q, demand, cp, sids, dates, cfg)
            s = sums[name]
            s["margin"] += float(res.totals["realized_margin"])
            s["fill"] += float(res.totals.get("fill_rate", 0.0))
            s["abs_err"] += float(np.abs(np.where(valid, actual - q[..., Q50_INDEX], 0.0)).sum())
            s["actual"] += float(np.where(valid, actual, 0.0).sum())
            s["inside"] += float(((q[..., lo] <= actual) & (actual <= q[..., hi]))[valid].sum())
            s["cells"] += float(valid.sum())
    n = max(len(origins), 1)
    return {name: {"margin": s["margin"] / n, "fill_rate": s["fill"] / n,
                   "wape": s["abs_err"] / s["actual"] if s["actual"] else float("nan"),
                   "inside_range": s["inside"] / s["cells"] if s["cells"] else float("nan"),
                   "origins": len(origins)} for name, s in sums.items()}


def forecast_panel(model: dict, P: Panel, diag: pd.DataFrame, O: int, cfg) -> np.ndarray:
    """The calibrated, unreconciled distribution at origin O: what orders read."""
    from .pipeline import candidate_quantiles
    horizons = tuple(range(1, HORIZON + 1))
    tr, meta = model["trained"], model["meta"]
    _, qs = candidate_quantiles(P, O, horizons, tr)
    pc = P.series["planning_class"].astype(str).to_numpy(dtype="U20")
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
    hg = np.array([horizon_group_of(h) for h in horizons], dtype="U12")
    pc2 = np.repeat(pc[:, None], len(horizons), axis=1)
    hg2 = np.broadcast_to(hg, pc2.shape)
    if cfg.enable_calibration:
        return model["conformal"].apply(combined, pc2, hg2)
    return combined


def starter(tables: dict, cfg=CFG, dispersion: float = 4.0) -> LiveResult:
    """An early forecast for 8 weeks to ~6 months of history: the statistical
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
