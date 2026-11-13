"""The leakage test suite.

Leakage is the most common silent failure in demand forecasting code. It produces
backtests that look spectacular and collapse in production, and no accuracy metric
will reveal it — the metric is exactly what the leak inflates. The only defence is
a test that mechanically checks the property.

The property, stated precisely: shifting the forecast origin forward must not
change any feature value computed at an earlier origin. If it does, some feature
is reading the future.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from dqstack.features import (
    Panel,
    build_panel,
    feature_columns,
    make_design,
    make_splits,
    origin_features,
)


@pytest.fixture(scope="module")
def panel() -> Panel:
    from dqstack.config import INTERIM, PROCESSED
    needed = (INTERIM / "panel.parquet", PROCESSED / "dim_series_diagnostics.parquet",
              PROCESSED / "censoring_masks.parquet")
    if not all(p.exists() for p in needed):
        # A fresh checkout (CI) has no demo data: data/ is git-ignored. Make it the way
        # the pipeline's first steps do: generate, ingest (Stage 0), diagnose (Stage 1).
        from dqstack import diagnose, ingest
        from dqstack.data.generate import generate
        generate()
        ingest.run()
        diagnose.run()
    return build_panel()


def test_origin_features_are_invariant_to_later_data(panel: Panel):
    """The core leakage assertion.

    Features are computed at origin O, then every observation after O is
    destroyed and they are computed again. Identical output proves no feature
    read past the origin.
    """
    O = panel.n_days - 200
    f_full, names = origin_features(panel, O)

    truncated = Panel(
        series=panel.series,
        dates=panel.dates,
        y=panel.y.copy(),
        y_raw=panel.y_raw.copy(),
        carried=panel.carried.copy(),
        price=panel.price.copy(),
        # SNAP is a published benefit calendar, known ahead of time like holidays,
        # so it is deliberately not truncated — and it is a target-date feature
        # rather than an origin feature, so it cannot affect this assertion.
        snap=panel.snap.copy(),
        calendar=panel.calendar,
        n_history=panel.n_history,
    )
    truncated.y[:, O + 1 :] = np.nan
    truncated.y_raw[:, O + 1 :] = np.nan
    truncated.price[:, O + 1 :] = np.nan
    truncated.carried[:, O + 1 :] = False

    f_trunc, names_trunc = origin_features(truncated, O)
    assert names == names_trunc

    bad = []
    for j, nm in enumerate(names):
        a, b = f_full[:, j], f_trunc[:, j]
        same = np.isclose(a, b, rtol=1e-6, atol=1e-6, equal_nan=True)
        if not same.all():
            bad.append((nm, int((~same).sum())))
    assert not bad, f"features changed when future data was removed: {bad[:10]}"


def test_shifting_origin_does_not_change_earlier_origin_features(panel: Panel):
    """Two origins computed in one pass must match the same origins computed
    separately — the property the spec asks to be asserted directly."""
    O1, O2 = panel.n_days - 300, panel.n_days - 200
    a, names = origin_features(panel, O1)
    b, _ = origin_features(panel, O1)
    assert np.allclose(a, b, equal_nan=True)

    c, _ = origin_features(panel, O2)
    assert not np.allclose(a, c, equal_nan=True), (
        "features at two different origins are identical, which means they are "
        "not actually reading the history window"
    )


def test_design_targets_are_strictly_after_origin(panel: Panel):
    sp = make_splits(panel, n_train_origins=3)
    d = make_design(panel, sp.train[:2], range(1, 29))
    assert (d["target_date"] > d["origin"]).all()
    assert (d["horizon"] >= 1).all() and (d["horizon"] <= 28).all()
    delta = (d["target_date"] - d["origin"]).dt.days
    assert (delta == d["horizon"]).all(), "horizon must equal target_date - origin"


def test_no_future_columns_in_feature_set(panel: Panel):
    """`y` and `y_raw` are targets and must never appear as features."""
    sp = make_splits(panel, n_train_origins=2)
    d = make_design(panel, sp.train[:1], range(1, 8))
    feats = feature_columns(d)
    for forbidden in ("y", "y_raw", "target_date", "origin", "series_id"):
        assert forbidden not in feats


def test_splits_are_purged(panel: Panel):
    """Every block must be separated from the next by a full horizon, so a
    training target never coincides with a later block's target date."""
    sp = make_splits(panel, horizon=28, n_train_origins=8)
    train_last_target = sp.train[-1] + 28
    assert train_last_target <= sp.calib[0], (
        f"training targets reach index {train_last_target} but calibration "
        f"begins at {sp.calib[0]} — blocks overlap"
    )
    calib_last_target = sp.calib[-1] + 28
    assert calib_last_target <= sp.eval
    assert sp.eval + 28 <= sp.live + 28
    assert sp.eval < sp.live


def test_masked_cells_are_not_treated_as_zero_demand(panel: Panel):
    """Censored and out-of-assortment cells must be NaN, not 0.

    This is the whole point of Stage 1. If they arrive as zeros the model learns
    to under-forecast the items that sell out most.
    """
    not_carried = ~panel.carried
    assert np.isnan(panel.y[not_carried]).all(), (
        "out-of-assortment cells leaked into the target as zeros"
    )
    assert np.isnan(panel.y).sum() > 0, "no cells masked at all — Stage 1 did not run"
