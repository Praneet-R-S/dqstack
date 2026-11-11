"""Stage 6 below the median: the stated floor must hold under the strict definition.

For count data a level tau is honest when the share of outcomes *strictly* below it
is at most tau; outcomes equal to it (most often zeros) are not misses. The additive
per-stratum offset met that on the calibration window and broke at the next origins,
in two ways these tests reproduce: a level change between origins (one offset in
units cannot fit every series size), and an upward shift of zero-heavy series (a
positive floor under a series that sells nothing most days). The fix is a factor per
level, from the strict conformal rank. It must not touch the median or the upper
levels, which set stock.
"""

import pickle

import numpy as np
from scipy import stats

from dqstack.calibrate import GLOBAL, ConformalCalibrator
from dqstack.config import Q50_INDEX, QUANTILES

TAUS = np.asarray(QUANTILES)
LOW = [k for k, t in enumerate(TAUS) if t < 0.5]
M = Q50_INDEX


def _labels(n: int, planning_class: str):
    return np.full(n, planning_class, dtype="U20"), np.full(n, "h01_07", dtype="U12")


def _volume_series(rng, n_series: int = 600):
    """Series selling every day, sizes from 2 to 60 a day."""
    return np.exp(rng.uniform(np.log(2), np.log(60), n_series)), rng.uniform(2.0, 8.0, n_series)


def _draw_volume(rng, mu, r, reps: int, level: float = 1.0):
    """Outcomes and a forecast that tracks the level but is too narrow, so its
    lower levels sit too high (about 21% of outcomes below the stated 5% floor)."""
    m = mu * level
    p = r / (r + m)
    exact = stats.nbinom.ppf(TAUS[None, :], r[:, None], p[:, None])
    q = 0.55 * exact + 0.45 * exact[:, [M]]
    s = np.repeat(np.arange(len(mu)), reps)
    return rng.negative_binomial(r[s], p[s]).astype("float64"), q[s]


def _draw_zero_heavy(rng, mu, r, reps: int, shift: float, zero_share: float = 0.7):
    """Series that sell nothing on 70% of days. The forecast is blind to the zeros
    and shifted upward by `shift` units, as reconciliation shifts a whole
    distribution with its median."""
    p = r / (r + mu)
    q = np.maximum(stats.nbinom.ppf(TAUS[None, :], r[:, None], p[:, None]) + shift, 0.0)
    s = np.repeat(np.arange(len(mu)), reps)
    y = rng.negative_binomial(r[s], p[s]).astype("float64")
    y[rng.random(len(s)) < zero_share] = 0.0
    return y, q[s]


def _additive_floor(y_fit, q_fit, q_new, k):
    """The rule this replaces below the median: one conformal offset in units."""
    d = float(np.quantile(y_fit - q_fit[:, k], TAUS[k]))
    return np.maximum(q_new[:, k] + d, 0.0)


def _strict(y, q):
    return float((y < q).mean())


def test_the_floor_holds_when_the_level_moves_between_origins():
    """Fitted at one level, applied where demand is 2.5 times higher (a season, a
    promotion).

    One offset in units is too small for series that have grown, so the additive
    rule goes over its stated floor; the factor scales with each series.
    """
    rng = np.random.default_rng(101)
    mu, r = _volume_series(rng)
    y_fit, q_fit = _draw_volume(rng, mu, r, reps=30)
    cal = ConformalCalibrator().fit(y_fit, q_fit, *_labels(len(y_fit), "smooth"))

    y_up, q_up = _draw_volume(rng, mu, r, reps=30, level=2.5)
    out = cal.apply(q_up, *_labels(len(y_up), "smooth"))
    for k in LOW:
        tau = TAUS[k]
        assert _strict(y_up, q_up[:, k]) > tau + 0.10, "the raw floor should be too high here"
        assert _strict(y_up, _additive_floor(y_fit, q_fit, q_up, k)) > tau + 0.03, (
            "scenario no longer separates the additive rule from the factor")
        assert _strict(y_up, out[:, k]) <= tau, (
            f"{_strict(y_up, out[:, k]):.4f} of outcomes fell below the stated {tau:.0%} floor")


def test_the_floor_is_as_close_to_nominal_as_it_can_be_without_going_over():
    """Where nothing moved, the factor must not buy honesty by over-widening."""
    rng = np.random.default_rng(102)
    mu, r = _volume_series(rng)
    y_fit, q_fit = _draw_volume(rng, mu, r, reps=30)
    cal = ConformalCalibrator().fit(y_fit, q_fit, *_labels(len(y_fit), "smooth"))

    y_new, q_new = _draw_volume(rng, mu, r, reps=30)
    out = cal.apply(q_new, *_labels(len(y_new), "smooth"))
    for k in LOW:
        tau = TAUS[k]
        share = _strict(y_new, out[:, k])
        # 18,000 fresh outcomes: about 1.5 points of sampling noise at the 10% level
        assert tau - 0.025 <= share <= tau + 0.01, f"{share:.4f} below the {tau:.0%} floor"


def test_zero_heavy_series_get_the_honest_count_floor_of_zero():
    """70% zeros: the true 5%, 10% and 25% levels are zero.

    The additive rule kept a positive floor under these series; shifted upward at
    the next origin, that floor sat above the zeros and three quarters of outcomes
    fell below a stated 5% floor. The factor learns zero, which no shift moves, and
    is calibrated in the count sense: tau inside [P(y < q), P(y <= q)].
    """
    rng = np.random.default_rng(103)
    mu, r = rng.uniform(1.0, 10.0, 600), np.full(600, 3.0)
    y_fit, q_fit = _draw_zero_heavy(rng, mu, r, reps=30, shift=1.0)
    cal = ConformalCalibrator().fit(y_fit, q_fit, *_labels(len(y_fit), "intermittent"))

    y_new, q_new = _draw_zero_heavy(rng, mu, r, reps=30, shift=4.0)
    out = cal.apply(q_new, *_labels(len(y_new), "intermittent"))
    for k in LOW:
        tau = TAUS[k]
        assert _strict(y_new, _additive_floor(y_fit, q_fit, q_new, k)) > 0.5
        assert _strict(y_new, out[:, k]) <= tau
        assert float((y_new <= out[:, k]).mean()) >= tau, "floor below the tau-quantile"

    report = cal.adjustment_report().set_index("nominal")
    assert report.loc[0.05, "reason"].startswith("scaled")
    assert report.loc[0.05, "global_scale"] == 0.0


def test_the_median_and_upper_levels_are_computed_exactly_as_before():
    """They set stock. The lower-tail change must not reach them at all.

    Two checks: the upper offsets are still the documented additive conformal ones,
    and the median and every upper level come out bit-identical when only the lower
    levels of the inputs are changed.
    """
    rng = np.random.default_rng(104)
    mu, r = _volume_series(rng, 300)
    y_v, q_v = _draw_volume(rng, mu, r, reps=30)
    mu2, r2 = rng.uniform(1.0, 10.0, 300), np.full(300, 3.0)
    y_z, q_z = _draw_zero_heavy(rng, mu2, r2, reps=30, shift=1.0)
    y = np.concatenate([y_v, y_z])
    q = np.concatenate([q_v, q_z])
    pc = np.concatenate([_labels(len(y_v), "smooth")[0], _labels(len(y_z), "intermittent")[0]])
    hg = np.full(len(y), "h01_07", dtype="U12")

    cal = ConformalCalibrator().fit(y, q, pc, hg)
    half = (np.arange(len(y)) % 2) == 0
    for k in range(M + 1, len(TAUS)):
        tau = TAUS[k]
        d = float(np.quantile(y[half] - q[half, k], tau))
        without = abs(float((y[~half] <= q[~half, k]).mean()) - tau)
        with_ = abs(float((y[~half] <= q[~half, k] + d).mean()) - tau)
        assert cal.offsets[GLOBAL][k] == (d if with_ < without - 1e-9 else 0.0)
    assert cal.offsets[GLOBAL][M] == 0.0

    # Same outcomes, same median and upper levels, different lower levels.
    shrink = rng.uniform(0.3, 1.0, size=(len(q), 1))
    q_alt = q.copy()
    q_alt[:, :M] *= shrink
    cal_alt = ConformalCalibrator().fit(y, q_alt, pc, hg)
    q_new, q_new_alt = q[::-1].copy(), q_alt[::-1].copy()
    out = cal.apply(q_new, pc, hg)
    out_alt = cal_alt.apply(q_new_alt, pc, hg)
    assert np.array_equal(out[:, M:], out_alt[:, M:])
    assert np.array_equal(out[:, M], q_new[:, M].astype("float32"))


def test_lower_levels_stay_monotone_non_negative_and_under_the_median():
    rng = np.random.default_rng(105)
    mu, r = _volume_series(rng, 300)
    y_v, q_v = _draw_volume(rng, mu, r, reps=20)
    y_z, q_z = _draw_zero_heavy(rng, rng.uniform(1.0, 10.0, 300), np.full(300, 3.0),
                                reps=20, shift=2.0)
    y = np.concatenate([y_v, y_z])
    q = np.concatenate([q_v, q_z])
    pc = np.concatenate([_labels(len(y_v), "smooth")[0], _labels(len(y_z), "lumpy")[0]])
    hg = np.full(len(y), "h08_14", dtype="U12")
    cal = ConformalCalibrator().fit(y, q, pc, hg)
    out = cal.apply(q.reshape(20, -1, len(TAUS)), pc.reshape(20, -1), hg.reshape(20, -1))
    assert out.shape == (20, len(y) // 20, len(TAUS))
    assert (out >= 0).all()
    assert (np.diff(out, axis=-1) >= -1e-6).all()
    assert (out[..., :M] <= out[..., [M]] + 1e-6).all()


def test_an_already_calibrated_floor_is_left_where_it_is():
    """A correctly specified continuous forecast: the factor must stay at about 1."""
    rng = np.random.default_rng(106)
    n, mu, sd = 40_000, 50.0, 12.0
    y = rng.normal(mu, sd, size=n)
    q = np.repeat(stats.norm.ppf(TAUS, mu, sd)[None, :], n, axis=0)
    cal = ConformalCalibrator().fit(y, q, *_labels(n, "smooth"))
    for s, factors in cal.scales.items():
        moved = np.abs(factors[:M] - 1.0) * q[0, :M]
        assert moved.max() < 0.05 * sd, f"stratum {s} moved a calibrated floor by {moved.max():.2f}"


def test_a_thin_stratum_falls_back_by_size_not_to_the_zero_heavy_pool():
    """A new product too thin for its own factor must not inherit a floor of zero.

    Pooled over everything, the factor is set by the zero-heavy series (it is 0 at
    the 5% level). Those series sit at low predicted medians, so the fallback, which
    uses series of similar predicted median, gives a fast-moving new product the
    floor of other fast movers instead.
    """
    rng = np.random.default_rng(107)
    mu, r = _volume_series(rng, 400)
    y_v, q_v = _draw_volume(rng, mu, r, reps=30)
    y_z, q_z = _draw_zero_heavy(rng, rng.uniform(0.3, 2.0, 600), np.full(600, 3.0),
                                reps=30, shift=1.0)
    mu_new = rng.uniform(20.0, 60.0, 40)
    r_new = rng.uniform(2.0, 8.0, 40)
    y_n, q_n = _draw_volume(rng, mu_new, r_new, reps=4)          # 160 rows: too thin
    y = np.concatenate([y_v, y_z, y_n])
    q = np.concatenate([q_v, q_z, q_n])
    pc = np.concatenate([_labels(len(y_v), "smooth")[0], _labels(len(y_z), "intermittent")[0],
                         _labels(len(y_n), "cold_start")[0]])
    hg = np.full(len(y), "h01_07", dtype="U12")
    cal = ConformalCalibrator().fit(y, q, pc, hg)
    assert "cold_start|h01_07" not in cal.scales
    assert cal.scales[GLOBAL][0] == 0.0, "scenario: the pooled factor should be zero"

    y_t, q_t = _draw_volume(rng, mu_new, r_new, reps=200)
    out = cal.apply(q_t, *_labels(len(y_t), "cold_start"))
    assert float((out[:, 0] > 0).mean()) > 0.9, "the new products' floor was zeroed"
    for k in LOW:
        assert _strict(y_t, out[:, k]) <= TAUS[k] + 0.02


def test_a_calibrator_saved_before_the_factor_keeps_its_additive_floor():
    """Saved models are pickles (live.py). One written before this change has no
    factors and must apply exactly as it did until it is retrained."""
    cal = ConformalCalibrator()
    cal.offsets = {GLOBAL: np.array([-2.0, -1.0, -0.5, 0.0, 1.0, 2.0, 3.0, 4.0, 5.0])}
    cal.adjusted = {GLOBAL: [True] * len(TAUS)}
    for name in ("scales", "level_edges", "level_scales"):
        del cal.__dict__[name]                 # what the earlier class pickled
    old = pickle.loads(pickle.dumps(cal))

    q = np.array([[3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0, 14.0, 16.0]])
    out = old.apply(q, np.array(["smooth"]), np.array(["h01_07"]))
    assert np.array_equal(out, old._monotone_about_median(q + old.offsets[GLOBAL]))
    assert len(old.adjustment_report()) == len(TAUS)
