"""Stage 6 above the median is validated forward in time.

Interleaved halves share every origin, so they cannot see a calibration window that
drifts. On the demo data the uncalibrated orders forecast over-covered at its upper
levels early in the window, less so at its end, and under-covered by the evaluation
origins; offsets fitted on the whole window removed a surplus that had already gone.
Given each row's origin, an upper offset is fitted on the earlier origins, kept only if
it lowers that level's pinball loss on the latest one, and then estimated on the
latest one. The floors (below the median) keep their own guard and do not change.
"""

import re
from pathlib import Path

import numpy as np
from scipy import stats

import dqstack
from dqstack.calibrate import GLOBAL, ConformalCalibrator, _pinball
from dqstack.config import Q50_INDEX, QUANTILES

TAUS = np.asarray(QUANTILES)
M = Q50_INDEX
HIGH = [k for k, t in enumerate(TAUS) if t > 0.5]


def _origins(rng, levels, n_series=600, reps=5, narrow=1.0):
    """Rows for consecutive origins. The forecast is the NB at a fixed level; demand
    is drawn at `level` times it, so a level below 1 means the forecast is too high.
    `narrow` < 1 shrinks the forecast towards its median (too narrow)."""
    mu = np.exp(rng.uniform(np.log(5), np.log(40), n_series))
    r = np.full(n_series, 6.0)
    exact = stats.nbinom.ppf(TAUS[None, :], r[:, None], (r / (r + mu))[:, None])
    q_series = narrow * exact + (1 - narrow) * exact[:, [M]]
    ys, qs, os_ = [], [], []
    for o, g in enumerate(levels):
        s = np.repeat(np.arange(n_series), reps)
        m = mu[s] * g
        ys.append(rng.negative_binomial(r[s], r[s] / (r[s] + m)).astype("float64"))
        qs.append(q_series[s])
        os_.append(np.full(len(s), o))
    return np.concatenate(ys), np.concatenate(qs), np.concatenate(os_)


def _labels(n):
    return np.full(n, "smooth", dtype="U20"), np.full(n, "h01_07", dtype="U12")


def test_upper_offsets_from_a_window_that_has_drifted_are_declined():
    """Demand climbs through the window to where the forecast is right at its last
    origin, and past it at the next. Halves lower every upper level for the early
    surplus; the forward check sees the latest origin needs nothing."""
    rng = np.random.default_rng(401)
    y, q, o = _origins(rng, [0.80, 0.85, 0.90, 0.95, 1.00])
    pc, hg = _labels(len(y))
    halves = ConformalCalibrator().fit(y, q, pc, hg)
    forward = ConformalCalibrator().fit(y, q, pc, hg, origin=o)
    assert forward.upper_check == "latest origin" and halves.upper_check == "interleaved halves"
    assert (halves.offsets[GLOBAL][HIGH] < 0).all(), "scenario: halves should lower the top"
    assert (forward.offsets[GLOBAL][HIGH] == 0).all()

    y_next, q_next, _ = _origins(np.random.default_rng(402), [1.05])
    pcn, hgn = _labels(len(y_next))
    out_h, out_f = halves.apply(q_next, pcn, hgn), forward.apply(q_next, pcn, hgn)
    loss_h = sum(_pinball(y_next, out_h[:, k].astype("float64"), TAUS[k]) for k in HIGH)
    loss_f = sum(_pinball(y_next, out_f[:, k].astype("float64"), TAUS[k]) for k in HIGH)
    assert loss_f < loss_h
    k90 = QUANTILES.index(0.90)
    assert abs((y_next <= out_f[:, k90]).mean() - 0.9) < abs((y_next <= out_h[:, k90]).mean() - 0.9)


def test_a_correction_the_latest_origin_still_needs_is_kept():
    """A forecast that is too narrow at every origin: the forward check must keep the
    widening, and the next origin's upper levels come out close to nominal."""
    rng = np.random.default_rng(403)
    y, q, o = _origins(rng, [1.0] * 5, narrow=0.6)
    pc, hg = _labels(len(y))
    cal = ConformalCalibrator().fit(y, q, pc, hg, origin=o)
    assert (cal.offsets[GLOBAL][HIGH[:-1]] > 0).all()
    y_next, q_next, _ = _origins(np.random.default_rng(404), [1.0], narrow=0.6)
    out = cal.apply(q_next, *_labels(len(y_next)))
    for k in HIGH[:-1]:
        assert abs(float((y_next <= out[:, k]).mean()) - TAUS[k]) < 0.02


def test_the_floors_and_the_median_do_not_depend_on_the_origins():
    rng = np.random.default_rng(405)
    y, q, o = _origins(rng, [0.8, 0.9, 1.0, 1.1, 1.2], narrow=0.7)
    pc, hg = _labels(len(y))
    halves = ConformalCalibrator().fit(y, q, pc, hg)
    forward = ConformalCalibrator().fit(y, q, pc, hg, origin=o)
    for s in halves.scales:
        assert np.array_equal(halves.scales[s], forward.scales[s])
    assert forward.offsets[GLOBAL][M] == 0.0
    out = forward.apply(q, pc, hg)
    assert np.array_equal(out[:, M], q[:, M].astype("float32"))
    assert (np.diff(out, axis=-1) >= -1e-6).all() and (out >= 0).all()


def test_one_origin_or_none_falls_back_to_the_halves():
    rng = np.random.default_rng(406)
    y, q, _ = _origins(rng, [0.9] * 3)
    pc, hg = _labels(len(y))
    none = ConformalCalibrator().fit(y, q, pc, hg)
    single = ConformalCalibrator().fit(y, q, pc, hg, origin=np.zeros(len(y)))
    assert single.upper_check == "interleaved halves"
    for s in none.offsets:
        assert np.array_equal(none.offsets[s], single.offsets[s])


def test_a_calibrator_pickled_before_the_forward_check_applies_as_it_did():
    """apply() never reads the new field: a saved calibrator without it forecasts the same."""
    rng = np.random.default_rng(407)
    y, q, o = _origins(rng, [0.85, 0.9, 1.0])
    pc, hg = _labels(len(y))
    cal = ConformalCalibrator().fit(y, q, pc, hg)
    before = cal.apply(q, pc, hg)
    del cal.__dict__["upper_check"]                 # what the earlier class pickled
    assert np.array_equal(cal.apply(q, pc, hg), before)
    assert len(cal.adjustment_report()) == len(TAUS)


def test_the_pipeline_gives_every_calibrator_its_origins():
    src = (Path(dqstack.__file__).parent / "pipeline.py").read_text(encoding="utf-8")
    fits = re.findall(r"ConformalCalibrator\(\)\.fit\((.*?)\)\s*(?:if|\n)", src, flags=re.S)
    assert len(fits) == 3
    assert all("origin=corg_a" in f for f in fits)
