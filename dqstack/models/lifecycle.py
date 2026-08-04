"""Stage 3.6 — cohort lifecycle curves, for items under eight weeks old.

This is the one place where a simple bespoke method should beat both gradient
boosting and a zero-shot foundation model, because it encodes domain structure
neither has access to: new items follow a launch curve, and the shape of that
curve is a property of the *category and price band*, not of the individual item.

Method. Take every series with a known launch and at least 12 weeks of history.
Normalise its first 84 days by its own week-2-to-3 mean velocity, giving a shape
curve invariant to scale. Cluster those shapes by (department, price band) into a
cohort library. For a cold-start series, match it to a cohort on attributes and
scale the cohort's curve by whatever early velocity has been observed so far.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..features import Panel

CURVE_DAYS = 84
ANCHOR = slice(7, 21)          # week 2-3: past the launch spike, before maturity
PRICE_BANDS = (0.0, 2.0, 5.0, 12.0, np.inf)


@dataclass
class CohortCurves:
    curves: dict[str, np.ndarray] = field(default_factory=dict)
    fallback: np.ndarray | None = None
    weekday_profile: dict[str, np.ndarray] = field(default_factory=dict)

    @staticmethod
    def _key(dept: str, price: float) -> str:
        band = int(np.searchsorted(PRICE_BANDS, price, side="right") - 1)
        return f"{dept}|b{max(band, 0)}"

    def fit(self, P: Panel, max_origin: int) -> "CohortCurves":
        y = P.y[:, : max_origin + 1]
        carried = P.carried[:, : max_origin + 1]
        S, T = y.shape
        dept = P.series["dept_id"].astype(str).to_numpy()
        price = P.series["avg_price"].to_numpy(dtype="float64")

        buckets: dict[str, list[np.ndarray]] = {}
        for i in range(S):
            live = np.flatnonzero(carried[i])
            if len(live) == 0:
                continue
            f = int(live[0])
            if f == 0 or f + CURVE_DAYS > T:
                continue                       # need an observed launch and a full curve
            seg = y[i, f : f + CURVE_DAYS].astype("float64")
            if np.isnan(seg).mean() > 0.35:
                continue
            seg = np.nan_to_num(seg, nan=np.nanmean(seg))
            anchor = seg[ANCHOR].mean()
            if anchor < 0.25:
                continue
            buckets.setdefault(self._key(dept[i], price[i]), []).append(seg / anchor)

        for k, v in buckets.items():
            if len(v) >= 4:
                self.curves[k] = np.median(np.stack(v), axis=0).astype("float32")
        if buckets:
            allc = [c for v in buckets.values() for c in v]
            self.fallback = np.median(np.stack(allc), axis=0).astype("float32")
        else:
            self.fallback = np.ones(CURVE_DAYS, dtype="float32")

        # Weekday shape per category, so a launch curve does not flatten the
        # weekend peak that dominates grocery.
        wd = P.calendar["dayofweek"].to_numpy()[: max_origin + 1]
        for cat in np.unique(P.series["cat_id"].astype(str)):
            m = P.series["cat_id"].astype(str).to_numpy() == cat
            prof = np.array([np.nanmean(y[m][:, wd == d]) for d in range(7)])
            prof = prof / max(np.nanmean(prof), 1e-9)
            self.weekday_profile[cat] = np.nan_to_num(prof, nan=1.0)
        return self

    def predict(self, P: Panel, O: int, horizons: np.ndarray) -> np.ndarray:
        S = P.n_series
        y = P.y[:, : O + 1]
        carried = P.carried[:, : O + 1]
        dept = P.series["dept_id"].astype(str).to_numpy()
        cat = P.series["cat_id"].astype(str).to_numpy()
        price = P.series["avg_price"].to_numpy(dtype="float64")
        wd_future = P.calendar["dayofweek"].to_numpy()

        out = np.zeros((S, len(horizons)), dtype="float64")
        for i in range(S):
            live = np.flatnonzero(carried[i])
            if len(live) == 0:
                continue
            f = int(live[0])
            age = O - f + 1

            obs = y[i, f : O + 1].astype("float64")
            obs = obs[~np.isnan(obs)]
            if len(obs) == 0:
                continue
            recent = float(np.mean(obs[-min(len(obs), 21):]))
            prof = self.weekday_profile.get(cat[i], np.ones(7))

            if age >= CURVE_DAYS:
                # Past the launch window the curve has no information left, and
                # extrapolating it is what made this candidate produce absurd
                # values. A mature series gets its observed level with the
                # category weekday shape — an honest abstention rather than a
                # confident guess, which is what lets the meta-learner give this
                # candidate near-zero weight on mature series without the
                # combination being poisoned by the magnitude.
                for j, h in enumerate(horizons):
                    out[i, j] = max(recent * prof[wd_future[min(O + h, len(wd_future) - 1)]], 0.0)
                continue

            curve = self.curves.get(self._key(dept[i], price[i]), self.fallback)
            # Scale on the stretch of the curve the series has actually lived
            # through, not the whole curve.
            k = int(np.clip(age, 1, CURVE_DAYS))
            ref = float(np.mean(curve[max(0, k - 21):k]))
            scale = recent / max(ref, 0.05)

            for j, h in enumerate(horizons):
                pos = age - 1 + h
                base = float(curve[pos]) if pos < CURVE_DAYS else float(curve[-1])
                out[i, j] = max(scale * base * prof[wd_future[min(O + h, len(wd_future) - 1)]], 0.0)

        # Final guard: a launch curve should never predict many times the
        # velocity already observed. Ratios beyond this are curve-library
        # artefacts, not demand signal.
        hist_max = np.nanmax(np.where(np.isfinite(y), y, 0.0), axis=1)
        cap = np.maximum(4.0 * hist_max, 6.0)[:, None]
        return np.minimum(out, cap)
