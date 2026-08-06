"""Rolling-origin backtest: pairing, aggregation and honest verdicts.

The paired comparison is what the thesis's central claim rests on, so its logic is
tested directly rather than trusted because it calls scipy.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from dqstack.backtest import SYSTEM, paired_compare, summarise


def _frame(a_vals, b_vals, a="A", b="B", metric="wspl"):
    rows = []
    for i, (x, y) in enumerate(zip(a_vals, b_vals)):
        o = f"2026-01-{i + 1:02d}"
        rows.append({"origin": o, "method": a, metric: x})
        rows.append({"origin": o, "method": b, metric: y})
    return pd.DataFrame(rows)


def test_pairing_detects_a_consistent_small_advantage():
    """A method that wins every origin by a hair must be identified as better.

    This is exactly the case an unpaired comparison of means would miss when
    between-origin variance dwarfs the effect — which is the situation here, since
    origins differ by holidays and promotions.
    """
    rng = np.random.default_rng(0)
    difficulty = rng.uniform(0.5, 2.5, size=8)      # shared, large
    a = difficulty * 1.00
    b = difficulty * 1.04                            # B always 4% worse
    out = paired_compare(_frame(a, b), "wspl", "A", "B")

    assert out["a_wins"] == 8, out
    assert out["mean_diff"] < 0
    assert out["sign_test_p"] < 0.01
    assert out["verdict"].startswith("A better at 8/8")

    # The unpaired view is genuinely uninformative on the same data.
    assert np.std(a) > abs(np.mean(a) - np.mean(b)), (
        "test fixture is not exercising the case pairing exists for"
    )


def test_direction_is_respected_for_higher_is_better_metrics():
    a = [100.0, 110.0, 120.0, 130.0]
    b = [90.0, 105.0, 118.0, 125.0]
    lower = paired_compare(_frame(a, b, metric="realized_margin"),
                           "realized_margin", "A", "B", lower_is_better=True)
    higher = paired_compare(_frame(a, b, metric="realized_margin"),
                            "realized_margin", "A", "B", lower_is_better=False)
    assert lower["a_wins"] == 0
    assert higher["a_wins"] == 4


def test_a_coin_flip_is_not_reported_as_significant():
    a = [1.0, 1.2, 0.9, 1.4, 1.1, 1.3, 0.95, 1.25]
    b = [1.2, 1.0, 1.1, 1.2, 1.3, 1.1, 1.15, 1.05]
    out = paired_compare(_frame(a, b), "wspl", "A", "B")
    assert out["sign_test_p"] > 0.05, "an even split was reported as a real difference"


def test_identical_methods_give_no_preference():
    v = [1.0, 1.1, 0.9, 1.3]
    out = paired_compare(_frame(v, list(v)), "wspl", "A", "B")
    assert out["mean_diff"] == pytest.approx(0.0)
    assert out["wilcoxon_p"] == pytest.approx(1.0)


def test_nan_origins_are_dropped_not_propagated():
    a = [1.0, np.nan, 0.9, 1.1]
    b = [1.2, 1.0, 1.1, 1.3]
    out = paired_compare(_frame(a, b), "wspl", "A", "B")
    assert out["n"] == 3, "a NaN origin was not excluded from the pairing"
    assert np.isfinite(out["mean_diff"])


def test_insufficient_origins_is_reported_not_faked():
    out = paired_compare(_frame([1.0], [1.2]), "wspl", "A", "B")
    assert out["verdict"] == "insufficient origins"
    assert "wilcoxon_p" not in out


# ------------------------------------------------------------------ summarise

def _full_frame(n_origins=6):
    rng = np.random.default_rng(7)
    rows = []
    for i in range(n_origins):
        o = f"2026-02-{i + 1:02d}"
        d = rng.uniform(0.8, 1.6)
        for m, mult in ((SYSTEM, 1.00), ("lgbm_quantile", 1.03),
                        ("no calibration", 1.09), ("no reconciliation", 1.05)):
            rows.append({
                "origin": o, "method": m,
                "wspl": d * mult, "wrmsse": d * mult * 1.2,
                "mase": d * mult * 0.8, "bias": -0.02 * mult,
                "realized_margin": 100_000 / mult,
                "fill_rate": 0.9, "lost_margin": 5_000 * mult,
            })
        rows.append({
            "origin": o, "method": "best single (LGBM-Q)",
            "wspl": d * 1.03, "wrmsse": d * 1.24, "mase": d * 0.82, "bias": -0.03,
            "realized_margin": 100_000 / 1.03, "fill_rate": 0.9, "lost_margin": 5_150,
        })
    stages = pd.DataFrame([
        {"origin": f"2026-02-{i + 1:02d}", "anchor": 1.5, "best_single": 1.1,
         "combination": 1.05, "reconciliation": 1.02, "calibration": 1.02}
        for i in range(n_origins)
    ])
    return pd.DataFrame(rows), stages


def test_summary_ranks_by_wspl_and_counts_stage_improvements():
    per_origin, stages = _full_frame()
    s = summarise(per_origin, stages)

    assert s["n_origins"] == 6
    assert s["by_method"][0]["method"] == SYSTEM, "best mean WSPL should sort first"
    assert s["mean_wspl_rank"][SYSTEM] == pytest.approx(1.0)

    ladder = s["stage_ladder"]
    assert ladder["anchor"]["origins_improved_on_previous"] is None
    assert ladder["combination"]["origins_improved_on_previous"] == 6
    # calibration is flat by construction in the fixture, so it must NOT be
    # credited with an improvement
    assert ladder["calibration"]["origins_improved_on_previous"] == 0


def test_summary_reports_std_so_a_single_origin_cannot_masquerade_as_a_result():
    per_origin, stages = _full_frame()
    s = summarise(per_origin, stages)
    row = next(r for r in s["by_method"] if r["method"] == SYSTEM)
    assert "wspl_std" in row and row["wspl_std"] > 0, (
        "spread across origins must be reported alongside the mean"
    )
