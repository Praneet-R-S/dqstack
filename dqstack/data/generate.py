"""M5-shaped panel generator with known latent demand.

Why synthetic. The real M5 competition data records *sales*, never *demand*.
Every zero is ambiguous: no customer wanted it, the store did not stock it, or
it sold out at 11am. That ambiguity is the subject of Stage 1, and it cannot be
scored on M5 because the ground truth does not exist in the file.

This generator emits both:

    latent_demand   what customers wanted      (never visible to the pipeline)
    sales           what the till recorded     (the only input models get)

plus the boolean masks `was_carried` and `was_censored`. Stage 1's censoring
detector is therefore evaluable with precision and recall against truth, which
is a stronger result than anything obtainable from the public competition data.

The dynamics reproduce the structure that matters in M5: day-of-week and annual
seasonality, SNAP benefit-day lifts on foods, holiday closures and event lifts,
weekly price records with promotional depth and cross-price elasticity, level
shifts (regime breaks), assortment gaps aligned to store-weeks, launch ramps,
terminal decline, and stockout episodes correlated with demand pressure.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import RAW, SEED

# ------------------------------------------------------------------ topology

STATES = {"CA": 4, "TX": 3, "WI": 3}          # 10 stores
DEPARTMENTS = {
    "FOODS_1": ("FOODS", 16),
    "FOODS_2": ("FOODS", 18),
    "FOODS_3": ("FOODS", 20),
    "HOUSEHOLD_1": ("HOUSEHOLD", 14),
    "HOUSEHOLD_2": ("HOUSEHOLD", 12),
    "HOBBIES_1": ("HOBBIES", 12),
    "HOBBIES_2": ("HOBBIES", 8),
}                                              # 100 items
N_DAYS = 1_100                 # days of *sales* history
FUTURE_DAYS = 28               # calendar and price plan extend this far past it
END_DATE = pd.Timestamp("2026-08-04")   # last day with recorded sales

ARCHETYPES = {
    #                weight  mu_lo  mu_hi   disp  p_occ  spike
    "smooth":       (0.22,   22.0,  95.0,  16.0,  1.00,  False),
    "erratic":      (0.16,   14.0,  55.0,   1.3,  1.00,  False),
    "intermittent": (0.20,    1.2,   2.6,   8.0,  0.46,  False),
    "lumpy":        (0.16,    4.0,   9.0,   0.9,  0.22,  True),
    "cold_start":   (0.13,   10.0,  42.0,   6.0,  1.00,  False),
    "terminal":     (0.13,   12.0,  48.0,   5.0,  1.00,  False),
}

CAT_DOW = {  # Mon..Sun multipliers — grocery peaks on the weekend
    "FOODS":     np.array([0.86, 0.83, 0.86, 0.93, 1.12, 1.30, 1.10]),
    "HOUSEHOLD": np.array([0.90, 0.88, 0.91, 0.97, 1.10, 1.26, 0.98]),
    "HOBBIES":   np.array([0.80, 0.78, 0.84, 0.95, 1.18, 1.40, 1.05]),
}
CAT_ANNUAL = {  # amplitude, phase (radians) of the dominant annual harmonic
    "FOODS":     (0.10, 0.4),
    "HOUSEHOLD": (0.14, 1.9),
    "HOBBIES":   (0.28, 3.1),
}
CAT_SHELF_LIFE = {"FOODS": 21, "HOUSEHOLD": 720, "HOBBIES": 720}
CAT_MARGIN = {"FOODS": 0.26, "HOUSEHOLD": 0.34, "HOBBIES": 0.44}


# ------------------------------------------------------------------ calendar

def _build_calendar(rng: np.random.Generator) -> pd.DataFrame:
    # The calendar runs FUTURE_DAYS past the last sales date. This is not a
    # convenience: holidays, benefit days and the promotional price plan are
    # genuinely known ahead of time, and a forecast for next month is impossible
    # without them. Sales stop at END_DATE; the calendar does not.
    dates = pd.date_range(end=END_DATE + pd.Timedelta(days=FUTURE_DAYS),
                          periods=N_DAYS + FUTURE_DAYS, freq="D")
    iso = dates.isocalendar()
    cal = pd.DataFrame(
        {
            "date": dates,
            "wm_yr_wk": (iso["year"].to_numpy() * 100 + iso["week"].to_numpy()).astype("int64"),
            "wday": dates.dayofweek.to_numpy() + 1,          # 1 = Monday
            "month": dates.month.to_numpy(),
            "year": dates.year.to_numpy(),
            "day": dates.day.to_numpy(),
        }
    )

    # Events. Closure days are structural zeros, not censored demand.
    ev_name: list[str | None] = [None] * len(cal)
    ev_type: list[str | None] = [None] * len(cal)

    def _mark(mask: np.ndarray, name: str, kind: str) -> None:
        for i in np.flatnonzero(mask):
            ev_name[i], ev_type[i] = name, kind

    md = list(zip(cal["month"], cal["day"]))
    md_arr = np.array([m * 100 + d for m, d in md])

    _mark(md_arr == 1225, "Christmas", "National")
    _mark(md_arr == 1224, "ChristmasEve", "National")
    _mark(md_arr == 1231, "NewYearEve", "National")
    _mark(md_arr == 101, "NewYear", "National")
    _mark(md_arr == 704, "IndependenceDay", "National")
    _mark(md_arr == 1031, "Halloween", "Cultural")
    _mark(md_arr == 214, "ValentinesDay", "Cultural")

    # Floating holidays: 4th Thursday of November, 1st Monday of September,
    # 2nd Sunday of May, 1st Sunday of February.
    dow = cal["wday"].to_numpy()
    day = cal["day"].to_numpy()
    mon = cal["month"].to_numpy()
    _mark((mon == 11) & (dow == 4) & (day >= 22) & (day <= 28), "Thanksgiving", "National")
    _mark((mon == 9) & (dow == 1) & (day <= 7), "LaborDay", "National")
    _mark((mon == 5) & (dow == 7) & (day >= 8) & (day <= 14), "MothersDay", "Cultural")
    _mark((mon == 2) & (dow == 7) & (day <= 7), "SuperBowl", "Sporting")
    # Easter drifts; approximate with a fixed spring Sunday per year.
    for yr in np.unique(cal["year"]):
        offset = int(rng.integers(0, 26))
        target = pd.Timestamp(year=int(yr), month=3, day=24) + pd.Timedelta(days=offset)
        while target.dayofweek != 6:
            target += pd.Timedelta(days=1)
        hit = np.flatnonzero(cal["date"].to_numpy() == np.datetime64(target))
        for i in hit:
            ev_name[i], ev_type[i] = "Easter", "Religious"

    cal["event_name"] = ev_name
    cal["event_type"] = ev_type

    # SNAP benefit days — state-specific windows inside the first half of the
    # month. These lift food demand materially and are a documented M5 driver.
    snap_windows = {"CA": (1, 10), "TX": (1, 15), "WI": (2, 15)}
    for st, (lo, hi) in snap_windows.items():
        elig = np.arange(lo, hi + 1)
        chosen = rng.choice(elig, size=min(10, len(elig)), replace=False)
        cal[f"snap_{st}"] = np.isin(day, chosen).astype("int64")

    return cal


# ------------------------------------------------------------------ master data

def _build_masters(rng: np.random.Generator) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows = []
    for dept, (cat, n) in DEPARTMENTS.items():
        for k in range(1, n + 1):
            rows.append({"item_id": f"{dept}_{k:03d}", "dept_id": dept, "cat_id": cat})
    items = pd.DataFrame(rows)

    n = len(items)
    keys = list(ARCHETYPES)
    probs = np.array([ARCHETYPES[k][0] for k in keys], dtype="float64")
    probs /= probs.sum()
    items["archetype"] = rng.choice(keys, size=n, p=probs)

    lo = items["archetype"].map(lambda a: ARCHETYPES[a][1]).to_numpy()
    hi = items["archetype"].map(lambda a: ARCHETYPES[a][2]).to_numpy()
    items["mu_base"] = lo * np.exp(rng.uniform(0.0, np.log(hi / lo), size=n))
    items["dispersion"] = items["archetype"].map(lambda a: ARCHETYPES[a][3]).to_numpy()
    items["p_occurrence"] = items["archetype"].map(lambda a: ARCHETYPES[a][4]).to_numpy()
    items["spiky"] = items["archetype"].map(lambda a: ARCHETYPES[a][5]).to_numpy()

    items["unit_cost"] = np.round(np.exp(rng.normal(0.85, 0.62, size=n)) + 0.45, 2)
    items["margin_rate"] = np.clip(
        items["cat_id"].map(CAT_MARGIN).to_numpy() + rng.normal(0, 0.055, size=n), 0.06, 0.72
    ).round(3)
    items["shelf_life_days"] = items["cat_id"].map(CAT_SHELF_LIFE).astype("int64")
    items["elasticity"] = np.where(
        items["cat_id"].to_numpy() == "FOODS",
        rng.uniform(-2.6, -1.1, size=n),
        rng.uniform(-1.9, -0.5, size=n),
    ).round(3)
    items["promo_rate"] = rng.uniform(0.02, 0.13, size=n).round(3)

    srows = []
    for st, cnt in STATES.items():
        for i in range(1, cnt + 1):
            srows.append(
                {
                    "store_id": f"{st}_{i}",
                    "state_id": st,
                    "cluster_id": f"{st}_C{1 + (i - 1) % 2}",
                }
            )
    stores = pd.DataFrame(srows)
    stores["size_mult"] = np.exp(rng.normal(0.0, 0.42, size=len(stores))).round(3)
    return items, stores


# ------------------------------------------------------------------ panel

def generate(seed: int = SEED, write: bool = True) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    cal = _build_calendar(rng)
    items, stores = _build_masters(rng)

    dates = cal["date"].to_numpy()
    T = len(cal)              # history + FUTURE_DAYS
    TH = N_DAYS               # history only; lifecycle events are anchored here
    t = np.arange(T)

    grid = items[["item_id", "dept_id", "cat_id"]].merge(stores[["store_id", "state_id"]], how="cross")
    grid = grid.merge(
        items[["item_id", "archetype", "mu_base", "dispersion", "p_occurrence", "spiky",
               "elasticity", "promo_rate", "unit_cost"]],
        on="item_id", how="left",
    ).merge(stores[["store_id", "size_mult"]], on="store_id", how="left")
    S = len(grid)

    # ---- deterministic seasonal surface ------------------------------------
    dow_idx = cal["wday"].to_numpy() - 1
    doy = cal["date"].dt.dayofyear.to_numpy()
    cat = grid["cat_id"].to_numpy()

    dow_mat = np.stack([CAT_DOW[c][dow_idx] for c in ("FOODS", "HOUSEHOLD", "HOBBIES")])
    ann_mat = np.stack(
        [
            1.0 + CAT_ANNUAL[c][0] * np.sin(2 * np.pi * doy / 365.25 + CAT_ANNUAL[c][1])
            for c in ("FOODS", "HOUSEHOLD", "HOBBIES")
        ]
    )
    cat_row = np.searchsorted(np.array(["FOODS", "HOBBIES", "HOUSEHOLD"]), cat)
    # searchsorted gives alphabetical index; remap to the stack order above
    order = {"FOODS": 0, "HOUSEHOLD": 1, "HOBBIES": 2}
    cat_row = np.array([order[c] for c in cat])

    mu = (grid["mu_base"].to_numpy() * grid["size_mult"].to_numpy())[:, None] * np.ones((S, T))
    mu *= dow_mat[cat_row]
    mu *= ann_mat[cat_row]

    # ---- slow trend + regime breaks --------------------------------------
    trend = rng.normal(0.0, 0.00035, size=S)[:, None] * t[None, :]
    mu *= np.exp(trend)

    break_flag = rng.random(S) < 0.32
    regime_start = np.zeros(S, dtype="int64")
    for i in np.flatnonzero(break_flag):
        b = int(rng.integers(220, TH - 150))
        shift = float(rng.uniform(0.55, 1.75))
        mu[i, b:] *= shift
        regime_start[i] = b

    # ---- lifecycle: launch ramp and terminal decline ---------------------
    arch = grid["archetype"].to_numpy()
    first_day = np.zeros(S, dtype="int64")
    last_day = np.full(S, T - 1, dtype="int64")

    cold = np.flatnonzero(arch == "cold_start")
    first_day[cold] = rng.integers(TH - 62, TH - 26, size=len(cold))
    term = np.flatnonzero(arch == "terminal")
    for i in term:
        onset = int(rng.integers(TH - 320, TH - 130))
        decay = np.ones(T)
        decay[onset:] = np.exp(-np.arange(T - onset) / float(rng.uniform(90, 190)))
        mu[i] *= decay
        if rng.random() < 0.45:
            last_day[i] = min(TH - 1, onset + int(rng.integers(200, 320)))
    # staggered history start for everything else
    other = np.setdiff1d(np.arange(S), np.concatenate([cold, term]))
    stagger = other[rng.random(len(other)) < 0.18]
    first_day[stagger] = rng.integers(0, 260, size=len(stagger))

    # launch ramp: 28-day S-curve from 35% to full velocity
    for i in np.flatnonzero(first_day > 0):
        f = first_day[i]
        ramp_len = min(28, T - f)
        mu[i, f:f + ramp_len] *= 0.35 + 0.65 / (1 + np.exp(-(np.arange(ramp_len) - 10) / 4.0))

    # ---- weekly prices with promotions ----------------------------------
    week_codes, week_idx = np.unique(cal["wm_yr_wk"].to_numpy(), return_inverse=True)
    W = len(week_codes)
    base_price = (grid["unit_cost"].to_numpy() / (1.0 - 0.32)) * rng.uniform(0.9, 1.2, size=S)
    price_w = np.repeat(base_price[:, None], W, axis=1)
    # slow annual drift
    price_w *= np.exp(rng.normal(0, 0.0009, size=S)[:, None] * np.arange(W)[None, :])
    promo = rng.random((S, W)) < grid["promo_rate"].to_numpy()[:, None]
    depth = rng.uniform(0.10, 0.38, size=(S, W))
    price_w = np.where(promo, price_w * (1.0 - depth), price_w).round(2)
    price_w = np.maximum(price_w, 0.05)

    price_d = price_w[:, week_idx]
    ref_price = np.repeat(base_price[:, None], T, axis=1)
    elas = grid["elasticity"].to_numpy()[:, None]
    mu *= np.clip((price_d / ref_price) ** elas, 0.35, 3.2)

    # ---- events and SNAP -------------------------------------------------
    ev = cal["event_name"].to_numpy()
    ev_mult = np.ones(T)
    for nm, m in {
        "Thanksgiving": 1.42, "ChristmasEve": 1.55, "NewYearEve": 1.30,
        "SuperBowl": 1.34, "Easter": 1.22, "Halloween": 1.18,
        "IndependenceDay": 1.20, "LaborDay": 1.12, "MothersDay": 1.14,
        "ValentinesDay": 1.16, "NewYear": 0.72,
    }.items():
        ev_mult[ev == nm] = m
    mu *= ev_mult[None, :]

    snap_by_state = {st: cal[f"snap_{st}"].to_numpy() for st in STATES}
    snap_mat = np.stack([snap_by_state[s] for s in grid["state_id"].to_numpy()])
    food_mask = (cat == "FOODS")[:, None]
    mu *= np.where(snap_mat.astype(bool) & food_mask, 1.19, 1.0)

    # ---- latent demand draw ---------------------------------------------
    mu = np.clip(mu, 1e-4, None)
    disp = grid["dispersion"].to_numpy()[:, None]
    p_nb = disp / (disp + mu)
    latent = rng.negative_binomial(np.broadcast_to(disp, mu.shape), p_nb).astype("int32")

    p_occ = grid["p_occurrence"].to_numpy()[:, None]
    sparse = p_occ < 1.0
    occur = rng.random((S, T)) < np.broadcast_to(p_occ, (S, T))
    spiky = grid["spiky"].to_numpy().astype(bool)[:, None]
    boost = np.where(spiky, rng.integers(2, 7, size=(S, T)), 1)
    latent = np.where(sparse, np.where(occur, latent * boost, 0), latent).astype("int32")

    # Christmas: stores closed. Structural zero, and the pipeline must not read
    # it as demand collapse.
    closed = (ev == "Christmas")
    latent[:, closed] = 0

    # ---- availability (assortment) --------------------------------------
    # Absence of a price record for a store-week is the availability signal.
    carried_w = np.ones((S, W), dtype=bool)
    fw = week_idx[first_day]
    lw = week_idx[last_day]
    wcol = np.arange(W)[None, :]
    carried_w &= (wcol >= fw[:, None]) & (wcol <= lw[:, None])
    # random mid-life assortment gaps on a minority of series
    gap_series = np.flatnonzero(rng.random(S) < 0.22)
    for i in gap_series:
        for _ in range(int(rng.integers(1, 4))):
            g0 = int(rng.integers(fw[i], max(fw[i] + 1, lw[i] - 2)))
            carried_w[i, g0:g0 + int(rng.integers(1, 5))] = False
    carried_d = carried_w[:, week_idx]

    # ---- stockout censoring ---------------------------------------------
    # Pressure rises with demand relative to the series' own recent level, so
    # stockouts concentrate on exactly the fast-moving items that matter.
    level = np.maximum(mu.mean(axis=1, keepdims=True), 1e-6)
    pressure = np.clip(mu / (level * 1.35), 0.0, 3.0)
    hazard = np.clip(0.026 * pressure ** 1.7, 0.0, 0.10)
    hazard *= (~spiky).astype(float) * 0.85 + 0.15
    start_out = (rng.random((S, T)) < hazard) & carried_d

    censored = np.zeros((S, T), dtype=bool)
    partial = np.zeros((S, T), dtype="float32")
    si, ti = np.nonzero(start_out)
    dur = rng.integers(1, 5, size=len(si))
    for k in range(len(si)):
        a, b = ti[k], min(T, ti[k] + int(dur[k]))
        censored[si[k], a:b] = True
        # first day of an episode is usually partial, later days total
        partial[si[k], a] = float(rng.uniform(0.15, 0.70))
    censored &= carried_d
    censored[:, closed] = False

    sales = latent.copy()
    frac = np.where(partial > 0, partial, 0.0)
    sales = np.where(censored, np.floor(latent * frac).astype("int32"), sales)
    sales = np.where(~carried_d, 0, sales).astype("int32")

    # ---- long form -------------------------------------------------------
    # Sales and ground truth stop at the last historical day. Prices and the
    # calendar run FUTURE_DAYS further, which is what gives the live origin
    # something to forecast onto.
    item_col = np.repeat(grid["item_id"].to_numpy(), TH)
    store_col = np.repeat(grid["store_id"].to_numpy(), TH)
    date_col = np.tile(dates[:TH], S)

    sales_df = pd.DataFrame(
        {
            "item_id": pd.Categorical(item_col),
            "store_id": pd.Categorical(store_col),
            "date": date_col,
            "sales": sales[:, :TH].reshape(-1),
            # ground truth — held out of the pipeline, used only for scoring
            "latent_demand": latent[:, :TH].reshape(-1),
            "was_carried": carried_d[:, :TH].reshape(-1),
            "was_censored": censored[:, :TH].reshape(-1),
        }
    )

    keep = carried_w.reshape(-1)
    price_df = pd.DataFrame(
        {
            "item_id": pd.Categorical(np.repeat(grid["item_id"].to_numpy(), W)[keep]),
            "store_id": pd.Categorical(np.repeat(grid["store_id"].to_numpy(), W)[keep]),
            "wm_yr_wk": np.tile(week_codes, S)[keep],
            "sell_price": price_w.reshape(-1)[keep],
        }
    )

    items_out = items[["item_id", "dept_id", "cat_id", "unit_cost", "margin_rate", "shelf_life_days"]]
    stores_out = stores[["store_id", "state_id", "cluster_id"]]
    truth = pd.DataFrame(
        {
            "item_id": grid["item_id"].to_numpy(),
            "store_id": grid["store_id"].to_numpy(),
            "archetype": arch,
            "generated_regime_start": np.where(
                regime_start > 0, cal["date"].to_numpy()[np.clip(regime_start, 0, T - 1)], np.datetime64("NaT")
            ),
            "first_day": cal["date"].to_numpy()[first_day],
            "last_day": cal["date"].to_numpy()[last_day],
        }
    )

    out = {
        "sales": sales_df,
        "prices": price_df,
        "calendar": cal.drop(columns=["day"]),
        "items": items_out,
        "stores": stores_out,
        "truth": truth,
    }

    if write:
        for name, df in out.items():
            df.to_parquet(RAW / f"{name}.parquet", index=False)

    return out


if __name__ == "__main__":
    tables = generate()
    s = tables["sales"]
    print(f"series      {len(tables['truth']):,}")
    print(f"panel rows  {len(s):,}")
    print(f"date range  {s['date'].min().date()} .. {s['date'].max().date()}")
    print(f"zero share  {(s['sales'] == 0).mean():.1%}")
    print(f"carried     {s['was_carried'].mean():.1%}")
    print(f"censored    {s['was_censored'].mean():.2%} of all rows")
    print(f"units lost  {(s['latent_demand'] - s['sales'])[s['was_censored']].sum():,}")
    print(tables["truth"]["archetype"].value_counts().to_string())
