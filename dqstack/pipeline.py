"""The pipeline. All seven stages, end to end, from one command.

    python -m dqstack.pipeline

Origin discipline. Four disjoint, purged blocks of forecast origins:

    train    fits the candidate models
    meta     out-of-sample candidate losses -> FFORMA weights, NB dispersion
    conformal held out again -> Stage 6 offsets
    eval     one origin with known actuals -> every reported metric
    live     the final origin, no actuals -> the forecast the UI serves

Meta and conformal are deliberately separate. Fitting the combination weights and
the conformal offsets on the same origins would calibrate the intervals on data
the weights had already adapted to, and the coverage guarantee would be optimistic
in exactly the way the stage exists to prevent.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

from . import calibrate as cal_mod
from . import combine as comb
from . import decide as dec
from . import backtest as bt
from . import sensitivity as sens
from . import diagnose, evaluate, ingest, reconcile
from .config import (
    ARTIFACTS,
    CFG,
    HORIZON,
    HORIZON_GROUPS,
    INTERIM,
    PROCESSED,
    Q50_INDEX,
    QUANTILES,
    Config,
)
from .features import (
    ID_COLS, Panel, build_panel, horizon_group_of, make_splits, series_frame,
)
from .models import foundation, lifecycle, neural, statistical
from .models.dist import fit_dispersion, nb_quantiles, sanitize
from .models.gbm import GBMPool

ENSEMBLE = [
    "seasonal_naive", "ets", "croston_sba", "tsb", "adida",
    "lgbm_tweedie", "lgbm_quantile", "nhits", "zeroshot_prior", "lifecycle_cohort",
]


def log(msg: str = "") -> None:
    print(msg, flush=True)


def section(title: str) -> None:
    log()
    log(f"{'=' * 76}")
    log(f"  {title}")
    log(f"{'=' * 76}")


# ------------------------------------------------------------------ containers

@dataclass
class Trained:
    gbm: GBMPool
    gbm_nomask: GBMPool | None
    zeroshot: foundation.ZeroShotPrior
    nn: neural.NeuralCandidate
    cohorts: lifecycle.CohortCurves
    P_nomask: Panel | None = None
    dispersion: dict[str, dict[str, float]] = field(default_factory=dict)


@dataclass
class Bundle:
    """Everything one origin produces, per candidate and combined."""

    origin: int
    horizons: tuple[int, ...]
    points: dict[str, np.ndarray]
    quantiles: dict[str, np.ndarray]
    combined: np.ndarray | None = None
    reconciled: np.ndarray | None = None
    calibrated: np.ndarray | None = None


# ------------------------------------------------------------------ candidates

def candidate_points(
    P: Panel, O: int, horizons: tuple[int, ...], tr: Trained
) -> dict[str, np.ndarray]:
    """Conditional means from every candidate at one origin. (S, H) each."""
    h = np.asarray(horizons)
    out = statistical.forecast_all(P, O, h)
    point, _, _ = tr.gbm.predict(P, O, horizons)
    out["lgbm_tweedie"] = np.nan_to_num(point, nan=0.0)
    out["zeroshot_prior"] = tr.zeroshot.predict(P, O, horizons)
    out["nhits"] = tr.nn.predict(P, O, horizons)
    out["lifecycle_cohort"] = tr.cohorts.predict(P, O, h)
    return out


CLAMP_HITS: dict[str, int] = {}


def plausibility_cap(P: Panel, O: int) -> np.ndarray:
    """Per-series upper bound on any single day's forecast.

    Defence in depth, learned the hard way. A candidate that returns an absurd
    value for one series can wreck the *combination* even at a fraction of a
    percent of weight, because Stage 4 averages levels rather than ranks. One
    misbehaving ensemble member should degrade the ensemble, not destroy it.

    The bound is deliberately loose — several times the highest daily demand the
    series has ever recorded, with a floor for thin history — so it never binds
    on a plausible forecast. When it does bind, that is logged rather than
    silently swallowed, because a clamp firing often means a candidate is broken.
    """
    hist = P.y[:, max(0, O - 364) : O + 1]
    hi = np.nanmax(np.where(np.isfinite(hist), hist, 0.0), axis=1)
    mean = np.nanmean(np.where(np.isfinite(hist), hist, np.nan), axis=1)
    mean = np.nan_to_num(mean, nan=0.0)
    return np.maximum(np.maximum(8.0 * hi, 20.0 * mean), 25.0)


def candidate_quantiles(
    P: Panel, O: int, horizons: tuple[int, ...], tr: Trained
) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    """Lift every candidate to a predictive distribution. (S, H, Q) each.

    LightGBM quantile heads are native. Everything else is lifted through a
    Negative Binomial whose dispersion was fitted per (planning_class,
    horizon_group) on the meta origins.
    """
    points = candidate_points(P, O, horizons, tr)
    _, qhat, _ = tr.gbm.predict(P, O, horizons)

    pc = P.series["planning_class"].astype(str).to_numpy(dtype="U20")
    hg = np.array([horizon_group_of(h) for h in horizons], dtype="U12")
    strata = np.char.add(np.char.add(pc[:, None], "|"), np.broadcast_to(hg, (len(pc), len(hg))))

    quants: dict[str, np.ndarray] = {"lgbm_quantile": sanitize(qhat)}
    for name, mu in points.items():
        disp = tr.dispersion.get(name, {})
        if not disp:
            r = np.full(mu.shape, 8.0)
        else:
            default = float(np.median(list(disp.values())))
            r = np.vectorize(lambda s: disp.get(s, default))(strata).astype("float64")
        quants[name] = sanitize(nb_quantiles(mu, r))

    cap = plausibility_cap(P, O)[:, None, None]
    for name, q in quants.items():
        over = int((q > cap).sum())
        if over:
            CLAMP_HITS[name] = CLAMP_HITS.get(name, 0) + over
        quants[name] = np.minimum(q, cap)
        points[name] = np.minimum(points[name], cap[:, :, 0]) if name in points else None
    points = {k: v for k, v in points.items() if v is not None}
    return points, quants


def bundle_at(P: Panel, O: int, tr: Trained, horizons: tuple[int, ...]) -> Bundle:
    pts, qs = candidate_quantiles(P, O, horizons, tr)
    return Bundle(origin=O, horizons=horizons, points=pts, quantiles=qs)


# ------------------------------------------------------------------ run

def run(cfg: Config = CFG, quick: bool = False, ablate_censoring: bool = True,
        light: bool = False) -> dict:
    """Run every stage.

    `light` trims the rolling backtest to four origins and skips the cost
    sensitivity sweep. Used by the ablation runner: an ablation arm is asking
    whether a *stage* earns its place, which the eval-origin economics answer,
    whereas the eight-origin backtest and the 75-scenario sweep answer questions
    about origin stability and cost robustness that only need asking once.
    """
    t_start = time.perf_counter()
    timings: dict[str, float] = {}
    horizons = tuple(range(1, HORIZON + 1))

    # ---------------------------------------------------------- stage 0
    section("STAGE 0 — ingest and validate")
    t0 = time.perf_counter()
    ing = ingest.run()
    timings["stage0_ingest"] = time.perf_counter() - t0
    log(f"  panel {len(ing.panel):,} rows, {ing.panel['series_id'].nunique():,} series")
    for f in ing.feed_runs:
        bad = [c for c in f.checks if c.outcome != "pass"]
        log(f"  {f.table:14s} {f.outcome}" + (f"  ({len(bad)} non-pass)" if bad else ""))

    # ---------------------------------------------------------- stage 1
    section("STAGE 1 — diagnose each series")
    t0 = time.perf_counter()
    hist_panel = ing.panel[~ing.panel["is_future"]].reset_index(drop=True)
    diag, masks = diagnose.run(hist_panel, cfg)
    timings["stage1_diagnose"] = time.perf_counter() - t0
    log("  planning classes:")
    for k, v in diag["planning_class"].value_counts().items():
        log(f"    {k:<14s} {v:>5,}")
    log(f"  mean data-health score {diag['health_score'].mean():.3f}")

    truth = pd.read_parquet(INTERIM / "truth.parquet")
    truth["series_id"] = truth["item_id"].astype(str) + "|" + truth["store_id"].astype(str)
    cens = masks.merge(
        truth[["series_id", "date", "was_censored", "was_carried"]],
        on=["series_id", "date"], how="inner",
    ).merge(hist_panel[["series_id", "date", "sales"]], on=["series_id", "date"], how="left")
    cens_score = diagnose.score_censoring(cens, truth)
    log(f"  censoring detection: precision {cens_score['precision']:.3f}, "
        f"recall {cens_score['recall']:.3f} "
        f"(recall on full-zero days {cens_score['recall_full_zero']:.3f})")

    # ---------------------------------------------------------- stage 2
    section("STAGE 2 — featurize")
    t0 = time.perf_counter()
    P = build_panel(ing.panel, diag, masks, apply_mask=cfg.enable_censor_masking)
    sp = make_splits(P, horizon=HORIZON, n_train_origins=12 if quick else 32,
                     n_eval_origins=4 if light else 8)
    # Blocks come pre-purged from make_splits, which asserts that no block's
    # target dates reach into the next one's.
    meta_origins, conf_origins = sp.meta, sp.conformal
    timings["stage2_featurize"] = time.perf_counter() - t0
    log(f"  matrix {P.n_series} series x {P.n_days} days; "
        f"{np.isnan(P.y).mean():.1%} of cells masked as not-demand-signal")
    log("  " + sp.describe().replace("\n", "\n  "))
    log(f"  meta origins {len(meta_origins)}, conformal origins {len(conf_origins)}")

    # ---- static-feature vintages, one per origin block.
    #
    # Stage 1's outputs are *features*. Computed once over the full history and
    # attached to a training row at an earlier origin, they leak: a 2025 example
    # would carry that series' 2026 intermittency, health score, revenue and age.
    # It cannot leak the target value, only the series' character — which is
    # exactly the mild leakage that survives review and inflates a backtest.
    #
    # Each block therefore gets diagnostics computed as of its *first* origin, so
    # every static feature is strictly backward-looking. Later origins inside a
    # block see slightly stale diagnostics, which is fine: staleness is what a
    # production system has too, whereas foresight is not.
    ids = P.series[ID_COLS]
    vintages: dict[str, Panel] = {}
    for name, at in (("train", sp.train[0]), ("meta", sp.meta[0]),
                     ("conformal", sp.conformal[0]), ("eval", sp.evals[0])):
        d_v, _ = diagnose.run(hist_panel, cfg, asof=P.dates[at])
        vintages[name] = P.with_series(series_frame(ids, d_v))
        log(f"  diagnostics vintage {name:<10s} as of {P.dates[at].date()}")
    vintages["live"] = P            # full history is correct for the served forecast
    log(f"  class churn train->live "
        f"{(vintages['train'].series['planning_class'].to_numpy() != P.series['planning_class'].to_numpy()).mean():.1%}")

    # ---------------------------------------------------------- stage 3
    section("STAGE 3 — train the candidate pool")
    t0 = time.perf_counter()
    # Early-stopping validation origins must be purged from the fitting origins by
    # a full horizon. Taking the last three origins as validation while fitting on
    # everything up to them leaves the final fitting origins forecasting 28 days
    # forward into the validation targets, so early stopping was choosing tree
    # counts against targets the model had partly been trained on.
    n_purge = max(1, HORIZON // 7)
    valid_for_es = sp.train[-3:]
    fit_origins = sp.train[: -(3 + n_purge)]
    log(f"  early-stopping split: fit on {len(fit_origins)} origins, "
        f"validate on {len(valid_for_es)}, {n_purge} purged between")

    log("  LightGBM (Tweedie point, category-segmented + 9 quantile heads)")
    P_tr = vintages["train"]
    gbm = GBMPool().fit(P_tr, fit_origins, valid_for_es,
                        respect_regimes=cfg.enable_regime_windows)

    log("  zero-shot prior (covariate-only global model, foundation-model slot)")
    zs = foundation.ZeroShotPrior().fit(P_tr, fit_origins,
                                        respect_regimes=cfg.enable_regime_windows)

    log("  NHITS (neural candidate)")
    nn = neural.NeuralCandidate(horizon=HORIZON).fit(
        P_tr, max_origin=sp.train[-1], n_samples=20_000 if quick else 60_000
    )

    log("  cohort lifecycle curves")
    cohorts = lifecycle.CohortCurves().fit(P_tr, max_origin=sp.train[-1])
    log(f"      {len(cohorts.curves)} cohort curves fitted")

    gbm_nomask = None
    if ablate_censoring:
        log("  ablation arm: Tweedie trained on unmasked sales (stockout zeros kept)")
        P_nomask = build_panel(ing.panel, diag, masks, apply_mask=False)
        P_nomask = P_nomask.with_series(vintages["train"].series)
        gbm_nomask = GBMPool().fit(P_nomask, fit_origins, valid_for_es,
                                   verbose=False, with_quantiles=False,
                                   respect_regimes=cfg.enable_regime_windows)

    tr = Trained(gbm=gbm, gbm_nomask=gbm_nomask, zeroshot=zs, nn=nn, cohorts=cohorts,
                 P_nomask=(P_nomask if ablate_censoring else None))
    timings["stage3_train"] = time.perf_counter() - t0
    log(f"  trained in {timings['stage3_train']:.0f}s")

    # ---- dispersion fitting on meta origins
    log("  fitting NB dispersion per (class, horizon group) on meta origins")
    pc = P.series["planning_class"].astype(str).to_numpy(dtype="U20")
    hg_arr = np.array([horizon_group_of(h) for h in horizons], dtype="U12")
    acc_actual, acc_points = [], {}
    for O in meta_origins:
        y = np.stack([P.y[:, O + h] for h in horizons], axis=1)
        acc_actual.append(y)
        for name, mu in candidate_points(vintages["meta"], O, horizons, tr).items():
            acc_points.setdefault(name, []).append(mu)
    A = np.concatenate(acc_actual, axis=0)
    strata_2d = np.char.add(
        np.char.add(np.tile(pc, len(meta_origins))[:, None], "|"),
        np.broadcast_to(hg_arr, (len(A), len(hg_arr))),
    )
    for name, chunks in acc_points.items():
        M = np.concatenate(chunks, axis=0)
        tr.dispersion[name] = fit_dispersion(A.ravel(), M.ravel(), strata_2d.ravel())
    log(f"  dispersion strata fitted for {len(tr.dispersion)} candidates")

    # ---------------------------------------------------------- stage 4
    section("STAGE 4 — combine (FFORMA meta-learner)")
    t0 = time.perf_counter()
    loss_frames = []
    q_by_origin: dict[int, dict[str, np.ndarray]] = {}
    for O in meta_origins:
        y = np.stack([P.y[:, O + h] for h in horizons], axis=1)
        _, qs = candidate_quantiles(vintages["meta"], O, horizons, tr)
        qs = {k: v for k, v in qs.items() if k in ENSEMBLE}
        q_by_origin[O] = qs
        loss_frames.append(comb.candidate_losses(y, qs, np.isfinite(y)))
    # nanmean across origins, not a plain sum. A series whose targets are entirely
    # masked at one origin gets NaN there, and `sum()` propagates it, so that
    # series' loss row became NaN for every candidate and MetaLearner.fit dropped
    # it — silently excluding ~15% of the panel from the weights that are then
    # applied to it.
    losses = pd.concat(loss_frames).groupby(level=0).mean()
    losses = losses.reindex(range(P.n_series))
    losses.index = P.series.index

    # The meta-learner declares 19 features, but P.series carries only the subset
    # build_panel copies across, so two of them (longest_zero_run, iqr_over_median)
    # were silently absent and the learner trained on 17 while reporting 19. Join
    # the missing columns from the diagnostics frame itself.
    extra = [c for c in comb.META_FEATURES
             if c not in vintages["meta"].series.columns and c in diag.columns]
    diag_aligned = vintages["meta"].series.copy()
    if extra:
        d_meta, _ = diagnose.run(hist_panel, cfg, asof=P.dates[sp.meta[0]])
        diag_aligned = diag_aligned.merge(
            d_meta[["series_id", *extra]].assign(
                series_id=lambda d: d["series_id"].astype(str)),
            on="series_id", how="left",
        )
        log(f"  meta-features restored from diagnostics: {extra}")
    meta = comb.MetaLearner().fit(diag_aligned, losses, cfg)
    W_fforma = meta.weights(diag_aligned, cfg)

    # Convex stacking on the loss of the combination actually issued, fitted on the
    # same meta origins. Both arms are then scored on those origins; the arm chosen
    # is the one that wins there, and the eval origins remain untouched
    # out-of-sample. Choosing on the eval origins would be selecting a method on the
    # data used to report it.
    y_meta = np.concatenate(
        [np.stack([P.y[:, O + h] for h in horizons], axis=1).ravel() for O in meta_origins]
    )
    q_meta = {
        name: np.concatenate([q_by_origin[O][name].reshape(-1, len(QUANTILES))
                              for O in meta_origins])
        for name in meta.candidates
    }
    strata_meta = np.concatenate(
        [np.repeat(pc, len(horizons)) for _ in meta_origins]
    )
    valid_meta = np.isfinite(y_meta)

    stack_tbl, _ = comb.stacked_weights(
        y_meta, q_meta, meta.candidates, valid_meta, strata_meta
    )
    W_stack = comb.weights_from_strata(pc, stack_tbl, len(meta.candidates))

    def _meta_loss(Wm: np.ndarray) -> float:
        tot, n = 0.0, 0
        for O in meta_origins:
            yy = np.stack([P.y[:, O + h] for h in horizons], axis=1)
            cq = comb.combine_quantiles(q_by_origin[O], Wm, meta.candidates)
            pl = comb.pinball(yy, cq)
            v = np.isfinite(yy)
            tot += float(np.nansum(np.where(v, pl, np.nan)))
            n += int(v.sum())
        return tot / max(n, 1)

    l_ff, l_st = _meta_loss(W_fforma), _meta_loss(W_stack)
    best_single = min(
        (float(np.nanmean(comb.pinball(
            np.stack([P.y[:, O + h] for h in horizons], axis=1), q_by_origin[O][c])))
         for c in meta.candidates for O in [meta_origins[0]]),
    )
    log(f"  combined pinball on the meta origins (the weights' own objective):")
    log(f"    FFORMA proxy weights   {l_ff:.5f}")
    log(f"    convex stack weights   {l_st:.5f}")
    log(f"    -> using {'stack' if l_st <= l_ff else 'fforma'} weights")

    if cfg.enable_combination:
        W = W_stack if (cfg.combine.method == "stack" and l_st <= l_ff) else W_fforma
    else:
        W = meta.select_weights(diag_aligned, losses)
        log("  ablation: selection instead of combination (one-hot argmin weights)")
    timings["stage4_combine"] = time.perf_counter() - t0

    wrep = comb.weight_report(W, meta.candidates, diag_aligned)
    log("  mean ensemble weight by planning class:")
    log("    " + wrep.to_string().replace("\n", "\n    "))
    eff = float(np.exp(-(W * np.log(np.clip(W, 1e-12, None))).sum(axis=1)).mean())
    log(f"  mean effective candidates per series {eff:.2f} of {len(meta.candidates)}")

    # ---------------------------------------------------------- stage 5
    section("STAGE 5 — reconcile")
    t0 = time.perf_counter()
    # Every meta origin contributes residuals. Even so, the node count (~1,200)
    # exceeds the residual sample, so the Schafer-Strimmer intensity will be
    # high and MinT(shrink) will sit close to MinT(WLS). That is the honest
    # outcome at this scale and the intensity is reported rather than hidden.
    rec_origins = meta_origins
    rc = reconcile.Reconciler()
    rc.H = reconcile.build_hierarchy(P.series)

    # W must be the covariance of the errors of the vector actually reconciled —
    # the independent base forecast at every node. Projecting bottom-level errors up
    # through S instead gives the covariance of bottom-up-implied aggregate errors,
    # which presumes exactly the coherence reconciliation exists to impose and which
    # the independent base forecasts do not satisfy.
    node_res = []
    for O in rec_origins:
        y = np.stack([P.y[:, O + h] for h in horizons], axis=1)
        y_nodes = rc.H.S @ np.nan_to_num(y, nan=0.0)                # (nodes, H)
        nb = rc.base_forecasts(P.y, O, horizons)                    # (nodes, H)
        bottom_med = comb.combine_quantiles(
            q_by_origin[O], W, meta.candidates)[..., Q50_INDEX]
        nb = nb.copy()
        nb[rc.H.bottom_slice()] = bottom_med                        # as reconcile() does
        node_res.append((nb - y_nodes).T)                           # (H, nodes)
    rc.fit(P.series, node_residuals=np.concatenate(node_res, axis=0), cfg=cfg)
    timings["stage5_reconcile"] = time.perf_counter() - t0
    log(f"  hierarchy {rc.H.n_nodes} nodes over {rc.H.n_bottom} bottom series, "
        f"{len(reconcile.LEVELS)} levels")
    log(f"  MinT shrinkage intensity {rc.shrinkage:.4f}")
    log(f"  projection error |G.S - I|max {rc.projection_error:.2e}"
        f"{'  (OK)' if rc.projection_error < 1e-6 else '  (WARNING: ill-conditioned)'}")

    # ---------------------------------------------------------- stage 6
    section("STAGE 6 — calibrate (split conformal / CQR)")
    t0 = time.perf_counter()
    cy, cq_list, cpc, chg = [], [], [], []
    for O in conf_origins:
        y = np.stack([P.y[:, O + h] for h in horizons], axis=1)
        _, qs = candidate_quantiles(vintages["conformal"], O, horizons, tr)
        cqq = comb.combine_quantiles(qs, W, meta.candidates)
        if cfg.enable_reconciliation:
            cqq = rc.reconcile_quantiles(cqq, rc.base_forecasts(P.y, O, horizons))
        cy.append(y.ravel())
        cq_list.append(cqq.reshape(-1, len(QUANTILES)))
        cpc.append(np.repeat(pc, len(horizons)))
        chg.append(np.tile(hg_arr, len(pc)))
    cy_a = np.concatenate(cy)
    cq_a = np.concatenate(cq_list)
    cpc_a = np.concatenate(cpc)
    chg_a = np.concatenate(chg)
    conformal = cal_mod.ConformalCalibrator().fit(
        cy_a, cq_a, cpc_a, chg_a, valid=np.isfinite(cy_a), cfg=cfg
    )
    timings["stage6_calibrate"] = time.perf_counter() - t0
    log(f"  strata fitted {len(conformal.offsets) - 1} + global "
        f"({len(cy_a):,} calibration points)")
    adj = conformal.adjustment_report()
    log("  per-level adjustment (offsets validated on a held-out half):")
    log("    " + adj.round(3).to_string(index=False).replace("\n", "\n    "))

    # ---------------------------------------------------------- eval origin
    section("EVALUATION — eval origin, actuals known")
    t0 = time.perf_counter()
    Oe = sp.eval
    y_eval = np.stack([P.y[:, Oe + h] for h in horizons], axis=1)
    valid_eval = np.isfinite(y_eval)
    pts_e, qs_e = candidate_quantiles(vintages["eval"], Oe, horizons, tr)

    comb_e = comb.combine_quantiles(qs_e, W, meta.candidates)
    node_base_e = rc.base_forecasts(P.y, Oe, horizons)
    recon_e = (rc.reconcile_quantiles(comb_e, node_base_e)
               if cfg.enable_reconciliation else comb_e)
    pc_2d = np.repeat(pc[:, None], len(horizons), axis=1)
    hg_2d = np.broadcast_to(hg_arr, (len(pc), len(horizons)))
    calib_e = conformal.apply(recon_e, pc_2d, hg_2d) if cfg.enable_calibration else recon_e

    # The decision path. Reconciliation is coherent and more accurate at every
    # aggregate level, and less accurate at the leaves — and the order-up-to level is
    # a leaf-level decision, so it reads the unreconciled distribution. See
    # ReconcileConfig.for_decisions for the measurements behind this.
    decide_src = calib_e if cfg.reconcile.for_decisions else (
        conformal.apply(comb_e, pc_2d, hg_2d) if cfg.enable_calibration else comb_e
    )
    log(f"  decision path reads the "
        f"{'reconciled' if cfg.reconcile.for_decisions else 'unreconciled'} distribution")

    # weights and scalers
    tail = hist_panel[(hist_panel["date"] > P.dates[Oe] - pd.Timedelta(days=28))
                      & (hist_panel["date"] <= P.dates[Oe])]
    wts = evaluate.m5_weights(tail).reindex(P.series["series_id"]).fillna(0.0).to_numpy()
    # NaN is passed through deliberately: scale_denominator uses nanmean, so masked
    # days are excluded rather than read as a crash to zero and back.
    y_train_win = P.y[:, max(0, Oe - 365) : Oe + 1].astype("float64")
    denom = evaluate.scale_denominator(y_train_win)
    denom_abs = np.nanmean(np.abs(np.diff(y_train_win, axis=1)), axis=1)

    med = lambda q: q[..., Q50_INDEX]
    stage_forecasts = {
        "0 seasonal naive (anchor)": pts_e["seasonal_naive"],
        "1 best single (LGBM-Q)": med(qs_e["lgbm_quantile"]),
        "2 + combination": med(comb_e),
        "3 + reconciliation": med(recon_e),
        "4 + calibration": med(calib_e),
    }
    fva = evaluate.forecast_value_added(
        y_eval, stage_forecasts, "0 seasonal naive (anchor)", denom, wts, valid_eval
    )
    log("  FVA ladder (WRMSSE, lower is better):")
    log("    " + fva.round(4).to_string(index=False).replace("\n", "\n    "))

    cov_before = conformal.coverage_table(
        y_eval.ravel(), recon_e.reshape(-1, len(QUANTILES)),
        calib_e.reshape(-1, len(QUANTILES)), valid=valid_eval.ravel()
    )
    log("\n  coverage, nominal vs empirical, before and after conformal:")
    log("    " + cov_before.round(4).to_string(index=False).replace("\n", "\n    "))

    per_class_fva = evaluate.fva_by_class(
        y_eval, med(calib_e), pts_e["seasonal_naive"], pc, denom, wts, valid_eval
    )
    log("\n  FVA by planning class:")
    log("    " + per_class_fva.round(4).to_string(index=False).replace("\n", "\n    "))

    # candidate leaderboard
    board = []
    for name in ENSEMBLE:
        board.append({
            "method": name,
            "wrmsse": evaluate.wrmsse(y_eval, med(qs_e[name]), denom, wts, valid_eval),
            "wspl": evaluate.wspl(y_eval, qs_e[name], denom, wts, valid_eval),
            "mase": float(np.nanmean(evaluate.mase(y_eval, med(qs_e[name]), denom_abs, valid_eval))),
            "bias": evaluate.bias(y_eval, med(qs_e[name]), valid_eval),
        })
    board.append({
        "method": "DQ-Stack (full)",
        "wrmsse": evaluate.wrmsse(y_eval, med(calib_e), denom, wts, valid_eval),
        "wspl": evaluate.wspl(y_eval, calib_e, denom, wts, valid_eval),
        "mase": float(np.nanmean(evaluate.mase(y_eval, med(calib_e), denom_abs, valid_eval))),
        "bias": evaluate.bias(y_eval, med(calib_e), valid_eval),
    })
    leaderboard = pd.DataFrame(board).sort_values("wspl")
    log("\n  candidate leaderboard:")
    log("    " + leaderboard.round(4).to_string(index=False).replace("\n", "\n    "))
    timings["evaluate"] = time.perf_counter() - t0

    # ---------------------------------------------------------- rolling backtest
    section(f"ROLLING BACKTEST — {len(sp.evals)} evaluation origins")
    t0 = time.perf_counter()

    tmat_all = truth.pivot_table(index="series_id", columns="date",
                                 values="latent_demand", aggfunc="first")                     .reindex(P.series["series_id"])

    items_bt = ing.items.set_index("item_id")
    it_bt = P.series["item_id"].astype(str).to_numpy()
    price_bt = np.nan_to_num(P.series["avg_price"].to_numpy(dtype="float64"), nan=3.0)
    price_bt = np.where(price_bt <= 0.05, 3.0, price_bt)
    cp_bt = dec.CostParams(
        price=price_bt,
        unit_cost=items_bt.loc[it_bt, "unit_cost"].to_numpy(),
        margin_rate=items_bt.loc[it_bt, "margin_rate"].to_numpy(),
        shelf_life=items_bt.loc[it_bt, "shelf_life_days"].to_numpy().astype("float64"),
    )

    bt_rows, bt_cov, bt_stages, bt_hier = [], [], [], []
    for O in sp.evals:
        # Diagnostics dated at *this* origin, not at the block start. Dating the
        # whole block at its first origin is backward-looking and safe, but it also
        # means an item that launches mid-block is never seen as young: the
        # cold_start class was empty at every eval origin, so the lifecycle curves
        # and the zero-shot prior — the two candidates that exist for exactly that
        # case — were never actually exercised by the evaluation.
        d_o, _ = diagnose.run(hist_panel, cfg, asof=P.dates[O])
        P_o = P.with_series(series_frame(ids, d_o))
        rows, cov, stage, hier = bt.evaluate_origin(
            O, P, P_o, tr, W, meta, rc, conformal, cp_bt,
            tmat_all, hist_panel, horizons, ENSEMBLE, cfg,
        )
        bt_rows.extend(rows)
        bt_cov.append(cov)
        bt_stages.append(stage)
        bt_hier.append(hier)
        n_cold = int((d_o["planning_class"] == "cold_start").sum())
        log(f"    classes at this origin: cold_start {n_cold}, "
            f"terminal {int((d_o['planning_class'] == 'terminal').sum())}")
        log(f"  origin {P.dates[O].date()}  "
            f"system WSPL {[r for r in rows if r['method'] == bt.SYSTEM][0]['wspl']:.4f}")

    per_origin = pd.DataFrame(bt_rows)
    hier_all = pd.concat(bt_hier, ignore_index=True)
    bt_summary = bt.summarise(per_origin, pd.DataFrame(bt_stages))
    bt_summary["hierarchy"] = bt.summarise_hierarchy(hier_all)
    log("")
    bt.report(per_origin, bt_summary, log=log)
    log("\n  --- Stage 5 judged across the hierarchy, not only at the leaves ---")
    bt.report_hierarchy(bt_summary["hierarchy"], log=log)
    bt.persist(per_origin, bt_summary)
    hier_all.to_csv(ARTIFACTS / "backtest_hierarchical.csv", index=False)
    pd.concat(bt_cov, ignore_index=True).to_csv(
        ARTIFACTS / "backtest_coverage.csv", index=False)
    timings["backtest"] = time.perf_counter() - t0

    # ---------------------------------------------------------- stage 7
    section("STAGE 7 — decide (critical fractile, simulator, ledger)")
    t0 = time.perf_counter()
    items = ing.items.set_index("item_id")
    it = P.series["item_id"].astype(str).to_numpy()
    price = np.nan_to_num(P.series["avg_price"].to_numpy(dtype="float64"), nan=3.0)
    price = np.where(price <= 0.05, 3.0, price)
    cp = dec.CostParams(
        price=price,
        unit_cost=items.loc[it, "unit_cost"].to_numpy(),
        margin_rate=items.loc[it, "margin_rate"].to_numpy(),
        shelf_life=items.loc[it, "shelf_life_days"].to_numpy().astype("float64"),
    )

    # True demand over the eval horizon — the simulator must see demand, not
    # censored sales, or lost sales become invisible.
    tmat = truth.pivot_table(index="series_id", columns="date", values="latent_demand",
                             aggfunc="first").reindex(P.series["series_id"])
    demand_eval = tmat[[P.dates[Oe + h] for h in horizons]].to_numpy(dtype="float64")
    demand_eval = np.nan_to_num(demand_eval)

    sids = P.series["series_id"].to_numpy()
    eval_dates = pd.DatetimeIndex([P.dates[Oe + h] for h in horizons])
    res_full, frac_full = dec.decide(decide_src, demand_eval, cp, sids, eval_dates, cfg)
    log(f"  target quantile: mean {frac_full['target_quantile'].mean():.3f}, "
        f"range {frac_full['target_quantile'].min():.3f}–{frac_full['target_quantile'].max():.3f}")
    log(f"  ledger rows {len(res_full.ledger):,}")
    for k in ("fill_rate", "cycle_service_level", "realized_margin", "lost_margin",
              "holding_cost", "waste_cost", "conservation_residual", "min_on_hand"):
        v = res_full.totals[k]
        log(f"    {k:24s} {v:,.2f}")

    # ---- economic comparison across policies and forecasts
    econ_rows = []

    def econ(name: str, q: np.ndarray, cfg_use=cfg):
        r, f = dec.decide(q, demand_eval, cp, sids, eval_dates, cfg_use)
        econ_rows.append({
            "method": name,
            "wrmsse": evaluate.wrmsse(y_eval, med(q), denom, wts, valid_eval),
            "realized_margin": r.totals["realized_margin"],
            "fill_rate": r.totals["fill_rate"],
            "lost_margin": r.totals["lost_margin"],
            "holding_cost": r.totals["holding_cost"],
            "waste_cost": r.totals["waste_cost"],
            "avg_inventory": r.totals["avg_inventory"],
            "mean_target_q": float(f["target_quantile"].mean()),
        })
        return r

    econ("DQ-Stack (full)", decide_src)
    econ("no calibration", recon_e)
    econ("no reconciliation", comb_e)
    econ("best single (LGBM-Q)", qs_e["lgbm_quantile"])
    econ("Tweedie only", qs_e["lgbm_tweedie"])
    econ("seasonal naive", qs_e["seasonal_naive"])
    econ("Croston/SBA only", qs_e["croston_sba"])

    cfg_blanket = replace(cfg, enable_critical_fractile=False)
    econ("full, blanket 95% service", decide_src, cfg_blanket)

    if tr.gbm_nomask is not None:
        # Predict from the panel it was TRAINED on. Predicting from the masked
        # panel made this a label-side ablation only, and gave it a train/serve
        # feature mismatch on every lag and rolling statistic — so the arm was
        # measuring two things at once and attributing both to censoring.
        P_nm = (
            tr.P_nomask.with_series(vintages["eval"].series)
            if tr.P_nomask is not None
            else P
        )
        pnm, _, _ = tr.gbm_nomask.predict(P_nm, Oe, horizons)
        disp_nm = tr.dispersion.get("lgbm_tweedie", {})
        default = float(np.median(list(disp_nm.values()))) if disp_nm else 8.0
        strata2 = np.char.add(np.char.add(pc[:, None], "|"), hg_2d)
        r_nm = np.vectorize(lambda s: disp_nm.get(s, default))(strata2).astype("float64")
        q_nm = sanitize(nb_quantiles(np.nan_to_num(pnm), r_nm))
        econ("no censoring correction", q_nm)

    econ_df = pd.DataFrame(econ_rows)
    log("\n  economics by configuration:")
    log("    " + econ_df.round(3).to_string(index=False).replace("\n", "\n    "))

    inv = evaluate.accuracy_vs_economics(econ_df)
    log(f"\n  accuracy vs economics: Spearman rho {inv['spearman_rho']:.3f} "
        f"(p={inv['p_value']:.4f}) across {inv['n_methods']} configurations")
    if inv["inversions"]:
        log(f"  {len(inv['inversions'])} rank inversions found. Largest:")
        for d in inv["inversions"][:3]:
            log(f"    '{d['more_accurate']}' is more accurate but "
                f"'{d['more_profitable']}' earns {d['margin_gap']:,.0f} more margin")
    else:
        log("  no rank inversions at this origin")
    timings["stage7_decide"] = time.perf_counter() - t0

    # ---------------------------------------------------------- cost sensitivity
    sens_sum = {}
    if not light:
        section("COST SENSITIVITY — is the critical fractile robust to its assumptions?")
        t0 = time.perf_counter()
        sens_tbl = sens.sweep(decide_src, demand_eval, cp, sids, eval_dates, cfg)
        sens_sum = sens.summarise(sens_tbl)
        sens.report(sens_tbl, sens_sum, log=log)
        sens.persist(sens_tbl, sens_sum)
        timings["sensitivity"] = time.perf_counter() - t0

    # ---------------------------------------------------------- live origin
    section("LIVE FORECAST — final origin, served to the UI")
    t0 = time.perf_counter()
    Ol = sp.live
    pts_l, qs_l = candidate_quantiles(P, Ol, horizons, tr)
    comb_l = comb.combine_quantiles(qs_l, W, meta.candidates)
    recon_l = (rc.reconcile_quantiles(comb_l, rc.base_forecasts(P.y, Ol, horizons))
               if cfg.enable_reconciliation else comb_l)
    calib_l = conformal.apply(recon_l, pc_2d, hg_2d) if cfg.enable_calibration else recon_l

    decide_l = calib_l if cfg.reconcile.for_decisions else (
        conformal.apply(comb_l, pc_2d, hg_2d) if cfg.enable_calibration else comb_l
    )
    risk = cfg.decision.review_period + cfg.decision.lead_time
    frac_l = dec.critical_fractile(cp, risk, cfg)
    S_star_l = dec.risk_period_quantile(decide_l[:, :risk, :], frac_l["target_quantile"])
    frac_l["order_up_to"] = S_star_l
    wk_base, mo_base = rc.temporal_base(P.y, Ol, HORIZON)
    temporal = reconcile.temporal_reconcile(med(calib_l), wk_base, mo_base)
    log(f"  {P.n_series:,} series x {HORIZON}-day horizon x {len(QUANTILES)} quantiles")
    # A clamp that fires often means a candidate is broken, so it is reported
    # rather than counted into a dict nobody reads. Silence here was how a
    # 5,000x lifecycle forecast reached the ensemble in the first place.
    if CLAMP_HITS:
        tot = sum(CLAMP_HITS.values())
        worst = sorted(CLAMP_HITS.items(), key=lambda kv: -kv[1])[:4]
        log(f"  plausibility clamp bound {tot:,} quantile values across the run: "
            + ", ".join(f"{k} {v:,}" for k, v in worst))
    else:
        log("  plausibility clamp never bound (no candidate produced an implausible value)")
    log(f"  temporal consistency: |sum(daily) - monthly| max "
        f"{np.abs(temporal['daily'].sum(axis=1) - temporal['monthly'][:, 0]).max():.2e}")
    log(f"  recommended order units {S_star_l.sum():,.0f}")
    timings["live_forecast"] = time.perf_counter() - t0

    timings["total"] = time.perf_counter() - t_start

    # ---------------------------------------------------------- persist
    section("ARTIFACTS")
    res_full.ledger.to_parquet(ARTIFACTS / "decision_ledger.parquet", index=False)
    leaderboard.to_csv(ARTIFACTS / "leaderboard.csv", index=False)
    fva.to_csv(ARTIFACTS / "fva_ladder.csv", index=False)
    per_class_fva.to_csv(ARTIFACTS / "fva_by_class.csv", index=False)
    cov_before.to_csv(ARTIFACTS / "coverage_table.csv", index=False)
    econ_df.to_csv(ARTIFACTS / "economics.csv", index=False)
    wrep.to_csv(ARTIFACTS / "ensemble_weights_by_class.csv")

    metrics = {
        "generated_at": pd.Timestamp.utcnow().isoformat(timespec="seconds"),
        "n_series": int(P.n_series),
        "n_panel_rows": int(len(ing.panel)),
        "horizon": HORIZON,
        "quantiles": list(QUANTILES),
        "origins": {
            "train": [str(P.dates[o].date()) for o in sp.train],
            "meta": [str(P.dates[o].date()) for o in meta_origins],
            "conformal": [str(P.dates[o].date()) for o in conf_origins],
            "eval": str(P.dates[Oe].date()),
            "live": str(P.dates[Ol].date()),
        },
        "censoring_detection": cens_score,
        "leaderboard": leaderboard.to_dict("records"),
        "fva_ladder": fva.to_dict("records"),
        "fva_by_class": per_class_fva.to_dict("records"),
        "coverage": cov_before.to_dict("records"),
        "economics": econ_df.to_dict("records"),
        "accuracy_vs_economics": {k: v for k, v in inv.items() if k != "table"},
        "ensemble_weights_by_class": wrep.to_dict("index"),
        "mean_effective_candidates": eff,
        "plausibility_clamp_hits": dict(CLAMP_HITS),
        "mint_shrinkage": rc.shrinkage,
        "mint_projection_error": rc.projection_error,
        "simulator_totals": res_full.totals,
        "backtest": bt_summary,
        "cost_sensitivity": sens_sum,
        "timings_seconds": {k: round(v, 2) for k, v in timings.items()},
        "config": {
            "enable_censor_masking": cfg.enable_censor_masking,
            "enable_regime_windows": cfg.enable_regime_windows,
            "enable_combination": cfg.enable_combination,
            "enable_reconciliation": cfg.enable_reconciliation,
            "enable_calibration": cfg.enable_calibration,
            "enable_critical_fractile": cfg.enable_critical_fractile,
            "review_period": cfg.decision.review_period,
            "lead_time": cfg.decision.lead_time,
        },
    }
    (ARTIFACTS / "metrics.json").write_text(json.dumps(metrics, indent=2, default=str))
    log(f"  wrote {ARTIFACTS}")

    state = {
        "P": P, "diag": diag, "masks": masks, "panel": ing.panel, "items": ing.items,
        "splits": sp, "trained": tr, "meta": meta, "weights": W, "reconciler": rc,
        "conformal": conformal, "metrics": metrics, "cost": cp,
        "eval": {"origin": Oe, "y": y_eval, "valid": valid_eval, "candidates": qs_e,
                 "combined": comb_e, "reconciled": recon_e, "calibrated": calib_e,
                 "fractile": frac_full, "sim": res_full, "leaderboard": leaderboard},
        "live": {"origin": Ol, "candidates": qs_l, "calibrated": calib_l,
                 "fractile": frac_l, "temporal": temporal, "points": pts_l},
        "truth": truth,
    }

    log()
    log(f"  total runtime {timings['total']:.0f}s")
    for k, v in timings.items():
        if k != "total":
            log(f"    {k:22s} {v:>7.1f}s")
    return state


def _utf8_stdout() -> None:
    """Make stdout UTF-8 regardless of platform default.

    Redirected stdout on Windows defaults to cp1252, so a single non-ASCII character
    in a log line raises UnicodeEncodeError and takes down a long-running job at the
    point it tries to report progress. That killed a 25-minute ablation arm on an
    en-dash in a label.
    """
    import sys

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main() -> None:
    _utf8_stdout()
    ap = argparse.ArgumentParser(description="Run the DQ-Stack pipeline end to end.")
    ap.add_argument("--quick", action="store_true", help="fewer origins and epochs")
    ap.add_argument("--no-ablate", action="store_true", help="skip the censoring ablation arm")
    ap.add_argument("--export", action="store_true", help="write the UI data bundle")
    a = ap.parse_args()
    state = run(quick=a.quick, ablate_censoring=not a.no_ablate)
    if a.export:
        from .export import export_bundle
        export_bundle(state)


if __name__ == "__main__":
    main()
