"""Stage 2 — featurize.

Design decision that makes leakage structurally impossible rather than merely
tested-for: the panel is reshaped into dense `(n_series, n_days)` matrices, and
every feature for forecast origin `O` is computed from the slice `[:, :O+1]`.
There is no code path by which a post-origin value can enter a feature, because
post-origin columns are never in scope.

This is the *direct* multi-horizon formulation. For a target date `t = O + h`:

    origin features   computed from y[:, :O+1]   — identical for all h
    target features   calendar and planned price at t — legitimately known ahead
    horizon           h, as an explicit feature

Recursive forecasting would instead feed predictions back as lags and compound
error over 28 days. Direct avoids that entirely (spec §"ML anti-patterns").
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass

import numpy as np
import pandas as pd

# Empty/all-NaN slices are the correct representation of "no history yet".
warnings.filterwarnings("ignore", message="Mean of empty slice")
warnings.filterwarnings("ignore", message="All-NaN slice encountered")
warnings.filterwarnings("ignore", message="Degrees of freedom <= 0")

from .config import (
    FOURIER_ANNUAL_K,
    FOURIER_WEEKLY_K,
    HORIZON_GROUPS,
    INTERIM,
    LAGS,
    PROCESSED,
    ROLL_WINDOWS,
)

CAT_FEATURES = [
    "item_id", "dept_id", "cat_id", "store_id", "state_id", "cluster_id",
    "planning_class", "event_name", "event_type", "horizon_group",
]


DIAG_COLS = [
    "series_id", "planning_class", "adi", "cv2", "health_score", "regime_start",
    "age_weeks", "weeks_since_break", "seasonal_strength", "trend_strength",
    "spectral_entropy", "acf7", "acf364", "zero_share", "cv", "mean_demand",
    "censored_rate", "ooa_rate", "abc", "xyz", "avg_price", "price_volatility",
    "revenue", "days_since_last_sale", "is_terminal", "n_obs",
]
ID_COLS = ["series_id", "item_id", "store_id", "dept_id", "cat_id",
           "state_id", "cluster_id"]


def series_frame(ids: pd.DataFrame, diag: pd.DataFrame) -> pd.DataFrame:
    """Attach a diagnostics *vintage* to the stable series identity columns.

    Split out from build_panel so a caller can rebuild the static-feature frame
    from Stage 1 run at a different as-of date, without touching the matrices.
    """
    out = ids.merge(
        diag[DIAG_COLS].assign(series_id=lambda d: d["series_id"].astype(str)),
        on="series_id", how="left",
    )
    out["planning_class"] = out["planning_class"].fillna("cold_start")
    return out


# ------------------------------------------------------------------ panel -> matrices

@dataclass
class Panel:
    """Dense matrix view of the panel. All Stage 2 work happens against this."""

    series: pd.DataFrame          # one row per series, aligned to matrix rows
    dates: pd.DatetimeIndex
    y: np.ndarray                 # (S, T) masked demand — NaN where not signal
    y_raw: np.ndarray             # (S, T) recorded sales, unmasked
    carried: np.ndarray           # (S, T) bool
    price: np.ndarray             # (S, T) float, NaN when not carried
    calendar: pd.DataFrame
    n_history: int = 0            # columns [0, n_history) have observed sales

    @property
    def n_series(self) -> int:
        return self.y.shape[0]

    @property
    def n_days(self) -> int:
        return self.y.shape[1]

    def index_of(self, d) -> int:
        return int(self.dates.get_loc(pd.Timestamp(d)))

    def with_series(self, series: pd.DataFrame) -> "Panel":
        """Same matrices, different static-feature vintage.

        The y/carried/price matrices are raw observations and do not depend on
        when Stage 1 ran; only the diagnostics do. Swapping just the series frame
        lets each origin block train on diagnostics computed as of that block,
        without rebuilding a 1,000 x 1,128 panel five times.
        """
        return Panel(series=series, dates=self.dates, y=self.y, y_raw=self.y_raw,
                     carried=self.carried, price=self.price, calendar=self.calendar,
                     n_history=self.n_history)


def build_panel(
    panel: pd.DataFrame | None = None,
    diag: pd.DataFrame | None = None,
    masks: pd.DataFrame | None = None,
    apply_mask: bool = True,
) -> Panel:
    if panel is None:
        panel = pd.read_parquet(INTERIM / "panel.parquet")
    if diag is None:
        diag = pd.read_parquet(PROCESSED / "dim_series_diagnostics.parquet")
    if masks is None:
        masks = pd.read_parquet(PROCESSED / "censoring_masks.parquet")

    panel = panel.copy()
    panel["series_id"] = panel["series_id"].astype(str)
    masks = masks.copy()
    masks["series_id"] = masks["series_id"].astype(str)
    panel = panel.merge(masks, on=["series_id", "date"], how="left")

    dates = pd.DatetimeIndex(np.sort(panel["date"].unique()))
    sids = np.sort(panel["series_id"].unique())
    s_index = {s: i for i, s in enumerate(sids)}
    d_index = {d: i for i, d in enumerate(dates)}

    S, T = len(sids), len(dates)
    ri = panel["series_id"].map(s_index).to_numpy()
    ci = panel["date"].map(d_index).to_numpy()

    def scatter(values, dtype, fill):
        out = np.full((S, T), fill, dtype=dtype)
        out[ri, ci] = values
        return out

    y_raw = scatter(panel["sales"].to_numpy(dtype="float32"), "float32", np.nan)
    carried = scatter(panel["is_carried"].to_numpy(), bool, False)
    price = scatter(panel["sell_price"].to_numpy(dtype="float32"), "float32", np.nan)
    suspect = scatter(panel["suspected_censored"].fillna(False).to_numpy(), bool, False)

    y = y_raw.copy()
    y[~carried] = np.nan                     # structural: not demand signal at all
    if apply_mask:
        y[suspect] = np.nan                  # probabilistic: suspected stockout

    meta = (
        panel.groupby("series_id", observed=True)
        .agg(
            item_id=("item_id", "first"),
            store_id=("store_id", "first"),
            dept_id=("dept_id", "first"),
            cat_id=("cat_id", "first"),
            state_id=("state_id", "first"),
            cluster_id=("cluster_id", "first"),
        )
        .reset_index()
    )
    ids = pd.DataFrame({"series_id": sids}).merge(meta, on="series_id", how="left")
    series = series_frame(ids[ID_COLS], diag)

    cal = (
        panel[["date", "wday", "month", "year", "event_name", "event_type", "snap"]]
        .drop_duplicates("date")
        .sort_values("date")
        .reset_index(drop=True)
    )
    assert len(cal) == T, f"calendar rows {len(cal)} != matrix columns {T}"

    observed = np.flatnonzero(np.isfinite(y_raw).any(axis=0))
    n_history = int(observed.max()) + 1 if len(observed) else T
    return Panel(series, dates, y, y_raw, carried, price,
                 _calendar_features(cal), n_history=n_history)


# ------------------------------------------------------------------ calendar

def _calendar_features(cal: pd.DataFrame) -> pd.DataFrame:
    c = cal.copy()
    d = pd.DatetimeIndex(c["date"])
    c["dayofweek"] = d.dayofweek
    c["dayofmonth"] = d.day
    c["dayofyear"] = d.dayofyear
    c["weekofyear"] = d.isocalendar().week.to_numpy()
    c["quarter"] = d.quarter
    c["is_weekend"] = (d.dayofweek >= 5).astype("int8")
    c["is_month_start"] = d.is_month_start.astype("int8")
    c["is_month_end"] = d.is_month_end.astype("int8")

    # Holiday proximity. Signed distance to the nearest event in each direction,
    # which matters more in grocery than the event flag itself.
    is_ev = c["event_name"].notna().to_numpy()
    n = len(c)
    idx = np.arange(n)
    ev_idx = idx[is_ev]
    if len(ev_idx):
        nxt = np.searchsorted(ev_idx, idx, side="left")
        prv = nxt - 1
        days_to = np.where(nxt < len(ev_idx), ev_idx[np.clip(nxt, 0, len(ev_idx) - 1)] - idx, 999)
        days_from = np.where(prv >= 0, idx - ev_idx[np.clip(prv, 0, len(ev_idx) - 1)], 999)
    else:
        days_to = np.full(n, 999)
        days_from = np.full(n, 999)
    c["days_to_holiday"] = np.clip(days_to, 0, 60)
    c["days_from_holiday"] = np.clip(days_from, 0, 60)
    c["holiday_window"] = ((c["days_to_holiday"] <= 3) | (c["days_from_holiday"] <= 3)).astype("int8")

    for k in range(1, FOURIER_WEEKLY_K + 1):
        c[f"fw_sin{k}"] = np.sin(2 * np.pi * k * c["dayofweek"] / 7.0)
        c[f"fw_cos{k}"] = np.cos(2 * np.pi * k * c["dayofweek"] / 7.0)
    for k in range(1, FOURIER_ANNUAL_K + 1):
        c[f"fa_sin{k}"] = np.sin(2 * np.pi * k * c["dayofyear"] / 365.25)
        c[f"fa_cos{k}"] = np.cos(2 * np.pi * k * c["dayofyear"] / 365.25)

    c["event_name"] = c["event_name"].fillna("none")
    c["event_type"] = c["event_type"].fillna("none")
    return c


# ------------------------------------------------------------------ origin features

def _nan_moment(a: np.ndarray, order: int) -> np.ndarray:
    mu = np.nanmean(a, axis=1, keepdims=True)
    sd = np.nanstd(a, axis=1, keepdims=True)
    with np.errstate(invalid="ignore", divide="ignore"):
        z = (a - mu) / np.where(sd > 1e-9, sd, np.nan)
        return np.nanmean(z**order, axis=1)


def origin_features(P: Panel, O: int) -> tuple[np.ndarray, list[str]]:
    """Features computed strictly from `P.y[:, :O+1]`. One row per series.

    All-NaN windows are expected and correct: a cold-start series has no
    180-day history, and the right feature value there is missing, not zero.
    LightGBM handles NaN natively, so the warnings are suppressed rather than
    papered over with a fill value that would misrepresent the data.
    """
    y = P.y[:, : O + 1]
    S = P.n_series
    cols: list[np.ndarray] = []
    names: list[str] = []

    def add(name: str, v: np.ndarray) -> None:
        cols.append(np.asarray(v, dtype="float32").reshape(S))
        names.append(name)

    # --- lags relative to the origin. ylag_1 is the most recent known day.
    for L in LAGS:
        j = O - L + 1
        add(f"ylag_{L}", y[:, j] if j >= 0 else np.full(S, np.nan))

    # --- rolling statistics over windows ending at the origin
    for w in ROLL_WINDOWS:
        lo = max(0, O - w + 1)
        win = y[:, lo : O + 1]
        if win.shape[1] == 0:
            continue
        with np.errstate(invalid="ignore"):
            add(f"rmean_{w}", np.nanmean(win, axis=1))
            add(f"rstd_{w}", np.nanstd(win, axis=1))
            add(f"rmed_{w}", np.nanmedian(win, axis=1))
            add(f"rmin_{w}", np.nanmin(win, axis=1))
            add(f"rmax_{w}", np.nanmax(win, axis=1))
            add(f"rzero_{w}", np.nanmean((win == 0).astype("float32"), axis=1))
        if w >= 28:
            add(f"rskew_{w}", _nan_moment(win, 3))

    # --- mean of the last four weekly means: cheap level-trend signal
    wk = []
    for k in range(4):
        a, b = O - 7 * (k + 1) + 1, O - 7 * k + 1
        if a >= 0:
            with np.errstate(invalid="ignore"):
                wk.append(np.nanmean(y[:, a:b], axis=1))
    if len(wk) == 4:
        add("wmean_of_wmeans", np.nanmean(np.stack(wk), axis=0))
        add("wmean_trend", wk[0] - wk[3])
        add("wmean_ratio", wk[0] / np.where(wk[3] > 0.05, wk[3], np.nan))

    # --- days since last non-zero sale, and since last observation
    nz = np.where(np.nan_to_num(y, nan=0.0) > 0, np.arange(O + 1)[None, :], -1)
    last_nz = np.maximum.accumulate(nz, axis=1)[:, -1]
    add("days_since_nonzero", np.where(last_nz >= 0, O - last_nz, 999))
    seen = np.where(~np.isnan(y), np.arange(O + 1)[None, :], -1)
    last_seen = np.maximum.accumulate(seen, axis=1)[:, -1]
    add("days_since_observed", np.where(last_seen >= 0, O - last_seen, 999))

    # --- price at origin and its recent history
    pr = P.price[:, : O + 1]
    p_now = pr[:, -1]
    with np.errstate(invalid="ignore"):
        p_ref = np.nanmean(pr[:, max(0, O - 27) : O + 1], axis=1)
        add("price", p_now)
        add("price_ratio_28", p_now / np.where(p_ref > 0, p_ref, np.nan))
        p_prev_wk = pr[:, O - 7] if O >= 7 else np.full(S, np.nan)
        add("price_chg_wk", p_now - p_prev_wk)
        add("price_pct_wk", (p_now - p_prev_wk) / np.where(p_prev_wk > 0, p_prev_wk, np.nan))
        chg = np.abs(np.diff(np.nan_to_num(pr, nan=0.0), axis=1)) > 1e-6
        ci = np.where(chg, np.arange(1, O + 1)[None, :], -1)
        last_chg = np.maximum.accumulate(ci, axis=1)[:, -1] if O >= 1 else np.full(S, -1)
        add("days_since_price_chg", np.where(last_chg >= 0, O - last_chg, 999))
        add("discount_flag", (p_now < 0.93 * p_ref).astype("float32"))

    # --- availability pressure
    add("carried_rate_28", P.carried[:, max(0, O - 27) : O + 1].mean(axis=1))

    # --- cross-series: aggregate strength borrowed from the hierarchy
    dept_store = (P.series["dept_id"].astype(str) + "|" + P.series["store_id"].astype(str)).to_numpy()
    store = P.series["store_id"].astype(str).to_numpy()
    y0 = np.nan_to_num(y, nan=0.0)
    for name, key in (("dept_store", dept_store), ("store", store)):
        codes, inv = np.unique(key, return_inverse=True)
        agg = np.zeros((len(codes), y0.shape[1]), dtype="float32")
        np.add.at(agg, inv, y0)
        for w in (7, 28):
            lo = max(0, O - w + 1)
            m = agg[:, lo : O + 1].mean(axis=1)
            add(f"{name}_agg_mean_{w}", m[inv])
        own = y0[:, max(0, O - 27) : O + 1].mean(axis=1)
        parent = agg[:, max(0, O - 27) : O + 1].mean(axis=1)[inv]
        add(f"{name}_share", own / np.where(parent > 1e-6, parent, np.nan))

    # --- time-expanding target encodings.
    # Computed from history up to the origin only, so they are out-of-fold in
    # time. This is strictly stronger than K-fold encoding and cheaper, and it
    # is what makes in-fold target-encoding leakage impossible here.
    wd = P.calendar["dayofweek"].to_numpy()[: O + 1]
    enc_specs = {
        "enc_dept_store": dept_store,
        "enc_store": store,
        "enc_item": P.series["item_id"].astype(str).to_numpy(),
        "enc_cat_state": (P.series["cat_id"].astype(str) + "|" + P.series["state_id"].astype(str)).to_numpy(),
    }
    lo = max(0, O - 181)
    for nm, key in enc_specs.items():
        codes, inv = np.unique(key, return_inverse=True)
        for d in range(7):
            sel = np.flatnonzero(wd[lo:] == d) + lo
            if len(sel) == 0:
                continue
            block = y0[:, sel].mean(axis=1)
            grp = np.zeros(len(codes), dtype="float32")
            cnt = np.zeros(len(codes), dtype="float32")
            np.add.at(grp, inv, block)
            np.add.at(cnt, inv, 1.0)
            vals = grp / np.maximum(cnt, 1)
            add(f"{nm}_wd{d}", vals[inv])

    return np.column_stack(cols), names


# ------------------------------------------------------------------ design matrix

STATIC_NUMERIC = [
    "adi", "cv2", "health_score", "age_weeks", "weeks_since_break",
    "seasonal_strength", "trend_strength", "spectral_entropy", "acf7", "acf364",
    "zero_share", "cv", "censored_rate", "ooa_rate", "price_volatility",
]

TARGET_CAL = [
    "dayofweek", "dayofmonth", "dayofyear", "weekofyear", "month", "quarter",
    "is_weekend", "is_month_start", "is_month_end", "days_to_holiday",
    "days_from_holiday", "holiday_window", "snap",
] + [f"fw_{t}{k}" for k in range(1, FOURIER_WEEKLY_K + 1) for t in ("sin", "cos")] \
  + [f"fa_{t}{k}" for k in range(1, FOURIER_ANNUAL_K + 1) for t in ("sin", "cos")]


def horizon_group_of(h: int) -> str:
    for name, (a, b) in HORIZON_GROUPS.items():
        if a <= h <= b:
            return name
    raise ValueError(f"horizon {h} outside configured groups")


def make_design(
    P: Panel,
    origins: list[int],
    horizons: range | tuple[int, ...],
    with_target: bool = True,
    respect_regimes: bool = True,
) -> pd.DataFrame:
    """Assemble the model-ready matrix for a set of origins.

    One row per (series, origin, horizon). Origin features are computed once per
    origin and broadcast across horizons, which is both correct and ~28x cheaper
    than recomputing per target date.
    """
    S = P.n_series
    frames = []
    cal = P.calendar
    dates = P.dates

    for O in origins:
        of, of_names = origin_features(P, O)
        for h in horizons:
            t = O + h
            if t >= P.n_days:
                continue
            df = pd.DataFrame(of, columns=of_names)
            df["series_id"] = P.series["series_id"].to_numpy()
            df["origin"] = dates[O]
            df["target_date"] = dates[t]
            df["horizon"] = h
            df["horizon_group"] = horizon_group_of(h)

            row = cal.iloc[t]
            for c in TARGET_CAL:
                df[c] = row[c]
            df["event_name"] = row["event_name"]
            df["event_type"] = row["event_type"]

            # Planned price at the target date is known in advance from the
            # promotional calendar, so using it is not leakage.
            fut_p = P.price[:, t]
            df["future_price"] = fut_p
            with np.errstate(invalid="ignore"):
                df["future_price_ratio"] = fut_p / np.where(of[:, of_names.index("price")] > 0,
                                                            of[:, of_names.index("price")], np.nan)
            df["future_carried"] = P.carried[:, t].astype("int8")

            if with_target:
                df["y"] = P.y[:, t]
                df["y_raw"] = P.y_raw[:, t]
            frames.append(df)

    out = pd.concat(frames, ignore_index=True)

    meta_cols = ["series_id", "item_id", "dept_id", "cat_id", "store_id", "state_id",
                 "cluster_id", "planning_class"] + STATIC_NUMERIC
    out = out.merge(P.series[meta_cols], on="series_id", how="left")

    for c in CAT_FEATURES:
        if c in out.columns:
            out[c] = out[c].astype("category")

    # Rows where the item was not carried on the target date carry no demand
    # signal and must not become training examples.
    if with_target:
        out = out[out["future_carried"] == 1].reset_index(drop=True)

        # Stage 1's regime segmentation only means something if it truncates
        # something. `regime_start` was previously computed, carried through the
        # feature frame and shown in the UI while changing no training example at
        # all — the entire PELT stage had no effect on any model, which is also
        # why a defect in the pruning rule was invisible in every metric.
        #
        # Applied to training rows rather than by blanking feature windows: the
        # specification asks for the training window to start at the most recent
        # credible break, not for pre-break history to become unreadable. A lag-364
        # feature spanning a break is still information, and Stage 1 already hands
        # the model `weeks_since_break` so it can discount it.
        if respect_regimes and "regime_start" in P.series.columns:
            rs = P.series[["series_id", "regime_start"]].dropna()
            if len(rs):
                out = out.merge(rs, on="series_id", how="left")
                keep = out["regime_start"].isna() | (out["target_date"] >= out["regime_start"])
                out = out[keep].drop(columns=["regime_start"]).reset_index(drop=True)
    return out


def feature_columns(df: pd.DataFrame) -> list[str]:
    drop = {"series_id", "origin", "target_date", "y", "y_raw", "future_carried"}
    return [c for c in df.columns if c not in drop]


# ------------------------------------------------------------------ split plan

@dataclass
class SplitPlan:
    """Rolling-origin, purged split.

    Five blocks, each separated from the next by one full horizon so that no
    block's *target dates* ever overlap a later block's. Purging by origin alone
    is not enough: an origin 7 days before the next block still forecasts 28 days
    forward, so its targets run 21 days into that block. Halving one contiguous
    calibration block — which is what this used to do — left the FFORMA meta
    origins and the conformal origins sharing 21 target dates, so Stage 6 was
    calibrated partly on data Stage 4 had already fitted to, and the coverage
    guarantee was optimistic in exactly the way the stage exists to prevent.
    """

    train: list[int]
    meta: list[int]
    conformal: list[int]
    eval: int
    live: int
    dates: pd.DatetimeIndex
    horizon: int = 28

    @property
    def calib(self) -> list[int]:
        """Backwards-compatible view of both held-out blocks."""
        return self.meta + self.conformal

    def target_span(self, origins: list[int] | int) -> tuple[int, int]:
        o = [origins] if isinstance(origins, int) else origins
        return min(o) + 1, max(o) + self.horizon

    def assert_purged(self) -> None:
        """Fails loudly rather than quietly training on overlapping targets."""
        blocks = [("train", self.train), ("meta", self.meta),
                  ("conformal", self.conformal), ("eval", [self.eval])]
        for (n1, b1), (n2, b2) in zip(blocks[:-1], blocks[1:]):
            end1 = self.target_span(b1)[1]
            start2 = self.target_span(b2)[0]
            if end1 >= start2:
                raise ValueError(
                    f"{n1} targets end at index {end1} but {n2} targets begin at "
                    f"{start2} — blocks overlap by {end1 - start2 + 1} days"
                )

    def describe(self) -> str:
        d = self.dates

        def line(name: str, o: list[int]) -> str:
            lo, hi = self.target_span(o)
            return (f"{name:<9} {len(o):>3} origins  {d[o[0]].date()} .. {d[o[-1]].date()}"
                    f"   targets {d[lo].date()} .. {d[hi].date()}")

        return "\n".join([
            line("train", self.train),
            line("meta", self.meta),
            line("conformal", self.conformal),
            line("eval", [self.eval]) + "  (actuals known)",
            line("live", [self.live]) + "  (no actuals; the served forecast)",
        ])


def make_splits(
    P: Panel,
    horizon: int = 28,
    n_train_origins: int = 32,
    step: int = 7,
    n_meta_origins: int = 5,
    n_conformal_origins: int = 5,
    min_origin: int = 400,
) -> SplitPlan:
    # The live origin is the last day with observed sales. Its targets land in
    # the extended future region, which has calendar and planned price but no
    # actuals — that is the forecast the workspace serves.
    live = P.n_history - 1
    ev = live - horizon

    def block(end: int, n: int) -> list[int]:
        return list(range(end - step * (n - 1), end + 1, step))

    # Walk backwards from the eval origin, inserting a full horizon of purge
    # between every pair of blocks.
    conformal = block(ev - horizon, n_conformal_origins)
    meta = block(conformal[0] - horizon, n_meta_origins)
    train = block(meta[0] - horizon, n_train_origins)

    train = [o for o in train if o >= min_origin]
    if not train:
        raise ValueError(
            f"no training origins survive the purge: need history beyond index "
            f"{min_origin}, but the first training origin would be "
            f"{meta[0] - horizon - step * (n_train_origins - 1)}"
        )

    sp = SplitPlan(train=train, meta=meta, conformal=conformal, eval=ev,
                   live=live, dates=P.dates, horizon=horizon)
    sp.assert_purged()
    return sp


if __name__ == "__main__":
    P = build_panel()
    print(f"matrix        {P.n_series} series x {P.n_days} days")
    print(f"masked cells  {np.isnan(P.y).sum():,} of {P.y.size:,} "
          f"({np.isnan(P.y).mean():.1%} not demand signal)")
    sp = make_splits(P)
    print()
    print(sp.describe())

    d = make_design(P, sp.train[:2], range(1, 29))
    fc = feature_columns(d)
    print(f"\ndesign sample {len(d):,} rows x {len(fc)} features")
    print(f"memory        {d.memory_usage(deep=True).sum() / 1e6:.0f} MB for 2 origins")
