"""Stage-boundary contracts.

Each stage in the pipeline promises something to the next one. These tests assert
those promises directly, so a regression surfaces as a failing contract rather
than as a slightly worse number in a metrics table that nobody re-reads.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from dqstack.calibrate import GLOBAL as GLOBAL_KEY, ConformalCalibrator
from dqstack.config import QUANTILES
from dqstack.models.dist import fit_dispersion, nb_quantiles, sanitize
from dqstack.reconcile import (
    Reconciler,
    build_hierarchy,
    shrink_covariance,
    temporal_reconcile,
)
from dqstack.schemas import ALL_SCHEMAS, QualityGateFailure, SALES


# ------------------------------------------------------------------ stage 0

def test_quality_gate_rejects_a_broken_feed():
    """A schema violation must be reported as a blocking failure — FR-02."""
    bad = pd.DataFrame(
        {
            "item_id": ["A", "A"],
            "store_id": ["S", "S"],
            "date": pd.to_datetime(["2026-01-01", "2026-01-01"]),   # duplicate PK
            "sales": [3, -8],                                       # negative sales
        }
    )
    errs = SALES.validate(bad)
    assert errs, "validator accepted a feed with a duplicate key and negative sales"
    assert any("primary key" in e for e in errs)
    assert any("below minimum" in e for e in errs)


def test_schema_fingerprint_is_stable_and_sensitive():
    a = SALES.fingerprint()
    assert a == SALES.fingerprint(), "fingerprint must be deterministic"
    others = {s.fingerprint() for s in ALL_SCHEMAS.values()}
    assert len(others) == len(ALL_SCHEMAS), "distinct schemas collided on one fingerprint"


def test_quality_gate_failure_is_an_exception_type():
    assert issubclass(QualityGateFailure, RuntimeError)


# ------------------------------------------------------------------ stage 3

def test_quantiles_are_monotone_and_non_negative_after_sanitize():
    """Independently trained quantile heads cross. Sanitising must fix that."""
    rng = np.random.default_rng(1)
    q = rng.normal(5, 4, size=(200, 28, len(QUANTILES)))     # deliberately crossed
    s = sanitize(q)
    assert (s >= 0).all(), "negative demand survived sanitisation"
    assert (np.diff(s, axis=-1) >= -1e-6).all(), "quantiles still cross after sorting"


def test_clip_happens_before_sort():
    """If sorting came first, clipping afterwards could reintroduce a crossing.
    A row that is entirely negative must collapse to all zeros, in order."""
    q = np.array([[[-9.0, -4.0, -7.0, -1.0, -3.0, -2.0, -6.0, -5.0, -8.0]]])
    s = sanitize(q)
    assert np.allclose(s, 0.0)
    assert (np.diff(s, axis=-1) >= 0).all()


def test_nb_quantiles_collapse_to_zero_at_zero_mean():
    q = nb_quantiles(np.zeros((5, 3)), 4.0)
    assert np.allclose(q, 0.0), "a zero-mean series must have an all-zero distribution"


def test_nb_quantiles_are_ordered_and_widen_with_dispersion():
    mu = np.full((1, 1), 20.0)
    tight = nb_quantiles(mu, 50.0)[0, 0]
    loose = nb_quantiles(mu, 0.5)[0, 0]
    assert (np.diff(tight) >= 0).all()
    hi = QUANTILES.index(0.90)
    lo = QUANTILES.index(0.10)
    assert (loose[hi] - loose[lo]) > (tight[hi] - tight[lo]), (
        "lower dispersion parameter must produce a wider interval"
    )


def test_dispersion_fitting_recovers_overdispersion():
    """Synthetic NB data with known r must be recovered approximately."""
    rng = np.random.default_rng(4)
    n = 40_000
    mu = rng.uniform(2, 40, size=n)
    r_true = 6.0
    y = rng.negative_binomial(r_true, r_true / (r_true + mu))
    out = fit_dispersion(y.astype("float64"), mu, np.full(n, "s", dtype="U4"))
    assert "s" in out
    assert 2.0 < out["s"] < 20.0, f"recovered r={out['s']:.2f}, expected near {r_true}"


# ------------------------------------------------------------------ stage 5

def _toy_series(n_items: int = 6, n_stores: int = 3) -> pd.DataFrame:
    rows = []
    for i in range(n_items):
        for s in range(n_stores):
            rows.append(
                {
                    "series_id": f"I{i}|S{s}",
                    "item_id": f"I{i}",
                    "store_id": f"S{s}",
                    "dept_id": f"D{i % 2}",
                    "cat_id": f"C{i % 2}",
                    "state_id": f"ST{s % 2}",
                    "cluster_id": f"CL{s}",
                }
            )
    return pd.DataFrame(rows)


def test_hierarchy_bottom_rows_are_the_identity():
    series = _toy_series()
    H = build_hierarchy(series)
    bottom = H.S[H.bottom_slice()]
    assert bottom.shape == (len(series), len(series))
    assert np.allclose(bottom, np.eye(len(series))), (
        "the bottom level of the summing matrix must be the identity"
    )


def test_total_row_sums_every_series():
    H = build_hierarchy(_toy_series())
    assert np.allclose(H.S[0], 1.0)


def test_reconciliation_produces_coherent_aggregates():
    """After projection, every aggregate must equal the sum of its components."""
    rng = np.random.default_rng(9)
    series = _toy_series()
    n = len(series)
    resid = rng.normal(0, 2.0, size=(120, n))
    rc = Reconciler().fit(series, resid)

    bottom = rng.uniform(2, 30, size=(n, 28))
    rec = rc.reconcile(bottom)
    agg = rc.H.S @ rec
    for lvl_i, lvl in enumerate(rc.H.node_level):
        members = rc.H.S[lvl_i] > 0
        assert np.allclose(agg[lvl_i], rec[members].sum(axis=0), atol=1e-6)


def test_mint_projection_satisfies_g_times_s_equals_identity():
    """The defining property of the MinT projection.

    A G that misses the identity applies a spurious correction to every forecast
    in the system, which presents as "reconciliation hurts accuracy" and is
    really a conditioning failure. This caught exactly that bug.
    """
    rng = np.random.default_rng(31)
    series = _toy_series(n_items=10, n_stores=4)
    n = len(series)
    rc = Reconciler().fit(series, rng.normal(0, 2.0, size=(200, n)))
    assert rc.projection_error is not None
    assert rc.projection_error < 1e-6, (
        f"|G·S - I|max = {rc.projection_error:.2e}; the projection is ill-conditioned"
    )


def test_coherent_input_is_a_fixed_point():
    """With no independent aggregate forecasts the input is already coherent, so
    reconciliation must return it unchanged. If it does not, the projection is
    injecting noise rather than resolving disagreement."""
    rng = np.random.default_rng(32)
    series = _toy_series(n_items=8, n_stores=3)
    n = len(series)
    rc = Reconciler().fit(series, rng.normal(0, 1.5, size=(200, n)))
    bottom = rng.uniform(1, 30, size=(n, 28))
    assert np.allclose(rc.reconcile(bottom), bottom, atol=1e-5)


def test_independent_level_forecasts_actually_move_the_bottom():
    """When aggregate levels disagree with the sum of the bottom, reconciliation
    must change the bottom — that is the whole point of the stage."""
    rng = np.random.default_rng(33)
    series = _toy_series(n_items=8, n_stores=3)
    n = len(series)
    rc = Reconciler().fit(series, rng.normal(0, 1.5, size=(200, n)))
    bottom = rng.uniform(5, 25, size=(n, 28))
    node_base = rc.H.S @ bottom
    node_base[0] *= 1.35            # the total says demand is 35% higher
    out = rc.reconcile(bottom, node_base)
    assert not np.allclose(out, bottom, atol=1e-3)
    assert out.sum() > bottom.sum(), "an upward aggregate view must raise the bottom"


def test_reconciliation_preserves_quantile_ordering():
    rng = np.random.default_rng(13)
    series = _toy_series()
    n = len(series)
    rc = Reconciler().fit(series, rng.normal(0, 2.0, size=(120, n)))
    base = rng.uniform(1, 20, size=(n, 28, 1))
    q = sanitize(base * (0.4 + 1.6 * np.asarray(QUANTILES)[None, None, :]))
    out = rc.reconcile_quantiles(q)
    assert (np.diff(out, axis=-1) >= -1e-5).all(), "reconciliation inverted the fan"
    assert (out >= 0).all()


def test_shrinkage_intensity_is_a_valid_proportion():
    rng = np.random.default_rng(2)
    W, lam = shrink_covariance(rng.normal(0, 1, size=(50, 30)))
    assert 0.0 <= lam <= 1.0
    assert np.allclose(W, W.T, atol=1e-8), "covariance estimate must be symmetric"
    assert (np.linalg.eigvalsh(W) > -1e-8).all(), "covariance must be PSD"


def test_temporal_reconciliation_is_arithmetically_consistent():
    rng = np.random.default_rng(6)
    daily = rng.uniform(0, 40, size=(50, 28))
    out = temporal_reconcile(daily)
    assert np.allclose(out["daily"].sum(axis=1), out["monthly"][:, 0], atol=1e-6)
    assert np.allclose(out["weekly"].sum(axis=1), out["monthly"][:, 0], atol=1e-6)
    weekly_from_daily = out["daily"].reshape(50, 4, 7).sum(axis=2)
    assert np.allclose(weekly_from_daily, out["weekly"], atol=1e-6), (
        "daily values must sum to their own weekly buckets"
    )


# ------------------------------------------------------------------ stage 6

def test_conformal_calibration_improves_coverage():
    """The stage must measurably move empirical coverage toward nominal.

    Base quantiles are deliberately too narrow, which is the documented failure
    mode of both quantile regressors and foundation models.
    """
    rng = np.random.default_rng(21)
    n = 30_000
    y = rng.negative_binomial(5, 5 / (5 + 18), size=n).astype("float64")
    narrow = np.quantile(y, QUANTILES)[None, :] * 0.55 + y.mean() * 0.45
    q = np.repeat(narrow, n, axis=0)

    pc = np.full(n, "smooth", dtype="U20")
    hg = np.full(n, "h01_07", dtype="U12")
    half = n // 2
    cal = ConformalCalibrator().fit(y[:half], q[:half], pc[:half], hg[:half])
    q_after = cal.apply(q[half:], pc[half:], hg[half:])

    tbl = cal.coverage_table(y[half:], q[half:], q_after)
    assert tbl["gap_after"].mean() < tbl["gap_before"].mean(), (
        f"calibration did not improve coverage: "
        f"{tbl['gap_before'].mean():.4f} -> {tbl['gap_after'].mean():.4f}"
    )
    assert tbl["gap_after"].mean() < 0.03, "post-calibration coverage still far from nominal"


def test_median_is_never_conformally_shifted():
    """CQR conformalises interval bounds, not the point forecast.

    On zero-inflated demand P(y <= median) is bounded below by P(y = 0), so a
    nominal 0.50 target is unachievable; a procedure that chases it biases every
    median upward. That regression cost 19% of WRMSSE before this was pinned.
    """
    rng = np.random.default_rng(41)
    n = 20_000
    y = rng.negative_binomial(3, 3 / (3 + 8), size=n).astype("float64")
    y[rng.random(n) < 0.43] = 0.0                    # heavy zero inflation
    q = np.repeat(np.quantile(y, QUANTILES)[None, :] * 0.6, n, axis=0)
    pc = np.full(n, "intermittent", dtype="U20")
    hg = np.full(n, "h01_07", dtype="U12")

    cal = ConformalCalibrator().fit(y, q, pc, hg)
    med_k = QUANTILES.index(0.50)
    for stratum, off in cal.offsets.items():
        assert off[med_k] == 0.0, f"stratum {stratum} shifted the median by {off[med_k]}"

    out = cal.apply(q, pc, hg)
    assert np.allclose(out[:, med_k], q[:, med_k], atol=1e-6), (
        "the median moved even though its offset was zero — check apply()"
    )


def test_offsets_are_declined_when_they_do_not_help_out_of_sample():
    """An already-calibrated forecast must be left alone.

    The guard exists because on zero-inflated data the lower-tail targets are
    also unreachable, and 'adjusting' toward them makes coverage worse.
    """
    from scipy import stats as sstats

    rng = np.random.default_rng(42)
    n = 40_000
    mu, sd = 50.0, 12.0
    y = rng.normal(mu, sd, size=n)
    q = np.repeat(sstats.norm.ppf(np.asarray(QUANTILES), mu, sd)[None, :], n, axis=0)
    pc = np.full(n, "smooth", dtype="U20")
    hg = np.full(n, "h01_07", dtype="U12")

    cal = ConformalCalibrator().fit(y, q, pc, hg)

    # The property that matters is not that every offset is declined — sampling
    # noise means a negligible shift can genuinely reduce the gap on the check
    # half, and keeping it is harmless. What must hold is that no *material*
    # correction is applied to a forecast that was already right.
    worst = float(np.abs(cal.offsets[GLOBAL_KEY]).max())
    assert worst < 0.05 * sd, (
        f"largest offset {worst:.3f} is {worst / sd:.1%} of sigma on an "
        "already-calibrated forecast; the guard is letting through real distortion"
    )
    tbl = cal.adjustment_report()
    assert len(tbl) == len(QUANTILES)
    assert tbl.loc[tbl["nominal"] == 0.50, "reason"].iloc[0].startswith("point forecast")

    # And coverage must not get worse anywhere.
    out = cal.apply(q, pc, hg)
    before = cal.coverage_table(y, q, out)
    assert before["gap_after"].mean() <= before["gap_before"].mean() + 1e-3


def test_calibration_output_stays_monotone_and_non_negative():
    rng = np.random.default_rng(22)
    n = 5_000
    y = rng.poisson(6, size=n).astype("float64")
    q = np.sort(rng.uniform(0, 12, size=(n, len(QUANTILES))), axis=1)
    pc = np.full(n, "lumpy", dtype="U20")
    hg = np.full(n, "h15_28", dtype="U12")
    cal = ConformalCalibrator().fit(y, q, pc, hg)
    out = cal.apply(q, pc, hg)
    assert (out >= 0).all()
    assert (np.diff(out, axis=-1) >= -1e-6).all()


def test_thin_strata_fall_back_to_global():
    rng = np.random.default_rng(23)
    n = 900                          # below min_stratum for any single stratum
    y = rng.poisson(4, size=n).astype("float64")
    q = np.sort(rng.uniform(0, 9, size=(n, len(QUANTILES))), axis=1)
    pc = np.full(n, "cold_start", dtype="U20")
    hg = np.full(n, "h01_07", dtype="U12")
    cal = ConformalCalibrator().fit(y, q, pc, hg)
    assert "__global__" in cal.offsets
    out = cal.apply(q, pc, hg)
    assert out.shape == q.shape


def test_pit_is_uniform_for_a_calibrated_forecast():
    from scipy import stats as sstats

    rng = np.random.default_rng(24)
    n = 8_000
    mu, sd = 30.0, 7.0
    y = rng.normal(mu, sd, size=n)
    q = np.repeat(sstats.norm.ppf(np.asarray(QUANTILES), mu, sd)[None, :], n, axis=0)
    cal = ConformalCalibrator()
    pit = cal.pit_values(y, q)
    inside = pit[(pit > 0) & (pit < 1)]
    ks = sstats.kstest(inside, "uniform").statistic
    assert ks < 0.10, f"PIT is not uniform for a correctly specified forecast (KS={ks:.3f})"
