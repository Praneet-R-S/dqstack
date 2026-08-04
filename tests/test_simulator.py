"""Stage 7 simulator validation — the four tests the spec names as a gate.

An inventory simulator that quietly loses units will produce a decision ledger
that looks reasonable and reports the wrong margin, and the error is invisible
because there is nothing to compare it against. These are the checks that make
the ledger trustworthy enough to be the results dataset for the whole thesis.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from dqstack.config import CFG, Config, DecisionConfig, QUANTILES
from dqstack.decide import (
    CostParams,
    critical_fractile,
    quantile_moments,
    risk_period_quantile,
    simulate,
)


def _params(n: int, shelf_life: float = 720.0) -> CostParams:
    return CostParams(
        price=np.full(n, 4.0),
        unit_cost=np.full(n, 2.0),
        margin_rate=np.full(n, 0.30),
        shelf_life=np.full(n, shelf_life),
    )


def _dates(h: int) -> pd.DatetimeIndex:
    return pd.date_range("2026-08-05", periods=h, freq="D")


# ------------------------------------------------------------------ conservation

def test_conservation_of_units():
    """opening + received == sold + on_hand_end + waste, exactly."""
    rng = np.random.default_rng(7)
    n, h = 60, 28
    demand = rng.poisson(9.0, size=(n, h)).astype("float64")
    cp = _params(n)
    order_up_to = np.full(n, 95.0)
    res = simulate(demand, order_up_to, cp, np.arange(n).astype(str), _dates(h))

    opening = res.daily["opening"][:, 0] - res.daily["arrivals"][:, 0]
    received = res.daily["arrivals"].sum(axis=1)
    sold = res.daily["sales"].sum(axis=1)
    waste = res.daily["waste"].sum(axis=1)
    end = res.daily["closing"][:, -1]

    residual = (opening + received) - (sold + waste + end)
    assert np.abs(residual).max() < 1e-9, f"units lost: max residual {np.abs(residual).max()}"
    assert res.totals["conservation_residual"] < 1e-9


def test_no_negative_inventory_ever():
    rng = np.random.default_rng(11)
    n, h = 80, 28
    # Demand deliberately far above the order-up-to level to force stockouts.
    demand = rng.poisson(40.0, size=(n, h)).astype("float64")
    cp = _params(n)
    res = simulate(demand, np.full(n, 12.0), cp, np.arange(n).astype(str), _dates(h))
    for k in ("opening", "closing", "sales", "lost", "waste"):
        assert (res.daily[k] >= -1e-9).all(), f"{k} went negative"
    assert res.totals["min_on_hand"] >= -1e-9
    # And sales can never exceed demand.
    assert (res.daily["sales"] <= res.daily["demand"] + 1e-9).all()


def test_matches_closed_form_under_deterministic_demand():
    """Constant demand d, review R, lead L, order-up-to S.

    With S set above the risk-period requirement the policy never stocks out, so
    every unit of demand is sold and the closed-form total is d * H. Steady-state
    on-hand after a receipt is S - d*L, which is the textbook result.
    """
    n, h = 5, 28
    d = 10.0
    demand = np.full((n, h), d)
    cp = _params(n)
    cfg = Config(decision=DecisionConfig(review_period=7, lead_time=2,
                                         initial_stock_cover_days=10.0))
    R, L = cfg.decision.review_period, cfg.decision.lead_time
    S = d * (R + L) + 5.0
    res = simulate(demand, np.full(n, S), cp, np.arange(n).astype(str), _dates(h), cfg)

    assert res.daily["lost"].sum() == pytest.approx(0.0, abs=1e-9)
    assert res.daily["sales"].sum() == pytest.approx(n * d * h, rel=1e-9)
    # Each review order replenishes exactly one review period of demand.
    steady_orders = res.daily["orders"][0][res.daily["orders"][0] > 0][1:]
    assert np.allclose(steady_orders, d * R, atol=1e-6), (
        f"steady-state order should be d*R = {d * R}, got {steady_orders}"
    )


def test_higher_target_quantile_increases_service_and_inventory():
    """Monotonicity. Raising the target quantile must raise both the achieved
    service level and the average inventory — if it does not, the quantile is not
    actually controlling the policy."""
    rng = np.random.default_rng(3)
    n, h = 120, 28
    demand = rng.negative_binomial(4, 4 / (4 + 12), size=(n, h)).astype("float64")
    cp = _params(n)

    mu = demand.mean()
    fills, invs = [], []
    for q in (0.50, 0.75, 0.90, 0.99):
        S = np.full(n, float(np.quantile(demand.sum(axis=1) / h * 9, q)) + mu * q * 4)
        res = simulate(demand, S, cp, np.arange(n).astype(str), _dates(h))
        fills.append(res.totals["fill_rate"])
        invs.append(res.totals["avg_inventory"])

    assert all(b >= a - 1e-9 for a, b in zip(fills, fills[1:])), f"fill not monotone: {fills}"
    assert all(b >= a - 1e-9 for a, b in zip(invs, invs[1:])), f"inventory not monotone: {invs}"


# ------------------------------------------------------------------ fractile

def test_critical_fractile_responds_to_cost_asymmetry():
    """A high-margin item must be planned at a higher quantile than a low-margin,
    high-holding-cost one. This is the formula's entire purpose."""
    cp = CostParams(
        price=np.array([10.0, 10.0]),
        unit_cost=np.array([1.0, 8.0]),
        margin_rate=np.array([0.60, 0.05]),
        shelf_life=np.array([720.0, 720.0]),
    )
    out = critical_fractile(cp, risk_days=9)
    assert out["target_quantile"][0] > out["target_quantile"][1]
    assert (out["target_quantile"] >= CFG.decision.min_quantile).all()
    assert (out["target_quantile"] <= CFG.decision.max_quantile).all()


def test_blanket_service_level_ablation_is_flat():
    cp = _params(4)
    cp.margin_rate[:] = [0.1, 0.3, 0.5, 0.7]
    cfg = Config(enable_critical_fractile=False)
    out = critical_fractile(cp, risk_days=9, cfg=cfg)
    assert np.allclose(out["target_quantile"], 0.95)


def test_perishable_gets_lower_quantile_than_durable():
    cp = CostParams(
        price=np.array([5.0, 5.0]),
        unit_cost=np.array([2.0, 2.0]),
        margin_rate=np.array([0.3, 0.3]),
        shelf_life=np.array([7.0, 720.0]),
    )
    out = critical_fractile(cp, risk_days=9)
    assert out["target_quantile"][0] < out["target_quantile"][1], (
        "spoilage exposure must lower the target quantile for a perishable"
    )


# ------------------------------------------------------------------ distribution

def test_quantile_moments_recover_a_known_distribution():
    """Moments read off a quantile function must approximate the true moments."""
    from scipy import stats

    mu, sd = 40.0, 9.0
    q = stats.norm.ppf(np.asarray(QUANTILES), loc=mu, scale=sd)[None, :]
    m, v = quantile_moments(q)
    assert m[0] == pytest.approx(mu, rel=0.05)
    assert np.sqrt(v[0]) == pytest.approx(sd, rel=0.25)


def test_risk_period_quantile_is_monotone_in_target():
    rng = np.random.default_rng(5)
    n, L, Q = 40, 9, len(QUANTILES)
    base = rng.uniform(4, 20, size=(n, L, 1))
    q = base * np.asarray(QUANTILES)[None, None, :] * 2.0
    prev = None
    for target in (0.5, 0.7, 0.9, 0.99):
        s = risk_period_quantile(q, np.full(n, target))
        if prev is not None:
            assert (s >= prev - 1e-9).all()
        prev = s


def test_risk_period_quantile_exceeds_expected_demand_at_high_target():
    n, L = 30, 9
    q = np.tile(np.linspace(5, 25, len(QUANTILES)), (n, L, 1))
    mean_total = quantile_moments(q)[0].sum(axis=1)
    s = risk_period_quantile(q, np.full(n, 0.95))
    assert (s > mean_total).all(), "a 95% target must cover more than mean demand"
