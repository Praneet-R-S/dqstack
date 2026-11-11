"""Rolling-origin backtest with paired inference.

Why this exists. Every number in a single-origin evaluation is one draw. The
central claim of Stage 4 — that combining candidates beats selecting the best one
— is a claim about *variance*: selection is a high-variance decision made on noisy
validation estimates, and combination trades a little bias for less variance. A
comparison at one origin cannot distinguish "combination is better" from
"selection got lucky this month", and reporting one origin would be arguing for a
variance reduction using a sample of size one.

So the same trained system is evaluated at every origin in the eval block and the
methods are compared *pairwise within origin*. Pairing matters: origins differ
enormously in difficulty — one contains a holiday, another a promotion — and that
shared difficulty is precisely the noise a paired test removes. An unpaired
comparison of means across origins would be swamped by it.

What is held fixed. Models are trained once, on the training block, and evaluated
at every eval origin. This is a rolling-*evaluation* backtest, not a rolling-
retraining one: it measures how a fixed system behaves as the forecast origin
moves, which is the question here. It does not measure the value of retraining
weekly, and the results must not be read as if it did.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats

from . import combine as comb
from . import decide as dec
from . import evaluate
from .config import ARTIFACTS, CFG, Q50_INDEX, QUANTILES

SYSTEM = "DQ-Stack (full)"


# ------------------------------------------------------------------ one origin

def evaluate_origin(
    O: int,
    P,
    P_vintage,
    tr,
    W: np.ndarray,
    meta,
    rc,
    conformal,
    cp,
    demand_truth: pd.DataFrame,
    hist_panel: pd.DataFrame,
    horizons: tuple[int, ...],
    ensemble: list[str],
    cfg=CFG,
    conformal_orders=None,
) -> tuple[list[dict], pd.DataFrame, dict, pd.DataFrame]:
    """Metrics for every candidate and for the assembled system at one origin.

    `conformal` calibrates the reconciled forecast; `conformal_orders`, the forecast
    orders read (defaults to `conformal`). The coverage table has a `path` column,
    "reconciled" and "orders", one set of rows each.
    """
    if conformal_orders is None:
        conformal_orders = conformal
    from .pipeline import candidate_quantiles

    y = np.stack([P.y[:, O + h] for h in horizons], axis=1)
    valid = np.isfinite(y)
    med = lambda q: q[..., Q50_INDEX]

    pts, qs = candidate_quantiles(P_vintage, O, horizons, tr)
    cq = comb.combine_quantiles(qs, W, meta.candidates)
    rec = (rc.reconcile_quantiles(cq, rc.base_forecasts(P.y, O, horizons))
           if cfg.enable_reconciliation else cq)

    pc = P.series["planning_class"].astype(str).to_numpy(dtype="U20")
    from .features import horizon_group_of
    hg = np.array([horizon_group_of(h) for h in horizons], dtype="U12")
    pc2 = np.repeat(pc[:, None], len(horizons), axis=1)
    hg2 = np.broadcast_to(hg, (len(pc), len(horizons)))
    cal = conformal.apply(rec, pc2, hg2) if cfg.enable_calibration else rec
    # What the system actually issues for ordering. See ReconcileConfig.for_decisions.
    decide_src = cal if cfg.reconcile.for_decisions else (
        conformal_orders.apply(cq, pc2, hg2) if cfg.enable_calibration else cq
    )

    # Weights and scalers are recomputed per origin: an M5 dollar-volume weight is
    # a property of the 28 days before the origin, not a constant.
    tail = hist_panel[(hist_panel["date"] > P.dates[O] - pd.Timedelta(days=28))
                      & (hist_panel["date"] <= P.dates[O])]
    wts = evaluate.m5_weights(tail).reindex(P.series["series_id"]).fillna(0.0).to_numpy()
    train_win = P.y[:, max(0, O - 365) : O + 1].astype("float64")
    denom = evaluate.scale_denominator(train_win)
    denom_abs = np.nanmean(np.abs(np.diff(train_win, axis=1)), axis=1)

    def row(name: str, q: np.ndarray) -> dict:
        return {
            "origin": str(P.dates[O].date()),
            "method": name,
            "wrmsse": evaluate.wrmsse(y, med(q), denom, wts, valid),
            "wspl": evaluate.wspl(y, q, denom, wts, valid),
            "mase": float(np.nanmean(evaluate.mase(y, med(q), denom_abs, valid))),
            "bias": evaluate.bias(y, med(q), valid),
            # Plain-language figures for a client: total absolute error over total
            # sales, and how often the outcome fell inside the stated 5-95% range.
            "wape": _wape(y, med(q), valid),
            "inside_range": _inside(y, q, valid),
        }

    rows = [row(n, qs[n]) for n in ensemble if n in qs]
    rows.append(row(SYSTEM, decide_src))
    rows.append(row("no calibration", rec))
    rows.append(row("no reconciliation", cq))

    # ---- economics, against true demand over this origin's horizon
    dates = pd.DatetimeIndex([P.dates[O + h] for h in horizons])
    dm = np.nan_to_num(demand_truth[list(dates)].to_numpy(dtype="float64"))
    sids = P.series["series_id"].to_numpy()

    econ = {}
    for name, q in (("DQ-Stack (full)", decide_src), ("no calibration", rec),
                    ("best single (LGBM-Q)", qs["lgbm_quantile"]),
                    ("seasonal naive", qs["seasonal_naive"])):
        res, _ = dec.decide(q, dm, cp, sids, dates, cfg)
        econ[name] = {
            "realized_margin": res.totals["realized_margin"],
            "fill_rate": res.totals["fill_rate"],
            "lost_margin": res.totals["lost_margin"],
        }
    for r in rows:
        e = econ.get(r["method"])
        if e:
            r.update(e)

    cov = conformal.coverage_table(
        y.ravel(), rec.reshape(-1, len(QUANTILES)),
        cal.reshape(-1, len(QUANTILES)), valid=valid.ravel()
    ).assign(path="reconciled")
    cov_orders = conformal_orders.coverage_table(
        y.ravel(), (rec if cfg.reconcile.for_decisions else cq).reshape(-1, len(QUANTILES)),
        decide_src.reshape(-1, len(QUANTILES)), valid=valid.ravel()
    ).assign(path="orders")
    cov = pd.concat([cov, cov_orders], ignore_index=True)
    cov["origin"] = str(P.dates[O].date())

    # Accuracy at every aggregation level, for the forecast before and after
    # reconciliation. Judging Stage 5 at the bottom level alone tests the one level
    # where MinT is expected to lose and none of the levels where it is expected to
    # win, so the bottom-only verdict is not a verdict at all.
    hier = evaluate.hierarchical_accuracy(
        y, {"pre_reconciliation": med(cq), "post_reconciliation": med(rec)},
        rc.H, valid,
    )
    hier["origin"] = str(P.dates[O].date())

    stage = {
        "origin": str(P.dates[O].date()),
        "anchor": evaluate.wrmsse(y, pts["seasonal_naive"], denom, wts, valid),
        "best_single": evaluate.wrmsse(y, med(qs["lgbm_quantile"]), denom, wts, valid),
        "combination": evaluate.wrmsse(y, med(cq), denom, wts, valid),
        "reconciliation": evaluate.wrmsse(y, med(rec), denom, wts, valid),
        "calibration": evaluate.wrmsse(y, med(cal), denom, wts, valid),
        "issued_for_decisions": evaluate.wrmsse(y, med(decide_src), denom, wts, valid),
    }
    return rows, cov, stage, hier


# ------------------------------------------------------------------ aggregation

def paired_compare(
    per_origin: pd.DataFrame, metric: str, a: str, b: str, lower_is_better: bool = True
) -> dict:
    """Paired comparison of two methods across origins.

    Reports the win count, the mean paired difference, and both a Wilcoxon
    signed-rank test and an exact sign test. With eight origins neither will reach
    conventional significance unless the effect is large, and saying so is the
    honest reading — eight paired observations is a small sample, and the correct
    conclusion is usually "consistent direction, underpowered", not "no effect".
    """
    pa = per_origin[per_origin["method"] == a].set_index("origin")[metric]
    pb = per_origin[per_origin["method"] == b].set_index("origin")[metric]
    common = pa.index.intersection(pb.index)
    x, y = pa.loc[common].to_numpy(float), pb.loc[common].to_numpy(float)
    ok = np.isfinite(x) & np.isfinite(y)
    x, y = x[ok], y[ok]
    n = len(x)
    if n < 2:
        return {"n": n, "verdict": "insufficient origins"}

    diff = x - y                                    # a minus b
    a_wins = int((diff < 0).sum()) if lower_is_better else int((diff > 0).sum())

    try:
        w_stat, w_p = stats.wilcoxon(x, y)
    except ValueError:                              # all differences zero
        w_stat, w_p = np.nan, 1.0
    sign_p = float(stats.binomtest(a_wins, n, 0.5).pvalue)

    return {
        "n": n,
        "a": a, "b": b, "metric": metric,
        "a_mean": float(x.mean()), "b_mean": float(y.mean()),
        "mean_diff": float(diff.mean()),
        "a_wins": a_wins, "b_wins": n - a_wins,
        "wilcoxon_p": float(w_p),
        "sign_test_p": sign_p,
        "verdict": (
            f"{a if a_wins > n / 2 else b} better at "
            f"{max(a_wins, n - a_wins)}/{n} origins"
        ),
    }


def summarise_hierarchy(hier: pd.DataFrame) -> dict:
    """Where reconciliation helps and where it costs, level by level."""
    piv = hier.pivot_table(index=["level", "origin"], columns="method", values="wrmsse")
    piv = piv.dropna()
    if piv.empty:
        return {}
    piv["delta"] = piv["post_reconciliation"] - piv["pre_reconciliation"]
    by_level = piv.groupby("level").agg(
        pre=("pre_reconciliation", "mean"),
        post=("post_reconciliation", "mean"),
        delta=("delta", "mean"),
        origins_improved=("delta", lambda d: int((d < 0).sum())),
        n_origins=("delta", "size"),
    ).reset_index()
    by_level["improved"] = by_level["delta"] < 0
    return {
        "by_level": by_level.round(5).to_dict("records"),
        "levels_improved": int(by_level["improved"].sum()),
        "levels_total": int(len(by_level)),
        "bottom_level_delta": float(
            by_level.loc[by_level["level"] == "item_store", "delta"].iloc[0]
        ) if (by_level["level"] == "item_store").any() else None,
    }


def _wape(y: np.ndarray, m: np.ndarray, valid: np.ndarray) -> float:
    if not valid.any():
        return float("nan")
    actual = np.where(valid, y, 0.0)
    total = float(actual.sum())
    return float(np.abs(np.where(valid, y - m, 0.0)).sum() / total) if total > 0 else float("nan")


def _inside(y: np.ndarray, q: np.ndarray, valid: np.ndarray) -> float:
    lo, hi = QUANTILES.index(0.05), QUANTILES.index(0.95)
    if not valid.any():
        return float("nan")
    ok = (q[..., lo] <= y) & (y <= q[..., hi])
    return float(ok[valid].mean())


def summarise(per_origin: pd.DataFrame, stages: pd.DataFrame) -> dict:
    """Aggregate the per-origin table, plus the comparisons that matter."""
    num = ["wrmsse", "wspl", "mase", "bias", "wape", "inside_range", "realized_margin",
           "fill_rate", "lost_margin"]
    have = [c for c in num if c in per_origin.columns]
    agg = (
        per_origin.groupby("method")[have]
        .agg(["mean", "std"])
        .round(4)
    )
    agg.columns = ["_".join(c) for c in agg.columns]
    agg = agg.reset_index().sort_values("wspl_mean")

    # Per-origin rank of each method, so a method that is never worst but rarely
    # best is distinguishable from one that alternates between the two.
    ranks = (
        per_origin.pivot_table(index="origin", columns="method", values="wspl")
        .rank(axis=1).mean().sort_values()
    )

    comparisons = [
        paired_compare(per_origin, "wspl", SYSTEM, "lgbm_quantile"),
        paired_compare(per_origin, "wrmsse", SYSTEM, "lgbm_quantile"),
        paired_compare(per_origin, "realized_margin", SYSTEM, "best single (LGBM-Q)",
                       lower_is_better=False),
        paired_compare(per_origin, "wspl", SYSTEM, "no calibration"),
        paired_compare(per_origin, "wspl", SYSTEM, "no reconciliation"),
        # Bias is signed, so it must be compared on magnitude. Ranking raw bias
        # "lower is better" declares the most severely *under*-forecasting method
        # the winner: the run that prompted this reported lgbm_quantile as better
        # on bias at 8/8 origins at -0.134 against the system's -0.063, which is
        # backwards.
        paired_compare(per_origin.assign(abs_bias=per_origin["bias"].abs()),
                       "abs_bias", SYSTEM, "lgbm_quantile"),
    ]

    st = stages.set_index("origin")
    ladder = {
        c: {"mean": float(st[c].mean()), "std": float(st[c].std()),
            "origins_improved_on_previous": None}
        for c in ("anchor", "best_single", "combination", "reconciliation", "calibration")
    }
    order = ["anchor", "best_single", "combination", "reconciliation", "calibration"]
    for prev, cur in zip(order[:-1], order[1:]):
        ladder[cur]["origins_improved_on_previous"] = int((st[cur] < st[prev]).sum())

    return {
        "n_origins": int(per_origin["origin"].nunique()),
        "origins": sorted(per_origin["origin"].unique()),
        "by_method": agg.to_dict("records"),
        "mean_wspl_rank": {k: round(float(v), 2) for k, v in ranks.items()},
        "paired_comparisons": [c for c in comparisons if c.get("n", 0) >= 2],
        "stage_ladder": ladder,
    }


def report_hierarchy(hsum: dict, log=print) -> None:
    if not hsum:
        log("  (no hierarchical results)")
        return
    log(f"  reconciliation improved accuracy at {hsum['levels_improved']} of "
        f"{hsum['levels_total']} aggregation levels")
    log(f"    {'level':<18s} {'pre':>9s} {'post':>9s} {'delta':>9s} {'origins better':>15s}")
    for r in sorted(hsum["by_level"], key=lambda x: x["delta"]):
        log(f"    {r['level']:<18s} {r['pre']:>9.4f} {r['post']:>9.4f} "
            f"{r['delta']:>+9.4f} {r['origins_improved']:>10d}/{r['n_origins']}")
    b = hsum.get("bottom_level_delta")
    if b is not None:
        log(f"\n  bottom level (item_store) delta {b:+.4f} — "
            f"{'degraded' if b > 0 else 'improved'}, which is the documented tradeoff")


def report(per_origin: pd.DataFrame, summary: dict, log=print) -> None:
    log(f"  {summary['n_origins']} evaluation origins: "
        f"{summary['origins'][0]} .. {summary['origins'][-1]}")

    cols = ["method", "wspl_mean", "wspl_std", "wrmsse_mean", "wrmsse_std",
            "mase_mean", "bias_mean"]
    tbl = pd.DataFrame(summary["by_method"])
    tbl = tbl[[c for c in cols if c in tbl.columns]]
    log("\n  mean +/- sd across origins (ranked by WSPL):")
    log("    " + tbl.round(4).to_string(index=False).replace("\n", "\n    "))

    log("\n  mean WSPL rank per origin (1 = best of the pool):")
    for k, v in summary["mean_wspl_rank"].items():
        log(f"    {k:<22s} {v:>5.2f}")

    log("\n  paired comparisons within origin:")
    for c in summary["paired_comparisons"]:
        log(f"    {c['metric']:<16s} {c['a']} vs {c['b']}")
        log(f"      {c['a']} {c['a_mean']:.4f} vs {c['b']} {c['b_mean']:.4f}   "
            f"diff {c['mean_diff']:+.4f}")
        log(f"      {c['verdict']};  Wilcoxon p={c['wilcoxon_p']:.3f}, "
            f"sign test p={c['sign_test_p']:.3f}")

    log("\n  stage ladder across origins (WRMSSE, and how often each stage helped):")
    for k, v in summary["stage_ladder"].items():
        imp = v["origins_improved_on_previous"]
        tail = "" if imp is None else f"   improved on previous at {imp}/{summary['n_origins']} origins"
        log(f"    {k:<16s} {v['mean']:.4f} +/- {v['std']:.4f}{tail}")


def persist(per_origin: pd.DataFrame, summary: dict) -> None:
    import json

    per_origin.to_csv(ARTIFACTS / "backtest_per_origin.csv", index=False)
    (ARTIFACTS / "backtest_summary.json").write_text(json.dumps(summary, indent=2, default=str))
