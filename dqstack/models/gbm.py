"""Stage 3.2 / 3.3 — LightGBM, the workhorse.

Two heads over the same design matrix:

  Tweedie point model   objective=tweedie, variance power 1.15. Retail unit
                        sales are zero-inflated non-negative counts, which is
                        precisely the family Tweedie deviance is built for. This
                        is what won M5, and it beats an L2 objective by a wide
                        margin on sparse data. RMSE here is a mistake, and MAPE
                        is undefined on a series that is 40% zeros.

  Quantile heads        objective=quantile, one model per level. Independently
                        trained heads cross, so the output is clipped at zero and
                        then sorted (see dist.sanitize).

Two structural choices, both from the spec:

  Grouped direct multi-horizon. Separate models per horizon group (1-7, 8-14,
  15-28) with horizon as a feature inside each group. Direct estimation avoids
  the error compounding of recursive iteration over 28 days; grouping avoids
  training 28 separate models for marginal benefit.

  Category segmentation on the point model. A single global model across all
  categories underperforms because category-specific weekday seasonality is
  strong. Quantile heads stay global: they are estimating spread, which pools
  across categories far better than level does, and 9 levels x 3 groups x 3
  categories would triple training time for no measured gain.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import lightgbm as lgb
import numpy as np
import pandas as pd

from ..config import (
    HORIZON_GROUPS,
    LGBM_EARLY_STOP,
    LGBM_QUANTILE,
    LGBM_ROUNDS,
    LGBM_TWEEDIE,
    QUANTILES,
    Q_LABELS,
)
from ..features import Panel, CAT_FEATURES, feature_columns, make_design
from .dist import sanitize


@dataclass
class GBMPool:
    """Trained LightGBM ensemble, keyed by horizon group."""

    point: dict[tuple[str, str], lgb.Booster] = field(default_factory=dict)
    quant: dict[tuple[str, float], lgb.Booster] = field(default_factory=dict)
    features: dict[str, list[str]] = field(default_factory=dict)
    categories: dict[tuple[str, str], pd.CategoricalDtype] = field(default_factory=dict)
    importance: dict[str, pd.DataFrame] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)
    segment_by: str = "cat_id"

    # -------------------------------------------------------------- helpers

    def _align(self, df: pd.DataFrame, group: str) -> pd.DataFrame:
        """Reindex categorical levels to those seen at fit time, per group.

        Without this, a prediction batch containing a subset of categories gets
        different integer codes than training used, and LightGBM silently
        mispredicts. Keyed by (group, column) rather than column alone: the
        `horizon_group` feature takes a different single value in each group, so a
        column-only key left every group holding the last one fitted, and at
        predict time that feature became NaN for two of the three groups after
        having been a valid category during training.
        """
        out = df.copy()
        for (g, c), dtype in self.categories.items():
            if g == group and c in out.columns:
                out[c] = out[c].astype(str).astype(dtype)
        return out[self.features[group]]

    # -------------------------------------------------------------- fit

    def fit(
        self,
        P: Panel,
        train_origins: list[int],
        valid_origins: list[int],
        verbose: bool = True,
        with_quantiles: bool = True,
        respect_regimes: bool = True,
    ) -> "GBMPool":
        for group, (lo, hi) in HORIZON_GROUPS.items():
            hs = tuple(range(lo, hi + 1))
            t0 = time.perf_counter()
            tr = make_design(P, train_origins, hs, respect_regimes=respect_regimes)
            va = make_design(P, valid_origins, hs, respect_regimes=respect_regimes)
            tr = tr[np.isfinite(tr["y"])].reset_index(drop=True)
            va = va[np.isfinite(va["y"])].reset_index(drop=True)

            feats = feature_columns(tr)
            self.features[group] = feats
            for c in CAT_FEATURES:
                if c in tr.columns:
                    dtype = pd.CategoricalDtype(
                        sorted(set(tr[c].astype(str)) | set(va[c].astype(str)))
                    )
                    self.categories[(group, c)] = dtype
            tr_x, va_x = self._align(tr, group), self._align(va, group)
            cats = [c for c in CAT_FEATURES if c in feats]

            if verbose:
                print(f"  [{group}] design {len(tr):,} train / {len(va):,} valid rows, "
                      f"{len(feats)} features", flush=True)

            # ---- Tweedie point model, segmented by category
            segs = sorted(tr[self.segment_by].astype(str).unique())
            for seg in segs:
                m_tr = tr[self.segment_by].astype(str).to_numpy() == seg
                m_va = va[self.segment_by].astype(str).to_numpy() == seg
                if m_tr.sum() < 5_000:
                    continue
                dtr = lgb.Dataset(tr_x[m_tr], label=tr["y"].to_numpy()[m_tr],
                                  categorical_feature=cats, free_raw_data=False)
                dva = lgb.Dataset(va_x[m_va], label=va["y"].to_numpy()[m_va],
                                  categorical_feature=cats, reference=dtr, free_raw_data=False)
                bst = lgb.train(
                    LGBM_TWEEDIE, dtr, num_boost_round=LGBM_ROUNDS, valid_sets=[dva],
                    callbacks=[lgb.early_stopping(LGBM_EARLY_STOP, verbose=False)],
                )
                self.point[(group, seg)] = bst
                if verbose:
                    print(f"      tweedie/{seg:<10s} {bst.best_iteration:>4} trees  "
                          f"valid {bst.best_score['valid_0']['tweedie']:.4f}", flush=True)

            imp = None
            # ---- quantile heads, global
            for q in (QUANTILES if with_quantiles else ()):
                params = dict(LGBM_QUANTILE, alpha=q)
                dtr = lgb.Dataset(tr_x, label=tr["y"].to_numpy(),
                                  categorical_feature=cats, free_raw_data=False)
                dva = lgb.Dataset(va_x, label=va["y"].to_numpy(),
                                  categorical_feature=cats, reference=dtr, free_raw_data=False)
                bst = lgb.train(
                    params, dtr, num_boost_round=LGBM_ROUNDS, valid_sets=[dva],
                    callbacks=[lgb.early_stopping(LGBM_EARLY_STOP, verbose=False)],
                )
                self.quant[(group, q)] = bst
                if abs(q - 0.5) < 1e-9:
                    imp = pd.DataFrame(
                        {
                            "feature": bst.feature_name(),
                            "gain": bst.feature_importance("gain"),
                            "split": bst.feature_importance("split"),
                        }
                    ).sort_values("gain", ascending=False)
            if imp is not None:
                self.importance[group] = imp

            self.timings[group] = time.perf_counter() - t0
            if verbose:
                print(f"      quantile heads x{len(QUANTILES)} done   "
                      f"({self.timings[group]:.0f}s for group)", flush=True)
        return self

    # -------------------------------------------------------------- predict

    def predict(
        self, P: Panel, origin: int, horizons: tuple[int, ...] | range
    ) -> tuple[np.ndarray, np.ndarray, pd.DataFrame]:
        """Returns (point (S,H), quantiles (S,H,Q), design rows for attribution)."""
        S, H = P.n_series, len(list(horizons))
        point = np.full((S, H), np.nan, dtype="float32")
        quant = np.full((S, H, len(QUANTILES)), np.nan, dtype="float32")
        keep_rows = []

        sid_order = {s: i for i, s in enumerate(P.series["series_id"].to_numpy())}
        hpos = {h: j for j, h in enumerate(horizons)}

        for group, (lo, hi) in HORIZON_GROUPS.items():
            hs = tuple(h for h in horizons if lo <= h <= hi)
            if not hs or group not in self.features:
                continue
            d = make_design(P, [origin], hs, with_target=False)
            X = self._align(d, group)
            ri = d["series_id"].map(sid_order).to_numpy()
            ci = d["horizon"].map(hpos).to_numpy()

            seg_vals = d[self.segment_by].astype(str).to_numpy()
            for seg in np.unique(seg_vals):
                bst = self.point.get((group, seg))
                m = seg_vals == seg
                if bst is None:
                    # Unseen segment. Fall back to the model trained on the most
                    # rows in this group — genuinely the largest, rather than
                    # whichever happened to be inserted first, which is what the
                    # previous comment claimed and the code did not do.
                    fallbacks = [k for k in self.point if k[0] == group]
                    if not fallbacks:
                        continue
                    bst = self.point[max(fallbacks, key=lambda k: self.point[k].num_trees())]
                point[ri[m], ci[m]] = np.maximum(bst.predict(X[m]), 0.0)

            for k, q in enumerate(QUANTILES):
                bst = self.quant.get((group, q))
                if bst is None:
                    continue
                quant[ri, ci, k] = bst.predict(X)
            keep_rows.append(d)

        return point, sanitize(quant), pd.concat(keep_rows, ignore_index=True)

    # -------------------------------------------------------------- attribution

    def contributions(
        self, P: Panel, origin: int, series_ids: list[str], horizon: int = 7, top: int = 8
    ) -> dict[str, list[dict]]:
        """Exact TreeSHAP contributions for the explanation panel.

        LightGBM's `pred_contrib` gives per-feature additive contributions, which
        is what the "Why" line in the planner workspace needs. Feature importance
        would only say what matters on average; this says what moved *this* number.
        """
        group = next(g for g, (lo, hi) in HORIZON_GROUPS.items() if lo <= horizon <= hi)
        if group not in self.features:
            return {}
        d = make_design(P, [origin], (horizon,), with_target=False)
        d = d[d["series_id"].isin(series_ids)].reset_index(drop=True)
        if d.empty:
            return {}
        X = self._align(d, group)

        out: dict[str, list[dict]] = {}
        seg_vals = d[self.segment_by].astype(str).to_numpy()
        names = self.features[group]
        for seg in np.unique(seg_vals):
            bst = self.point.get((group, seg))
            if bst is None:
                continue
            m = seg_vals == seg
            contrib = bst.predict(X[m], pred_contrib=True)
            sids = d["series_id"].to_numpy()[m]
            for i, sid in enumerate(sids):
                row = contrib[i][:-1]
                base = float(contrib[i][-1])
                order = np.argsort(-np.abs(row))[:top]
                out[str(sid)] = [
                    {"feature": names[j], "contribution": float(row[j]),
                     "value": (None if pd.isna(X[names[j]].to_numpy()[m][i])
                               else _as_py(X[names[j]].to_numpy()[m][i]))}
                    for j in order
                ] + [{"feature": "__base__", "contribution": base, "value": None}]
        return out


def _as_py(v):
    if isinstance(v, (np.floating, np.integer)):
        return float(v)
    return str(v)
