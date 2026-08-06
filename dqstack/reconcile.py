"""Stage 5 — reconciliation, cross-sectional and temporal.

Cross-sectional. MinT (minimum trace) with a shrinkage covariance estimator.
MinT dominates bottom-up and top-down at aggregate levels because it uses the
full error covariance structure instead of assuming a direction of aggregation:
bottom-up trusts noisy leaves, top-down destroys leaf-level detail, and MinT
finds the projection with minimum total forecast-error variance subject to
coherence.

Temporal. Daily, weekly and monthly forecasts made arithmetically consistent, so
"the weekly plan disagrees with the quarterly plan" is structurally impossible
rather than a recurring meeting.

The tradeoff is reported honestly rather than hidden: reconciliation frequently
improves aggregate accuracy while slightly degrading bottom-level accuracy. Which
matters depends on the decision being made, so `evaluate` measures both.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import CFG, Q50_INDEX

# The twelve aggregation levels. Level 12 is the bottom (item x store); the
# others are the groupings a planning organisation actually reviews.
LEVELS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("total",            ()),
    ("state",            ("state_id",)),
    ("store",            ("store_id",)),
    ("category",         ("cat_id",)),
    ("department",       ("dept_id",)),
    ("state_category",   ("state_id", "cat_id")),
    ("state_department", ("state_id", "dept_id")),
    ("store_category",   ("store_id", "cat_id")),
    ("store_department", ("store_id", "dept_id")),
    ("item",             ("item_id",)),
    ("item_state",       ("item_id", "state_id")),
    ("item_store",       ("item_id", "store_id")),      # bottom
)


@dataclass
class Hierarchy:
    S: np.ndarray                 # (n_nodes, n_bottom) summing matrix
    node_level: list[str]
    node_key: list[str]
    n_bottom: int

    @property
    def n_nodes(self) -> int:
        return self.S.shape[0]

    def bottom_slice(self) -> slice:
        return slice(self.n_nodes - self.n_bottom, self.n_nodes)


def build_hierarchy(series: pd.DataFrame) -> Hierarchy:
    n = len(series)
    rows: list[np.ndarray] = []
    node_level: list[str] = []
    node_key: list[str] = []

    for name, keys in LEVELS:
        if not keys:
            rows.append(np.ones(n, dtype="float32"))
            node_level.append(name)
            node_key.append("TOTAL")
            continue
        key = series[list(keys)].astype(str).agg("|".join, axis=1).to_numpy()
        for k in np.unique(key):
            rows.append((key == k).astype("float32"))
            node_level.append(name)
            node_key.append(k)

    S = np.stack(rows)
    return Hierarchy(S=S, node_level=node_level, node_key=node_key, n_bottom=n)


def shrink_covariance(residuals: np.ndarray) -> np.ndarray:
    """Schafer-Strimmer shrinkage toward the diagonal.

    The sample covariance of node-level residuals is singular whenever the number
    of nodes exceeds the number of residual observations, which is the normal case
    here (1,200 nodes, a few hundred origins x horizons). Shrinking the
    off-diagonal correlations toward zero by an analytically chosen intensity
    keeps the estimate invertible while retaining the cross-node structure that
    is the entire reason to prefer MinT.
    """
    R = residuals[np.isfinite(residuals).all(axis=1)].astype("float64")
    n, p = R.shape
    if n < 5:
        return np.eye(p, dtype="float64"), 1.0
    Rc = R - R.mean(axis=0, keepdims=True)
    Sm = (Rc.T @ Rc) / max(n - 1, 1)
    d = np.sqrt(np.clip(np.diag(Sm), 1e-9, None))
    Corr = Sm / np.outer(d, d)
    np.fill_diagonal(Corr, 1.0)

    # Variance of the sample correlations. Computed via two Gram matrices rather
    # than the (n, p, p) outer-product tensor the textbook formula implies: with
    # p ~ 1,200 nodes that tensor is several gigabytes, and it is unnecessary
    # because Var(z_i z_j) = E[(z_i z_j)^2] - E[z_i z_j]^2 and both terms are
    # p x p Gram matrices.
    Z = Rc / d
    m1 = (Z.T @ Z) / n
    Z2 = Z * Z
    m2 = (Z2.T @ Z2) / n
    # Schafer-Strimmer: Var_hat(r_ij) = n/(n-1)^3 * sum_t (w_tij - wbar_ij)^2.
    # (m2 - m1^2) is the population variance, i.e. that sum divided by n, so the
    # factor is n^2/(n-1)^3. Using n/(n-1)^2 leaves the estimate short by n/(n-1)
    # and biases the reported shrinkage intensity low.
    var_r = np.clip(m2 - m1**2, 0.0, None) * (n**2) / max((n - 1) ** 3, 1)

    off = ~np.eye(p, dtype=bool)
    num = float(var_r[off].sum())
    den = float((Corr[off] ** 2).sum())
    lam = float(np.clip(num / den, 0.0, 1.0)) if den > 0 else 1.0

    Corr_s = (1.0 - lam) * Corr + lam * np.eye(p)
    return (Corr_s * np.outer(d, d)).astype("float64"), lam


def mint_projection(H: Hierarchy, W: np.ndarray, tol: float = 1e-7) -> np.ndarray:
    """G = (S' W^-1 S)^-1 S' W^-1, so reconciled = S G y_hat.

    The defining property is `G @ S = I`. At this scale `S' W^-1 S` is
    1,000 x 1,000 and mildly ill-conditioned, and a G that misses the identity by
    even 1e-3 applies a spurious correction to every forecast in the system — which
    looks exactly like reconciliation "hurting accuracy" and is really a linear
    algebra failure. So the ridge is raised until the property actually holds, and
    the achieved error is returned to the caller for reporting.
    """
    S = H.S.astype("float64")
    # Solve rather than invert: W is large and its near-singular directions are
    # precisely where an explicit inverse loses precision.
    WiS = np.linalg.solve(W, S)
    A = S.T @ WiS
    scale = float(np.trace(A)) / A.shape[0]

    best, best_err = None, np.inf
    for ridge in (0.0, 1e-10, 1e-8, 1e-6, 1e-4, 1e-2):
        try:
            G = np.linalg.solve(A + np.eye(A.shape[0]) * ridge * scale, WiS.T)
        except np.linalg.LinAlgError:
            continue
        err = float(np.abs(G @ S - np.eye(H.n_bottom)).max())
        if err < best_err:
            best, best_err = G, err
        if err <= tol:
            return G
    if best is None:
        # Last resort: the pseudo-inverse always yields a valid projection.
        return np.linalg.pinv(S.T @ WiS) @ WiS.T
    return best


@dataclass
class Reconciler:
    H: Hierarchy | None = None
    G: np.ndarray | None = None
    shrinkage: float | None = None
    projection_error: float | None = None

    def fit(self, series: pd.DataFrame, bottom_residuals: np.ndarray, cfg=CFG) -> "Reconciler":
        """`bottom_residuals` is (n_obs, n_bottom) of in-sample forecast errors."""
        self.H = build_hierarchy(series)
        node_res = bottom_residuals @ self.H.S.T          # (n_obs, n_nodes)
        W, lam = shrink_covariance(node_res)
        self.shrinkage = lam
        self.G = mint_projection(self.H, W)
        # G must satisfy G @ S = I exactly. Any deviation is conditioning error
        # from the 1,000 x 1,000 solve, and it would be applied as a spurious
        # correction to every forecast, so it is measured rather than assumed.
        self.projection_error = float(
            np.abs(self.G @ self.H.S - np.eye(self.H.n_bottom)).max()
        )
        return self

    def base_forecasts(self, y_hist: np.ndarray, O: int, horizons) -> np.ndarray:
        """Independent base forecasts at every hierarchy node.

        This is the step that makes MinT meaningful rather than decorative. Given
        only bottom-level forecasts and aggregates computed by summing them, the
        input is already coherent, `G @ S @ bottom` reduces to `bottom`, and
        reconciliation can do nothing except add numerical noise.

        MinT earns its place when each level is forecast *independently* — an
        aggregate series is smoother and more predictable than its components, so
        a direct forecast of the department total genuinely carries information the
        sum of its items does not. Those disagreeing views are what the projection
        then resolves optimally.

        Aggregate levels are forecast with Holt-Winters, which suits them: the
        intermittency that makes bottom-level series hard largely disappears under
        aggregation.
        """
        from .models.statistical import ets_matrix

        assert self.H is not None
        # Masked cells are censored or out-of-assortment days. Zero-filling them
        # before aggregating pushes every aggregate below true demand — and it does
        # so hardest for the most-censored departments, which is exactly where the
        # aggregate view was supposed to be more reliable than its components. Fill
        # each series with its own observed mean instead, so a missing day is
        # treated as an average day rather than a lost sale.
        hist = y_hist[:, : O + 1].astype("float64")
        row_mean = np.nanmean(np.where(np.isfinite(hist), hist, np.nan), axis=1, keepdims=True)
        filled = np.where(np.isfinite(hist), hist, np.nan_to_num(row_mean))
        agg_hist = self.H.S @ filled
        return np.maximum(ets_matrix(agg_hist, np.asarray(horizons)), 0.0)

    def temporal_base(self, y_hist: np.ndarray, O: int, horizon: int
                      ) -> tuple[np.ndarray, np.ndarray]:
        """Independent weekly and monthly forecasts for the temporal hierarchy.

        Same argument as `base_forecasts` for the cross-sectional case: temporal
        reconciliation can only do something if the coarse views are produced
        independently of the fine one. Aggregating the daily forecast and calling
        that a weekly forecast makes the stage an identity.

        History is aggregated to weekly buckets and forecast there directly, which
        is the point — a weekly series is far less intermittent than its days.
        """
        from .models.statistical import ets_matrix

        y = np.nan_to_num(y_hist[:, : O + 1], nan=0.0)
        weeks = horizon // 7
        usable = (y.shape[1] // 7) * 7
        wk_hist = y[:, y.shape[1] - usable :].reshape(y.shape[0], -1, 7).sum(axis=2)
        wk = np.maximum(ets_matrix(wk_hist, np.arange(1, weeks + 1), period=4), 0.0)
        return wk, wk.sum(axis=1, keepdims=True)

    def reconcile(self, bottom: np.ndarray, node_base: np.ndarray | None = None) -> np.ndarray:
        """Optimally combine base forecasts across levels.

        `bottom`    (n_bottom, H) the ensemble's bottom-level forecast
        `node_base` (n_nodes, H)  independent base forecasts per node; the bottom
                    rows are overwritten with `bottom`, which is the better
                    estimator there.

        Returns the reconciled bottom level. Aggregates are then exactly
        `S @ result` by construction.
        """
        assert self.H is not None and self.G is not None
        if node_base is None:
            # Degenerate case: coherent input, so the projection is the identity.
            y_all = self.H.S @ bottom
        else:
            y_all = node_base.copy()
            y_all[self.H.bottom_slice()] = bottom
        return np.maximum(self.G @ y_all, 0.0)

    def reconcile_quantiles(self, q: np.ndarray, node_base: np.ndarray | None = None) -> np.ndarray:
        """Apply the projection to the median and shift the whole distribution.

        Reconciling each quantile level independently is tempting and wrong: the
        projection is a linear operator on means, not on quantiles, and applying
        it level-by-level can invert the ordering of the fan. Shifting the
        distribution by the median's correction preserves both coherence of the
        central forecast and the calibrated spread that Stage 6 established.
        """
        med = q[..., Q50_INDEX]
        rec = self.reconcile(med, node_base)
        delta = (rec - med)[..., None]
        return np.maximum(q + delta, 0.0)

    def incoherence(self, node_base: np.ndarray) -> float:
        """How far the independent base forecasts disagree across levels.

        This is the quantity reconciliation exists to remove, so it is the only
        one worth reporting. Measuring it *after* reconciliation is meaningless:
        aggregates are defined as `S @ bottom` there, so any such check compares
        an expression with itself and returns zero no matter what the projection
        did. (It previously did exactly that — `agg` and `direct` were the same
        expression — and reported a reassuring 0.0.)

        Returns the largest relative gap between a node's own base forecast and
        the sum of its constituent bottom-level base forecasts.
        """
        assert self.H is not None
        bottom = node_base[self.H.bottom_slice()]
        implied = self.H.S @ bottom
        denom = np.maximum(np.abs(implied), 1.0)
        return float((np.abs(node_base - implied) / denom).max())


# ------------------------------------------------------------------ temporal

TEMPORAL_WEEKS = 4       # 28 days = 4 weeks = 1 monthly bucket


def temporal_reconcile(
    daily: np.ndarray,
    weekly_base: np.ndarray | None = None,
    monthly_base: np.ndarray | None = None,
) -> dict[str, np.ndarray]:
    """Make daily, weekly and 28-day totals arithmetically consistent.

    The temporal hierarchy over a 28-day horizon has 28 leaves, 4 weekly nodes
    and 1 monthly node. With a diagonal covariance this reduces to a structural
    scaling — each daily value is rescaled so its week sums to the reconciled
    weekly total — which is both the closed-form MinT solution under equal
    variances and cheap enough to run vectorised over every series at once.
    """
    S, H = daily.shape
    assert H % 7 == 0, "temporal reconciliation expects whole weeks"
    weeks = H // 7

    if weekly_base is None and monthly_base is None:
        # Nothing to reconcile against. Derive the aggregates and say plainly that
        # this is an identity, because the previous version did exactly this while
        # presenting itself as reconciliation: it recomputed the monthly total as
        # the sum of the weekly totals, which is the sum of the dailies, so the
        # correction term was identically zero and the "reconciled" dailies were
        # the input. The reported temporal residual of ~1e-04 was measuring
        # floating-point noise, not agreement between disagreeing views.
        blocks = daily.reshape(S, weeks, 7)
        wk = blocks.sum(axis=2)
        return {
            "daily": np.maximum(daily, 0.0),
            "weekly": np.maximum(wk, 0.0),
            "monthly": np.maximum(wk.sum(axis=1, keepdims=True), 0.0),
            "reconciled": False,
        }

    # ---- genuine temporal MinT over 1 monthly + `weeks` weekly + H daily nodes.
    n_nodes = 1 + weeks + H
    St = np.zeros((n_nodes, H))
    St[0] = 1.0                                             # monthly
    for w in range(weeks):
        St[1 + w, w * 7 : (w + 1) * 7] = 1.0                # weekly
    St[1 + weeks :] = np.eye(H)                             # daily (bottom)

    # Variance scaling: an aggregate of k days carries k times the variance of a
    # day under independence, so weight each node by 1/k. This is MinT with a
    # diagonal W — the standard structural choice when there are too few residual
    # observations to estimate a full temporal covariance, and it is what makes an
    # aggregate view count for more than a single noisy day.
    w_diag = St.sum(axis=1)
    Wt = np.diag(w_diag)
    WiS = np.linalg.solve(Wt, St)
    Gt = np.linalg.solve(St.T @ WiS, WiS.T)                 # (H, n_nodes)

    blocks = daily.reshape(S, weeks, 7)
    wk_in = blocks.sum(axis=2) if weekly_base is None else np.asarray(weekly_base, float)
    mo_in = (
        wk_in.sum(axis=1, keepdims=True)
        if monthly_base is None
        else np.asarray(monthly_base, float).reshape(S, 1)
    )

    y_all = np.concatenate([mo_in, wk_in, daily], axis=1)   # (S, n_nodes)
    daily_rec = np.maximum(y_all @ Gt.T, 0.0)

    rec_blocks = daily_rec.reshape(S, weeks, 7)
    return {
        "daily": daily_rec,
        "weekly": rec_blocks.sum(axis=2),
        "monthly": daily_rec.sum(axis=1, keepdims=True),
        "reconciled": True,
    }
