"""Stage 6 — conformal calibration.

Model-produced prediction intervals rarely achieve their nominal coverage out of
the box. Split conformal fixes it cheaply and distribution-free.

Why this stage is load-bearing rather than cosmetic: Stage 7 reads a quantile off
this distribution to set an order-up-to level. If the P90 is not really the P90,
the service level is not what the planner thinks it is, and the entire causal
chain from calibration to economics — which is the thesis — breaks. A stage that
looks like a statistical nicety is in fact the joint that carries the load.

Method. For each nominal level tau, find the additive offset delta such that the
adjusted quantile achieves empirical coverage tau on a held-out calibration set:

    delta_tau = Quantile_tau( y - q_tau )   over the calibration set
    q_cal     = q_tau + delta_tau

This is the split-conformal construction applied per level (the CQR variant when
the base quantiles come from a quantile regressor, as they do here). It gives
finite-sample coverage guarantees under exchangeability.

That is the rule for the median and above. Below the median the adjustment is a
*factor*, not an offset, and its target is the strict side of the count CDF:

    c_tau = the k-th smallest  y / q_tau  on the calibration set,  k = floor(tau (n+1))
    q_cal = c_tau * q_tau            so that  P(y < q_cal) <= tau

The bound holds by the same conformal rank argument, and the factor fixes the two
ways an additive floor failed out of sample on count data:

  * scale. One additive offset per stratum is wrong for every series that is not
    the stratum's typical size, so it stopped holding as soon as an origin's mix
    or level moved. A factor follows each series' own level.
  * zeros. Where most outcomes are zero, the honest 5% and 10% levels *are* zero
    (P(y = 0) is above them). The factor finds that (c = 0). The offset instead
    left a positive floor under series that sell nothing most days, and when
    reconciliation shifted those distributions upward at a new origin, that floor
    sat above the zeros.

Stratified by planning class and horizon group, because a single global adjustment
would over-widen smooth series and under-widen lumpy ones — they have genuinely
different conditional spread and pooling them averages away the difference. Below
the median, a stratum too thin to fit falls back to a factor fitted among series
of similar predicted median rather than one pooled factor, because the pool is
dominated by zero-heavy series and its factor is usually zero.

Caveat stated rather than buried: exchangeability is violated under regime change,
which is exactly when correct intervals matter most. Mitigated with a rolling
calibration window and continuous coverage monitoring, not by assuming the
guarantee holds forever.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .config import CFG, QUANTILES
from .models.dist import sanitize

GLOBAL = "__global__"

# Bins of the predicted median for the lower-tail fallback of thin strata.
LEVEL_BINS = 4


def _median_index(quantiles) -> int:
    return int(np.argmin(np.abs(np.asarray(quantiles) - 0.5)))


def _strict_scale(y: np.ndarray, q: np.ndarray, tau: float) -> float | None:
    """Largest factor c with P(y < c q) <= tau, by the conformal rank.

    Scores are y / q; a row with q <= 0 can never fall strictly below c q (demand is
    not negative), so its score is infinite. With k = floor(tau (n+1)), taking the
    k-th smallest score gives at most k - 1 calibration rows strictly below, and for
    an exchangeable new row P(y < c q) <= k / (n+1) <= tau. None when the rank is
    not defined (too few rows, or fewer than k finite scores).
    """
    q = np.maximum(np.asarray(q, dtype="float64"), 0.0)
    n = len(y)
    k = int(np.floor(tau * (n + 1)))
    if k < 1:
        return None
    pos = q > 0
    s = np.full(n, np.inf)
    s[pos] = y[pos] / q[pos]
    c = float(np.partition(s, k - 1)[k - 1])
    return max(c, 0.0) if np.isfinite(c) else None


def _tie_aware_gap(y: np.ndarray, q: np.ndarray, tau: float) -> float:
    """Distance of tau outside [P(y < q), P(y <= q)]; zero when calibrated for counts."""
    return max(0.0, float((y < q).mean()) - tau, tau - float((y <= q).mean()))


def _pinball(y: np.ndarray, q: np.ndarray, tau: float) -> float:
    d = y - q
    return float(np.mean(np.maximum(tau * d, (tau - 1.0) * d)))


@dataclass
class ConformalCalibrator:
    offsets: dict[str, np.ndarray] = field(default_factory=dict)
    n_by_stratum: dict[str, int] = field(default_factory=dict)
    adjusted: dict[str, list[bool]] = field(default_factory=dict)
    quantiles: tuple[float, ...] = QUANTILES
    # Below the median: one factor per level and stratum (1.0 = untouched), and the
    # fallback for thin strata by bin of the predicted median. Calibrators pickled
    # before these existed have none and keep their additive lower offsets.
    scales: dict[str, np.ndarray] = field(default_factory=dict)
    level_edges: np.ndarray | None = None
    level_scales: dict[int, np.ndarray] = field(default_factory=dict)

    # ---------------------------------------------------------------------
    # Three rules that separate this from a naive per-level shift, all of them
    # learned from watching the naive version degrade the system.
    #
    # 1. The median is never shifted. CQR conformalises interval *bounds*; it
    #    does not move the point forecast, and for good reason. On demand that is
    #    43% zeros, P(y <= median) is bounded below by P(y = 0), so the nominal
    #    0.50 target is arithmetically unachievable. A procedure that chases it
    #    shifts every median upward by a unit or more, biasing the point forecast
    #    the planner reads and the metric it is scored on, in exchange for
    #    overshooting an unreachable number.
    #
    # 2. An offset is kept only if it demonstrably helps out of sample. The
    #    calibration set is split in two: offsets are estimated on one half and
    #    validated on the other, and a level keeps its offset only if the
    #    coverage gap actually shrinks on data the offset never saw. This matters
    #    most in the lower tail, where zero-inflation makes the nominal target
    #    unreachable too — there the honest action is to decline to adjust, and
    #    the guard discovers that from the data rather than being told. (Rule 3
    #    measures the lower tail on the strict side, where the target is reachable.)
    #
    # 3. Below the median the target is P(y < q) <= tau, by a factor (see the
    #    module docstring). The additive offset met that bound on the calibration
    #    set (0.050 / 0.100 / 0.251 at 5/10/25%) and went over it at the origins
    #    that followed (10% of outcomes below the stated 5% floor at the full run's
    #    eval origin, up to 23% in the quick run's backtest), because one offset
    #    per stratum cannot follow a series whose level, or whose shift by
    #    reconciliation, differs from the calibration window's. Its guard asks the
    #    tie-aware question: does the factor bring tau inside [P(y < q), P(y <= q)]
    #    on the held-out half, and, where both already bracket it, does it lower
    #    the pinball loss.
    # ---------------------------------------------------------------------

    # -------------------------------------------------------------- fit

    def fit(
        self,
        y: np.ndarray,
        q: np.ndarray,
        planning_class: np.ndarray,
        horizon_group: np.ndarray,
        valid: np.ndarray | None = None,
        cfg=CFG,
    ) -> "ConformalCalibrator":
        """
        `y`               (N,)      calibration actuals
        `q`               (N, Q)    predicted quantiles on the calibration set
        `planning_class`  (N,)
        `horizon_group`   (N,)
        `valid`           (N,)      exclude censored / not-carried targets
        """
        y = np.asarray(y, dtype="float64")
        q = np.asarray(q, dtype="float64")
        keep = np.isfinite(y) & np.isfinite(q).all(axis=1)
        if valid is not None:
            keep &= valid
        y, q = y[keep], q[keep]
        pc, hg = planning_class[keep], horizon_group[keep]

        # Interleaved halves rather than a temporal cut, so both halves span the
        # same origins and the validation measures the offset's generalisation
        # rather than a drift between two periods.
        half = (np.arange(len(y)) % 2) == 0

        self.offsets[GLOBAL], self.adjusted[GLOBAL] = self._fit_offsets(
            y[half], q[half], y[~half], q[~half]
        )
        self.scales[GLOBAL] = self._fit_scales(
            y[half], q[half], y[~half], q[~half], self.adjusted[GLOBAL]
        )
        self.n_by_stratum[GLOBAL] = int(len(y))

        strata = np.char.add(np.char.add(pc.astype("U20"), "|"), hg.astype("U12"))
        for s in np.unique(strata):
            m = strata == s
            n = int(m.sum())
            if n < cfg.calibrate.min_stratum:
                continue          # too thin to estimate reliably; global applies
            hm = half & m
            cm = (~half) & m
            self.offsets[str(s)], self.adjusted[str(s)] = self._fit_offsets(
                y[hm], q[hm], y[cm], q[cm]
            )
            self.scales[str(s)] = self._fit_scales(
                y[hm], q[hm], y[cm], q[cm], self.adjusted[str(s)]
            )
            self.n_by_stratum[str(s)] = n

        # The lower-tail fallback for thin strata, by bin of the predicted median.
        # The pooled factor is set by the zero-heavy series that dominate it (zero
        # at the 5% and 10% levels on the demo data), which would put the floor of
        # every series in a thin stratum at zero, a new product selling dozens a
        # day included.
        med = q[:, _median_index(self.quantiles)]
        self.level_edges = np.unique(
            np.quantile(med, np.linspace(0.0, 1.0, LEVEL_BINS + 1)[1:-1])
        ) if len(med) else None
        self.level_scales = {}
        if self.level_edges is not None:
            b = np.searchsorted(self.level_edges, med, side="right")
            for j in np.unique(b):
                m = b == j
                if int(m.sum()) < cfg.calibrate.min_stratum:
                    continue      # the pooled factor applies to this bin
                self.level_scales[int(j)] = self._fit_scales(
                    y[half & m], q[half & m], y[~half & m], q[~half & m]
                )
        return self

    def _fit_offsets(
        self, y_fit: np.ndarray, q_fit: np.ndarray, y_chk: np.ndarray, q_chk: np.ndarray
    ) -> tuple[np.ndarray, list[bool]]:
        """Per-level conformity offset, validated out of sample.

        The empirical tau-quantile of (y - q_tau) is the shift that makes
        empirical coverage equal tau on the data it was estimated from. Whether
        it also does so on unseen data is a separate question, and this asks it.
        """
        K = len(self.quantiles)
        deltas = np.zeros(K, dtype="float64")
        kept = [False] * K
        if len(y_fit) < 30 or len(y_chk) < 30:
            return deltas, kept

        m = _median_index(self.quantiles)
        for k, tau in enumerate(self.quantiles):
            if abs(tau - 0.5) < 1e-9:
                continue                      # the point forecast is never shifted
            if k < m:
                continue                      # below the median: a factor, _fit_scales
            d = float(np.quantile(y_fit - q_fit[:, k], tau))
            gap_without = abs(float((y_chk <= q_chk[:, k]).mean()) - tau)
            gap_with = abs(float((y_chk <= q_chk[:, k] + d).mean()) - tau)
            if gap_with < gap_without - 1e-9:
                deltas[k] = d
                kept[k] = True
        return deltas, kept

    def _fit_scales(
        self, y_fit: np.ndarray, q_fit: np.ndarray, y_chk: np.ndarray, q_chk: np.ndarray,
        kept: list[bool] | None = None,
    ) -> np.ndarray:
        """Per-level factor below the median, validated out of sample.

        Estimated on one half by the strict conformal rank, kept only if on the
        other half it brings the level closer to calibrated in the count sense (tau
        inside [P(y < q), P(y <= q)]) or, where both bracket tau, lowers the pinball
        loss. `kept`, when given, records which levels kept their factor.
        """
        K = len(self.quantiles)
        scales = np.ones(K, dtype="float64")
        if len(y_fit) < 30 or len(y_chk) < 30:
            return scales

        for k in range(_median_index(self.quantiles)):
            tau = float(self.quantiles[k])
            c = _strict_scale(y_fit, q_fit[:, k], tau)
            if c is None:
                continue
            base = np.maximum(q_chk[:, k], 0.0)
            gap_without = _tie_aware_gap(y_chk, base, tau)
            gap_with = _tie_aware_gap(y_chk, c * base, tau)
            sharper = (gap_with <= gap_without + 1e-9
                       and _pinball(y_chk, c * base, tau) < _pinball(y_chk, base, tau) - 1e-12)
            if gap_with < gap_without - 1e-9 or sharper:
                scales[k] = c
                if kept is not None:
                    kept[k] = True
        return scales

    def adjustment_report(self) -> pd.DataFrame:
        """Which levels were adjusted, and which the guard declined to touch."""
        rows = []
        scaled = bool(getattr(self, "scales", None))
        m = _median_index(self.quantiles)
        for k, tau in enumerate(self.quantiles):
            g = self.adjusted.get(GLOBAL, [False] * len(self.quantiles))
            n_strata = sum(1 for s, a in self.adjusted.items() if s != GLOBAL and a[k])
            factor = scaled and k < m
            rows.append(
                {
                    "nominal": tau,
                    "global_offset": float(self.offsets[GLOBAL][k]),
                    "global_scale": float(self.scales[GLOBAL][k]) if factor else 1.0,
                    "global_adjusted": bool(g[k]),
                    "strata_adjusted": n_strata,
                    "reason": (
                        "point forecast, never shifted" if abs(tau - 0.5) < 1e-9
                        else "scaled: strict floor P(y < q) <= tau" if factor and g[k]
                        else "adjusted" if g[k]
                        else "declined: no out-of-sample gain"
                    ),
                }
            )
        return pd.DataFrame(rows)

    # -------------------------------------------------------------- apply

    def apply(
        self, q: np.ndarray, planning_class: np.ndarray, horizon_group: np.ndarray
    ) -> np.ndarray:
        """Adjust quantiles in place-shape. `q` is (N, Q) or (S, H, Q)."""
        shape = q.shape
        flat = q.reshape(-1, shape[-1]).astype("float64")
        pc = np.asarray(planning_class).reshape(-1).astype("U20")
        hg = np.asarray(horizon_group).reshape(-1).astype("U12")
        strata = np.char.add(np.char.add(pc, "|"), hg)

        adj = np.repeat(self.offsets[GLOBAL][None, :], len(flat), axis=0)
        for s in np.unique(strata):
            if s in self.offsets:
                adj[strata == s] = self.offsets[s]
        out = flat + adj
        if getattr(self, "scales", None):
            # Below the median the stored offsets are zero and the factor applies.
            # A calibrator pickled before factors existed has none, and keeps the
            # additive lower offsets it was fitted with.
            m = _median_index(self.quantiles)
            out[:, :m] = flat[:, :m] * self._lower_factors(flat, strata)[:, :m]
        return self._monotone_about_median(out).reshape(shape)

    def _lower_factors(self, flat: np.ndarray, strata: np.ndarray) -> np.ndarray:
        """(N, Q) factor per row: its stratum's, else its median bin's, else pooled."""
        fac = np.repeat(self.scales[GLOBAL][None, :], len(flat), axis=0)
        own = np.zeros(len(flat), dtype=bool)
        for s in np.unique(strata):
            if s in self.scales:
                hit = strata == s
                fac[hit] = self.scales[s]
                own |= hit
        edges = getattr(self, "level_edges", None)
        bins = getattr(self, "level_scales", None) or {}
        if edges is not None and bins and not own.all():
            thin = np.flatnonzero(~own)
            b = np.searchsorted(edges, flat[thin, _median_index(self.quantiles)], side="right")
            for j, sc in bins.items():
                fac[thin[b == j]] = sc
        return fac

    def _monotone_about_median(self, a: np.ndarray) -> np.ndarray:
        """Restore monotonicity without displacing the median.

        Pinning the median's *offset* to zero is not sufficient. The usual
        repair — clip at zero and sort the whole quantile vector — will happily
        promote an adjusted neighbour past the median, and after the sort that
        neighbour *is* the median. Measured on real output: a p25 shifted up by
        3 units moved a median of 5.0 to 7.0 with its own offset at zero, which
        is how this stage went on costing 19% of WRMSSE after the offset was
        already pinned.

        So the median is held as the anchor and each tail is made monotone
        toward it: lower levels are pushed down to at most their upper
        neighbour, upper levels up to at least their lower neighbour. The result
        is non-decreasing across the whole vector, non-negative, and leaves the
        point forecast exactly where the model put it.
        """
        taus = np.asarray(self.quantiles)
        m = int(np.argmin(np.abs(taus - 0.5)))
        a = np.nan_to_num(np.asarray(a, dtype="float64"),
                          nan=0.0, posinf=0.0, neginf=0.0)
        a = np.maximum(a, 0.0)

        # Sweeping outward from the median makes each clamp reference an already
        # settled neighbour, so one pass per side is enough.
        for k in range(m - 1, -1, -1):
            a[:, k] = np.minimum(a[:, k], a[:, k + 1])
        for k in range(m + 1, a.shape[1]):
            a[:, k] = np.maximum(a[:, k], a[:, k - 1])
        return a.astype("float32")

    # -------------------------------------------------------------- report

    def coverage_table(
        self,
        y: np.ndarray,
        q_before: np.ndarray,
        q_after: np.ndarray,
        planning_class: np.ndarray | None = None,
        valid: np.ndarray | None = None,
    ) -> pd.DataFrame:
        """Nominal versus empirical coverage, before and after adjustment.

        The tidiest result the pipeline produces, and the one that justifies the
        stage existing.
        """
        y = np.asarray(y, dtype="float64")
        keep = np.isfinite(y)
        if valid is not None:
            keep &= valid
        rows = []
        for k, tau in enumerate(self.quantiles):
            rows.append(
                {
                    "nominal": tau,
                    "empirical_before": float((y[keep] <= q_before[keep, k]).mean()),
                    "empirical_after": float((y[keep] <= q_after[keep, k]).mean()),
                    # P(y < q), the strict side. For count data the predictive CDF
                    # jumps at every integer, so a level tau is calibrated when it
                    # lies anywhere in [P(y < q), P(y <= q)] -- not when P(y <= q)
                    # equals tau. With 43% of days at zero, P(y <= 0) alone exceeds
                    # every lower level, so the non-strict column cannot reach the
                    # nominal there however good the model is. The pair together
                    # is the test that can actually pass or fail.
                    "strict_after": float((y[keep] < q_after[keep, k]).mean()),
                    # The mean *level* of this quantile, not an interval width —
                    # a width needs two levels. Named accurately because "width"
                    # invites reading a P50 column as an interval.
                    "mean_level_before": float(q_before[keep, k].mean()),
                    "mean_level_after": float(q_after[keep, k].mean()),
                }
            )
        df = pd.DataFrame(rows)
        df["gap_before"] = (df["empirical_before"] - df["nominal"]).abs()
        df["gap_after"] = (df["empirical_after"] - df["nominal"]).abs()
        # Calibrated in the discrete sense: nominal inside [strict, non-strict]. The
        # distance outside that interval is the honest miscalibration -- zero when
        # the nominal falls inside the jump, positive only when it genuinely misses.
        lo, hi = df["strict_after"], df["empirical_after"]
        df["tie_aware_gap"] = np.maximum(0.0, np.maximum(lo - df["nominal"],
                                                         df["nominal"] - hi))
        df["tie_aware_ok"] = df["tie_aware_gap"] <= 0.02
        return df

    def interval_coverage(
        self, y: np.ndarray, q: np.ndarray, lo: float = 0.10, hi: float = 0.90,
        valid: np.ndarray | None = None,
    ) -> float:
        i_lo = int(np.argmin(np.abs(np.asarray(self.quantiles) - lo)))
        i_hi = int(np.argmin(np.abs(np.asarray(self.quantiles) - hi)))
        keep = np.isfinite(y) if valid is None else (np.isfinite(y) & valid)
        return float(((y[keep] >= q[keep, i_lo]) & (y[keep] <= q[keep, i_hi])).mean())

    def pit_values(
        self, y: np.ndarray, q: np.ndarray, randomised: bool = True, seed: int = 0
    ) -> np.ndarray:
        """Probability Integral Transform on the predicted quantile function.

        A calibrated forecast gives a uniform PIT, and the histogram shape names
        the failure mode: U-shaped means intervals too narrow, hump-shaped too wide.

        Randomisation is on by default because the data are counts. Demand is
        integer and 43% of it is zero, so the predicted quantile vector is heavily
        tied — many series have q05 = q10 = q25 = 0 — and `np.interp` on a tied
        vector returns the *first* matching tau for every zero actual. That piles a
        large spike at one end of the histogram and makes a perfectly calibrated
        forecast look badly miscalibrated. The standard fix for discrete
        predictands is the randomised PIT: draw uniformly within the probability
        interval the observation actually occupies, which restores uniformity under
        correct calibration. Pass randomised=False for the raw non-randomised
        transform.
        """
        taus = np.asarray(self.quantiles, dtype="float64")
        y = np.asarray(y, dtype="float64")
        q = np.asarray(q, dtype="float64")
        K = len(taus)
        rng = np.random.default_rng(seed)

        n_below = (q < y[:, None]).sum(axis=1)      # levels strictly below y
        n_at = (q <= y[:, None]).sum(axis=1)        # levels at or below y

        # F(y-) and F(y). Only 9 grid points are known, so the interval below the
        # first level and above the last are open-ended and treated as such.
        lo = np.where(n_below > 0, taus[np.clip(n_below - 1, 0, K - 1)], 0.0)
        hi = np.where(n_at > 0, taus[np.clip(n_at - 1, 0, K - 1)], 0.0)
        hi = np.where(n_at >= K, 1.0, np.where(n_at > 0, hi, taus[0]))

        tied = n_at > n_below       # y coincides with one or more grid values
        out = np.empty(len(y))

        # Continuous case: y falls strictly between two grid levels, so the CDF
        # value has to be interpolated — a 9-level grid cannot resolve it otherwise,
        # and snapping to the nearest level would make a correctly specified
        # continuous forecast look badly calibrated.
        j = np.clip(n_below, 0, K - 1)
        q_lo = np.where(n_below > 0, q[np.arange(len(y)), np.clip(n_below - 1, 0, K - 1)], q[:, 0])
        q_hi = q[np.arange(len(y)), j]
        t_lo = np.where(n_below > 0, taus[np.clip(n_below - 1, 0, K - 1)], 0.0)
        t_hi = taus[j]
        span = q_hi - q_lo
        frac = np.where(np.abs(span) > 1e-12, (y - q_lo) / np.where(np.abs(span) > 1e-12, span, 1.0), 0.5)
        interp = np.clip(t_lo + np.clip(frac, 0.0, 1.0) * (t_hi - t_lo), 0.0, 1.0)
        interp = np.where(n_below >= K, 1.0, interp)
        out[~tied] = interp[~tied]

        # Discrete case: y lands exactly on tied grid values, so the CDF jumps and
        # F(y) is an interval rather than a point. Drawing uniformly inside it is
        # the randomised PIT, which is what restores uniformity for count data.
        if tied.any():
            if randomised:
                out[tied] = lo[tied] + rng.random(int(tied.sum())) * (hi[tied] - lo[tied])
            else:
                out[tied] = hi[tied]
        return out
