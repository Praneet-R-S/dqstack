"""Sensitivity of the decision layer to its assumed cost parameters.

Why this is not optional. Stage 7's central claim is that the target service
quantile should be the critical fractile rather than a blanket 95%:

    target quantile = underage / (underage + overage)
    underage        = price * margin_rate
    overage         = unit_cost * (holding_rate * risk_days + markdown_risk + spoilage)

Every term on the right is *assumed*. The margin rates come from the item master,
the holding rate and markdown risk are configured constants, and on a real
engagement they would be negotiated numbers nobody can produce to two decimal
places. A conclusion derived from them is therefore only as good as its robustness
to them, and the obvious examiner question — "what if your holding cost is wrong by
a factor of three?" — has to be answered with a table rather than a shrug.

What this sweeps. A grid over the holding rate, the markdown risk and a multiplier
on all margins, re-deriving the fractile and re-running the simulator in each cell.
Nothing is retrained: the predictive distributions are fixed inputs and only the
decision layer moves, which is the point — it isolates the sensitivity of the
*policy* from the sensitivity of the forecast.

The number that matters is not any single margin figure. It is the count of cost
scenarios in which the critical fractile still beats a blanket 95% service level.
If that count is every cell, the recommendation is robust and says so. If it is
half, the honest finding is that the advantage depends on cost assumptions the
project cannot pin down — which is a real result, and a far better thing to report
than a single margin number computed at one arbitrary point in the space.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd

from . import decide as dec
from .config import ARTIFACTS, CFG

# Multiplicative deviations from the configured value. 0.33x to 3x brackets the
# range over which a real holding cost could plausibly be misestimated.
HOLDING_MULT = (0.33, 0.5, 1.0, 2.0, 3.0)
MARKDOWN_MULT = (0.25, 0.5, 1.0, 2.0, 4.0)
MARGIN_MULT = (0.6, 1.0, 1.5)


def _run_cell(
    q_daily: np.ndarray,
    demand: np.ndarray,
    cp: dec.CostParams,
    series_ids: np.ndarray,
    dates: pd.DatetimeIndex,
    cfg,
) -> dict:
    res, frac = dec.decide(q_daily, demand, cp, series_ids, dates, cfg)
    t = res.totals
    return {
        "realized_margin": t["realized_margin"],
        "fill_rate": t["fill_rate"],
        "lost_margin": t["lost_margin"],
        "holding_cost": t["holding_cost"],
        "waste_cost": t["waste_cost"],
        "markdown_cost": t.get("markdown_cost", 0.0),
        "avg_inventory": t["avg_inventory"],
        "mean_target_q": float(frac["target_quantile"].mean()),
        "min_target_q": float(frac["target_quantile"].min()),
        "max_target_q": float(frac["target_quantile"].max()),
    }


def sweep(
    q_daily: np.ndarray,
    demand: np.ndarray,
    cp: dec.CostParams,
    series_ids: np.ndarray,
    dates: pd.DatetimeIndex,
    cfg=CFG,
    holding_mult: tuple[float, ...] = HOLDING_MULT,
    markdown_mult: tuple[float, ...] = MARKDOWN_MULT,
    margin_mult: tuple[float, ...] = MARGIN_MULT,
) -> pd.DataFrame:
    """Re-derive the fractile and re-simulate across a cost grid.

    Each cell is evaluated twice — once with the critical fractile, once with a
    blanket 95% service level — so the comparison is paired within cell and the
    verdict is not confounded by the cost change itself.
    """
    cfg_blanket = replace(cfg, enable_critical_fractile=False)
    rows: list[dict] = []

    for hm in holding_mult:
        for mm in markdown_mult:
            for gm in margin_mult:
                cp_cell = dec.CostParams(
                    price=cp.price,
                    unit_cost=cp.unit_cost,
                    margin_rate=np.clip(cp.margin_rate * gm, 0.01, 0.95),
                    shelf_life=cp.shelf_life,
                    holding_rate=cp.holding_rate * hm,
                    markdown_risk=float(np.clip(cp.markdown_risk * mm, 0.0, 0.95)),
                )
                frac_cell = _run_cell(q_daily, demand, cp_cell, series_ids, dates, cfg)
                blanket = _run_cell(q_daily, demand, cp_cell, series_ids, dates, cfg_blanket)

                rows.append({
                    "holding_mult": hm,
                    "markdown_mult": mm,
                    "margin_mult": gm,
                    "holding_rate": cp_cell.holding_rate,
                    "markdown_risk": cp_cell.markdown_risk,
                    **{f"fractile_{k}": v for k, v in frac_cell.items()},
                    **{f"blanket95_{k}": v for k, v in blanket.items()},
                    "margin_advantage": frac_cell["realized_margin"] - blanket["realized_margin"],
                    "fractile_wins": bool(
                        frac_cell["realized_margin"] > blanket["realized_margin"]
                    ),
                })
    return pd.DataFrame(rows)


def summarise(tbl: pd.DataFrame) -> dict:
    n = len(tbl)
    wins = int(tbl["fractile_wins"].sum())
    adv = tbl["margin_advantage"]

    # Where, if anywhere, does the recommendation fail? Reported explicitly rather
    # than left as a win-rate, because the *shape* of the failure region is the
    # useful part: a policy that loses only when holding cost is negligible is
    # telling you something different from one that loses at random.
    losses = tbl[~tbl["fractile_wins"]][
        ["holding_mult", "markdown_mult", "margin_mult", "margin_advantage"]
    ]

    by_holding = tbl.groupby("holding_mult")["fractile_wins"].mean().to_dict()
    by_markdown = tbl.groupby("markdown_mult")["fractile_wins"].mean().to_dict()
    by_margin = tbl.groupby("margin_mult")["fractile_wins"].mean().to_dict()

    return {
        "n_scenarios": n,
        "fractile_wins": wins,
        "win_rate": wins / n if n else float("nan"),
        "margin_advantage_mean": float(adv.mean()),
        "margin_advantage_min": float(adv.min()),
        "margin_advantage_max": float(adv.max()),
        "target_q_range_across_scenarios": [
            float(tbl["fractile_min_target_q"].min()),
            float(tbl["fractile_max_target_q"].max()),
        ],
        "win_rate_by_holding_mult": {str(k): float(v) for k, v in by_holding.items()},
        "win_rate_by_markdown_mult": {str(k): float(v) for k, v in by_markdown.items()},
        "win_rate_by_margin_mult": {str(k): float(v) for k, v in by_margin.items()},
        "losing_scenarios": losses.to_dict("records"),
        "verdict": (
            "robust: the critical fractile beats a blanket 95% service level in every "
            "cost scenario swept"
            if wins == n else
            f"conditional: the critical fractile wins in {wins} of {n} cost scenarios; "
            "the advantage depends on cost assumptions this project cannot pin down"
        ),
    }


def report(tbl: pd.DataFrame, summary: dict, log=print) -> None:
    log(f"  swept {summary['n_scenarios']} cost scenarios "
        f"(holding x{HOLDING_MULT}, markdown x{MARKDOWN_MULT}, margin x{MARGIN_MULT})")
    log(f"  critical fractile beats blanket 95% in "
        f"{summary['fractile_wins']}/{summary['n_scenarios']} "
        f"({summary['win_rate']:.0%})")
    log(f"  margin advantage: mean {summary['margin_advantage_mean']:,.0f}, "
        f"range {summary['margin_advantage_min']:,.0f} .. "
        f"{summary['margin_advantage_max']:,.0f}")
    lo, hi = summary["target_q_range_across_scenarios"]
    log(f"  target quantile spans {lo:.3f} .. {hi:.3f} across the grid "
        f"(a blanket policy fixes it at 0.950)")

    log("\n  win rate by assumed holding cost:")
    for k, v in summary["win_rate_by_holding_mult"].items():
        log(f"    x{k:<5s} {v:.0%}")
    log("  win rate by assumed markdown risk:")
    for k, v in summary["win_rate_by_markdown_mult"].items():
        log(f"    x{k:<5s} {v:.0%}")
    log("  win rate by margin multiplier:")
    for k, v in summary["win_rate_by_margin_mult"].items():
        log(f"    x{k:<5s} {v:.0%}")

    if summary["losing_scenarios"]:
        log(f"\n  {len(summary['losing_scenarios'])} scenario(s) where a blanket 95% wins:")
        for r in summary["losing_scenarios"][:8]:
            log(f"    holding x{r['holding_mult']}, markdown x{r['markdown_mult']}, "
                f"margin x{r['margin_mult']}  -> {r['margin_advantage']:,.0f}")
    log(f"\n  {summary['verdict']}")


def persist(tbl: pd.DataFrame, summary: dict) -> None:
    import json

    tbl.to_csv(ARTIFACTS / "cost_sensitivity.csv", index=False)
    (ARTIFACTS / "cost_sensitivity_summary.json").write_text(
        json.dumps(summary, indent=2, default=str)
    )
