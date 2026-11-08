"""Evaluation — three metric tiers, plus the two results the thesis turns on.

Tier 1  point accuracy         WRMSSE, MASE, bias
Tier 2  distributional quality  WSPL (scaled pinball), empirical coverage, PIT
Tier 3  realised economics      simulator margin, fill rate, lost margin, waste

The two findings that matter are comparisons *between* tiers, not values inside
them:

1. **Forecast Value Added per stage.** Each pipeline stage is scored against the
   seasonal-naive anchor. A stage with negative FVA is destroying value and
   should be disabled — which is a result worth reporting, not a bug to hide.

2. **Rank inversion between accuracy and economics.** If the method that wins on
   WRMSSE is not the method that wins on realised margin, then promoting on
   accuracy is promoting the wrong thing, and the whole argument for an
   economics-gated pipeline follows. Finding and explaining at least one concrete
   inversion is a definition-of-done item.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats

from .combine import pinball
from .config import QUANTILES


# ------------------------------------------------------------------ tier 1

def scale_denominator(y_train: np.ndarray, period: int = 1) -> np.ndarray:
    """Mean squared seasonal difference of the training tail — the M5 scaler.

    Computed per series on the training window only. Series whose scaler is zero
    (never sold, or a single flat value) are excluded rather than given an
    infinite score.
    """
    # The scaler must be measured on the same footing as the numerator. Replacing
    # masked history with zeros inflates the seasonal difference — a censored day
    # reads as a crash to zero and a recovery — which deflates RMSSE for precisely
    # the most-censored series while the numerator excludes those days entirely.
    y_train = np.asarray(y_train, dtype="float64")
    d = np.diff(y_train, n=1, axis=1) if period == 1 else y_train[:, period:] - y_train[:, :-period]
    with np.errstate(invalid="ignore"):
        return np.nanmean(d**2, axis=1)


def rmsse(y: np.ndarray, yhat: np.ndarray, denom: np.ndarray,
          valid: np.ndarray | None = None) -> np.ndarray:
    err = (y - yhat) ** 2
    if valid is not None:
        err = np.where(valid, err, np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.sqrt(np.nanmean(err, axis=1) / np.where(denom > 0, denom, np.nan))


def wrmsse(y, yhat, denom, weights, valid=None) -> float:
    r = rmsse(y, yhat, denom, valid)
    m = np.isfinite(r) & np.isfinite(weights)
    if not m.any():
        return float("nan")
    total = float(weights[m].sum())
    if total <= 0:
        # A subgroup whose series all had zero revenue in the weighting window —
        # a whole department out of assortment, say. Dividing by zero would return
        # nan or inf and quietly poison an aggregate; an unweighted mean is the
        # meaningful answer and is what a reader would assume.
        return float(np.nanmean(r[m]))
    w = weights[m] / total
    return float((w * r[m]).sum())


def mase(y, yhat, denom_abs, valid=None) -> np.ndarray:
    err = np.abs(y - yhat)
    if valid is not None:
        err = np.where(valid, err, np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.nanmean(err, axis=1) / np.where(denom_abs > 0, denom_abs, np.nan)


def bias(y, yhat, valid=None) -> float:
    d = yhat - y
    if valid is not None:
        d = np.where(valid, d, np.nan)
    return float(np.nansum(d) / max(np.nansum(np.where(valid, y, np.nan) if valid is not None else y), 1e-9))


def mean_from_quantiles(q: np.ndarray, quantiles=QUANTILES) -> np.ndarray:
    """Expected value implied by a stored quantile ladder, E[Y] = integral of Q(u).

    Exists because `bias` was only ever scored on the median, and on demand that is
    roughly 43% zeros and right-skewed the median sits below the mean by
    construction. Every model therefore reported a negative bias of 15-30% against
    actual totals, which reads as systematic under-forecasting when it is the median
    behaving exactly as a median should. Whether a forecaster under-forecasts is a
    question about its *mean*, so this provides one.

    Trapezoidal between stored levels. The lower tail runs linearly from Q=0 at u=0
    (demand cannot be negative); the upper tail is held flat at the top level, which
    under-states a heavy tail slightly, so a small negative residual here is the
    integration, not the model.
    """
    taus = np.asarray(quantiles, dtype="float64")
    q = np.asarray(q, dtype="float64")
    out = 0.5 * q[..., 0] * taus[0]                       # [0, tau_1]
    for k in range(len(taus) - 1):                        # interior trapezoids
        out = out + 0.5 * (q[..., k] + q[..., k + 1]) * (taus[k + 1] - taus[k])
    out = out + q[..., -1] * (1.0 - taus[-1])             # [tau_K, 1]
    return out


# ------------------------------------------------------------------ tier 2

def wspl(y: np.ndarray, q: np.ndarray, denom: np.ndarray, weights: np.ndarray,
         valid: np.ndarray | None = None) -> float:
    """Weighted scaled pinball loss — the distributional analogue of WRMSSE."""
    pl = pinball(y, q)                                  # (S, H)
    if valid is not None:
        pl = np.where(valid, pl, np.nan)
    scale = np.sqrt(np.where(denom > 0, denom, np.nan))
    per = np.nanmean(pl, axis=1) / scale
    m = np.isfinite(per) & np.isfinite(weights)
    if not m.any():
        return float("nan")
    w = weights[m] / weights[m].sum()
    return float((w * per[m]).sum())


def coverage(y: np.ndarray, q: np.ndarray, valid: np.ndarray | None = None,
             quantiles=QUANTILES) -> pd.DataFrame:
    m = np.isfinite(y) if valid is None else (np.isfinite(y) & valid)
    rows = []
    for k, tau in enumerate(quantiles):
        rows.append({"nominal": tau, "empirical": float((y[m] <= q[..., k][m]).mean())})
    df = pd.DataFrame(rows)
    df["gap"] = df["empirical"] - df["nominal"]
    return df


# ------------------------------------------------------------------ hierarchy

def hierarchical_accuracy(
    y: np.ndarray,
    forecasts: dict[str, np.ndarray],
    H,
    valid: np.ndarray | None = None,
) -> pd.DataFrame:
    """WRMSSE at every aggregation level, for each supplied forecast.

    Reconciliation cannot be judged at the bottom level alone, and judging it there
    is how a stage that is working gets recorded as a stage that is broken. MinT
    trades a little leaf-level accuracy for coherence and for accuracy at the
    aggregates a planning organisation actually reviews — the specification says so
    explicitly — so measuring only item-store WRMSSE tests the one level where it is
    expected to lose and none of the levels where it is expected to win.

    Aggregation is applied to actuals and forecasts identically. Masked target days
    are excluded by zeroing both sides, which keeps an aggregate from being
    penalised for a component that had no demand signal that day.
    """
    rows = []
    v = np.ones_like(y, dtype=bool) if valid is None else valid
    y_m = np.where(v, np.nan_to_num(y), 0.0)

    for level in sorted(set(H.node_level)):
        idx = [i for i, l in enumerate(H.node_level) if l == level]
        S_l = H.S[idx]                                   # (n_nodes_at_level, n_bottom)
        y_agg = S_l @ y_m                                # (n_nodes, H)
        # Scaler and weights at this level, from the aggregated actuals themselves.
        d = np.diff(y_agg, axis=1)
        denom = np.nanmean(d**2, axis=1)
        w = y_agg.sum(axis=1)
        w = w / max(w.sum(), 1e-9)

        for name, f in forecasts.items():
            f_agg = S_l @ np.where(v, np.nan_to_num(f), 0.0)
            r = rmsse(y_agg, f_agg, denom)
            m = np.isfinite(r) & np.isfinite(w)
            score = float((w[m] / max(w[m].sum(), 1e-9) * r[m]).sum()) if m.any() else np.nan
            rows.append({
                "level": level,
                "n_nodes": len(idx),
                "method": name,
                "wrmsse": score,
            })
    return pd.DataFrame(rows)


def coherence_gap(forecast: np.ndarray, H) -> float:
    """Largest relative disagreement between a level and the sum of its components.

    Zero after reconciliation by construction; the interesting figure is what it was
    *before*, which is the size of the problem the stage solved.
    """
    bottom = forecast
    implied = H.S @ bottom
    out = 0.0
    for level in set(H.node_level):
        idx = [i for i, l in enumerate(H.node_level) if l == level]
        agg = implied[idx]
        out = max(out, float(np.abs(agg - H.S[idx] @ bottom).max()))
    return out


# ------------------------------------------------------------------ FVA

def forecast_value_added(
    y: np.ndarray,
    stage_forecasts: dict[str, np.ndarray],
    anchor: str,
    denom: np.ndarray,
    weights: np.ndarray,
    valid: np.ndarray | None = None,
) -> pd.DataFrame:
    """FVA ladder: each stage's error reduction against the naive anchor.

    Reported as a ladder rather than a single number so a *negative* step is
    visible. A stage that improves on the anchor but degrades its own predecessor
    is destroying value at the margin even though its FVA is positive, and only
    the ladder shows that.
    """
    base = wrmsse(y, stage_forecasts[anchor], denom, weights, valid)
    rows, prev = [], None
    for name, f in stage_forecasts.items():
        w = wrmsse(y, f, denom, weights, valid)
        rows.append(
            {
                "stage": name,
                "wrmsse": w,
                "fva_vs_anchor": (base - w) / base if base else np.nan,
                "delta_vs_previous": np.nan if prev is None else (prev - w) / prev,
            }
        )
        prev = w
    return pd.DataFrame(rows)


def fva_by_class(
    y: np.ndarray, forecast: np.ndarray, anchor_forecast: np.ndarray,
    planning_class: np.ndarray, denom: np.ndarray, weights: np.ndarray,
    valid: np.ndarray | None = None,
) -> pd.DataFrame:
    rows = []
    for c in np.unique(planning_class):
        m = planning_class == c
        b = wrmsse(y[m], anchor_forecast[m], denom[m], weights[m], None if valid is None else valid[m])
        s = wrmsse(y[m], forecast[m], denom[m], weights[m], None if valid is None else valid[m])
        rows.append({"planning_class": c, "n_series": int(m.sum()),
                     "anchor_wrmsse": b, "system_wrmsse": s,
                     "fva": (b - s) / b if b else np.nan})
    return pd.DataFrame(rows).sort_values("fva", ascending=False)


# ------------------------------------------------------------------ tier 3 linkage

def accuracy_vs_economics(table: pd.DataFrame) -> dict:
    """Spearman rank correlation between an accuracy ranking and an economics
    ranking of the same candidates, plus every concrete inversion found.

    `table` needs columns: method, wrmsse (lower better), realized_margin
    (higher better).
    """
    t = table.dropna(subset=["wrmsse", "realized_margin"]).copy()
    # Several arms share one forecast and differ only in the ordering policy built on
    # it -- the current setup, "no reconciliation", "blanket 95% service". Their
    # WRMSSE is equal in fact and unequal only in float noise at the twelfth decimal,
    # and that noise decided which of them ranked "most accurate". Rho swung from
    # 0.53 to 0.38 between two otherwise identical runs, and the inversion list
    # reported arms as more accurate than themselves. Six decimals is far below any
    # accuracy difference worth acting on and far above the noise, so equal forecasts
    # now tie, as they should.
    t["wrmsse"] = t["wrmsse"].round(6)
    t["rank_accuracy"] = t["wrmsse"].rank(ascending=True)
    t["rank_economics"] = t["realized_margin"].rank(ascending=False)
    rho, p = stats.spearmanr(t["rank_accuracy"], t["rank_economics"])

    inversions = []
    rows = t.to_dict("records")
    for i in range(len(rows)):
        for j in range(len(rows)):
            a, b = rows[i], rows[j]
            if a["wrmsse"] < b["wrmsse"] and a["realized_margin"] < b["realized_margin"]:
                inversions.append(
                    {
                        "more_accurate": a["method"],
                        "more_profitable": b["method"],
                        "wrmsse_gap": float(b["wrmsse"] - a["wrmsse"]),
                        "margin_gap": float(b["realized_margin"] - a["realized_margin"]),
                    }
                )
    inversions.sort(key=lambda d: -d["margin_gap"])
    return {
        "spearman_rho": float(rho),
        "p_value": float(p),
        "n_methods": int(len(t)),
        "inversions": inversions,
        "table": t.sort_values("wrmsse").to_dict("records"),
    }


# ------------------------------------------------------------------ weights

def m5_weights(panel_tail: pd.DataFrame) -> pd.Series:
    """Dollar-volume weights over the last 28 days, as M5 defines them.

    Weighting by revenue rather than units is what stops the metric being
    dominated by a long tail of one-unit-a-week items that carry no commercial
    consequence.
    """
    rev = panel_tail.assign(
        revenue=panel_tail["sales"] * panel_tail["sell_price"].fillna(0.0)
    ).groupby("series_id", observed=True)["revenue"].sum()
    total = rev.sum()
    return rev / total if total > 0 else rev + 1.0 / max(len(rev), 1)
