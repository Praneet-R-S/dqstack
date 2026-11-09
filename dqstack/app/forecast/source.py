"""One company's data, from the database, in the shape the forecasting engine reads
(the five feeds of dqstack/ingest.py).

What the engine sees, and why:

* A series is one product in one store. A product sold each is forecast in units;
  one sold by weight or volume in steps of 100 g or 100 ml, because the engine
  models counts. `Series.scale` converts back.
* A day with no sales row is a day with zero sales -- but only while the product
  is carried there, and only up to that store's own last upload. A standalone store
  that forgot yesterday's file must not look as if it sold nothing.
* Days the shelf was known to be empty (the ledger saw zero stock) and days the
  store was closed are missing, not zero: neither says anything about demand.
* National festivals go into the engine's event calendar. State festivals, and a
  store's own local events, go into the state flag column (`snap_<state>`), the
  slot the demo uses for benefit days, so a festival lifts only the stores it
  applies to.
* A product launched with no sales yet is carried from the day it was added, so
  the engine's cold-start candidates (similar products, launch curves) forecast it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...config import HORIZON
from ..models import (CalendarEvent, Company, DailySales, PriceRecord, Product, StockMovement,
                      Store)

STATE_CODES = {
    "Andhra Pradesh": "AP", "Arunachal Pradesh": "AR", "Assam": "AS", "Bihar": "BR",
    "Chhattisgarh": "CG", "Goa": "GA", "Gujarat": "GJ", "Haryana": "HR",
    "Himachal Pradesh": "HP", "Jharkhand": "JH", "Karnataka": "KA", "Kerala": "KL",
    "Madhya Pradesh": "MP", "Maharashtra": "MH", "Manipur": "MN", "Meghalaya": "ML",
    "Mizoram": "MZ", "Nagaland": "NL", "Odisha": "OD", "Punjab": "PB", "Rajasthan": "RJ",
    "Sikkim": "SK", "Tamil Nadu": "TN", "Telangana": "TS", "Tripura": "TR",
    "Uttar Pradesh": "UP", "Uttarakhand": "UK", "West Bengal": "WB", "Delhi": "DL",
    "Jammu and Kashmir": "JK", "Ladakh": "LA", "Puducherry": "PY", "Chandigarh": "CH",
    "Andaman and Nicobar Islands": "AN", "Dadra and Nagar Haveli and Daman and Diu": "DN",
    "Lakshadweep": "LD",
}
REAL_SOURCES = ("history", "upload", "entry", "checkout", "pos")
DEFAULT_MARGIN = 0.30


class NotEnoughHistory(Exception):
    def __init__(self, days: int):
        super().__init__(f"{days} days of sales history")
        self.days = days


def scale_of(unit: str) -> int:
    """Thousandths of the stock unit per engine unit."""
    return 1000 if unit == "each" else 100


def week_id(d: pd.Series) -> pd.Series:
    iso = d.dt.isocalendar()
    return (iso["year"].astype(int) * 100 + iso["week"].astype(int)).astype("int64")


@dataclass
class CompanyData:
    tables: dict[str, pd.DataFrame]
    products: dict[str, Product]
    stores: dict[str, Store]
    start: date
    cutoff: date
    history_days: int

    def scale(self, product_id: str) -> int:
        return scale_of(self.products[product_id].unit)


def load(db: Session, company_id: str, as_of: date | None = None) -> CompanyData:
    co = db.get(Company, company_id)
    stores = {s.id: s for s in db.scalars(select(Store).where(
        Store.company_id == company_id, Store.active.is_(True)))}
    products = {p.id: p for p in db.scalars(select(Product).where(
        Product.company_id == company_id))}
    if not stores or not products:
        raise NotEnoughHistory(0)
    q = select(DailySales.store_id, DailySales.product_id, DailySales.day,
               DailySales.units_milli, DailySales.out_of_stock, DailySales.oos_source,
               DailySales.source).where(DailySales.company_id == company_id,
                                        DailySales.store_id.in_(list(stores)))
    if as_of is not None:
        q = q.where(DailySales.day <= as_of)
    ds = pd.DataFrame(db.execute(q).all(), columns=["store_id", "product_id", "day", "units",
                                                    "oos", "oos_source", "source"])
    real = ds[ds["source"].isin(REAL_SOURCES)]
    if real.empty:
        raise NotEnoughHistory(0)
    ds["day"] = pd.to_datetime(ds["day"])
    real = ds[ds["source"].isin(REAL_SOURCES)]
    start = real["day"].min()
    cutoff = real["day"].max()
    history_days = int((cutoff - start).days) + 1
    store_last = real.groupby("store_id")["day"].max().to_dict()

    # ---- missing days: closures and known stock-outs
    closures = _closures(db, company_id, stores, start.date(), cutoff.date())
    empty = ds[ds["oos"] & (ds["oos_source"] == "ledger")][["store_id", "product_id", "day"]]

    # ---- where each product is carried, per store
    first_sale = (real[real["units"] > 0].groupby(["store_id", "product_id"])["day"].min())
    sold_anywhere = set(real.loc[real["units"] > 0, "product_id"])
    first_move = {(s, p): pd.Timestamp(d) for s, p, d in db.execute(
        select(StockMovement.store_id, StockMovement.product_id, func.min(StockMovement.at))
        .where(StockMovement.company_id == company_id)
        .group_by(StockMovement.store_id, StockMovement.product_id)).all()}
    horizon_end = cutoff + pd.Timedelta(days=HORIZON)
    carried: dict[tuple[str, str], tuple[pd.Timestamp, pd.Timestamp]] = {}
    # A product added after the last day of data is priced (so it is forecast) but
    # has no observed days: zero sales before it existed would read as no demand.
    observed_from: dict[tuple[str, str], pd.Timestamp] = {}
    for sid in stores:
        for pid, p in products.items():
            begin = first_sale.get((sid, pid))
            moved = first_move.get((sid, pid))
            if moved is not None:
                moved = moved.tz_localize(None).normalize() if moved.tzinfo else moved.normalize()
                begin = moved if begin is None else min(begin, moved)
            if begin is None and pid not in sold_anywhere and p.status != "discontinued":
                made = pd.Timestamp(p.created_at).tz_localize(None).normalize() \
                    if p.created_at else start
                begin = max(min(made, cutoff), start)
                observed_from[(sid, pid)] = max(made, start)
            if begin is None:
                continue
            if p.status == "discontinued":
                last = real[(real["store_id"] == sid) & (real["product_id"] == pid)]["day"].max()
                end = last if pd.notna(last) else begin
            else:
                end = horizon_end
            carried[(sid, pid)] = (begin, end)
    if not carried:
        raise NotEnoughHistory(history_days)

    # ---- sales: dense from first carried day to the store's last upload
    frames = []
    units = real.groupby(["store_id", "product_id", "day"])["units"].sum()
    units_df = units.reset_index()
    for (sid, pid), (begin, end) in carried.items():
        last = min(store_last.get(sid, cutoff), end, cutoff)
        first = max(begin, observed_from.get((sid, pid), begin))
        if last < first:
            continue
        days = pd.date_range(first, last, freq="D")
        frames.append(pd.DataFrame({"store_id": sid, "item_id": pid, "date": days}))
    sales = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["store_id", "item_id", "date"])
    sales = sales.merge(units_df.rename(columns={"product_id": "item_id", "day": "date"}),
                        on=["store_id", "item_id", "date"], how="left")
    sales["units"] = sales["units"].fillna(0)
    scale = {pid: scale_of(p.unit) for pid, p in products.items()}
    sales["sales"] = np.rint(sales["units"] / sales["item_id"].map(scale)).clip(lower=0).astype(
        "int64")
    drop = pd.concat([closures, empty.rename(columns={"product_id": "item_id", "day": "date"})],
                     ignore_index=True)
    if not drop.empty:
        key = sales.set_index(["store_id", "item_id", "date"]).index
        gone = pd.MultiIndex.from_frame(drop[["store_id", "item_id", "date"]].astype(
            {"date": "datetime64[ns]"}))
        sales = sales[~key.isin(gone)]
    sales = sales[["item_id", "store_id", "date", "sales"]].reset_index(drop=True)

    # ---- calendar, items, stores, prices
    calendar = _calendar(db, company_id, stores, start, horizon_end)
    items = _items(products)
    store_tbl = pd.DataFrame([{"store_id": s.id, "state_id": STATE_CODES.get(s.state or "", "XX"),
                               "cluster_id": s.size or "medium"} for s in stores.values()])
    prices = _prices(db, company_id, products, carried, calendar)
    # The engine's row-count check compares a feed with its sender's declaration.
    # This feed is built here, so its declaration is what was built; whether every
    # store's daily file arrived is checked at Day close, where it can be acted on.
    tables = {"sales": sales, "prices": prices, "calendar": calendar, "items": items,
              "stores": store_tbl, "_declared_rows": {"sales": len(sales)}}
    return CompanyData(tables=tables, products=products, stores=stores, start=start.date(),
                       cutoff=cutoff.date(), history_days=history_days)


def _closures(db: Session, company_id: str, stores: dict, lo: date, hi: date) -> pd.DataFrame:
    rows = []
    for e in db.scalars(select(CalendarEvent).where(
            CalendarEvent.company_id == company_id, CalendarEvent.kind == "closure",
            CalendarEvent.day >= lo, CalendarEvent.day <= hi)):
        for sid in (e.store_ids or list(stores)):
            if sid in stores:
                rows.append({"store_id": sid, "date": pd.Timestamp(e.day)})
    if not rows:
        return pd.DataFrame(columns=["store_id", "item_id", "date"])
    cl = pd.DataFrame(rows)
    products = db.scalars(select(Product.id).where(Product.company_id == company_id)).all()
    return cl.merge(pd.DataFrame({"item_id": products}), how="cross")


def _calendar(db: Session, company_id: str, stores: dict, start: pd.Timestamp,
              end: pd.Timestamp) -> pd.DataFrame:
    days = pd.date_range(start, end, freq="D")
    cal = pd.DataFrame({"date": days})
    cal["wm_yr_wk"] = week_id(cal["date"])
    cal["wday"] = ((cal["date"].dt.dayofweek + 2) % 7) + 1     # M5 convention: Saturday = 1
    cal["month"] = cal["date"].dt.month
    cal["year"] = cal["date"].dt.year
    names: dict[pd.Timestamp, list[str]] = {}
    kinds: dict[pd.Timestamp, str] = {}
    codes = sorted({STATE_CODES.get(s.state or "", "XX") for s in stores.values()})
    flags = {c: np.zeros(len(cal), dtype="int64") for c in codes}
    pos = {d: i for i, d in enumerate(days)}
    store_code = {sid: STATE_CODES.get(s.state or "", "XX") for sid, s in stores.items()}
    for e in db.scalars(select(CalendarEvent).where(
            CalendarEvent.company_id == company_id, CalendarEvent.kind != "closure",
            CalendarEvent.day >= start.date(), CalendarEvent.day <= end.date())):
        d = pd.Timestamp(e.day)
        if e.store_ids:
            for sid in e.store_ids:
                if sid in store_code:
                    flags[store_code[sid]][pos[d]] = 1
        elif e.states:
            for st in e.states:
                c = STATE_CODES.get(st)
                if c in flags:
                    flags[c][pos[d]] = 1
        else:
            names.setdefault(d, []).append(e.name)
            kinds[d] = "National" if e.kind == "holiday" else "Festival"
    cal["event_name"] = cal["date"].map(lambda d: " / ".join(sorted(names[d])) if d in names
                                        else None)
    cal["event_type"] = cal["date"].map(lambda d: kinds.get(d))
    for c, v in flags.items():
        cal[f"snap_{c}"] = v
    return cal


def _items(products: dict) -> pd.DataFrame:
    rows = []
    for pid, p in products.items():
        sc = scale_of(p.unit)
        price = p.price_paise or p.mrp_paise
        cost = p.cost_paise
        if cost is None and price:
            cost = int(price * (1 - DEFAULT_MARGIN))
        if price is None and cost:
            price = int(cost / (1 - DEFAULT_MARGIN))
        price = price or 100
        cost = cost or int(price * (1 - DEFAULT_MARGIN))
        margin = min(max((price - cost) / price, 0.0), 0.9) if price else DEFAULT_MARGIN
        shelf = p.shelf_life_days or (7 if p.perishable else 365)
        rows.append({"item_id": pid, "dept_id": p.department or p.category or "General",
                     "cat_id": p.category or "General",
                     "unit_cost": max(cost * sc / 1000 / 100, 0.01),
                     "margin_rate": float(margin), "shelf_life_days": int(max(shelf, 1))})
    return pd.DataFrame(rows)


def _prices(db: Session, company_id: str, products: dict, carried: dict,
            calendar: pd.DataFrame) -> pd.DataFrame:
    """Weekly price per product and store, for every week it is carried: the
    catalogue price, overridden by price records (all stores, then the store's own),
    averaged over the week so a three-day promotion counts as three days."""
    days = pd.DatetimeIndex(calendar["date"])
    weeks = calendar["wm_yr_wk"].to_numpy()
    records: dict[str, list[PriceRecord]] = {}
    for r in db.scalars(select(PriceRecord).where(PriceRecord.company_id == company_id)):
        records.setdefault(r.product_id, []).append(r)
    out = []
    for (sid, pid), (begin, end) in carried.items():
        p = products[pid]
        sc = scale_of(p.unit)
        base = p.price_paise or p.mrp_paise or (int(p.cost_paise / (1 - DEFAULT_MARGIN))
                                                 if p.cost_paise else 100)
        daily = np.full(len(days), float(base))
        recs = sorted(records.get(pid, []), key=lambda r: (r.store_id is not None, r.start_date))
        for r in recs:
            if r.store_id not in (None, sid):
                continue
            lo = np.searchsorted(days, pd.Timestamp(r.start_date))
            hi = np.searchsorted(days, pd.Timestamp(r.end_date) + pd.Timedelta(days=1)) \
                if r.end_date else len(days)
            daily[lo:hi] = float(r.price_paise)
        mask = (days >= begin) & (days <= end)
        if not mask.any():
            continue
        frame = pd.DataFrame({"wk": weeks[mask], "price": daily[mask]})
        wk = frame.groupby("wk")["price"].mean()
        out.append(pd.DataFrame({"item_id": pid, "store_id": sid, "wm_yr_wk": wk.index.astype("int64"),
                                 "sell_price": np.maximum(wk.to_numpy() * sc / 1000 / 100, 0.01)}))
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame(
        columns=["item_id", "store_id", "wm_yr_wk", "sell_price"])
