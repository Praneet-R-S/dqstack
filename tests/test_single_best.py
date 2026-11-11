"""Stage 4: issue the single best method when it earns more than the blend.

Training keeps the method with the lowest loss on the meta origins and a calibrator
fitted on that method's own forecasts. A saved model can then be issued two ways
(live.issued_as): the blend, or that method alone. The app's training scores both on
the same recent weeks and records which one the version issues; these tests cover
the forecast each way gives and what a saved model keeps.
"""

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import dqstack
from dqstack import combine, live
from dqstack.calibrate import GLOBAL, ConformalCalibrator
from dqstack.config import CFG, HORIZON, Q50_INDEX, QUANTILES

TAUS = np.asarray(QUANTILES)
M = Q50_INDEX


def _calibrator(lower_factor: float, upper_offset: float) -> ConformalCalibrator:
    c = ConformalCalibrator()
    c.offsets = {GLOBAL: np.where(np.arange(len(TAUS)) > M, upper_offset, 0.0)}
    c.adjusted = {GLOBAL: [True] * len(TAUS)}
    c.scales = {GLOBAL: np.where(np.arange(len(TAUS)) < M, lower_factor, 1.0)}
    return c


def _q(n: int, base: list[float]) -> np.ndarray:
    b = np.asarray(base, dtype="float64")
    return np.repeat(np.repeat(b[None, None, :], HORIZON, axis=1), n, axis=0)


QA = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0]
QB = [2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0, 16.0, 18.0]


def _fake_engine(monkeypatch, n: int, calls: list | None = None):
    """forecast_panel without trained models: methods "a" and "b" give known
    distributions, and the blend is their average."""
    import dqstack.pipeline as pipeline

    def cq(P, O, h, tr):
        if calls is not None:
            calls.append((id(tr), O))
        return {}, {"a": _q(n, QA), "b": _q(n, QB)}

    monkeypatch.setattr(pipeline, "candidate_quantiles", cq)
    monkeypatch.setattr(combine, "weights_from_strata",
                        lambda pc, tbl, k: np.full((len(pc), k), 1.0 / k))
    monkeypatch.setattr(combine, "combine_quantiles",
                        lambda qs, W, cands: (qs["a"] + qs["b"]) / 2.0)


def _panel(n: int):
    return SimpleNamespace(series=pd.DataFrame({"planning_class": ["smooth"] * n,
                                                "series_id": [f"s{i}" for i in range(n)]}))


def _strata(n: int):
    pc2 = np.full((n, HORIZON), "smooth", dtype="U20")
    hg2 = np.broadcast_to(np.array([live.horizon_group_of(h) for h in range(1, HORIZON + 1)],
                                   dtype="U12"), pc2.shape)
    return pc2, hg2


def _model(**extra) -> dict:
    return {"trained": object(), "meta": SimpleNamespace(candidates=["a", "b"]),
            "weights_method": "stack", "stack_tbl": None, "meta_extra": None,
            "conformal": _calibrator(0.5, 3.0), "conformal_orders": _calibrator(0.9, 1.0),
            "cfg": CFG, **extra}


def test_issued_as_single_forecasts_the_best_method_with_its_own_calibrator(monkeypatch):
    n = 3
    _fake_engine(monkeypatch, n)
    single_cal = _calibrator(0.7, 2.0)
    model = _model(best_single="b", conformal_single=single_cal)
    pc2, hg2 = _strata(n)

    blend = live.forecast_panel(live.issued_as(model, "blend"), _panel(n), None, 0, CFG)
    single = live.forecast_panel(live.issued_as(model, "single"), _panel(n), None, 0, CFG)

    combined = (_q(n, QA) + _q(n, QB)) / 2.0
    assert np.array_equal(blend, model["conformal_orders"].apply(combined, pc2, hg2))
    assert np.array_equal(single, single_cal.apply(_q(n, QB), pc2, hg2))
    assert np.array_equal(single[..., M], _q(n, QB)[..., M].astype("float32")), "median moved"
    # The model as saved issues the blend: the default, and what older versions issue.
    assert np.array_equal(live.forecast_panel(model, _panel(n), None, 0, CFG), blend)


def test_issuing_leaves_the_saved_model_as_it_was():
    model = _model(best_single="b", conformal_single=_calibrator(0.7, 2.0))
    single = live.issued_as(model, "single")
    assert single["issue"] == "single" and "issue" not in model
    assert single["trained"] is model["trained"]        # one trained model, two issues
    assert live.issued_as("starter", "single") == "starter"
    with pytest.raises(ValueError):
        live.issued_as(model, "best")


def test_a_model_saved_before_stage_4_kept_its_best_method_can_only_issue_the_blend(monkeypatch):
    n = 2
    _fake_engine(monkeypatch, n)
    old = _model()
    assert not live.can_issue_single(old)
    assert live.can_issue_single(_model(best_single="a", conformal_single=_calibrator(1, 0)))
    assert not live.can_issue_single("starter")
    # Asked to issue a method it never kept, it issues the blend, unchanged.
    a = live.forecast_panel(live.issued_as(old, "single"), _panel(n), None, 0, CFG)
    b = live.forecast_panel(old, _panel(n), None, 0, CFG)
    assert np.array_equal(a, b)


@dataclass
class _Trained:
    gbm_nomask: object = None
    P_nomask: object = None


def test_a_saved_model_keeps_its_single_best_method_and_calibrator():
    cal = _calibrator(0.6, 1.5)
    state = {"trained": _Trained(), "meta": SimpleNamespace(candidates=["a", "b"]),
             "conformal": _calibrator(0.5, 3.0), "conformal_orders": _calibrator(0.9, 1.0),
             "weights_method": "stack", "stack_tbl": None, "meta_extra": None,
             "best_single": "b", "conformal_single": cal, "P": _panel(2)}
    model = live.load_model(live.saved_model(state))
    assert model["best_single"] == "b" and live.can_issue_single(model)
    assert np.array_equal(model["conformal_single"].scales[GLOBAL], cal.scales[GLOBAL])
    state.pop("best_single"), state.pop("conformal_single")
    assert "best_single" not in live.load_model(live.saved_model(state))


def test_one_trained_model_issued_two_ways_forecasts_its_methods_once(monkeypatch):
    """Scoring the blend and the single method on the same weeks must not double the
    slowest step: the methods' forecasts are shared between the two."""
    n, calls = 2, []
    _fake_engine(monkeypatch, n, calls)
    model = _model(best_single="a", conformal_single=_calibrator(1.0, 0.0))
    shared: dict = {}
    for m in (live.issued_as(model, "blend"), live.issued_as(model, "single")):
        live.forecast_panel(m, _panel(n), None, 5, CFG, shared)
    assert len(calls) == 1
    live.forecast_panel(model, _panel(n), None, 12, CFG, shared)     # another origin
    assert len(calls) == 2


def test_the_engine_fits_the_single_methods_calibrator_on_that_methods_forecasts():
    """A source check: the calibrator must be fitted on the forecast it corrects
    (the lesson of the orders calibrator), here the best method's own quantiles."""
    pipe = (Path(dqstack.__file__).parent / "pipeline.py").read_text(encoding="utf-8")
    assert "cq_single.append(qs[best_single]" in pipe
    assert '"best_single": best_single, "conformal_single": conformal_single' in pipe
    assert "best_single = min(single_loss, key=single_loss.get)" in pipe
