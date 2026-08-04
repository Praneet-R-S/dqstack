"""Stage 3.5 — the foundation-model slot.

Honest statement of what this is, because the thesis depends on not overclaiming.

The specification calls for Chronos-2 / TimesFM / Moirai here, and the argument
for them is specific: a series with six weeks of history gives a global gradient
booster almost nothing to learn from, and a pretrained model can produce a
credible forecast with no series-specific fitting at all. That is a real
capability gap, and it is the honest reason to include the family.

Those checkpoints are multi-gigabyte downloads and are not available in this
environment. Rather than stub the slot out or fake a result, it is filled by a
model that satisfies the same contract — *forecasts a series it has never seen,
using no lagged history of that series* — implemented as a covariate-only global
LightGBM:

    permitted features    calendar, Fourier terms, price and price ratios,
                          hierarchy identifiers, planning class, horizon
    forbidden features    every lag, every rolling statistic, every
                          series-specific encoding

Because no feature references the target series' own past, the model transfers to
an unseen series with zero shots. It is trained across all series, so what it has
learned is the cross-sectional structure of demand — which is a crude version of
what pretraining buys you.

`ZeroShotPrior` is API-compatible with a Chronos-2 call: swap the `predict` body
for a batched pipeline call and the rest of the pipeline is unchanged. What must
*not* be claimed in the thesis is that this measures foundation-model
performance. It measures the value of the slot, not the value of Chronos.

The spec's warning about the family applies to this stand-in too and is worth
verifying either way: strong point accuracy paired with prediction intervals that
miss nominal coverage, especially in the tails. Stage 6 exists to fix that, and
the coverage table before and after conformal adjustment is where it shows up.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import lightgbm as lgb
import numpy as np
import pandas as pd

from ..config import HORIZON_GROUPS, LGBM_TWEEDIE
from ..features import CAT_FEATURES, Panel, make_design

# Any feature whose name starts with one of these reads the series' own history
# and is therefore inadmissible for a zero-shot model.
HISTORY_PREFIXES = (
    "ylag_", "rmean_", "rstd_", "rmed_", "rmin_", "rmax_", "rzero_", "rskew_",
    "wmean_", "days_since_", "enc_", "dept_store_agg", "store_agg",
    "dept_store_share", "store_share", "carried_rate",
)
# Diagnostic scalars are series-specific summaries; excluded for the same reason.
HISTORY_EXACT = {
    "adi", "cv2", "cv", "zero_share", "mean_demand", "acf7", "acf364",
    "seasonal_strength", "trend_strength", "spectral_entropy", "age_weeks",
    "weeks_since_break", "censored_rate", "ooa_rate", "health_score",
    "price", "price_ratio_28", "price_chg_wk", "price_pct_wk", "discount_flag",
    "price_volatility",
}


def admissible(features: list[str]) -> list[str]:
    return [
        f for f in features
        if not f.startswith(HISTORY_PREFIXES) and f not in HISTORY_EXACT
    ]


@dataclass
class ZeroShotPrior:
    models: dict[str, lgb.Booster] = field(default_factory=dict)
    features: dict[str, list[str]] = field(default_factory=dict)
    categories: dict[str, pd.CategoricalDtype] = field(default_factory=dict)

    def _align(self, df: pd.DataFrame, group: str) -> pd.DataFrame:
        out = df.copy()
        for c, dtype in self.categories.items():
            if c in out.columns:
                out[c] = out[c].astype(str).astype(dtype)
        return out[self.features[group]]

    def fit(self, P: Panel, train_origins: list[int], rounds: int = 260) -> "ZeroShotPrior":
        from ..features import feature_columns

        for group, (lo, hi) in HORIZON_GROUPS.items():
            hs = tuple(range(lo, hi + 1))
            d = make_design(P, train_origins, hs)
            d = d[np.isfinite(d["y"])].reset_index(drop=True)
            feats = admissible(feature_columns(d))
            self.features[group] = feats
            for c in CAT_FEATURES:
                if c in feats:
                    self.categories[c] = pd.CategoricalDtype(sorted(set(d[c].astype(str))))
            X = self._align(d, group)
            cats = [c for c in CAT_FEATURES if c in feats]
            params = dict(LGBM_TWEEDIE, num_leaves=96, learning_rate=0.06)
            self.models[group] = lgb.train(
                params,
                lgb.Dataset(X, label=d["y"].to_numpy(), categorical_feature=cats),
                num_boost_round=rounds,
            )
        return self

    def predict(self, P: Panel, O: int, horizons: tuple[int, ...] | range) -> np.ndarray:
        S, H = P.n_series, len(list(horizons))
        out = np.full((S, H), np.nan, dtype="float32")
        sid_order = {s: i for i, s in enumerate(P.series["series_id"].to_numpy())}
        hpos = {h: j for j, h in enumerate(horizons)}
        for group, (lo, hi) in HORIZON_GROUPS.items():
            hs = tuple(h for h in horizons if lo <= h <= hi)
            if not hs or group not in self.models:
                continue
            d = make_design(P, [O], hs, with_target=False)
            pred = np.maximum(self.models[group].predict(self._align(d, group)), 0.0)
            out[d["series_id"].map(sid_order).to_numpy(), d["horizon"].map(hpos).to_numpy()] = pred
        return np.nan_to_num(out, nan=0.0)
