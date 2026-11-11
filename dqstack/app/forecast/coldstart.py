"""Estimates for products and stores with little or no sales of their own
(specification section 13, scenarios 1 and 14).

The engine forecasts what it has seen. Three situations need more, and each is
labelled on the recommendation (reason key `estimate`) so Today and the Product
screen say how the number was made:

* **A new store in a company that already sells elsewhere** ("new-store mode").
  Each product is forecast from the same product in the company's established
  stores -- those with at least 8 weeks of their own sales -- averaged across them
  (quantile by quantile) and scaled by this store's size relative to them. The
  scale is, in order of preference: the store's own first weeks of sales against
  the established stores' sales on the same days, category by category and shrunk
  towards the store-wide ratio (needs 2 weeks of its sales); else the size classes
  in the store settings (small, medium, large; see SIZE_FACTOR); else 1, and the
  label says it was not scaled.
* **A new product, in the early-forecast tier.** The early forecast's statistical
  candidates have nothing to work with for a product that has not sold, so it is
  forecast from the established products of the same category in the same store
  (the median at each quantile level). The full engine has cold-start candidates
  of its own (launch curves, category encodings) and is left alone.
* **A company with less than 8 weeks of sales.** Its own early forecast, labelled
  "estimate, little history"; a product that has never sold borrows from its
  category as above, from products with at least 2 weeks of sales.

Handover. A borrowed forecast is blended with the series' own, quantile by quantile,
the own share being the part of 8 weeks it has (own days / 56). At 8 weeks a series
is on its own forecast: "switches to its own forecast after 8 weeks of sales",
without the jump a hard switch would make.

Company separation is absolute: donors and peers are rows of this company's own
forecast. Nothing here can see another company's data.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

MIN_DAYS = 14        # the first forecast: see jobs.plan for why two weeks
OWN_DAYS = 56        # 8 weeks: a series' own forecast has taken over completely

# Relative demand by store size class. The demo company (app/demo.py) has its
# medium store sell 0.65 of its large one; small is taken as one more step of the
# same size. A vendor assumption, used only when the store has no sales of its own.
SIZE_FACTOR = {"small": 0.65, "medium": 1.0, "large": round(1 / 0.65, 3)}
SHRINK_UNITS = 50.0        # pseudo-count pulling a category's ratio to the store's
RATIO_BOUNDS = (0.1, 5.0)


def _days(n: int) -> str:
    return f"{n} day{'s' * (n != 1)}"


def store_ratio(sales: pd.DataFrame, cat_of: dict, new: str, donors: set[str],
                first_day, store_days: int, sizes: dict[str, str | None]
                ) -> tuple[dict[str, float], float, str]:
    """(ratio per category, store-wide ratio, plain words for the label) for a new
    store against the average established store."""
    lo, hi = RATIO_BOUNDS
    if store_days >= MIN_DAYS and first_day is not None:
        win = sales[sales["date"] >= pd.Timestamp(first_day)]
        own = win[win["store_id"] == new]
        don = win[win["store_id"].isin(donors)]
        n_d = max(len(donors), 1)
        mean_d = float(don["sales"].sum()) / n_d
        if mean_d > 0:
            r = float(np.clip(own["sales"].sum() / mean_d, lo, hi))
            own_c = own.groupby(own["item_id"].map(cat_of))["sales"].sum()
            don_c = don.groupby(don["item_id"].map(cat_of))["sales"].sum() / n_d
            by_cat = {c: float(np.clip((own_c.get(c, 0.0) + SHRINK_UNITS * r)
                                       / (don_c.get(c, 0.0) + SHRINK_UNITS), lo, hi))
                      for c in set(don_c.index) | set(own_c.index)}
            return by_cat, r, (f"scaled by this store's first {_days(store_days)} of sales "
                               f"(about {round(100 * r)}% of an established store)")
    size = (sizes.get(new) or "").lower()
    if size in SIZE_FACTOR:
        f = [SIZE_FACTOR.get((sizes.get(d) or "medium").lower(), 1.0) for d in sorted(donors)]
        r = float(np.clip(SIZE_FACTOR[size] / (sum(f) / len(f)), lo, hi))
        return {}, r, f"scaled by store size ({size}: about {round(100 * r)}% of your other stores)"
    return {}, 1.0, "not scaled, because this store's size is not set"


def apply(res, data, tier: str) -> dict[int, dict]:
    """Replace or blend the forecast of series without enough sales of their own, in
    place in `res.quantiles`, and return a label for every series that is an
    estimate: row -> {"basis": "no_history" | "little_history", "text": ...,
    "own_weight": share of its own forecast}."""
    q = res.quantiles
    series = res.P.series
    item = series["item_id"].astype(str).to_numpy()
    store = series["store_id"].astype(str).to_numpy()
    hist = np.asarray(res.history_days, dtype=int)
    items = data.tables["items"]
    cat_of = dict(zip(items["item_id"].astype(str), items["cat_id"].astype(str)))
    cat = np.array([cat_of.get(i, "General") for i in item], dtype=object)
    store_days = getattr(data, "store_days", {}) or {}
    new_stores = getattr(data, "new_stores", set()) or set()
    donors = getattr(data, "donor_stores", set()) or set()
    labels: dict[int, dict] = {}

    def blend(r: int, borrowed: np.ndarray, w: float) -> None:
        q[r] = np.maximum(w * q[r] + (1.0 - w) * borrowed, 0.0)

    # ---- new stores: the same product in the established stores
    if new_stores and donors:
        row_of = {(i, s): r for r, (i, s) in enumerate(zip(item, store))}
        sizes = {sid: st.size for sid, st in data.stores.items()}
        first = getattr(data, "store_first", {}) or {}
        sales = data.tables["sales"]
        for n in sorted(new_stores):
            days = int(store_days.get(n, 0))
            by_cat, r_all, how = store_ratio(sales, cat_of, n, donors, first.get(n), days, sizes)
            w = min(days, OWN_DAYS) / OWN_DAYS
            for r in np.flatnonzero(store == n):
                rows = [row_of[(item[r], d)] for d in sorted(donors) if (item[r], d) in row_of]
                if not rows:
                    continue
                borrowed = q[rows].mean(axis=0) * by_cat.get(cat[r], r_all)
                blend(r, borrowed, w)
                if w == 0:
                    text = (f"Estimate, no history: from the same product in your other "
                            f"stores, {how}. This store's own forecast starts as its "
                            f"sales come in and takes over at 8 weeks.")
                else:
                    text = (f"Estimate, little history: this store has {_days(days)} of "
                            f"sales, so its forecast is blended with the same product in "
                            f"your other stores, {how}. Its own forecast takes over at 8 weeks.")
                labels[int(r)] = {"basis": "no_history" if w == 0 else "little_history",
                                  "text": text, "own_weight": round(w, 3)}

    # ---- new products, early-forecast tier: similar products in the same category
    if tier == "starter":
        established = data.history_days >= OWN_DAYS
        peer_min = OWN_DAYS if established else MIN_DAYS
        in_new = np.isin(store, list(new_stores)) if new_stores else np.zeros(len(store), bool)
        if established:
            want = (hist < OWN_DAYS) & ~in_new
        else:
            want = hist == 0
        peer_ok = (hist >= peer_min) & ~in_new
        ccode = pd.factorize(cat)[0]
        gcode = pd.factorize(store)[0] * (int(ccode.max(initial=0)) + 1) + ccode
        groups: dict[int, np.ndarray | None] = {}
        for r in np.flatnonzero(want):
            if int(r) in labels:
                continue
            key = int(gcode[r])
            if key not in groups:                 # same store and category, else category
                peers = np.flatnonzero(peer_ok & (gcode == key))
                if not len(peers):
                    peers = np.flatnonzero(peer_ok & (ccode == ccode[r]))
                groups[key] = np.median(q[peers], axis=0) if len(peers) else None
            borrowed = groups[key]
            if borrowed is None:
                continue
            own = int(hist[r])
            w = min(own, OWN_DAYS) / OWN_DAYS if established else 0.0
            blend(r, borrowed, w)
            if w == 0:
                text = (f"Estimate, no history: from similar products in {cat[r]}. Its own "
                        f"forecast takes over after 8 weeks of sales.")
            else:
                text = (f"Estimate, little history: {_days(own)} of its own sales, blended "
                        f"with similar products in {cat[r]}. Its own forecast takes over "
                        f"at 8 weeks.")
            labels[int(r)] = {"basis": "no_history" if w == 0 else "little_history",
                              "text": text, "own_weight": round(w, 3)}

    # ---- everything else with less than 8 weeks of its own sales
    for r in np.flatnonzero(hist < OWN_DAYS):
        if int(r) in labels:
            continue
        own = int(hist[r])
        if own == 0:
            text = ("Estimate, no history: from comparable products until it has 8 weeks "
                    "of sales." if tier != "starter" else
                    "Estimate, no history: no sales yet, and no similar products to "
                    "compare it with.")
            labels[int(r)] = {"basis": "no_history", "text": text, "own_weight": 1.0}
        else:
            labels[int(r)] = {"basis": "little_history", "own_weight": 1.0,
                              "text": f"Estimate, little history: {_days(own)} of sales so "
                                      f"far. Full forecasting starts at 8 weeks."}
    return labels
