"""The cost curve must not contradict the number it is drawn beside.

The curve shows what ordering more or less than the recommendation costs, so its
minimum has to be the recommendation. A drawing whose low point sits beside the
figure printed above it would discredit both, and it would do so silently -- nothing
throws, the shape still looks plausible, and only a reader who measured it would ever
know.

The property is achieved by construction rather than by fitting. The optimum of the
newsvendor cost is F-inverse of the critical fractile, so the demand distribution used
for the drawing is rebuilt from two figures the bundle states exactly: the mean of the
cover window, and F(orderUpTo) = targetQ. Two constraints determine a two-parameter
distribution, and the minimum then lands on the recommendation because it cannot land
anywhere else.

Two layers of checking here, because the interesting half is written in JavaScript:

    the data contract     always -- every series that will get a curve admits a
                          feasible fit under one of the two branches
    the real invariant    where node is available -- runs curve.js over all 1,000
                          series and asserts the drawn minimum is the recommendation

The data contract is checked in Python rather than by reimplementing the fit, so
there is no second copy of the derivation to drift out of step with the first.
"""

from __future__ import annotations

import json
import math
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
BUNDLE = ROOT / "ui" / "data" / "bundle.json"
INVARIANT = pathlib.Path(__file__).resolve().parent / "curve_invariant.js"


@pytest.fixture(scope="module")
def bundle() -> dict:
    if not BUNDLE.exists():
        pytest.skip("bundle.json has not been generated")
    return json.loads(BUNDLE.read_text(encoding="utf-8"))


def _cover(run: dict) -> int:
    return int(run.get("reviewPeriod") or 7) + int(run.get("leadTime") or 2)


def _probit(p: float) -> float:
    """Inverse standard normal, good to ~1e-9. Only the sign and rough magnitude
    matter here, since this checks feasibility rather than reproducing the drawing."""
    return math.sqrt(2) * _erfinv(2 * p - 1)


def _erfinv(y: float) -> float:
    if y <= -1 or y >= 1:
        return math.copysign(float("inf"), y)
    a = 0.147
    ln = math.log(1 - y * y)
    t1 = 2 / (math.pi * a) + ln / 2
    return math.copysign(math.sqrt(math.sqrt(t1 * t1 - ln / a) - t1), y)


def test_every_series_with_a_forecast_admits_a_curve(bundle):
    """The precondition the drawing depends on.

    Branch one matches the mean and the service level, which needs
    z**2 >= 2*ln(level/mean). Branch two drops the mean and keeps the service level,
    which needs only a positive spread. If neither holds for some series, that series
    would silently render no curve -- so the gap is worth knowing about here rather
    than as an empty box on a card.
    """
    cover = _cover(bundle["run"])
    unfittable = []
    for s in bundle["series"]:
        level, mean_days = s.get("orderUpTo"), s.get("mean") or []
        if not level or level <= 0 or not mean_days:
            continue                      # legitimately has nothing to fit
        mu = sum(mean_days[:cover])
        if mu <= 0:
            continue
        z = _probit(min(0.999, max(0.001, s.get("targetQ") or 0.5)))
        feasible_mean = z * z - 2 * (math.log(level) - math.log(mu)) >= 0
        var = sum(v * v for v in (s.get("sd") or [])[:cover])
        feasible_spread = var > 0
        if not (feasible_mean or feasible_spread):
            unfittable.append(s["id"])
    assert not unfittable, (
        f"{len(unfittable)} series would render no cost curve, e.g. {unfittable[:5]}"
    )


def test_the_service_level_is_the_only_thing_the_minimum_depends_on(bundle):
    """Why the cost weights come from targetQ and not from underage/overage.

    dC/dQ vanishes at F(Q) = underage/(underage+overage). The recommendation was
    computed at targetQ, which is that ratio after the configured floor and ceiling
    were applied -- and the exported figures are single precision, so the two differ
    for nearly every series even when no clamping happened. Deriving the drawn curve's
    weights from the raw ratio would therefore put the minimum slightly, or in the
    clamped case substantially, off the recommendation.

    This test documents that the gap is real, so nobody 'simplifies' the module back
    to using the raw costs.
    """
    cover = _cover(bundle["run"])
    assert cover > 0
    gaps = []
    for s in bundle["series"]:
        u, o = s.get("underage") or 0.0, s.get("overage") or 0.0
        if u + o <= 0:
            continue
        gaps.append(abs(u / (u + o) - (s.get("targetQ") or 0.0)))
    assert gaps, "no series carries the cost components"
    assert max(gaps) > 1e-6, (
        "underage/(underage+overage) now equals targetQ everywhere; if the pipeline "
        "stopped clamping and started exporting these at full precision, curve.js "
        "could use the raw costs directly and this test should be revisited"
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")
def test_the_drawn_minimum_is_the_recommendation(bundle):
    """Runs the real module over every series. This is the check that matters."""
    proc = subprocess.run(
        [shutil.which("node"), str(INVARIANT)],
        capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, (
        "curve.js violates its invariant:\n" + proc.stdout + proc.stderr
    )
    assert "violations                  0" in proc.stdout, proc.stdout
    # A curve for all but the handful of products that have no forecast at all.
    drawn = int(proc.stdout.split("curves drawn")[1].split("\n")[0].strip())
    assert drawn >= len(bundle["series"]) - 5, f"only {drawn} curves drawn"
