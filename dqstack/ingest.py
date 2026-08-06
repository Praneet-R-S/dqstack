"""Stage 0 — ingest and validate.

Reads the raw tables, validates each against its declared schema, runs the
blocking quality gate, and only then assembles the analysis panel.

The design choice that matters: a failed blocking check raises and the run
stops. The alternative — log a warning and continue — trains a model on
corrupted data, produces a forecast that looks plausible, and the error is not
discovered for weeks (spec §"Stage 0", FR-02).

Ground-truth columns (`latent_demand`, `was_censored`, `was_carried`) are split
off here into a separate frame that no modelling stage is permitted to read.
They exist only for scoring Stage 1.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import ARTIFACTS, CFG, INTERIM, RAW
from .schemas import (
    ALL_SCHEMAS,
    FeedCheck,
    FeedRun,
    QualityGateFailure,
    utc_stamp,
)

TRUTH_COLUMNS = ("latent_demand", "was_carried", "was_censored")


@dataclass
class IngestResult:
    panel: pd.DataFrame
    truth: pd.DataFrame
    calendar: pd.DataFrame
    items: pd.DataFrame
    stores: pd.DataFrame
    feed_runs: list[FeedRun]

    @property
    def rejected(self) -> list[FeedRun]:
        return [f for f in self.feed_runs if f.outcome == "rejected"]


def _expected_rows(name: str, tables: dict[str, pd.DataFrame]) -> int | None:
    """Row-count expectation derived from the topology, not from the feed.

    For sales the expectation is the full rectangle over the feed's own date
    span, so a feed that silently drops a store-day fails the check. The span
    comes from the sales table rather than the calendar, because the calendar
    deliberately extends past the last sales date (the promotional plan and
    holiday calendar are known ahead of time).
    """
    if name == "sales":
        n_series = len(tables["items"]) * len(tables["stores"])
        s = tables["sales"]
        span = (s["date"].max() - s["date"].min()).days + 1
        return n_series * span
    if name == "calendar":
        cal = tables["calendar"]
        return (cal["date"].max() - cal["date"].min()).days + 1
    return None


def _validate_table(
    name: str, df: pd.DataFrame, tables: dict[str, pd.DataFrame], run_id: str
) -> FeedRun:
    schema = ALL_SCHEMAS[name]
    # Fingerprinting indexes the declared columns, so a feed missing one would
    # raise a bare pandas KeyError before the quality gate could report it as a
    # blocking failure — the gate exists to turn bad feeds into a clear rejection,
    # not a stack trace.
    present = [c for c in schema.names if c in df.columns]
    run = FeedRun(
        run_id=run_id,
        table=name,
        started_at=utc_stamp(),
        row_count=len(df),
        expected_row_count=_expected_rows(name, tables),
        declared_fingerprint=schema.fingerprint(),
        schema_fingerprint=schema.observed_fingerprint(df[present]),
    )

    # --- schema conformance (blocking) — FR-01
    errs = schema.validate(df)
    run.checks.append(
        FeedCheck(
            "schema_conformance",
            "pass" if not errs else "fail",
            "all declared columns conform" if not errs else "; ".join(errs[:6]),
        )
    )

    # --- schema fingerprint drift (warning) — FR-05
    prior_path = ARTIFACTS / "accepted_fingerprints.json"
    prior = json.loads(prior_path.read_text()) if prior_path.exists() else {}
    known = prior.get(name)
    if known is None:
        detail, outcome = "no prior accepted run; establishing baseline", "pass"
    elif known == run.schema_fingerprint:
        detail, outcome = "matches last accepted run", "pass"
    else:
        detail, outcome = f"changed from {known} to {run.schema_fingerprint}", "warn"
    run.checks.append(FeedCheck("schema_fingerprint", outcome, detail, blocking=False))

    # --- row count vs expectation (blocking) — FR-03
    if run.expected_row_count:
        drift = abs(run.row_count - run.expected_row_count) / run.expected_row_count
        ok = drift <= CFG.gate.row_count_tolerance
        run.checks.append(
            FeedCheck(
                "row_count",
                "pass" if ok else "fail",
                f"{run.row_count:,} rows vs {run.expected_row_count:,} expected "
                f"({drift:.2%} drift, tolerance {CFG.gate.row_count_tolerance:.0%})",
            )
        )

    # --- null density (blocking)
    required = [c.name for c in schema.columns if not c.nullable]
    null_frac = float(df[required].isna().to_numpy().mean()) if required else 0.0
    run.checks.append(
        FeedCheck(
            "null_density",
            "pass" if null_frac <= CFG.gate.max_null_fraction else "fail",
            f"{null_frac:.4%} nulls across non-nullable columns",
        )
    )

    # --- freshness (warning) — FR-04
    if "date" in df.columns:
        mx = pd.Timestamp(df["date"].max())
        run.max_date = mx.date().isoformat()
        # "Now" is the last day sales were recorded. The calendar and price plan
        # deliberately run ahead of that, so using the calendar's max date as the
        # as-of would report every feed as a month stale.
        asof = pd.Timestamp(tables["sales"]["date"].max())
        run.freshness_days = int((asof - mx).days)
        ok = run.freshness_days <= CFG.gate.freshness_sla_days
        run.checks.append(
            FeedCheck(
                "freshness_sla",
                "pass" if ok else "warn",
                f"latest date {run.max_date}, {run.freshness_days}d behind as-of "
                f"(SLA {CFG.gate.freshness_sla_days}d)",
                blocking=False,
            )
        )

    return run


def _reconcile_totals(
    sales: pd.DataFrame, control: pd.DataFrame | None, run_id: str
) -> FeedRun:
    """Compare ingested aggregate totals against source-declared totals — FR-06.

    The declaration must come from a *different artefact* than the figure being
    checked. Deriving both sides from the sales column makes the comparison an
    identity that can never fail however badly the feed is broken — a check that
    reports "pass" unconditionally is worse than no check, because it is credited
    in the run record.

    The control file is emitted by the source at generation time and carries units
    and row counts per store, so a truncated, duplicated or partially loaded feed
    fails here rather than silently training.
    """
    run = FeedRun(run_id=run_id, table="sales_totals", started_at=utc_stamp(),
                  row_count=len(sales))
    if control is None or control.empty:
        run.checks.append(
            FeedCheck("total_reconciliation", "warn",
                      "no source control file present; FR-06 not exercised",
                      blocking=False)
        )
        return run

    got = sales.groupby("store_id", observed=True)["sales"].sum()
    rows = sales.groupby("store_id", observed=True).size()
    want = control.set_index("store_id")["declared_units"]
    want_rows = control.set_index("store_id")["declared_rows"]

    common = want.index.intersection(got.index)
    missing = [s for s in want.index if s not in got.index]
    declared, ingested = float(want.loc[common].sum()), float(got.loc[common].sum())
    diff = abs(declared - ingested) / max(declared, 1.0)
    ok = diff <= CFG.gate.total_reconciliation_tolerance and not missing

    run.checks.append(
        FeedCheck(
            "total_reconciliation",
            "pass" if ok else "fail",
            f"declared {declared:,.0f} vs ingested {ingested:,.0f} ({diff:.4%} gap)"
            + (f"; {len(missing)} stores absent from the feed" if missing else ""),
        )
    )
    row_gap = int((want_rows.loc[common] - rows.loc[common]).abs().sum())
    run.checks.append(
        FeedCheck(
            "row_count_by_store",
            "pass" if row_gap == 0 else "fail",
            f"{row_gap:,} rows differ from the source declaration",
        )
    )
    return run


def run(strict: bool | None = None) -> IngestResult:
    strict = CFG.gate.blocking if strict is None else strict
    run_id = f"feed_{uuid.uuid4().hex[:10]}"

    tables = {
        name: pd.read_parquet(RAW / f"{name}.parquet")
        for name in ("sales", "prices", "calendar", "items", "stores")
    }
    ctrl_path = RAW / "sales_control.parquet"
    control = pd.read_parquet(ctrl_path) if ctrl_path.exists() else None
    for name in ("sales", "calendar"):
        tables[name]["date"] = pd.to_datetime(tables[name]["date"])

    feed_runs = [_validate_table(n, tables[n], tables, run_id) for n in ALL_SCHEMAS]
    feed_runs.append(_reconcile_totals(tables["sales"], control, run_id))

    (ARTIFACTS / "feed_runs.json").write_text(
        json.dumps([f.to_dict() for f in feed_runs], indent=2)
    )

    rejected = [f for f in feed_runs if f.outcome == "rejected"]
    if rejected and strict:
        lines = [
            f"  {f.table}: " + "; ".join(c.detail for c in f.checks if c.outcome == "fail")
            for f in rejected
        ]
        raise QualityGateFailure(
            "Blocking data-quality checks failed; run stopped before training.\n"
            + "\n".join(lines)
        )

    accepted = {f.table: f.schema_fingerprint for f in feed_runs if f.schema_fingerprint}
    (ARTIFACTS / "accepted_fingerprints.json").write_text(json.dumps(accepted, indent=2))

    # ---------------------------------------------------------------- panel
    sales = tables["sales"]
    truth = sales[["item_id", "store_id", "date", *TRUTH_COLUMNS]].copy()
    hist = sales.drop(columns=list(TRUTH_COLUMNS))

    cal = tables["calendar"]

    # The panel is the full series x calendar rectangle, not the sales rows.
    # Rows past the last sales date carry a null target and a known calendar and
    # planned price — which is exactly the shape the live forecast needs. Building
    # the panel from sales rows instead would leave nothing to forecast onto.
    series_keys = (
        tables["items"][["item_id"]]
        .merge(tables["stores"][["store_id"]], how="cross")
    )
    panel = series_keys.merge(cal[["date"]], how="cross")
    panel = panel.merge(hist, on=["item_id", "store_id", "date"], how="left")
    # A row is "future" because it lies past the last observed sales date, not
    # merely because the feed had no row for it. Conflating the two would label a
    # genuine mid-history gap as future and silently exclude it from Stage 1.
    last_observed = hist["date"].max()
    panel["is_future"] = panel["date"] > last_observed
    n_gap = int(panel["sales"].isna().sum() - panel["is_future"].sum())
    if n_gap > 0:
        print(f"  note: {n_gap:,} in-history rows absent from the sales feed "
              f"(treated as missing, not future)", flush=True)

    panel = panel.merge(cal[["date", "wm_yr_wk"]], on="date", how="left")
    panel = panel.merge(
        tables["prices"], on=["item_id", "store_id", "wm_yr_wk"], how="left"
    )
    panel = panel.merge(tables["items"][["item_id", "dept_id", "cat_id"]], on="item_id", how="left")
    panel = panel.merge(
        tables["stores"][["store_id", "state_id", "cluster_id"]], on="store_id", how="left"
    )

    # FR-07: price-record presence is the availability signal.
    panel["is_carried"] = panel["sell_price"].notna()

    # State-specific SNAP flag resolved to a single column per row.
    snap_long = cal.melt(
        id_vars=["date"],
        value_vars=[c for c in cal.columns if c.startswith("snap_")],
        var_name="state_col",
        value_name="snap",
    )
    snap_long["state_id"] = snap_long["state_col"].str.removeprefix("snap_")
    panel = panel.merge(
        snap_long[["date", "state_id", "snap"]], on=["date", "state_id"], how="left"
    )
    panel = panel.merge(cal[["date", "event_name", "event_type", "wday", "month", "year"]],
                        on="date", how="left")

    panel["series_id"] = (
        panel["item_id"].astype(str) + "|" + panel["store_id"].astype(str)
    ).astype("category")
    panel = panel.sort_values(["series_id", "date"], kind="stable").reset_index(drop=True)

    panel.to_parquet(INTERIM / "panel.parquet", index=False)
    truth.to_parquet(INTERIM / "truth.parquet", index=False)

    return IngestResult(
        panel=panel,
        truth=truth,
        calendar=cal,
        items=tables["items"],
        stores=tables["stores"],
        feed_runs=feed_runs,
    )


if __name__ == "__main__":
    res = run()
    print(f"panel rows   {len(res.panel):,}")
    print(f"series       {res.panel['series_id'].nunique():,}")
    print(f"carried      {res.panel['is_carried'].mean():.1%}")
    print(f"price nulls  {res.panel['sell_price'].isna().mean():.1%} (= not carried)")
    print()
    for f in res.feed_runs:
        print(f"{f.table:14s} {f.outcome:24s} {f.row_count:>10,} rows")
        for c in f.checks:
            mark = {"pass": "ok  ", "warn": "warn", "fail": "FAIL"}[c.outcome]
            print(f"    [{mark}] {c.check}: {c.detail}")
