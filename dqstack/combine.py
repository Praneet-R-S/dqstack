"""Stage 4 — combination via meta-learning (FFORMA).

Do not select one model per series. Combine them.

The reason is a variance argument, not a taste argument. Selection is a hard
argmax over noisy validation estimates: if two candidates are within noise, the
argmax picks one and discards the other entirely, and it picks differently on a
resample. Combination hedges that. The M4 competition established feature-based
weighted combination as the stronger approach, and this reproduces its mechanism.

The objective is the real FFORMA one, not a classification proxy. A meta-learner
that classified "which model won" would optimise argmin accuracy, which is not
what we want — being wrong between two near-equal candidates costs almost
nothing, and being wrong between a good and a terrible one costs a lot. Instead
the softmax weights `p` are trained to minimise the weight-averaged candidate
loss directly:

    L(p) = sum_k p_k * L_k

    dL/dscore_k  = p_k * (L_k - sum_j p_j L_j)

which penalises weight on bad candidates in proportion to how bad they are.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import lightgbm as lgb
import numpy as np
import pandas as pd

from .config import CFG, QUANTILES

META_FEATURES = [
    "adi", "cv2", "cv", "zero_share", "mean_demand", "seasonal_strength",
    "trend_strength", "spectral_entropy", "acf7", "acf364", "age_weeks",
    "weeks_since_break", "health_score", "censored_rate", "ooa_rate",
    "price_volatility", "n_obs", "longest_zero_run", "iqr_over_median",
]


def pinball(y: np.ndarray, q: np.ndarray, quantiles=QUANTILES) -> np.ndarray:
    """Mean pinball (quantile) loss over the quantile axis.

    `y` is (..,) and `q` is (.., Q). This is the scoring rule the whole system is
    optimised against, because it is proper for the full predictive distribution
    rather than only its centre.
    """
    y = np.asarray(y, dtype="float64")[..., None]
    q = np.asarray(q, dtype="float64")
    tau = np.asarray(quantiles, dtype="float64")
    d = y - q
    loss = np.maximum(tau * d, (tau - 1.0) * d)
    return loss.mean(axis=-1)


def candidate_losses(
    actual: np.ndarray, cand_q: dict[str, np.ndarray], valid_mask: np.ndarray
) -> pd.DataFrame:
    """Per-series average pinball loss for each candidate.

    `actual`    (S, H)
    `cand_q`    name -> (S, H, Q)
    `valid_mask` (S, H) bool — excludes censored and out-of-assortment targets,
                 which would otherwise reward whichever model under-forecasts most.
    """
    rows = {}
    for name, q in cand_q.items():
        per = pinball(actual, q)                     # (S, H)
        per = np.where(valid_mask, per, np.nan)
        rows[name] = np.nanmean(per, axis=1)
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ stacking

def stacked_weights(
    y: np.ndarray,
    cand_q: dict[str, np.ndarray],
    candidates: list[str],
    valid: np.ndarray,
    strata: np.ndarray,
    min_rows: int = 2_000,
) -> tuple[dict[str, np.ndarray], np.ndarray]:
    """Per-stratum weights minimising the pinball loss of the *combined* forecast.

    This exists because the FFORMA objective is a proxy that cannot express the
    thing Stage 4 is arguing for.

        FFORMA proxy   L(p) = sum_k p_k * L_k
        what we want   L(p) = pinball(y, sum_k p_k * q_k)

    The proxy is the expected loss of *randomly selecting* candidate k with
    probability p_k. It is linear in p, so its minimum over the simplex sits at a
    vertex — pure selection — and any diversification the softmax produces is
    regularisation rather than optimisation. Measured on this data the proxy left
    the clearly-best candidate holding 14-26% of the weight while seven others
    shared the rest, and the resulting combination lost to that single candidate at
    every evaluation origin.

    The second form is the loss of the combination actually issued. Pinball loss is
    a maximum of linear functions of the forecast, hence convex in the weights, so
    the constrained minimum is well defined and reachable — and it is interior
    whenever averaging genuinely helps. Solved per stratum with SLSQP on the
    simplex, which is the classical quantile-stacking construction.

    Returns (weights per stratum, the per-series weight matrix).
    """
    from scipy.optimize import minimize

    K = len(candidates)
    Q = np.stack([np.nan_to_num(cand_q[c], nan=0.0) for c in candidates], axis=2)
    taus = np.asarray(QUANTILES, dtype="float64")

    def loss(w, yy, qq):
        comb = np.tensordot(qq, w, axes=([1], [0]))          # (n, Qlev)
        d = yy[:, None] - comb
        return float(np.maximum(taus * d, (taus - 1.0) * d).mean())

    out: dict[str, np.ndarray] = {}
    x0 = np.full(K, 1.0 / K)
    cons = ({"type": "eq", "fun": lambda w: w.sum() - 1.0},)
    bnds = [(0.0, 1.0)] * K

    for s in np.unique(strata):
        m = (strata == s) & valid
        if m.sum() < min_rows:
            continue
        yy = y[m]
        qq = Q[m]                                            # (n, K, Qlev)
        qq = np.swapaxes(qq, 1, 2)                           # (n, Qlev, K)
        res = minimize(loss, x0, args=(yy, qq), method="SLSQP",
                       bounds=bnds, constraints=cons,
                       options={"maxiter": 120, "ftol": 1e-9})
        w = np.clip(res.x, 0.0, None)
        out[str(s)] = w / max(w.sum(), 1e-12)

    # Global fallback for thin strata.
    m = valid
    yy, qq = y[m], np.swapaxes(Q[m], 1, 2)
    res = minimize(loss, x0, args=(yy, qq), method="SLSQP",
                   bounds=bnds, constraints=cons, options={"maxiter": 120, "ftol": 1e-9})
    g = np.clip(res.x, 0.0, None)
    out["__global__"] = g / max(g.sum(), 1e-12)
    return out, np.asarray(out["__global__"])


def weights_from_strata(
    strata_per_series: np.ndarray, table: dict[str, np.ndarray], K: int
) -> np.ndarray:
    """Expand per-stratum weights to one row per series."""
    W = np.repeat(table["__global__"][None, :], len(strata_per_series), axis=0)
    for s, w in table.items():
        if s == "__global__":
            continue
        W[strata_per_series.astype(str) == s] = w
    return W


# ------------------------------------------------------------------ FFORMA

def _fforma_objective(losses: np.ndarray, n_class: int):
    """LightGBM custom objective implementing the FFORMA gradient."""

    def obj(scores: np.ndarray, dataset: lgb.Dataset):
        n = losses.shape[0]
        s = scores.reshape(n_class, n).T if scores.ndim == 1 else scores
        s = s - s.max(axis=1, keepdims=True)
        p = np.exp(s)
        p /= p.sum(axis=1, keepdims=True)
        wl = (p * losses).sum(axis=1, keepdims=True)          # expected loss
        grad = p * (losses - wl)
        # FFORMA surrogate hessian: L*p*(1-p) - grad*p. The damping term is
        # grad*p, not grad*(1-2p) — the latter flips sign at p = 0.5 and can make
        # the surrogate negative, which the max() then clamps to the floor and
        # silently turns the update into a fixed-size step.
        hess = np.maximum(losses * p * (1.0 - p) - grad * p, 1e-6)
        return grad.T.reshape(-1), hess.T.reshape(-1)

    return obj


@dataclass
class MetaLearner:
    """Maps Stage-1 series features to a weight vector over candidates."""

    candidates: list[str] = field(default_factory=list)
    booster: lgb.Booster | None = None
    features: list[str] = field(default_factory=list)
    fallback_weights: np.ndarray | None = None
    class_weights: dict[str, np.ndarray] = field(default_factory=dict)
    feature_medians: dict[str, float] = field(default_factory=dict)

    def fit(
        self, diag: pd.DataFrame, losses: pd.DataFrame, cfg=CFG, verbose: bool = True
    ) -> "MetaLearner":
        self.candidates = list(losses.columns)
        K = len(self.candidates)
        feats = [f for f in META_FEATURES if f in diag.columns]
        self.features = feats

        L = losses.to_numpy(dtype="float64")
        ok = np.isfinite(L).all(axis=1)
        X = diag.loc[ok, feats].astype("float64")
        self.feature_medians = {c: float(v) for c, v in X.median().items()}
        L = L[ok]

        # Normalise each series' losses to its own best candidate. Without this a
        # single high-volume series with large absolute pinball loss dominates the
        # gradient and the weights collapse toward whatever suits that one series.
        L = L / np.maximum(L.min(axis=1, keepdims=True), 1e-9)
        L = np.clip(L, 0.0, 12.0)

        params = {
            "num_class": K,
            "learning_rate": cfg.combine.meta_lr,
            "num_leaves": cfg.combine.meta_leaves,
            "min_data_in_leaf": 20,
            "feature_fraction": 0.85,
            "lambda_l2": 1.0,
            "verbosity": -1,
            "seed": cfg.seed,
        }
        ds = lgb.Dataset(X, label=np.zeros(len(X)), free_raw_data=False)
        self.booster = lgb.train(
            params, ds, num_boost_round=cfg.combine.meta_rounds,
            fobj=_fforma_objective(L, K) if _accepts_fobj() else None,
        ) if _accepts_fobj() else self._fit_manual(X, L, params, cfg)

        # Fallbacks for series the meta-learner cannot score (no diagnostics).
        inv = 1.0 / np.maximum(L.mean(axis=0), 1e-9)
        self.fallback_weights = inv / inv.sum()
        klass = diag.loc[ok, "planning_class"].to_numpy()
        for k in np.unique(klass):
            m = klass == k
            iv = 1.0 / np.maximum(L[m].mean(axis=0), 1e-9)
            self.class_weights[str(k)] = iv / iv.sum()

        if verbose:
            print(f"  meta-learner  {len(X):,} series x {K} candidates, {len(feats)} features")
        return self

    def _fit_manual(self, X, L, params, cfg):
        """Boosting loop with the FFORMA gradient, for LightGBM builds whose
        `train` no longer accepts `fobj` (removed in 4.x)."""
        K = L.shape[1]
        n = len(X)
        scores = np.zeros((n, K))
        self._trees: list[list[lgb.Booster]] = []
        Xv = X.to_numpy() if hasattr(X, "to_numpy") else X
        for it in range(cfg.combine.meta_rounds):
            s = scores - scores.max(axis=1, keepdims=True)
            p = np.exp(s)
            p /= p.sum(axis=1, keepdims=True)
            wl = (p * L).sum(axis=1, keepdims=True)
            grad = p * (L - wl)
            # Same surrogate as _fforma_objective; this is the executing path on
            # LightGBM 4.x, where `fobj` no longer exists.
            hess = np.maximum(L * p * (1 - p) - grad * p, 1e-6)
            step = []
            for k in range(K):
                tgt = -grad[:, k] / hess[:, k]
                w = hess[:, k]
                b = lgb.train(
                    {
                        "objective": "regression", "learning_rate": 1.0,
                        "num_leaves": cfg.combine.meta_leaves, "min_data_in_leaf": 20,
                        "feature_fraction": 0.85, "lambda_l2": 1.0,
                        "verbosity": -1, "seed": cfg.seed + it,
                    },
                    lgb.Dataset(Xv, label=tgt, weight=w, feature_name=list(self.features)),
                    num_boost_round=1,
                )
                scores[:, k] += cfg.combine.meta_lr * b.predict(Xv)
                step.append(b)
            self._trees.append(step)
        return None

    def select_weights(self, diag: pd.DataFrame, losses: pd.DataFrame) -> np.ndarray:
        """One-hot on the per-series argmin. The *selection* ablation arm.

        This is what Stage 4 is arguing against, so it has to be runnable:
        cfg.enable_combination was previously reported in the metrics block and
        consulted nowhere, so a 'no combination' arm would have silently run the
        full combination and reported it as an ablation.
        """
        L = losses.to_numpy(dtype='float64')
        best = np.nanargmin(np.where(np.isfinite(L), L, np.inf), axis=1)
        W = np.zeros_like(L)
        W[np.arange(len(best)), best] = 1.0
        return W

    def weights(self, diag: pd.DataFrame, cfg=CFG) -> np.ndarray:
        """Weight vector per series. Non-negative and summing to one by
        construction (softmax), with an entropy floor guarding collapse."""
        K = len(self.candidates)
        n = len(diag)
        # Impute with the medians observed at fit time. Filling with 0.0 places a
        # series at an arbitrary point in feature space — zero ADI, zero health —
        # rather than at the population centre, and fit() dropped exactly these
        # rows, so predict was extrapolating into a region it never trained on.
        Xdf = diag[self.features].astype("float64")
        if self.feature_medians:
            Xdf = Xdf.fillna(pd.Series(self.feature_medians))
        X = Xdf.fillna(0.0).to_numpy()

        if getattr(self, "_trees", None):
            scores = np.zeros((n, K))
            for step in self._trees:
                for k, b in enumerate(step):
                    scores[:, k] += cfg.combine.meta_lr * b.predict(X)
        elif self.booster is not None:
            scores = self.booster.predict(X).reshape(n, K)
        else:
            return np.repeat(self.fallback_weights[None, :], n, axis=0)

        scores = scores / max(cfg.combine.temperature, 1e-6)
        scores -= scores.max(axis=1, keepdims=True)
        w = np.exp(scores)
        w /= w.sum(axis=1, keepdims=True)

        # Entropy floor: if the effective number of candidates receiving weight
        # falls below the configured minimum, blend toward the class prior. A
        # degenerate collapse onto one model would forfeit the variance benefit
        # that motivates combining in the first place.
        eff = np.exp(-(w * np.log(np.clip(w, 1e-12, None))).sum(axis=1))
        bad = eff < cfg.combine.min_effective_candidates
        if bad.any():
            prior = np.stack([
                self.class_weights.get(str(c), self.fallback_weights)
                for c in diag["planning_class"].to_numpy()[bad]
            ])
            w[bad] = 0.5 * w[bad] + 0.5 * prior
            w[bad] /= w[bad].sum(axis=1, keepdims=True)
        return w


def _accepts_fobj() -> bool:
    import inspect
    return "fobj" in inspect.signature(lgb.train).parameters


def combine_quantiles(cand_q: dict[str, np.ndarray], weights: np.ndarray,
                      candidates: list[str]) -> np.ndarray:
    """Weighted average per quantile level (vincentization).

    Averaging quantile functions rather than densities keeps the result a valid,
    monotone quantile function and preserves the support — a mixture of densities
    would widen the distribution in a way that misrepresents the ensemble's
    actual disagreement.
    """
    out = None
    for k, name in enumerate(candidates):
        q = cand_q[name]
        contrib = weights[:, k][:, None, None] * np.nan_to_num(q, nan=0.0)
        out = contrib if out is None else out + contrib
    return np.maximum(out, 0.0)


def weight_report(weights: np.ndarray, candidates: list[str],
                  diag: pd.DataFrame) -> pd.DataFrame:
    """Mean weight per candidate per planning class — a monitored artifact.

    A sudden shift in this table is an early signal that something upstream
    broke, and it is what the explanation panel reads to show a planner why a
    given series was forecast the way it was.
    """
    df = pd.DataFrame(weights, columns=candidates)
    df["planning_class"] = diag["planning_class"].to_numpy()
    return df.groupby("planning_class").mean().round(4)
