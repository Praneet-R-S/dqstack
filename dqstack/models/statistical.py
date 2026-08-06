"""Stage 3.1 — statistical baselines. Mandatory, not throwaway.

On genuinely intermittent series the Croston family routinely beats gradient
boosting, and a system that does not route to them leaves accuracy on the table.
Seasonal naive is never deployed alone; it is the anchor against which Forecast
Value Added is measured, so every later stage has to earn its place against it.

All methods are implemented vectorised across series over the dense `(S, T)`
matrix. A per-series Python loop over 1,000 series x 1,100 days would dominate
the runtime of the whole pipeline for no benefit.
"""

from __future__ import annotations

import numpy as np

from ..features import Panel


def _filled(P: Panel, O: int) -> np.ndarray:
    """History up to the origin with masked cells carried forward.

    Masked cells are censored or out-of-assortment days. Filling them with zero
    would reintroduce exactly the bias Stage 1 exists to remove, so they are
    filled by forward-fill and then by the series' own mean.
    """
    y = P.y[:, : O + 1].astype("float64").copy()
    S, T = y.shape
    idx = np.where(~np.isnan(y), np.arange(T)[None, :], -1)
    last = np.maximum.accumulate(idx, axis=1)
    rows = np.arange(S)[:, None]
    ff = np.where(last >= 0, y[rows, np.clip(last, 0, T - 1)], np.nan)
    mean = np.nanmean(ff, axis=1, keepdims=True)
    return np.nan_to_num(ff, nan=0.0) + np.where(np.isnan(ff), np.nan_to_num(mean), 0.0)


# ------------------------------------------------------------------ seasonal naive

def seasonal_naive(P: Panel, O: int, horizons: np.ndarray, period: int = 7) -> np.ndarray:
    """Last observed same-weekday value. The FVA anchor."""
    y = _filled(P, O)
    out = np.empty((P.n_series, len(horizons)), dtype="float64")
    for j, h in enumerate(horizons):
        # walk back whole periods until inside the observed window
        back = period * int(np.ceil(h / period))
        src = O - back + h
        src = min(max(src, 0), O)
        out[:, j] = y[:, src]
    return np.maximum(out, 0.0)


# ------------------------------------------------------------------ ETS

def ets_holt_winters(
    P: Panel, O: int, horizons: np.ndarray, period: int = 7, grid: bool = True
) -> np.ndarray:
    """Additive Holt-Winters with weekly seasonality, over a Panel."""
    return ets_matrix(_filled(P, O), horizons, period, grid)


def ets_matrix(
    y: np.ndarray, horizons: np.ndarray, period: int = 7, grid: bool = True
) -> np.ndarray:
    """Additive Holt-Winters on a plain `(N, T)` history matrix.

    Smoothing parameters are selected per row from a small grid by in-sample
    one-step SSE. The grid is evaluated for all rows simultaneously, so the cost
    is a handful of vectorised passes rather than N optimiser runs.

    Kept separate from the Panel-based wrapper so Stage 5 can forecast the
    *aggregate* levels of the hierarchy with the same estimator — those series
    are rows of a matrix, not entries in a panel.
    """
    y = np.nan_to_num(np.asarray(y, dtype="float64"), nan=0.0)
    S, T = y.shape
    n = min(T, 546)
    y = y[:, -n:]

    combos = (
        [(a, b, g) for a in (0.08, 0.20, 0.40) for b in (0.0, 0.03) for g in (0.10, 0.30)]
        if grid else [(0.20, 0.02, 0.20)]
    )

    best_sse = np.full(S, np.inf)
    best = np.zeros((S, 2 + period))  # level, trend, seasonal[period]
    best_par = np.zeros((S, 3))

    seas0 = np.zeros((S, period))
    for p in range(period):
        seas0[:, p] = y[:, p::period].mean(axis=1) - y.mean(axis=1)
    lvl0 = y[:, :period].mean(axis=1)

    for alpha, beta, gamma in combos:
        lvl = lvl0.copy()
        trend = np.zeros(S)
        seas = seas0.copy()
        sse = np.zeros(S)
        for t in range(period, n):
            sp = (t - period) % period
            fitted = lvl + trend + seas[:, sp]
            e = y[:, t] - fitted
            sse += e * e
            lvl_new = alpha * (y[:, t] - seas[:, sp]) + (1 - alpha) * (lvl + trend)
            trend = beta * (lvl_new - lvl) + (1 - beta) * trend
            seas[:, sp] = gamma * (y[:, t] - lvl_new) + (1 - gamma) * seas[:, sp]
            lvl = lvl_new
        better = sse < best_sse
        if better.any():
            best_sse[better] = sse[better]
            best[better, 0] = lvl[better]
            best[better, 1] = trend[better]
            best[better, 2:] = seas[better]
            best_par[better] = (alpha, beta, gamma)

    lvl, trend, seas = best[:, 0], best[:, 1], best[:, 2:]
    out = np.empty((S, len(horizons)))
    for j, h in enumerate(horizons):
        sp = (n - period + (h - 1)) % period
        out[:, j] = lvl + h * trend + seas[:, sp]
    return np.maximum(out, 0.0)


# ------------------------------------------------------------------ Croston family

def _croston_core(
    y: np.ndarray, alpha: float, variant: str
) -> tuple[np.ndarray, np.ndarray]:
    """Exponential smoothing of demand size and inter-demand interval.

    Returns (size, interval) at the end of the series.
    """
    S, T = y.shape
    nz = y > 0
    size = np.where(nz.any(axis=1), np.where(nz, y, 0).sum(axis=1) / np.maximum(nz.sum(axis=1), 1), 0.0)
    n_nz = np.maximum(nz.sum(axis=1), 1)
    interval = np.maximum(T / n_nz, 1.0)
    gap = np.zeros(S)

    for t in range(T):
        hit = nz[:, t]
        gap += 1.0
        if hit.any():
            size[hit] = alpha * y[hit, t] + (1 - alpha) * size[hit]
            interval[hit] = alpha * gap[hit] + (1 - alpha) * interval[hit]
            gap[hit] = 0.0
    return size, np.maximum(interval, 1.0)


def croston(
    P: Panel, O: int, horizons: np.ndarray, alpha: float = 0.10, variant: str = "sba"
) -> np.ndarray:
    """Croston / SBA / SBJ.

    Classic Croston is known to be positively biased; the Syntetos-Boylan
    approximation multiplies by (1 - alpha/2) and is the better default, which is
    why `sba` is the variant wired into the candidate pool.
    """
    y = _filled(P, O)
    y = y[:, -min(y.shape[1], 728):]
    size, interval = _croston_core(y, alpha, variant)
    rate = size / interval
    if variant == "sba":
        rate = rate * (1.0 - alpha / 2.0)
    elif variant == "sbj":
        rate = rate * (1.0 - alpha / (2.0 - alpha))
    return np.repeat(np.maximum(rate, 0.0)[:, None], len(horizons), axis=1)


def tsb(
    P: Panel, O: int, horizons: np.ndarray, alpha: float = 0.10, beta: float = 0.04
) -> np.ndarray:
    """Teunter-Syntetos-Babai.

    Smooths the *probability* of demand rather than the interval, so it decays
    the forecast when a series stops selling. That makes it the right member of
    the family for obsolescent and terminal items, where Croston holds its rate
    indefinitely.
    """
    y = _filled(P, O)
    y = y[:, -min(y.shape[1], 728):]
    S, T = y.shape
    nz = y > 0
    prob = np.clip(nz.mean(axis=1), 1e-3, 1.0)
    size = np.where(nz.any(axis=1), np.where(nz, y, 0).sum(axis=1) / np.maximum(nz.sum(axis=1), 1), 0.0)

    for t in range(T):
        hit = nz[:, t]
        prob = prob + beta * (hit.astype("float64") - prob)
        if hit.any():
            size[hit] = alpha * y[hit, t] + (1 - alpha) * size[hit]
    rate = prob * size
    return np.repeat(np.maximum(rate, 0.0)[:, None], len(horizons), axis=1)


def adida(P: Panel, O: int, horizons: np.ndarray, bucket: int = 7) -> np.ndarray:
    """Aggregate-Disaggregate Intermittent Demand Approach.

    Temporal aggregation into weekly buckets removes most of the zeros, a simple
    level forecast is made on the aggregate, and the result is disaggregated back
    to daily by the observed weekday profile. Effective on very sparse series
    where daily methods have almost no signal to work with.
    """
    y = _filled(P, O)
    S, T = y.shape
    usable = (T // bucket) * bucket
    blocks = y[:, T - usable :].reshape(S, -1, bucket)
    agg = blocks.sum(axis=2)
    k = min(8, agg.shape[1])
    level = agg[:, -k:].mean(axis=1) / bucket

    prof = blocks.sum(axis=1)
    prof = prof / np.maximum(prof.sum(axis=1, keepdims=True), 1e-9) * bucket
    prof = np.where(np.isfinite(prof), prof, 1.0)

    # Index the profile by the target date's actual weekday.
    #
    # `blocks` was reshaped from the *end* of the history, so profile column p
    # corresponds to weekday (O - usable + 1 + p) mod 7, not to p itself. Indexing
    # with `(O + h) % bucket` is only correct when the history length happens to be
    # a multiple of 7, and otherwise silently rotates every weekday effect —
    # applying Saturday's multiplier to a Tuesday. Reading the weekday off the
    # calendar removes the arithmetic entirely.
    wd = P.calendar["dayofweek"].to_numpy()
    first_block_day = (O + 1) - usable         # index of blocks[:, 0, 0]
    col_wd = wd[first_block_day : first_block_day + bucket]     # weekday per column

    out = np.empty((S, len(horizons)))
    for j, h in enumerate(horizons):
        target_wd = wd[min(O + h, len(wd) - 1)]
        col = int(np.flatnonzero(col_wd == target_wd)[0])
        out[:, j] = level * prof[:, col]
    return np.maximum(out, 0.0)


# ------------------------------------------------------------------ registry

def forecast_all(P: Panel, O: int, horizons: np.ndarray) -> dict[str, np.ndarray]:
    """Every statistical candidate at one origin. Returns (S, H) conditional means."""
    return {
        "seasonal_naive": seasonal_naive(P, O, horizons),
        "ets": ets_holt_winters(P, O, horizons),
        "croston_sba": croston(P, O, horizons, variant="sba"),
        "tsb": tsb(P, O, horizons),
        "adida": adida(P, O, horizons),
    }
