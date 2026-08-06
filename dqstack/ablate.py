"""Ablation runner — the full system minus each stage.

Two kinds of ablation, and they cost very different amounts.

**Post-hoc.** Combination, reconciliation, calibration and the critical fractile
all act on already-trained candidate forecasts, so turning them off requires no
retraining. The main pipeline run therefore reports these directly in
`artifacts/economics.csv` — there is no reason to spend a training run on them.

**Training-dependent.** Censor masking and regime windows change the *training
target and window*, so they genuinely require a fresh run of Stage 3. The main
run already covers censor masking cheaply via a second Tweedie model trained on
unmasked sales (the "no censoring correction" row). This module runs the
remaining full-retrain arms.

    python -m dqstack.ablate

Expect roughly one full training run per arm. Nothing here is on the critical
path for a single forecast; it exists to populate the ablation table.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pandas as pd

from .config import ARTIFACTS, CFG, Config
from .pipeline import run

# Each arm is (label, config-override, whether Stage 3 must be retrained).
ARMS: tuple[tuple[str, dict, bool], ...] = (
    ("full system", {}, True),
    ("no censor masking", {"enable_censor_masking": False}, True),
    ("no regime windows", {"enable_regime_windows": False}, True),
)


def _utf8_stdout() -> None:
    """Make stdout UTF-8 regardless of platform default.

    Redirected stdout on Windows defaults to cp1252, so a single non-ASCII character
    in a log line raises UnicodeEncodeError and takes down a long-running job at the
    point it tries to report progress. That killed a 25-minute ablation arm on an
    en-dash in a label.
    """
    import sys

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main() -> None:
    _utf8_stdout()
    rows: list[dict] = []
    posthoc: list[dict] = []

    for label, override, _ in ARMS:
        print(f"\n{'#' * 78}\n#  ABLATION ARM: {label}\n{'#' * 78}", flush=True)
        cfg = replace(CFG, **override) if override else CFG
        state = run(cfg=cfg, ablate_censoring=(label == "full system"), light=True)
        m = state["metrics"]
        sim = m["simulator_totals"]
        full = next((e for e in m["economics"] if e["method"] == "DQ-Stack (full)"), {})

        rows.append(
            {
                "arm": label,
                "retrained": True,
                "wrmsse": full.get("wrmsse"),
                "wspl": next(
                    (r["wspl"] for r in m["leaderboard"] if r["method"] == "DQ-Stack (full)"), None
                ),
                "realized_margin": sim["realized_margin"],
                "fill_rate": sim["fill_rate"],
                "lost_margin": sim["lost_margin"],
                "waste_cost": sim["waste_cost"],
                "censor_precision": m["censoring_detection"]["precision"],
                "censor_recall": m["censoring_detection"]["recall"],
                "fva_vs_anchor": m["fva_ladder"][-1]["fva_vs_anchor"],
                "backtest_system_wspl": next(
                    (r["wspl_mean"] for r in m["backtest"]["by_method"]
                     if r["method"] == "DQ-Stack (full)"), None),
                "recon_levels_improved": m["backtest"].get("hierarchy", {})
                    .get("levels_improved"),
            }
        )

        if label == "full system":
            # The post-hoc arms are already measured inside this run.
            for e in m["economics"]:
                if e["method"] == "DQ-Stack (full)":
                    continue
                posthoc.append(
                    {
                        "arm": e["method"],
                        "retrained": False,
                        "wrmsse": e["wrmsse"],
                        "realized_margin": e["realized_margin"],
                        "fill_rate": e["fill_rate"],
                        "lost_margin": e["lost_margin"],
                        "waste_cost": e["waste_cost"],
                    }
                )

    table = pd.DataFrame(rows + posthoc)
    base = table.loc[table["arm"] == "full system", "realized_margin"]
    if len(base):
        table["margin_vs_full"] = table["realized_margin"] - float(base.iloc[0])
    table = table.sort_values("realized_margin", ascending=False)

    out = ARTIFACTS / "ablation.csv"
    table.to_csv(out, index=False)
    (ARTIFACTS / "ablation.json").write_text(
        json.dumps(table.to_dict("records"), indent=2, default=str)
    )

    print(f"\n{'=' * 78}\n  ABLATION SUMMARY\n{'=' * 78}")
    print(table.round(4).to_string(index=False))
    print(f"\n  wrote {out}")
    print(
        "\n  Read the margin_vs_full column. A stage whose removal *improves* realised\n"
        "  margin is destroying value and should be disabled — that is a result, not a\n"
        "  bug, and it is why every stage carries an ablation switch."
    )


if __name__ == "__main__":
    main()
