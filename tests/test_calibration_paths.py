"""Stage 6 is fitted on the forecast it is applied to.

Orders read the unreconciled forecast (ReconcileConfig.for_decisions = False); the
totals views read the reconciled one. Their errors differ, so each has its own
calibrator. One calibrator fitted on the reconciled forecast and applied to both
corrected the orders' forecast for errors it does not have.

Saved models are pickles kept in the app's database. A model saved before the
orders calibrator existed must still load and forecast exactly as it did.
"""

import gzip
import io
import pickle
import re
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
from scipy import stats

import dqstack
from dqstack import combine, live
from dqstack.calibrate import GLOBAL, ConformalCalibrator
from dqstack.config import CFG, HORIZON, Q50_INDEX, QUANTILES

TAUS = np.asarray(QUANTILES)
M = Q50_INDEX
LOW = [k for k, t in enumerate(TAUS) if t < 0.5]
HIGH = [k for k, t in enumerate(TAUS) if t > 0.5]


def _calibrator(lower_factor: float, upper_offset: float) -> ConformalCalibrator:
    """A fitted-looking calibrator with known corrections."""
    c = ConformalCalibrator()
    c.offsets = {GLOBAL: np.where(np.arange(len(TAUS)) > M, upper_offset, 0.0)}
    c.adjusted = {GLOBAL: [True] * len(TAUS)}
    c.scales = {GLOBAL: np.where(np.arange(len(TAUS)) < M, lower_factor, 1.0)}
    return c


def _legacy_calibrator() -> ConformalCalibrator:
    """What the class pickled before factors existed: additive offsets everywhere."""
    c = ConformalCalibrator()
    c.offsets = {GLOBAL: np.array([-2.0, -1.0, -0.5, 0.0, 1.0, 1.5, 2.0, 2.5, 3.0])}
    c.adjusted = {GLOBAL: [True] * len(TAUS)}
    for name in ("scales", "level_edges", "level_scales"):
        del c.__dict__[name]
    return c


def _blob(model: dict) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as f:
        pickle.dump(model, f, protocol=pickle.HIGHEST_PROTOCOL)
    return buf.getvalue()


def _fake_engine(monkeypatch, combined: np.ndarray):
    """forecast_panel without trained models: the combined forecast is given."""
    import dqstack.pipeline as pipeline

    monkeypatch.setattr(pipeline, "candidate_quantiles", lambda P, O, h, tr: ({}, {}))
    monkeypatch.setattr(combine, "weights_from_strata", lambda pc, tbl, n: np.ones((len(pc), n)))
    monkeypatch.setattr(combine, "combine_quantiles", lambda qs, W, cands: combined.copy())


def _panel(n: int):
    return SimpleNamespace(series=pd.DataFrame({"planning_class": ["smooth"] * n,
                                                "series_id": [f"s{i}" for i in range(n)]}))


def _combined(n: int) -> np.ndarray:
    base = np.array([2.0, 3.0, 5.0, 8.0, 11.0, 15.0, 18.0, 21.0, 25.0])
    return np.repeat(np.repeat(base[None, None, :], HORIZON, axis=1), n, axis=0)


def _model(**calibrators) -> dict:
    return {"trained": None, "meta": SimpleNamespace(candidates=["a", "b"]),
            "weights_method": "stack", "stack_tbl": None, "meta_extra": None,
            "series": ["s0", "s1", "s2"], "cfg": CFG, **calibrators}


def test_the_forecast_orders_read_is_calibrated_by_its_own_calibrator(monkeypatch):
    combined = _combined(3)
    _fake_engine(monkeypatch, combined)
    reported, orders = _calibrator(0.2, 9.0), _calibrator(0.9, 1.0)
    model = live.load_model(_blob(_model(conformal=reported, conformal_orders=orders)))

    q = live.forecast_panel(model, _panel(3), None, 0, CFG)
    pc2 = np.full((3, HORIZON), "smooth", dtype="U20")
    hg2 = np.broadcast_to(np.array([live.horizon_group_of(h) for h in range(1, HORIZON + 1)],
                                   dtype="U12"), pc2.shape)
    assert np.array_equal(q, model["conformal_orders"].apply(combined, pc2, hg2))
    assert not np.array_equal(q, model["conformal"].apply(combined, pc2, hg2))
    assert np.array_equal(q[..., M], combined[..., M].astype("float32")), "median moved"


def test_a_model_saved_before_the_orders_calibrator_still_forecasts_as_it_did(monkeypatch):
    """No `conformal_orders` key, and a calibrator from before the lower-tail factor:
    the forecast is what that model always produced."""
    combined = _combined(3)
    _fake_engine(monkeypatch, combined)
    old = live.load_model(_blob(_model(conformal=_legacy_calibrator())))
    assert "conformal_orders" not in old
    assert live.orders_calibrator(old) is old["conformal"]

    q = live.forecast_panel(old, _panel(3), None, 0, CFG)
    expected = old["conformal"]._monotone_about_median(
        combined.reshape(-1, len(TAUS)) + old["conformal"].offsets[GLOBAL]
    ).reshape(combined.shape)
    assert np.array_equal(q, expected)


@dataclass
class _Trained:
    """Stands in for the trained candidates; saved_model drops the ablation arm."""
    gbm_nomask: object = None
    P_nomask: object = None


def test_a_saved_model_carries_both_calibrators():
    reported, orders = _calibrator(0.2, 9.0), _calibrator(0.9, 1.0)
    state = {"trained": _Trained(), "meta": SimpleNamespace(candidates=["a"]),
             "conformal": reported, "conformal_orders": orders,
             "weights_method": "stack", "stack_tbl": None, "meta_extra": None,
             "P": _panel(2)}
    model = live.load_model(live.saved_model(state))
    assert np.array_equal(live.orders_calibrator(model).scales[GLOBAL], orders.scales[GLOBAL])
    assert np.array_equal(model["conformal"].offsets[GLOBAL], reported.offsets[GLOBAL])


def test_the_engine_applies_the_orders_calibrator_on_every_orders_path():
    """pipeline (eval and live origins), the backtest and the live forecast.

    A source check, like the one for the median index: reverting any of these to
    the reconciled calibrator would pass every numeric test on a fixture where
    the two forecasts happen to coincide.
    """
    root = Path(dqstack.__file__).parent
    pipe = (root / "pipeline.py").read_text(encoding="utf-8")
    bt = (root / "backtest.py").read_text(encoding="utf-8")
    lv = (root / "live.py").read_text(encoding="utf-8")
    assert pipe.count("conformal_orders.apply(comb_") == 2           # eval and live origins
    assert "conformal_orders.apply(cq," in bt
    assert "orders_calibrator(model).apply(combined" in lv
    for src in (pipe, bt):
        assert not re.search(r"\bconformal\.apply\((comb_e|comb_l|cq)\b", src)
    assert 'model["conformal"].apply(' not in lv


def test_a_calibrator_fitted_on_another_forecast_misses_and_its_own_does_not():
    """The mismatch in miniature. The reconciled forecast is the orders' forecast
    moved by a per-row error, as MinT moves a leaf's whole distribution with its
    median. Fitted on that and applied to the orders' forecast, every floor falls
    below its stated level and the upper levels over-cover; fitted on the orders'
    forecast, every level is within 1.5 points."""
    rng = np.random.default_rng(301)
    n_series, reps = 500, 40
    mu = np.exp(rng.uniform(np.log(3), np.log(40), n_series))
    r = rng.uniform(2.0, 8.0, n_series)
    p = r / (r + mu)
    exact = stats.nbinom.ppf(TAUS[None, :], r[:, None], p[:, None])
    q_orders = 0.6 * exact + 0.4 * exact[:, [M]]                 # too narrow
    s = np.repeat(np.arange(n_series), reps)
    y = rng.negative_binomial(r[s], p[s]).astype("float64")
    q_o = q_orders[s]
    shift = rng.normal(0.0, 0.5, len(s)) * mu[s]                  # reconciliation's error
    q_r = np.maximum(q_o + shift[:, None], 0.0)
    pc = np.full(len(s), "smooth", dtype="U20")
    hg = np.full(len(s), "h01_07", dtype="U12")

    fitted_elsewhere = ConformalCalibrator().fit(y, q_r, pc, hg)
    fitted_here = ConformalCalibrator().fit(y, q_o, pc, hg)

    y_new = rng.negative_binomial(r[s], p[s]).astype("float64")
    wrong = fitted_elsewhere.apply(q_o, pc, hg)
    right = fitted_here.apply(q_o, pc, hg)
    for k in LOW:                       # a floor: P(y < q) <= tau, as close as it can
        assert float((y_new < wrong[:, k]).mean()) < TAUS[k] - 0.01, "scenario"
        assert TAUS[k] - 0.015 <= float((y_new < right[:, k]).mean()) <= TAUS[k] + 0.01
    for k in HIGH:                      # a ceiling: P(y <= q) close to tau
        assert abs(float((y_new <= right[:, k]).mean()) - TAUS[k]) < 0.015
    assert float((y_new <= wrong[:, HIGH[0]]).mean()) > TAUS[HIGH[0]] + 0.04, "scenario"
    assert np.array_equal(right[:, M], q_o[:, M].astype("float32"))
