"""Stage 1 internals: changepoint detection, censoring, terminal decline.

The PELT test compares against an unpruned O(n^2) optimal-partitioning DP. That is
the only honest way to test a pruned exact algorithm: pruning must not change the
answer, so the reference implementation is the same objective without pruning. A
test that merely checked "some changepoints were found" passed happily while the
pruning rule was wrong and the detector was returning nothing.
"""

from __future__ import annotations

import numpy as np
import pytest

from dqstack.diagnose import _longest_run, detect_censoring, pelt_l2


# ------------------------------------------------------------------ reference

def exact_partition(x: np.ndarray, penalty: float, min_size: int) -> tuple[list[int], float]:
    """Unpruned optimal partitioning. Same objective as pelt_l2, no pruning."""
    n = len(x)
    cs = np.concatenate([[0.0], np.cumsum(x)])
    cs2 = np.concatenate([[0.0], np.cumsum(x * x)])

    def cost(a: int, b: int) -> float:
        m = b - a
        s = cs[b] - cs[a]
        return float(cs2[b] - cs2[a] - s * s / m)

    F = np.full(n + 1, np.inf)
    F[0] = -penalty
    prev = np.zeros(n + 1, dtype=int)
    for t in range(min_size, n + 1):
        for s in range(0, t - min_size + 1):
            if not np.isfinite(F[s]):
                continue
            v = F[s] + cost(s, t) + penalty
            if v < F[t]:
                F[t], prev[t] = v, s
    cps, t = [], n
    while t > 0:
        s = prev[t]
        if s > 0:
            cps.append(int(s))
        if s == t:
            break
        t = s
    return sorted(cps), float(F[n])


def objective(x: np.ndarray, cps: list[int], penalty: float) -> float:
    bounds = [0] + list(cps) + [len(x)]
    total = 0.0
    for a, b in zip(bounds[:-1], bounds[1:]):
        seg = x[a:b]
        total += float(((seg - seg.mean()) ** 2).sum()) + penalty
    return total - penalty


# ------------------------------------------------------------------ pelt

def _piecewise(rng, n=140, k=4, min_size=12, noise=0.35):
    cuts = sorted(rng.choice(np.arange(min_size, n - min_size), size=k - 1, replace=False))
    levels = rng.normal(0, 3, size=k)
    out = np.empty(n)
    bounds = [0] + list(cuts) + [n]
    for i, (a, b) in enumerate(zip(bounds[:-1], bounds[1:])):
        out[a:b] = levels[i]
    return out + rng.normal(0, noise, size=n)


@pytest.mark.parametrize("seed", range(12))
def test_pelt_matches_the_unpruned_optimum(seed):
    """Pruning must not change the answer. This is the property that was broken."""
    rng = np.random.default_rng(seed)
    x = _piecewise(rng)
    pen, ms = 8.0, 12
    got = pelt_l2(x, penalty=pen, min_size=ms)
    want, _ = exact_partition(x, penalty=pen, min_size=ms)
    assert objective(x, got, pen) <= objective(x, want, pen) + 1e-6, (
        f"pruned objective {objective(x, got, pen):.4f} worse than exact "
        f"{objective(x, want, pen):.4f}; got {got}, exact {want}"
    )


def test_pelt_finds_a_clear_level_shift():
    x = np.concatenate([np.zeros(80), np.full(80, 6.0)]) + \
        np.random.default_rng(3).normal(0, 0.2, size=160)
    cps = pelt_l2(x, penalty=8.0, min_size=20)
    assert len(cps) >= 1, "no changepoint found on an unmistakable level shift"
    assert min(abs(c - 80) for c in cps) <= 3, f"changepoint {cps} not near the true break at 80"


def test_pelt_reports_nothing_on_stationary_noise():
    x = np.random.default_rng(4).normal(0, 1, size=200)
    assert pelt_l2(x, penalty=40.0, min_size=30) == []


def test_pelt_respects_minimum_segment_length():
    rng = np.random.default_rng(5)
    x = _piecewise(rng, n=200, k=6, min_size=8)
    ms = 40
    cps = pelt_l2(x, penalty=6.0, min_size=ms)
    bounds = [0] + cps + [len(x)]
    for a, b in zip(bounds[:-1], bounds[1:]):
        assert b - a >= ms, f"segment {a}:{b} shorter than min_size {ms}"


def test_pelt_terminates_on_degenerate_input():
    for x in (np.zeros(200), np.ones(200), np.arange(200.0)):
        pelt_l2(x, penalty=10.0, min_size=30)      # must simply return


# ------------------------------------------------------------------ censoring

def test_structural_censoring_is_exactly_the_not_carried_days():
    rng = np.random.default_rng(7)
    n = 200
    sales = rng.poisson(6, size=n).astype("float64")
    carried = np.ones(n, dtype=bool)
    carried[50:60] = False
    wday = (np.arange(n) % 7) + 1
    structural, conf = detect_censoring(sales, carried, wday)
    assert np.array_equal(structural, ~carried)
    assert (conf[~carried] == 0).all(), "not-carried days must not also carry a stockout score"


def test_probabilistic_detector_flags_a_zero_run_on_a_busy_series():
    rng = np.random.default_rng(8)
    n = 250
    sales = rng.poisson(12, size=n).astype("float64")   # rarely zero
    sales[200:206] = 0.0                                 # implausible run
    carried = np.ones(n, dtype=bool)
    wday = (np.arange(n) % 7) + 1
    _, conf = detect_censoring(sales, carried, wday)
    assert conf[200:206].max() > 0.55, "a six-day zero run on a 12/day series was not flagged"
    assert conf[:150].max() < 0.55, "flagged days on a series with no censoring"


def test_longest_run_counts_consecutive_true_values():
    assert _longest_run(np.array([False, True, True, False, True])) == 2
    assert _longest_run(np.zeros(10, dtype=bool)) == 0
    assert _longest_run(np.ones(7, dtype=bool)) == 7
