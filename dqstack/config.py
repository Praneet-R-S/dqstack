"""Central configuration for the DQ-Stack pipeline.

Every magic number in the system lives here so that ablation runs are a config
diff rather than a code diff. Stage boundaries reference these constants by
name; nothing downstream hardcodes a threshold.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- paths

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = DATA / "raw"
PROCESSED = Path(os.environ["DQ_PROCESSED_DIR"]) if os.environ.get("DQ_PROCESSED_DIR") \
    else DATA / "processed"
# The application server runs each company's training in its own process with its
# own scratch folders, so one company's run never reads or overwrites another's
# files. The demo pipeline uses the defaults.
INTERIM = Path(os.environ["DQ_INTERIM_DIR"]) if os.environ.get("DQ_INTERIM_DIR") \
    else DATA / "interim"
ARTIFACTS = Path(os.environ["DQ_ARTIFACTS_DIR"]) if os.environ.get("DQ_ARTIFACTS_DIR") \
    else ROOT / "artifacts"
UI_DATA = ROOT / "ui" / "data"

for _p in (RAW, INTERIM, PROCESSED, ARTIFACTS, UI_DATA):
    _p.mkdir(parents=True, exist_ok=True)

SEED = 20260804

# ---------------------------------------------------------------- horizon

HORIZON = 28                      # days forecast forward
HORIZON_GROUPS = {                # Stage 3: grouped direct multi-horizon
    "h01_07": (1, 7),
    "h08_14": (8, 14),
    "h15_28": (15, 28),
}

QUANTILES = (0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.975, 0.99)
Q_LABELS = tuple(f"p{int(round(q * 1000)):03d}" for q in QUANTILES)

# The index of the median. Never write `len(QUANTILES) // 2` — this tuple is
# deliberately asymmetric (three levels below the median, five above, because the
# upper tail is what sets inventory), so the midpoint index is 4, which is P75.
# Using it as the median silently scores every point-accuracy metric against the
# 75th percentile and makes the whole system look like it over-forecasts.
Q50_INDEX = QUANTILES.index(0.50)
assert QUANTILES[Q50_INDEX] == 0.50

# ---------------------------------------------------------------- stage 1

@dataclass(frozen=True)
class DiagnosisConfig:
    """Syntetos-Boylan-Croston quadrant thresholds plus two extra classes."""

    adi_threshold: float = 1.32
    cv2_threshold: float = 0.49
    cold_start_weeks: int = 8          # age below this -> cold_start
    terminal_lookback_days: int = 91   # window for decline test
    terminal_decline_ratio: float = 0.45  # recent mean / prior mean below this
    terminal_min_zero_run: int = 21    # or a long trailing zero run

    # censoring detection
    censor_prob_threshold: float = 0.55   # suspected-stockout confidence cutoff
    censor_min_history: int = 28

    # regime segmentation (PELT)
    pelt_penalty: float = 12.0
    pelt_min_segment: int = 56
    min_train_days: int = 180          # never truncate below this


@dataclass(frozen=True)
class HealthWeights:
    """Data-health score composition. Drives the confidence gutter in the UI."""

    censored: float = 0.35
    out_of_assortment: float = 0.20
    price_completeness: float = 0.25
    freshness: float = 0.20


# ---------------------------------------------------------------- stage 2

LAGS = (1, 2, 3, 7, 14, 21, 28, 35, 42, 56, 364)
ROLL_WINDOWS = (7, 14, 28, 56, 180)
FOURIER_WEEKLY_K = 3
FOURIER_ANNUAL_K = 6

# ---------------------------------------------------------------- stage 3

LGBM_TWEEDIE = {
    "objective": "tweedie",
    "tweedie_variance_power": 1.15,
    "metric": "tweedie",
    "learning_rate": 0.05,
    "num_leaves": 128,
    "min_data_in_leaf": 100,
    "feature_fraction": 0.70,
    "bagging_fraction": 0.80,
    "bagging_freq": 1,
    "lambda_l2": 0.5,
    "max_cat_threshold": 64,
    "num_threads": 0,
    "verbosity": -1,
    "seed": SEED,
}
LGBM_QUANTILE = {
    "objective": "quantile",
    "metric": "quantile",
    "learning_rate": 0.06,
    "num_leaves": 64,
    "min_data_in_leaf": 150,
    "feature_fraction": 0.70,
    "bagging_fraction": 0.80,
    "bagging_freq": 1,
    "lambda_l2": 0.5,
    "max_cat_threshold": 64,
    "num_threads": 0,
    "verbosity": -1,
    "seed": SEED,
}
LGBM_ROUNDS = 500
LGBM_EARLY_STOP = 40

NHITS = {
    "input_size": 112,
    "hidden": 192,
    "stacks": 3,
    "pool_kernels": (8, 4, 1),
    "epochs": 18,
    "batch_size": 512,
    "lr": 1e-3,
}

# ---------------------------------------------------------------- stage 4

CANDIDATES = (
    "seasonal_naive",
    "ets",
    "croston_sba",
    "tsb",
    "lgbm_tweedie",
    "lgbm_quantile",
    "nhits",
    "zeroshot_prior",
    "lifecycle_cohort",
)
BASELINE_CANDIDATE = "seasonal_naive"   # FVA anchor

@dataclass(frozen=True)
class CombineConfig:
    min_effective_candidates: float = 2.0   # entropy floor guards degenerate collapse
    weight_floor: float = 0.0
    temperature: float = 1.0
    meta_rounds: int = 150
    meta_leaves: int = 31
    meta_lr: float = 0.06
    # "stack" minimises the pinball loss of the combined forecast (convex in the
    # weights, interior optimum when averaging helps). "fforma" minimises the
    # feature-based proxy sum(p_k L_k), which is linear in the weights and whose
    # optimum is therefore pure selection. Both are runnable; the proxy is retained
    # because it is what the FFORMA literature specifies and the difference is a
    # reportable result.
    method: str = "stack"        # "stack" | "fforma"


# ---------------------------------------------------------------- stage 5

@dataclass(frozen=True)
class ReconcileConfig:
    # Reconcile for *reporting*, not for *ordering*.
    #
    # Measured across all twelve aggregation levels, MinT improves accuracy at the
    # top (total -0.45, state -0.36, category -0.31 WRMSSE) and degrades it at the
    # bottom (item_store +0.44), which takes leaf-level WRMSSE from 0.77 to 1.20 --
    # worse than the seasonal-naive anchor at 0.98. The ablation prices that: the
    # arm without reconciliation earns 150,692 more realised margin than the full
    # system, the largest positive delta in the table.
    #
    # The specification anticipates exactly this and declines to pick for us:
    # "reconciliation frequently improves aggregate accuracy while slightly
    # degrading bottom-level accuracy. Which matters depends on the decision being
    # made." Stage 7 sets an order-up-to level per item-location, so the decision is
    # made at the leaves, and there the trade is against us.
    #
    # So both are produced. The decision path consumes the unreconciled distribution;
    # the aggregate planning views consume the reconciled one, which is coherent by
    # construction and materially more accurate at every level a planning
    # organisation actually reviews. Reporting one number for both would have to be
    # wrong for one of them.
    for_decisions: bool = False
    method: str = "mint_shrink"
    shrink_lambda: float | None = None   # None -> Schafer-Strimmer analytic
    temporal_buckets: tuple[str, ...] = ("daily", "weekly", "monthly")


# ---------------------------------------------------------------- stage 6

@dataclass(frozen=True)
class CalibrateConfig:
    method: str = "cqr"                  # split-conformal on quantile residuals
    calibration_days: int = 56           # recent, disjoint from training
    stratify_by: tuple[str, ...] = ("planning_class", "horizon_group")
    min_stratum: int = 200               # fall back to global if thinner
    rolling: bool = True


# ---------------------------------------------------------------- stage 7

@dataclass(frozen=True)
class DecisionConfig:
    review_period: int = 7               # R
    lead_time: int = 2                   # L
    holding_cost_rate: float = 0.0022    # per unit per day, fraction of cost
    markdown_risk: float = 0.16          # fraction of cost lost on excess
    default_margin_rate: float = 0.30
    initial_stock_cover_days: float = 10.0
    shelf_life_days: int | None = None   # set per-category in the panel
    min_quantile: float = 0.50
    max_quantile: float = 0.995


# ---------------------------------------------------------------- gates

@dataclass(frozen=True)
class QualityGate:
    """Stage 0. A failed blocking check stops the run — FR-02."""

    row_count_tolerance: float = 0.02
    total_reconciliation_tolerance: float = 0.005
    max_null_fraction: float = 0.001
    freshness_sla_days: int = 2
    blocking: bool = True


@dataclass(frozen=True)
class Config:
    diagnosis: DiagnosisConfig = field(default_factory=DiagnosisConfig)
    health: HealthWeights = field(default_factory=HealthWeights)
    combine: CombineConfig = field(default_factory=CombineConfig)
    reconcile: ReconcileConfig = field(default_factory=ReconcileConfig)
    calibrate: CalibrateConfig = field(default_factory=CalibrateConfig)
    decision: DecisionConfig = field(default_factory=DecisionConfig)
    gate: QualityGate = field(default_factory=QualityGate)

    horizon: int = HORIZON
    quantiles: tuple[float, ...] = QUANTILES
    seed: int = SEED

    # ablation switches — every stage can be turned off for the ablation table
    enable_censor_masking: bool = True
    enable_regime_windows: bool = True
    enable_combination: bool = True
    enable_reconciliation: bool = True
    enable_calibration: bool = True
    enable_critical_fractile: bool = True   # False -> blanket 95% service level


CFG = Config()
