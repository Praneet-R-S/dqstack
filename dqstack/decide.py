"""Stage 7 — the decision layer. Where a forecast becomes money.

Two ideas do all the work.

**The critical fractile.** The target service quantile is not a policy choice, it
is a consequence of cost asymmetry:

    target quantile = underage / (underage + overage)
    underage        = lost gross margin per unit
    overage         = holding cost over the risk period + markdown risk

This replaces the industry habit of applying a blanket 95% service level to
everything. A low-margin, high-holding-cost item *should* be planned at a much
lower quantile than a high-margin one, and the formula says so precisely. It is
also the mechanism by which better calibration converts into better economics: the
quantile is read off the predictive distribution, so if the distribution is wrong
the service level is wrong, whatever the nominal target says.

**The order-up-to level** is then the predictive quantile of demand over the
risk period R + L, not over one day. Aggregating the daily distributions is done
by moment matching rather than by summing daily quantiles, because summing
quantiles assumes perfectly dependent daily demand and materially overstates the
spread of the total.

The simulator is fully vectorised over series. A per-SKU Python loop is the
difference between minutes and hours, and it is the most common performance
mistake in this layer.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy import stats

from .config import CFG, QUANTILES

# Correlation between successive days' demand deviations, used when aggregating
# daily distributions to the risk-period total. Zero would assume independence
# and understate the spread; one is the comonotonic bound and overstates it.
DAILY_RHO = 0.15


# ------------------------------------------------------------------ moments

def quantile_moments(q: np.ndarray, quantiles: tuple[float, ...] = QUANTILES
                     ) -> tuple[np.ndarray, np.ndarray]:
    """Mean and variance implied by a discrete quantile function.

    Integrates over the probability axis with trapezoidal weights on the
    quantile levels, extending the first and last level to 0 and 1 so the tails
    are not silently dropped.
    """
    taus = np.asarray(quantiles, dtype="float64")
    edges = np.concatenate([[0.0], (taus[1:] + taus[:-1]) / 2.0, [1.0]])
    w = np.diff(edges)
    w = w / w.sum()

    mean = (q * w).sum(axis=-1)
    var = ((q - mean[..., None]) ** 2 * w).sum(axis=-1)
    return mean, np.maximum(var, 1e-9)


def risk_period_quantile(
    q_daily: np.ndarray, target_q: np.ndarray, rho: float = DAILY_RHO
) -> np.ndarray:
    """Predictive quantile of total demand over the whole risk period.

    `q_daily`  (S, L_risk, Q) daily quantile forecasts spanning R + L days
    `target_q` (S,) per-series critical fractile

    Moments of the sum are accumulated analytically, then a Negative Binomial is
    matched to them and inverted at the target level. NB rather than Normal for
    the same reason as everywhere else in this system: the quantity is a
    non-negative overdispersed count and a Normal puts mass below zero.
    """
    mean_d, var_d = quantile_moments(q_daily)          # (S, L)
    mean_sum = mean_d.sum(axis=1)
    sd = np.sqrt(var_d)
    var_indep = var_d.sum(axis=1)
    cross = sd.sum(axis=1) ** 2 - (sd**2).sum(axis=1)
    var_sum = var_indep + rho * cross

    mu = np.clip(mean_sum, 1e-9, None)
    excess = np.clip(var_sum - mu, 1e-9, None)
    r = np.clip(mu**2 / excess, 0.05, 1e6)
    p = r / (r + mu)
    out = stats.nbinom.ppf(np.clip(target_q, 1e-6, 1 - 1e-9), r, p)
    return np.nan_to_num(np.maximum(out, 0.0))


# ------------------------------------------------------------------ fractile

@dataclass
class CostParams:
    price: np.ndarray          # (S,) selling price
    unit_cost: np.ndarray      # (S,)
    margin_rate: np.ndarray    # (S,)
    shelf_life: np.ndarray     # (S,) days
    holding_rate: float = CFG.decision.holding_cost_rate
    markdown_risk: float = CFG.decision.markdown_risk


def critical_fractile(cp: CostParams, risk_days: int, cfg=CFG) -> dict[str, np.ndarray]:
    """Per-series target service quantile, with its cost components exposed.

    Returned as a dict rather than a bare array because the planner workspace
    shows *why* an item is planned at 0.71 rather than 0.95, and that explanation
    is the numerator and denominator of this formula.
    """
    underage = cp.price * cp.margin_rate
    holding = cp.unit_cost * cp.holding_rate * risk_days
    markdown = cp.unit_cost * cp.markdown_risk
    # Perishables carry additional spoilage exposure over the risk period.
    spoil = np.where(
        cp.shelf_life < 60, cp.unit_cost * np.clip(risk_days / cp.shelf_life, 0, 1) * 0.5, 0.0
    )
    overage = holding + markdown + spoil

    q = underage / np.maximum(underage + overage, 1e-9)
    if not cfg.enable_critical_fractile:
        # Ablation arm: the industry default applied to everything.
        q = np.full_like(q, 0.95)
    q = np.clip(q, cfg.decision.min_quantile, cfg.decision.max_quantile)
    return {
        "target_quantile": q,
        "underage_cost": underage,
        "overage_cost": overage,
        "holding_component": holding,
        "markdown_component": markdown,
        "spoilage_component": spoil,
    }


# ------------------------------------------------------------------ simulator

@dataclass
class SimResult:
    ledger: pd.DataFrame
    daily: dict[str, np.ndarray]
    totals: dict[str, float]


def simulate(
    demand: np.ndarray,
    order_up_to: np.ndarray,
    cp: CostParams,
    series_ids: np.ndarray,
    dates: pd.DatetimeIndex,
    cfg=CFG,
    initial_stock: np.ndarray | None = None,
) -> SimResult:
    """Periodic-review order-up-to policy, run forward day by day.

    `demand` is *true* demand, not recorded sales. Using sales would make lost
    sales invisible and the simulator would score a stockout as a perfect day —
    the same censoring error the pipeline exists to avoid, reintroduced at the
    last step.

    Vectorised over series; the only loop is over the 28 days of the horizon,
    which is irreducible because inventory is a recursion.
    """
    S, H = demand.shape
    R, L = cfg.decision.review_period, cfg.decision.lead_time

    on_hand = (
        initial_stock
        if initial_stock is not None
        else np.round(demand[:, :7].mean(axis=1) * cfg.decision.initial_stock_cover_days)
    ).astype("float64")
    opening_initial = on_hand.copy()

    pipeline = np.zeros((S, H + L + 1), dtype="float64")

    # Spoilage applies only to stock that cannot be sold within its shelf life.
    # Bleeding a flat 1/shelf_life of on-hand every day — the obvious shortcut —
    # destroys most of a perishable's inventory regardless of turnover, which
    # makes waste dominate the ledger and the economics meaningless. Under FIFO,
    # a fast-moving perishable wastes nothing; only cover beyond shelf life
    # expires. Tracking true unit ages per SKU would need a per-series queue and
    # cannot be vectorised, so the excess-cover approximation stands in and is
    # stated as an approximation.
    perishable = cp.shelf_life < 60
    shelf = np.maximum(cp.shelf_life, 1.0)
    rate = np.maximum(demand[:, :7].mean(axis=1), 0.05)   # EWMA seeded on early demand

    rec = {k: np.zeros((S, H)) for k in
           ("opening", "arrivals", "demand", "sales", "lost", "waste", "closing", "orders")}
    total_received = np.zeros(S)
    total_ordered = np.zeros(S)

    review_days = list(range(0, H, R))
    ledger_rows = []

    for t in range(H):
        arrivals = pipeline[:, t].copy()
        on_hand += arrivals
        opening = on_hand.copy()

        # Order decision happens at the start of a review day, on the position
        # after receipts: on-hand plus everything already in transit.
        order = np.zeros(S)
        if t in review_days:
            in_transit = pipeline[:, t + 1 :].sum(axis=1)
            order = np.maximum(order_up_to - on_hand - in_transit, 0.0)
            order = np.round(order)
            if t + L < pipeline.shape[1]:
                pipeline[:, t + L] += order
            total_ordered += order

        d = demand[:, t]
        sales = np.minimum(d, on_hand)
        lost = d - sales
        on_hand -= sales
        rate = 0.85 * rate + 0.15 * np.maximum(d, 0.0)

        sellable = rate * shelf
        excess = np.maximum(on_hand - sellable, 0.0)
        waste = np.where(perishable, np.floor(excess / shelf), 0.0)
        waste = np.minimum(waste, on_hand)
        on_hand -= waste

        total_received += arrivals
        for k, v in (("opening", opening), ("arrivals", arrivals), ("demand", d),
                     ("sales", sales), ("lost", lost), ("waste", waste),
                     ("closing", on_hand), ("orders", order)):
            rec[k][:, t] = v

    # Costs
    holding_cost = (rec["closing"] * cp.unit_cost[:, None] * cp.holding_rate).sum(axis=1)
    waste_cost = (rec["waste"] * cp.unit_cost[:, None]).sum(axis=1)
    gross_margin = (rec["sales"] * (cp.price * cp.margin_rate)[:, None]).sum(axis=1)
    lost_margin = (rec["lost"] * (cp.price * cp.margin_rate)[:, None]).sum(axis=1)

    # Markdown on whatever is still on the shelf when the horizon ends.
    #
    # Without this the simulator lets over-ordering be almost free: leftover stock
    # simply vanishes at t = H, holding is only 0.22%/day, and the only penalty
    # for ordering too much is spoilage on perishables. The critical fractile
    # meanwhile *prices* markdown_risk as a component of overage, so the policy was
    # being charged in the quantile for a cost the ledger never levied — the two
    # halves of Stage 7 disagreed about what overage means, and the promotion gate
    # reads the half that under-charges.
    #
    # Terminal stock is distinct from waste: waste is already gone and charged at
    # full cost, this is stock that survived and must be cleared at a discount.
    terminal_stock = rec["closing"][:, -1]
    markdown_cost = terminal_stock * cp.unit_cost * cp.markdown_risk
    realized_margin = gross_margin - holding_cost - waste_cost - markdown_cost

    # ---- ledger: one row per series-review-period
    for p, t0 in enumerate(review_days):
        t1 = min(t0 + R, H)
        sl = slice(t0, t1)
        ledger_rows.append(
            pd.DataFrame(
                {
                    "series_id": series_ids,
                    "period": p,
                    "period_start": dates[t0],
                    "period_end": dates[t1 - 1],
                    "order_up_to": order_up_to,
                    "order_placed": rec["orders"][:, t0],
                    "opening_stock": rec["opening"][:, t0],
                    "demand": rec["demand"][:, sl].sum(axis=1),
                    "sales": rec["sales"][:, sl].sum(axis=1),
                    "lost_sales": rec["lost"][:, sl].sum(axis=1),
                    "waste": rec["waste"][:, sl].sum(axis=1),
                    "closing_stock": rec["closing"][:, t1 - 1],
                    "holding_cost": (rec["closing"][:, sl] * cp.unit_cost[:, None]
                                     * cp.holding_rate).sum(axis=1),
                    "waste_cost": (rec["waste"][:, sl] * cp.unit_cost[:, None]).sum(axis=1),
                    "gross_margin": (rec["sales"][:, sl]
                                     * (cp.price * cp.margin_rate)[:, None]).sum(axis=1),
                    "lost_margin": (rec["lost"][:, sl]
                                    * (cp.price * cp.margin_rate)[:, None]).sum(axis=1),
                }
            )
        )
    ledger = pd.concat(ledger_rows, ignore_index=True)
    last_period = ledger["period"].max()
    ledger["markdown_cost"] = np.where(
        ledger["period"].to_numpy() == last_period,
        np.tile(markdown_cost, ledger["period"].nunique()),
        0.0,
    )
    ledger["realized_margin"] = (
        ledger["gross_margin"] - ledger["holding_cost"]
        - ledger["waste_cost"] - ledger["markdown_cost"]
    )
    ledger["fill_rate"] = ledger["sales"] / ledger["demand"].replace(0, np.nan)

    demand_tot = rec["demand"].sum(axis=1)
    sales_tot = rec["sales"].sum(axis=1)
    totals = {
        "demand": float(demand_tot.sum()),
        "sales": float(sales_tot.sum()),
        "lost_sales": float(rec["lost"].sum()),
        "waste": float(rec["waste"].sum()),
        "fill_rate": float(sales_tot.sum() / max(demand_tot.sum(), 1e-9)),
        "cycle_service_level": float((rec["lost"].sum(axis=1) == 0).mean()),
        "gross_margin": float(gross_margin.sum()),
        "holding_cost": float(holding_cost.sum()),
        "waste_cost": float(waste_cost.sum()),
        "markdown_cost": float(markdown_cost.sum()),
        "lost_margin": float(lost_margin.sum()),
        "realized_margin": float(realized_margin.sum()),
        "avg_inventory": float(rec["closing"].mean()),
        "inventory_units_end": float(rec["closing"][:, -1].sum()),
        # conservation audit, reported rather than asserted silently
        "conservation_residual": float(
            np.abs(
                (opening_initial + total_received)
                - (sales_tot + rec["waste"].sum(axis=1) + rec["closing"][:, -1])
            ).max()
        ),
        "min_on_hand": float(rec["closing"].min()),
    }
    return SimResult(ledger=ledger, daily=rec, totals=totals)


# ------------------------------------------------------------------ orchestration

def decide(
    q_daily: np.ndarray,
    demand_actual: np.ndarray,
    cp: CostParams,
    series_ids: np.ndarray,
    dates: pd.DatetimeIndex,
    cfg=CFG,
) -> tuple[SimResult, dict[str, np.ndarray]]:
    """Full Stage 7: fractile -> order-up-to -> simulate -> ledger."""
    R, L = cfg.decision.review_period, cfg.decision.lead_time
    risk_days = R + L
    frac = critical_fractile(cp, risk_days, cfg)
    S_star = risk_period_quantile(q_daily[:, :risk_days, :], frac["target_quantile"])

    res = simulate(demand_actual, S_star, cp, series_ids, dates, cfg)
    res.ledger["target_quantile"] = np.tile(frac["target_quantile"], res.ledger["period"].nunique())
    frac["order_up_to"] = S_star
    return res, frac
