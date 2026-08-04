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
    quantiles: tuple[float, ...] = QUANTILES

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

        self.offsets[GLOBAL] = self._offsets(y, q)
        self.n_by_stratum[GLOBAL] = int(len(y))

        strata = np.char.add(np.char.add(pc.astype("U20"), "|"), hg.astype("U12"))
        for s in np.unique(strata):
            m = strata == s
            n = int(m.sum())
            if n < cfg.calibrate.min_stratum:
                continue          # too thin to estimate reliably; global applies
            self.offsets[str(s)] = self._offsets(y[m], q[m])
            self.n_by_stratum[str(s)] = n
        return self

    def _offsets(self, y: np.ndarray, q: np.ndarray) -> np.ndarray:
        """Per-level conformity offset. The empirical tau-quantile of (y - q_tau)
        is exactly the shift that makes empirical coverage equal tau."""
        return np.array(
            [float(np.quantile(y - q[:, k], tau)) for k, tau in enumerate(self.quantiles)],
            dtype="float64",
        )

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
        return sanitize((flat + adj).reshape(shape))

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
