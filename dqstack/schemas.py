"""Stage 0 — declared schemas and validation primitives.

Lightweight stand-in for Pandera/Great Expectations. Kept dependency-free so
the quality gate cannot itself become a source of environment failure.

The contract: a *blocking* violation raises `QualityGateFailure` and the run
stops. Training on quietly corrupted data produces a forecast that looks fine
and is wrong, and the error surfaces weeks later (spec §"Stage 0").
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

import numpy as np
import pandas as pd

Kind = Literal["int", "float", "str", "date", "bool"]


class QualityGateFailure(RuntimeError):
    """Raised when a blocking data-quality check fails. Stops the run — FR-02."""


@dataclass(frozen=True)
class Column:
    name: str
    kind: Kind
    nullable: bool = False
    ge: float | None = None
    le: float | None = None
    unique: bool = False

    def check(self, s: pd.Series) -> list[str]:
        errs: list[str] = []
        if not self.nullable and s.isna().any():
            errs.append(f"{self.name}: {int(s.isna().sum())} nulls in non-nullable column")
        v = s.dropna()
        if self.kind == "int" and not pd.api.types.is_integer_dtype(v):
            # The int branch is entered precisely when the dtype is *not* integer,
            # so a string column lands here and `astype("float64")` raises a bare
            # ValueError before the gate can turn it into a reported rejection. The
            # whole point of Stage 0 is that a bad feed produces a clear verdict
            # rather than a stack trace.
            if not pd.api.types.is_numeric_dtype(v):
                errs.append(f"{self.name}: expected integral values, got {v.dtype}")
            elif len(v) and not np.allclose(v.to_numpy(dtype="float64") % 1, 0):
                errs.append(f"{self.name}: expected integral values")
        if self.kind == "float" and not pd.api.types.is_numeric_dtype(v):
            errs.append(f"{self.name}: expected numeric, got {v.dtype}")
        if self.kind == "date" and not pd.api.types.is_datetime64_any_dtype(v):
            errs.append(f"{self.name}: expected datetime, got {v.dtype}")
        if self.ge is not None and len(v) and pd.api.types.is_numeric_dtype(v):
            bad = int((v < self.ge).sum())
            if bad:
                errs.append(f"{self.name}: {bad} values below minimum {self.ge}")
        if self.le is not None and len(v) and pd.api.types.is_numeric_dtype(v):
            bad = int((v > self.le).sum())
            if bad:
                errs.append(f"{self.name}: {bad} values above maximum {self.le}")
        if self.unique and v.duplicated().any():
            errs.append(f"{self.name}: {int(v.duplicated().sum())} duplicate values")
        return errs


@dataclass(frozen=True)
class TableSchema:
    name: str
    columns: tuple[Column, ...]
    primary_key: tuple[str, ...] = ()

    @property
    def names(self) -> list[str]:
        return [c.name for c in self.columns]

    def fingerprint(self) -> str:
        """Stable hash of the declared shape. Compared to the last accepted
        run to detect schema drift by fingerprint — FR-05."""
        payload = [(c.name, c.kind, c.nullable) for c in self.columns]
        blob = json.dumps(payload, sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:16]

    def observed_fingerprint(self, df: pd.DataFrame) -> str:
        payload = [(c, str(df[c].dtype)) for c in sorted(df.columns)]
        blob = json.dumps(payload, sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:16]

    def validate(self, df: pd.DataFrame) -> list[str]:
        errs: list[str] = []
        missing = [c for c in self.names if c not in df.columns]
        if missing:
            errs.append(f"{self.name}: missing declared columns {missing}")
            return errs
        for col in self.columns:
            errs.extend(f"{self.name}.{e}" for e in col.check(df[col.name]))
        if self.primary_key:
            dup = int(df.duplicated(subset=list(self.primary_key)).sum())
            if dup:
                errs.append(f"{self.name}: {dup} rows violate primary key {self.primary_key}")
        return errs


# ------------------------------------------------------------------ declared schemas

SALES = TableSchema(
    name="sales",
    columns=(
        Column("item_id", "str"),
        Column("store_id", "str"),
        Column("date", "date"),
        Column("sales", "int", ge=0),
    ),
    primary_key=("item_id", "store_id", "date"),
)

PRICES = TableSchema(
    name="prices",
    columns=(
        Column("item_id", "str"),
        Column("store_id", "str"),
        Column("wm_yr_wk", "int"),
        Column("sell_price", "float", ge=0.01),
    ),
    primary_key=("item_id", "store_id", "wm_yr_wk"),
)

def calendar_schema(states: tuple[str, ...] = ("CA", "TX", "WI")) -> TableSchema:
    """The calendar carries one 0/1 flag column per state (`snap_<state>`): benefit
    days in the demo's three states, state festivals for an Indian company. The
    default is the demo's schema exactly, so its fingerprint is unchanged."""
    return TableSchema(
        name="calendar",
        columns=(
            Column("date", "date", unique=True),
            Column("wm_yr_wk", "int"),
            Column("wday", "int", ge=1, le=7),
            Column("month", "int", ge=1, le=12),
            Column("year", "int"),
            Column("event_name", "str", nullable=True),
            Column("event_type", "str", nullable=True),
            *(Column(f"snap_{s}", "int", ge=0, le=1) for s in states),
        ),
        primary_key=("date",),
    )


CALENDAR = calendar_schema()

ITEMS = TableSchema(
    name="items",
    columns=(
        Column("item_id", "str", unique=True),
        Column("dept_id", "str"),
        Column("cat_id", "str"),
        Column("unit_cost", "float", ge=0.01),
        Column("margin_rate", "float", ge=0.0, le=0.9),
        Column("shelf_life_days", "int", ge=1),
    ),
    primary_key=("item_id",),
)

STORES = TableSchema(
    name="stores",
    columns=(
        Column("store_id", "str", unique=True),
        Column("state_id", "str"),
        Column("cluster_id", "str"),
    ),
    primary_key=("store_id",),
)

ALL_SCHEMAS = {s.name: s for s in (SALES, PRICES, CALENDAR, ITEMS, STORES)}


# ------------------------------------------------------------------ feed run record

@dataclass
class FeedCheck:
    check: str
    outcome: Literal["pass", "warn", "fail"]
    detail: str
    blocking: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "outcome": self.outcome,
            "detail": self.detail,
            "blocking": self.blocking,
        }


@dataclass
class FeedRun:
    """One row per feed run: timings, counts, fingerprint, outcome — FR-03."""

    run_id: str
    table: str
    started_at: str
    row_count: int = 0
    expected_row_count: int | None = None
    schema_fingerprint: str = ""
    declared_fingerprint: str = ""
    max_date: str | None = None
    freshness_days: int | None = None
    checks: list[FeedCheck] = field(default_factory=list)

    @property
    def outcome(self) -> str:
        if any(c.outcome == "fail" and c.blocking for c in self.checks):
            return "rejected"
        if any(c.outcome in ("fail", "warn") for c in self.checks):
            return "accepted_with_warnings"
        return "accepted"

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "table": self.table,
            "started_at": self.started_at,
            "row_count": self.row_count,
            "expected_row_count": self.expected_row_count,
            "schema_fingerprint": self.schema_fingerprint,
            "declared_fingerprint": self.declared_fingerprint,
            "max_date": self.max_date,
            "freshness_days": self.freshness_days,
            "outcome": self.outcome,
            "checks": [c.to_dict() for c in self.checks],
        }


def utc_stamp(d: date | pd.Timestamp | None = None) -> str:
    ts = pd.Timestamp(d) if d is not None else pd.Timestamp.utcnow()
    return ts.isoformat(timespec="seconds")
