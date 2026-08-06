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

Stratified by planning class and horizon group, because a single global adjustment
would over-widen smooth series and under-widen lumpy ones — they have genuinely
different conditional spread and pooling them averages away the difference.

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


@dataclass
class ConformalCalibrator:
    offsets: dict[str, np.ndarray] = field(default_factory=dict)
    n_by_stratum: dict[str, int] = field(default_factory=dict)
    adjusted: dict[str, list[bool]] = field(default_factory=dict)
    quantiles: tuple[float, ...] = QUANTILES

    # ---------------------------------------------------------------------
    # Two rules that separate this from a naive per-level shift, both of them
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
    #    the guard discovers that from the data rather than being told.
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
            self.n_by_stratum[str(s)] = n
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

        for k, tau in enumerate(self.quantiles):
            if abs(tau - 0.5) < 1e-9:
                continue                      # the point forecast is never shifted
            d = float(np.quantile(y_fit - q_fit[:, k], tau))
            gap_without = abs(float((y_chk <= q_chk[:, k]).mean()) - tau)
            gap_with = abs(float((y_chk <= q_chk[:, k] + d).mean()) - tau)
            if gap_with < gap_without - 1e-9:
                deltas[k] = d
                kept[k] = True
        return deltas, kept

    def adjustment_report(self) -> pd.DataFrame:
        """Which levels were adjusted, and which the guard declined to touch."""
        rows = []
        for k, tau in enumerate(self.quantiles):
            g = self.adjusted.get(GLOBAL, [False] * len(self.quantiles))
            n_strata = sum(1 for s, a in self.adjusted.items() if s != GLOBAL and a[k])
            rows.append(
                {
                    "nominal": tau,
                    "global_offset": float(self.offsets[GLOBAL][k]),
                    "global_adjusted": bool(g[k]),
                    "strata_adjusted": n_strata,
                    "reason": (
                        "point forecast, never shifted" if abs(tau - 0.5) < 1e-9
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
        return self._monotone_about_median(flat + adj).reshape(shape)

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
                    "mean_width_before": float(q_before[keep, k].mean()),
                    "mean_width_after": float(q_after[keep, k].mean()),
                }
            )
        df = pd.DataFrame(rows)
        df["gap_before"] = (df["empirical_before"] - df["nominal"]).abs()
        df["gap_after"] = (df["empirical_after"] - df["nominal"]).abs()
        return df

    def interval_coverage(
        self, y: np.ndarray, q: np.ndarray, lo: float = 0.10, hi: float = 0.90,
        valid: np.ndarray | None = None,
    ) -> float:
        i_lo = int(np.argmin(np.abs(np.asarray(self.quantiles) - lo)))
        i_hi = int(np.argmin(np.abs(np.asarray(self.quantiles) - hi)))
        keep = np.isfinite(y) if valid is None else (np.isfinite(y) & valid)
        return float(((y[keep] >= q[keep, i_lo]) & (y[keep] <= q[keep, i_hi])).mean())

    def pit_values(self, y: np.ndarray, q: np.ndarray) -> np.ndarray:
        """Probability Integral Transform via linear interpolation on the
        predicted quantile function. A calibrated forecast gives uniform PIT;
        the histogram shape names the failure mode — U-shaped means intervals
        too narrow, hump-shaped means too wide."""
        taus = np.asarray(self.quantiles)
        out = np.empty(len(y))
        for i in range(len(y)):
            out[i] = float(np.interp(y[i], q[i], taus, left=0.0, right=1.0))
        return out
