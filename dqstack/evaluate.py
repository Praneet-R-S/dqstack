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
    w = weights[m] / weights[m].sum()
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
