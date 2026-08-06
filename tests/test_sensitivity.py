"""Cost-parameter sensitivity of the decision layer.

The sweep exists to answer "what if your holding cost is wrong by a factor of
three", so its own mechanics have to be right: the grid must actually change the
policy, the comparison must be paired within cell, and a conditional result must be
reported as conditional rather than rounded up to "robust".
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from dqstack.config import QUANTILES
from dqstack.decide import CostParams, critical_fractile
from dqstack.sensitivity import summarise, sweep


def _inputs(n=40, h=28):
    rng = np.random.default_rng(11)
    base = rng.uniform(3, 18, size=(n, h, 1))
    q = np.sort(base * (0.35 + 1.7 * np.asarray(QUANTILES))[None, None, :], axis=-1)
    demand = rng.poisson(base[:, :, 0]).astype("float64")
    cp = CostParams(
        price=rng.uniform(2, 12, size=n),
        unit_cost=rng.uniform(0.5, 6, size=n),
        margin_rate=rng.uniform(0.1, 0.5, size=n),
        shelf_life=np.where(rng.random(n) < 0.4, 21.0, 720.0),
    )
    dates = pd.date_range("2026-08-05", periods=h, freq="D")
    return q, demand, cp, np.arange(n).astype(str), dates


def test_sweep_covers_the_full_grid_and_moves_the_policy():
    q, d, cp, ids, dates = _inputs()
    tbl = sweep(q, d, cp, ids, dates,
                holding_mult=(0.5, 1.0, 2.0), markdown_mult=(0.5, 2.0), margin_mult=(1.0,))
    assert len(tbl) == 3 * 2 * 1

    # The grid must actually change the derived quantile, or it is testing nothing.
    assert tbl["fractile_mean_target_q"].nunique() > 1, (
        "cost scenarios produced identical target quantiles; the sweep is inert"
    )
    # Higher overage must lower the target quantile — the fractile's whole point.
    hi_over = tbl[(tbl.holding_mult == 2.0) & (tbl.markdown_mult == 2.0)]
    lo_over = tbl[(tbl.holding_mult == 0.5) & (tbl.markdown_mult == 0.5)]
    assert (hi_over["fractile_mean_target_q"].iloc[0]
            < lo_over["fractile_mean_target_q"].iloc[0])


def test_blanket_arm_is_actually_flat_at_95_percent():
    q, d, cp, ids, dates = _inputs()
    tbl = sweep(q, d, cp, ids, dates,
                holding_mult=(1.0, 3.0), markdown_mult=(1.0,), margin_mult=(1.0,))
    assert np.allclose(tbl["blanket95_mean_target_q"], 0.95), (
        "the comparison arm is not a blanket 95% policy"
    )
    # ...while the fractile arm responds to the cost change.
    assert tbl["fractile_mean_target_q"].nunique() == 2


def test_higher_margin_raises_the_target_quantile():
    """Underage rises with margin, so a high-margin assortment should be planned
    at a higher service quantile. If this fails the fractile is miswired."""
    q, d, cp, ids, dates = _inputs()
    tbl = sweep(q, d, cp, ids, dates,
                holding_mult=(1.0,), markdown_mult=(1.0,), margin_mult=(0.6, 1.0, 1.5))
    qs = tbl.sort_values("margin_mult")["fractile_mean_target_q"].to_numpy()
    assert (np.diff(qs) > 0).all(), f"target quantile not increasing in margin: {qs}"


def test_summary_reports_a_conditional_result_as_conditional():
    tbl = pd.DataFrame({
        "holding_mult": [1.0, 2.0, 3.0, 4.0],
        "markdown_mult": [1.0] * 4,
        "margin_mult": [1.0] * 4,
        "fractile_min_target_q": [0.5] * 4,
        "fractile_max_target_q": [0.9] * 4,
        "margin_advantage": [100.0, 50.0, -20.0, -80.0],
        "fractile_wins": [True, True, False, False],
    })
    s = summarise(tbl)
    assert s["fractile_wins"] == 2 and s["n_scenarios"] == 4
    assert s["win_rate"] == pytest.approx(0.5)
    assert s["verdict"].startswith("conditional")
    assert len(s["losing_scenarios"]) == 2, "losing cells must be enumerated, not just counted"


def test_summary_reports_a_robust_result_as_robust():
    tbl = pd.DataFrame({
        "holding_mult": [1.0, 2.0],
        "markdown_mult": [1.0, 1.0],
        "margin_mult": [1.0, 1.0],
        "fractile_min_target_q": [0.5, 0.5],
        "fractile_max_target_q": [0.9, 0.9],
        "margin_advantage": [10.0, 20.0],
        "fractile_wins": [True, True],
    })
    s = summarise(tbl)
    assert s["verdict"].startswith("robust")
    assert s["losing_scenarios"] == []


def test_target_quantile_stays_inside_configured_bounds_across_the_grid():
    from dqstack.config import CFG

    _, _, cp, _, _ = _inputs()
    for hm in (0.1, 1.0, 10.0):
        for gm in (0.2, 1.0, 2.0):
            cell = CostParams(
                price=cp.price, unit_cost=cp.unit_cost,
                margin_rate=np.clip(cp.margin_rate * gm, 0.01, 0.95),
                shelf_life=cp.shelf_life,
                holding_rate=cp.holding_rate * hm,
            )
            tq = critical_fractile(cell, risk_days=9)["target_quantile"]
            assert (tq >= CFG.decision.min_quantile - 1e-9).all()
            assert (tq <= CFG.decision.max_quantile + 1e-9).all()
