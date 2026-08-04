"""Predictive distributions — the interface contract for Stages 3 through 7.

Nothing downstream of Stage 3 is permitted to see a point forecast. Some
candidate models (LightGBM quantile heads) emit quantiles natively; the rest
emit a conditional mean, and this module lifts that mean into a distribution.

The lift is a Negative Binomial, not a Gaussian. Retail unit demand is a
zero-inflated non-negative count: a Gaussian assigns mass below zero, gives
symmetric intervals around a right-skewed quantity, and produces a nonsense
P10 for a series whose mean is 0.4 units/day. The NB variance function
Var = mu + mu^2/r reproduces the observed overdispersion, collapses correctly to
all-zero quantiles when mu -> 0, and is the count analogue of the Tweedie
objective used for the point model.

Dispersion `r` is fitted per (planning_class, horizon_group) by matching the
empirical mean-variance relationship on held-out origins, because a smooth
high-volume series and a lumpy sparse one have genuinely different spread at
the same predicted level.
"""

from __future__ import annotations

import numpy as np
from scipy import stats

from ..config import QUANTILES

MIN_R, MAX_R = 0.05, 60.0


def fit_dispersion(actual: np.ndarray, mu: np.ndarray, strata: np.ndarray) -> dict[str, float]:
    """Per-stratum NB dispersion by moment matching on prediction residuals.

    Bins predictions by level, measures the conditional variance of actuals in
    each bin, and regresses (Var - mu) on mu^2 through the origin. That is a
    direct estimate of 1/r under the NB variance function.
    """
    out: dict[str, float] = {}
    for s in np.unique(strata):
        m = (strata == s) & np.isfinite(actual) & np.isfinite(mu)
        if m.sum() < 200:
            continue
        a, p = actual[m].astype("float64"), np.clip(mu[m].astype("float64"), 1e-6, None)
        # Bin on predicted level so each bin has a near-constant mean.
        edges = np.unique(np.quantile(p, np.linspace(0, 1, 21)))
        if len(edges) < 4:
            continue
        idx = np.clip(np.searchsorted(edges, p, side="right") - 1, 0, len(edges) - 2)
        mus, vs = [], []
        for b in range(len(edges) - 1):
            sel = idx == b
            if sel.sum() < 30:
                continue
            mus.append(p[sel].mean())
            vs.append(a[sel].var())
        if len(mus) < 3:
            continue
        mus_a, vs_a = np.asarray(mus), np.asarray(vs)
        excess = vs_a - mus_a
        denom = float((mus_a**2 * mus_a**2).sum())
        inv_r = float((excess * mus_a**2).sum() / denom) if denom > 0 else 0.0
        out[str(s)] = float(np.clip(1.0 / inv_r, MIN_R, MAX_R)) if inv_r > 1e-9 else MAX_R
    return out


def nb_quantiles(
    mu: np.ndarray, r: np.ndarray | float, quantiles: tuple[float, ...] = QUANTILES
) -> np.ndarray:
    """Negative-Binomial quantiles for each element of `mu`.

    `mu` may be any shape; the result gains a trailing quantile axis.
    """
    mu = np.asarray(mu, dtype="float64")
    mu_s = np.clip(mu, 1e-9, None)
    r_arr = np.broadcast_to(np.asarray(r, dtype="float64"), mu.shape)
    r_arr = np.clip(r_arr, MIN_R, MAX_R)
    p = r_arr / (r_arr + mu_s)

    qs = np.asarray(quantiles, dtype="float64")
    out = np.empty(mu.shape + (len(qs),), dtype="float32")
    for k, q in enumerate(qs):
        out[..., k] = stats.nbinom.ppf(q, r_arr, p)
    # A series predicted at effectively zero has an all-zero distribution.
    out[mu <= 1e-8] = 0.0
    return np.nan_to_num(out, nan=0.0, posinf=0.0)


def sanitize(q: np.ndarray) -> np.ndarray:
    """Floor at zero, then enforce monotonicity across the quantile axis.

    Order matters: clip before sorting. Independently trained quantile heads
    cross, and clipping a crossed set after sorting can reintroduce a crossing.
    Crude, and correct (spec §3.3).
    """
    q = np.nan_to_num(np.asarray(q, dtype="float32"), nan=0.0, posinf=0.0, neginf=0.0)
    q = np.maximum(q, 0.0)
    return np.sort(q, axis=-1)


def median_of(q: np.ndarray, quantiles: tuple[float, ...] = QUANTILES) -> np.ndarray:
    """Extract the P50 slice from a quantile array."""
    return q[..., int(np.argmin(np.abs(np.asarray(quantiles) - 0.5)))]


def strata_key(planning_class: np.ndarray, horizon_group: np.ndarray) -> np.ndarray:
    return np.char.add(np.char.add(planning_class.astype("U20"), "|"), horizon_group.astype("U12"))
