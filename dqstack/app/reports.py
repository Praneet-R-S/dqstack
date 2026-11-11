"""The report set of section 14 (build step 11), each exportable to Excel and PDF.

Every report returns the same shape -- a title, columns, rows, totals and notes --
so one renderer serves them all. Quantities are thousandths and money paise in the
rows; the exports show units and rupees.

    sales                 by day and store: units, revenue
    sales_by_category     revenue and units per category
    best_worst            best and worst sellers by revenue
    stock_value           value at cost by store and category
    stock_turn            stock turn and days of cover over the period, by store and category
    slow_dead_stock       stock with no sale in 60 days (dead) or over 90 days of cover (slow)
    shrinkage             count losses and gains, at cost
    wastage               write-offs by reason, at cost, with expiry losses in their own column
    count_compliance      cycle counts done against those due, by store and ABC class
    forecast_accuracy     last night's forecasts scored against sales, by day and category
    supplier_performance  order performance: fill rate, on-time delivery, damaged share,
                          orders made by hand, emergency orders
    lost_sales            units the forecast says were wanted on out-of-stock days, at margin
    profit_comparison     stock targets from each product's costs vs one 95% service level
    accounting            sales, GST and purchases by day, for the accountant

Periods are days in the company's time zone; a moment is compared in UTC, the way
every time is stored.
"""

from __future__ import annotations

import io
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from . import rules as rules_mod
from . import stock as stock_mod
from .models import (Bill, Company, CountLine, CountSession, DailySales, Delivery, DeliveryLine,
                     ForecastScore, Product, PurchaseOrder, PurchaseOrderLine, StockMovement,
                     StockOnHand, Store, Supplier)
from .security import _aware

# column: (key, label, kind) with kind in text | qty | money | pct | num | date
Report = dict


def _out(title: str, columns: list, rows: list, totals: dict | None = None,
         notes: list | None = None) -> Report:
    return {"title": title, "columns": [{"key": k, "label": l, "kind": t} for k, l, t in columns],
            "rows": rows, "totals": totals or {}, "notes": notes or []}


def _stores(db: Session, store_ids: list[str]) -> dict[str, Store]:
    return {s.id: s for s in db.scalars(select(Store).where(Store.id.in_(store_ids)))}


def _bounds(db: Session, cid: str, start: date, end: date):
    """The company's time zone, and the period's first and after-last moments in UTC.
    Times are stored in UTC; SQLite drops a time's offset, so a bound in local time
    would be compared 5 hours 30 minutes out."""
    from .sales import tz_of
    tz = tz_of(db, cid)
    lo = datetime.combine(start, time(0, 0), tzinfo=tz).astimezone(timezone.utc)
    hi = datetime.combine(end + timedelta(days=1), time(0, 0), tzinfo=tz).astimezone(timezone.utc)
    return tz, lo, hi


def _ratio(a: int | float, b: int | float, places: int = 4) -> float | None:
    return round(a / b, places) if b else None


def sales(db, cid, store_ids, start, end) -> Report:
    st = _stores(db, store_ids)
    rows = []
    for d, sid, u, r in db.execute(
            select(DailySales.day, DailySales.store_id, func.sum(DailySales.units_milli),
                   func.sum(DailySales.revenue_paise))
            .where(DailySales.company_id == cid, DailySales.store_id.in_(store_ids),
                   DailySales.day >= start, DailySales.day <= end)
            .group_by(DailySales.day, DailySales.store_id)
            .order_by(DailySales.day, DailySales.store_id)):
        rows.append({"day": d.isoformat(), "store": st[sid].name, "units_milli": int(u or 0),
                     "revenue_paise": int(r or 0)})
    return _out("Sales by day and store", [("day", "Day", "date"), ("store", "Store", "text"),
                                           ("units_milli", "Units", "qty"),
                                           ("revenue_paise", "Revenue", "money")], rows,
                {"units_milli": sum(r["units_milli"] for r in rows),
                 "revenue_paise": sum(r["revenue_paise"] for r in rows)})


def sales_by_category(db, cid, store_ids, start, end) -> Report:
    rows = []
    for cat, u, r in db.execute(
            select(Product.category, func.sum(DailySales.units_milli),
                   func.sum(DailySales.revenue_paise))
            .join(Product, Product.id == DailySales.product_id)
            .where(DailySales.company_id == cid, DailySales.store_id.in_(store_ids),
                   DailySales.day >= start, DailySales.day <= end)
            .group_by(Product.category)):
        rows.append({"category": cat or "Uncategorised", "units_milli": int(u or 0),
                     "revenue_paise": int(r or 0)})
    rows.sort(key=lambda r: -r["revenue_paise"])
    total = sum(r["revenue_paise"] for r in rows) or 1
    for r in rows:
        r["share"] = round(r["revenue_paise"] / total, 4)
    return _out("Sales by category", [("category", "Category", "text"),
                                      ("units_milli", "Units", "qty"),
                                      ("revenue_paise", "Revenue", "money"),
                                      ("share", "Share", "pct")], rows,
                {"revenue_paise": sum(r["revenue_paise"] for r in rows)})


def best_worst(db, cid, store_ids, start, end, n: int = 10) -> Report:
    totals = {pid: (int(u or 0), int(r or 0)) for pid, u, r in db.execute(
        select(DailySales.product_id, func.sum(DailySales.units_milli),
               func.sum(DailySales.revenue_paise))
        .where(DailySales.company_id == cid, DailySales.store_id.in_(store_ids),
               DailySales.day >= start, DailySales.day <= end)
        .group_by(DailySales.product_id))}
    names = {p.id: p for p in db.scalars(select(Product).where(
        Product.company_id == cid, Product.status != "discontinued"))}
    ranked = sorted(((totals.get(pid, (0, 0)), p) for pid, p in names.items()),
                    key=lambda t: -t[0][1])
    rows = [{"rank": "best", "sku": p.sku, "name": p.name, "units_milli": u, "revenue_paise": r}
            for (u, r), p in ranked[:n]]
    rows += [{"rank": "worst", "sku": p.sku, "name": p.name, "units_milli": u,
              "revenue_paise": r} for (u, r), p in ranked[-n:][::-1] if len(ranked) > n]
    return _out("Best and worst sellers", [("rank", "", "text"), ("sku", "SKU", "text"),
                                           ("name", "Product", "text"),
                                           ("units_milli", "Units", "qty"),
                                           ("revenue_paise", "Revenue", "money")], rows)


def stock_value(db, cid, store_ids, start, end) -> Report:
    v = stock_mod.valuation(db, cid, store_ids)
    st = _stores(db, store_ids)
    rows = []
    for sid, s in v["stores"].items():
        for cat, c in sorted(s["categories"].items()):
            rows.append({"store": st[sid].name, "category": cat, "units_milli": c["units_milli"],
                         "value_paise": c["value_paise"]})
    notes = []
    if v["products_without_cost"]:
        notes.append(f"{v['products_without_cost']} products have no cost and are not valued.")
    return _out("Stock value at cost", [("store", "Store", "text"), ("category", "Category", "text"),
                                        ("units_milli", "Units", "qty"),
                                        ("value_paise", "Value at cost", "money")], rows,
                {"value_paise": v["total_paise"]}, notes)


def stock_turn(db, cid, store_ids, start, end) -> Report:
    """Stock turn and days of cover over the period, by store and category.

    Stock at the start and at the end of the period is rebuilt from the ledger (on hand
    now, less everything that moved since) and valued at each product's current average
    cost, as Stock value is; negative stock counts as none. Cost of goods sold is what
    sales took out, at the cost each sale was booked at, less customer returns.

        stock turn     = cost of goods sold / ((stock at cost, start + end) / 2)
        turns a year   = stock turn x 365 / days in the period
        days of cover  = stock at cost, end / (cost of goods sold / days in the period)

    Days of cover is "on hand / average daily sales" counted in rupees at cost rather than
    units, so that a category of kilos and pieces adds up; for one product at one cost the
    two are the same. A separate report from Stock value, which is a snapshot of now: these
    figures belong to a period."""
    _, lo, hi = _bounds(db, cid, start, end)
    st = _stores(db, store_ids)
    days = (end - start).days + 1
    prods = {p.id: p for p in db.scalars(select(Product).where(Product.company_id == cid))}
    soh = {(r.store_id, r.product_id): r for r in db.scalars(select(StockOnHand).where(
        StockOnHand.company_id == cid, StockOnHand.store_id.in_(store_ids)))}
    in_stores = (StockMovement.company_id == cid, StockMovement.store_id.in_(store_ids))
    by_pair = (StockMovement.store_id, StockMovement.product_id)

    def moved_since(t: datetime) -> dict:
        return {(s, p): int(q or 0) for s, p, q in db.execute(
            select(*by_pair, func.sum(StockMovement.qty_milli))
            .where(*in_stores, StockMovement.at >= t).group_by(*by_pair))}
    after_start, after_end = moved_since(lo), moved_since(hi)
    sold: dict = {}
    for s, p, q, at_cost, uncosted in db.execute(
            select(*by_pair, func.sum(StockMovement.qty_milli),
                   func.sum(StockMovement.qty_milli * func.coalesce(StockMovement.unit_cost_paise, 0)),
                   func.sum(case((StockMovement.unit_cost_paise.is_(None), StockMovement.qty_milli),
                                 else_=0)))
            .where(*in_stores, StockMovement.type.in_(("sale", "customer_return")),
                   StockMovement.at >= lo, StockMovement.at < hi).group_by(*by_pair)):
        sold[(s, p)] = (-int(q or 0), -int(at_cost or 0), -int(uncosted or 0))
    # store, category -> on hand at end, value at start, value at end, units sold, cost of sales
    agg: dict = defaultdict(lambda: [0, 0, 0, 0, 0])
    no_cost = 0
    for key in set(soh) | set(sold) | set(after_start):
        sid, pid = key
        p = prods.get(pid)
        if p is None or sid not in st:
            continue
        row = soh.get(key)
        now_milli = row.on_hand_milli if row else 0
        cost = row.avg_cost_paise if row and row.avg_cost_paise is not None else p.cost_paise
        opening = max(now_milli - after_start.get(key, 0), 0)
        closing = max(now_milli - after_end.get(key, 0), 0)
        units, at_cost, uncosted = sold.get(key, (0, 0, 0))
        if cost is None and (opening or closing):
            no_cost += 1
        a = agg[(st[sid].name, p.category or "Uncategorised")]
        a[0] += closing
        a[1] += opening * (cost or 0) // 1000
        a[2] += closing * (cost or 0) // 1000
        a[3] += units
        a[4] += (at_cost + uncosted * (cost or 0)) // 1000

    def figures(v_start: int, v_end: int, cogs: int) -> dict:
        avg = (v_start + v_end) / 2
        turn = cogs / avg if avg > 0 else None
        return {"turn": round(turn, 2) if turn is not None else None,
                "turn_year": round(turn * 365 / days, 1) if turn is not None else None,
                "days_of_cover": round(v_end * days / cogs, 1) if cogs > 0 else None}
    rows = []
    for (s, c), (oh, v0, v1, u, cogs) in sorted(agg.items()):
        if not (oh or v0 or v1 or u or cogs):
            continue
        rows.append({"store": s, "category": c, "on_hand_milli": oh, "start_value_paise": v0,
                     "end_value_paise": v1, "sold_milli": u, "cogs_paise": cogs,
                     **figures(v0, v1, cogs)})
    t0 = sum(r["start_value_paise"] for r in rows)
    t1 = sum(r["end_value_paise"] for r in rows)
    tc = sum(r["cogs_paise"] for r in rows)
    notes = [f"Stock turn: cost of goods sold over the {days} days divided by the average of "
             "the stock at cost at the start and the end. Turns a year scales it to 365 days.",
             "Days of cover: stock at cost at the end divided by the cost of goods sold per "
             "day over the period, so how long the stock lasts at the period's rate of sale. "
             "Blank when nothing sold.",
             "Stock at the start and end is rebuilt from the stock movements and valued at "
             "today's average cost; stock below zero counts as none."]
    if no_cost:
        notes.append(f"{no_cost} products have no cost and are not valued.")
    return _out("Stock turn and days of cover", [
        ("store", "Store", "text"), ("category", "Category", "text"),
        ("on_hand_milli", "On hand at end", "qty"),
        ("start_value_paise", "Stock at cost, start", "money"),
        ("end_value_paise", "Stock at cost, end", "money"),
        ("sold_milli", "Units sold", "qty"), ("cogs_paise", "Cost of goods sold", "money"),
        ("turn", "Stock turn", "num"), ("turn_year", "Turns a year", "num"),
        ("days_of_cover", "Days of cover", "num")], rows,
        {"start_value_paise": t0, "end_value_paise": t1, "cogs_paise": tc,
         "sold_milli": sum(r["sold_milli"] for r in rows), **figures(t0, t1, tc)}, notes)


def slow_dead_stock(db, cid, store_ids, start, end) -> Report:
    r = rules_mod.effective(db.get(Company, cid).rules)
    dead_days = int(r["dead_stock_days"])
    rows = []
    for sid, s in _stores(db, store_ids).items():
        for row in stock_mod.stock_rows(db, cid, s, include_discontinued=True, today=end):
            if row["on_hand_milli"] <= 0:
                continue
            soh = db.get(StockOnHand, {"store_id": sid, "product_id": row["product_id"]})
            last = soh.last_sale_at.date() if soh and soh.last_sale_at else None
            idle = (end - last).days if last else None
            if idle is None or idle >= dead_days:
                kind = "dead"
            elif row["days_of_cover"] is not None and row["days_of_cover"] > 90:
                kind = "slow"
            else:
                continue
            rows.append({"store": s.name, "sku": row["sku"], "name": row["name"], "kind": kind,
                         "on_hand_milli": row["on_hand_milli"],
                         "last_sale": last.isoformat() if last else "never",
                         "days_of_cover": row["days_of_cover"], "value_paise": row["value_paise"]})
    rows.sort(key=lambda x: -x["value_paise"])
    return _out("Slow and dead stock", [("store", "Store", "text"), ("sku", "SKU", "text"),
                                        ("name", "Product", "text"), ("kind", "", "text"),
                                        ("on_hand_milli", "On hand", "qty"),
                                        ("last_sale", "Last sale", "date"),
                                        ("days_of_cover", "Days of cover", "num"),
                                        ("value_paise", "Value at cost", "money")], rows,
                {"value_paise": sum(r["value_paise"] for r in rows)},
                [f"Dead: no sale in {dead_days} days. Slow: more than 90 days of cover."])


def _movements(db, cid, store_ids, start, end, types):
    _, lo, hi = _bounds(db, cid, start, end)
    return db.execute(select(StockMovement, Product).join(
        Product, Product.id == StockMovement.product_id).where(
        StockMovement.company_id == cid, StockMovement.store_id.in_(store_ids),
        StockMovement.type.in_(types), StockMovement.at >= lo, StockMovement.at < hi))


def shrinkage(db, cid, store_ids, start, end) -> Report:
    st = _stores(db, store_ids)
    agg: dict = defaultdict(lambda: [0, 0, 0, 0])      # loss units, loss value, gain units, gain value
    for m, p in _movements(db, cid, store_ids, start, end, ("count_adjustment",)):
        v = agg[(st[m.store_id].name, p.category or "Uncategorised")]
        value = abs(m.qty_milli) * (m.unit_cost_paise or 0) // 1000
        if m.qty_milli < 0:
            v[0] += -m.qty_milli
            v[1] += value
        else:
            v[2] += m.qty_milli
            v[3] += value
    rows = [{"store": s, "category": c, "lost_milli": a, "lost_paise": b, "found_milli": g,
             "found_paise": h, "net_paise": h - b} for (s, c), (a, b, g, h) in sorted(agg.items())]
    return _out("Shrinkage (count differences)", [
        ("store", "Store", "text"), ("category", "Category", "text"),
        ("lost_milli", "Units lost", "qty"), ("lost_paise", "Lost at cost", "money"),
        ("found_milli", "Units found", "qty"), ("found_paise", "Found at cost", "money"),
        ("net_paise", "Net", "money")], rows,
        {"lost_paise": sum(r["lost_paise"] for r in rows),
         "net_paise": sum(r["net_paise"] for r in rows)})


def wastage(db, cid, store_ids, start, end) -> Report:
    st = _stores(db, store_ids)
    agg: dict = defaultdict(lambda: [0, 0])
    for m, p in _movements(db, cid, store_ids, start, end, ("wastage", "damage")):
        reason = (m.reason or m.type).split(":")[0].strip().lower()
        if m.type == "damage" and not reason.startswith("returned damaged"):
            reason = "damaged"
        elif reason.startswith("returned damaged"):
            reason = "returned damaged"
        v = agg[(st[m.store_id].name, reason)]
        v[0] += -m.qty_milli
        v[1] += -m.qty_milli * (m.unit_cost_paise or 0) // 1000
    rows = [{"store": s, "reason": r, "units_milli": u, "value_paise": v,
             "expiry_paise": v if r == "expired" else 0}
            for (s, r), (u, v) in sorted(agg.items())]
    return _out("Wastage, damage and expiry", [("store", "Store", "text"),
                                               ("reason", "Reason", "text"),
                                               ("units_milli", "Units", "qty"),
                                               ("value_paise", "At cost", "money"),
                                               ("expiry_paise", "Of which expired", "money")],
                rows,
                {"value_paise": sum(r["value_paise"] for r in rows),
                 "expiry_paise": sum(r["expiry_paise"] for r in rows)},
                ["Expiry losses are the stock written off with the reason expired. Stock "
                 "refused at delivery because it had expired is not a loss: the supplier "
                 "credits it (Order performance counts it as damaged)."])


# A count, for compliance: any count line except one sent back for a recount (rejected)
# or replaced by a second person's count of the same product (superseded). Lines still
# waiting for a manager were counted all the same.
COUNTED = ("posted", "confirmed", "waiting", "conflict")


def count_schedule(db, cid, store_ids, start, end) -> dict:
    """Cycle counts due and done in the period, per store and ABC class.

    For each product the Stock take list would show (every product not discontinued):
    its first due date is its last count before the period plus its class's interval
    (A 7, B 28, C 91 days by default; business rules), or the period's first day if it
    was never counted, and never earlier than the first day. Counts due: one on that
    date and one every interval after it, up to the period's last day. Counts done: the
    counts made in the period (one per count session), at most the number due -- an
    early count still counts, extra counts do not. Returns {(store_id, class): {...}}."""
    from .floor import abc_classes
    tz, lo, hi = _bounds(db, cid, start, end)
    intervals = rules_mod.effective(db.get(Company, cid).rules)["count_interval_days"]
    products = list(db.scalars(select(Product).where(Product.company_id == cid,
                                                     Product.status != "discontinued")))
    base = (select(CountSession.store_id, CountLine.product_id).select_from(CountLine)
            .join(CountSession, CountSession.id == CountLine.session_id)
            .where(CountLine.company_id == cid, CountSession.store_id.in_(store_ids),
                   CountLine.status.in_(COUNTED)))
    last = {(s, p): _aware(t) for s, p, t in db.execute(
        base.add_columns(func.max(CountLine.counted_at)).where(CountLine.counted_at < lo)
        .group_by(CountSession.store_id, CountLine.product_id))}
    made = {(s, p): int(n) for s, p, n in db.execute(
        base.add_columns(func.count(func.distinct(CountLine.session_id)))
        .where(CountLine.counted_at >= lo, CountLine.counted_at < hi)
        .group_by(CountSession.store_id, CountLine.product_id))}
    out: dict = {}
    for sid in store_ids:
        computed = abc_classes(db, cid, sid)
        for p in products:
            cls = p.abc_class or computed.get(p.id, "C")
            interval = max(int(intervals.get(cls, 91)), 1)
            before = last.get((sid, p.id))
            first = before.astimezone(tz).date() + timedelta(days=interval) if before else start
            first = max(first, start)
            due = 1 + (end - first).days // interval if first <= end else 0
            n = made.get((sid, p.id), 0)
            v = out.setdefault((sid, cls), {"products": 0, "due": 0, "done": 0, "made": 0})
            v["products"] += 1
            v["due"] += due
            v["done"] += min(n, due)
            v["made"] += n
    return out


def count_compliance(db, cid, store_ids, start, end) -> Report:
    st = _stores(db, store_ids)
    sched = count_schedule(db, cid, store_ids, start, end)
    rows = [{"store": st[sid].name, "abc_class": cls, "products": v["products"],
             "counts_due": v["due"], "counts_done": v["done"],
             "compliance": _ratio(v["done"], v["due"]), "counts_made": v["made"]}
            for (sid, cls), v in sorted(sched.items(), key=lambda kv: (st[kv[0][0]].name, kv[0][1]))]
    due = sum(r["counts_due"] for r in rows)
    done = sum(r["counts_done"] for r in rows)
    return _out("Cycle count compliance", [
        ("store", "Store", "text"), ("abc_class", "Class", "text"),
        ("products", "Products", "num"), ("counts_due", "Counts due", "num"),
        ("counts_done", "Counts done", "num"), ("compliance", "Done", "pct"),
        ("counts_made", "Counts made", "num")], rows,
        {"counts_due": due, "counts_done": done, "compliance": _ratio(done, due),
         "counts_made": sum(r["counts_made"] for r in rows)},
        ["Counts due: each product's count date from its last count and its class's "
         "interval (business rules), then one every interval to the end of the period; a "
         "product never counted is due from the first day. Counts done: counts made in the "
         "period, up to the number due. A count sent back for a recount does not count."])


def forecast_accuracy(db, cid, store_ids, start, end) -> Report:
    agg: dict = defaultdict(lambda: [0, 0, 0, 0, 0])
    for s in db.scalars(select(ForecastScore).where(
            ForecastScore.company_id == cid, ForecastScore.store_id.in_(store_ids),
            ForecastScore.day >= start, ForecastScore.day <= end)):
        v = agg[(s.day.isoformat(), s.category)]
        v[0] += s.n
        v[1] += s.actual_milli
        v[2] += s.forecast_milli
        v[3] += s.abs_error_milli
        v[4] += s.inside_range
    rows = []
    for (d, cat), (n, a, f, e, inside) in sorted(agg.items()):
        rows.append({"day": d, "category": cat, "actual_milli": a, "forecast_milli": f,
                     "error": round(e / a, 4) if a else None,
                     "bias": round((f - a) / a, 4) if a else None,
                     "inside_range": round(inside / n, 4) if n else None})
    A = sum(r["actual_milli"] for r in rows)
    E = sum(v[3] for v in agg.values())
    return _out("Forecast accuracy and bias", [
        ("day", "Day", "date"), ("category", "Category", "text"),
        ("actual_milli", "Sold", "qty"), ("forecast_milli", "Forecast", "qty"),
        ("error", "Error (WAPE)", "pct"), ("bias", "Bias", "pct"),
        ("inside_range", "Inside the range", "pct")], rows,
        {"error": round(E / A, 4) if A else None},
        ["Error is total absolute error over total sales; the range is 5% to 95%."])


def accuracy_by_store(db, cid, store_ids, start, end) -> dict:
    """{store_id or None for all: {"actual_milli", "forecast_milli", "error", "bias"}}
    over the period, with the same definitions as Forecast accuracy: error is total
    absolute error over total sales (WAPE), bias is (forecast - sales) / sales."""
    sums: dict = defaultdict(lambda: [0, 0, 0])
    for sid, a, f, e in db.execute(
            select(ForecastScore.store_id, func.sum(ForecastScore.actual_milli),
                   func.sum(ForecastScore.forecast_milli), func.sum(ForecastScore.abs_error_milli))
            .where(ForecastScore.company_id == cid, ForecastScore.store_id.in_(store_ids),
                   ForecastScore.day >= start, ForecastScore.day <= end)
            .group_by(ForecastScore.store_id)):
        for k in (sid, None):
            sums[k][0] += int(a or 0)
            sums[k][1] += int(f or 0)
            sums[k][2] += int(e or 0)
    return {k: {"actual_milli": a, "forecast_milli": f, "error": _ratio(e, a),
                "bias": _ratio(f - a, a)} for k, (a, f, e) in sums.items()}


def emergency_reason(po: PurchaseOrder, supplier: Supplier | None, tz) -> str | None:
    """Why an approved order was an emergency order, or None.

    An emergency order is outside the supplier's normal rhythm: approved on a day of the
    week that is not one of the supplier's order days (when the supplier has order days),
    or asking for delivery sooner than the supplier's lead time (expected date minus the
    day it was approved, in days, less than the lead time). Who made the order does not
    matter: a suggested order approved off-cycle is an emergency too, and an order made
    by hand on an order day with the usual lead time is not. An order without a supplier
    has no rhythm to break and is never counted."""
    if supplier is None or po.approved_at is None:
        return None
    approved = _aware(po.approved_at).astimezone(tz).date()
    if supplier.order_days and approved.weekday() not in supplier.order_days:
        return "approved on a day the supplier does not take orders"
    if po.expected_date is not None and \
            (po.expected_date - approved).days < int(supplier.lead_time_days or 0):
        return "wanted sooner than the supplier's lead time"
    return None


def supplier_performance(db, cid, store_ids, start, end) -> Report:
    """Order performance, by supplier, for orders approved in the period."""
    from .db import utcnow
    tz, lo, hi = _bounds(db, cid, start, end)
    today = utcnow().astimezone(tz).date()
    sups = {s.id: s for s in db.scalars(select(Supplier).where(Supplier.company_id == cid))}
    agg: dict = defaultdict(lambda: {"orders": 0, "ordered": 0, "received": 0, "damaged": 0,
                                     "on_time": 0, "with_date": 0, "unplanned": 0,
                                     "emergency": 0})
    when = func.coalesce(PurchaseOrder.approved_at, PurchaseOrder.created_at)
    for po in db.scalars(select(PurchaseOrder).where(
            PurchaseOrder.company_id == cid, PurchaseOrder.store_id.in_(store_ids),
            PurchaseOrder.status.not_in(("draft", "cancelled")), when >= lo, when < hi)):
        sup = sups.get(po.supplier_id)
        a = agg[sup.name if sup else "No supplier"]
        a["orders"] += 1
        a["unplanned"] += int(po.origin != "suggested")
        a["emergency"] += int(emergency_reason(po, sup, tz) is not None)
        deliveries = list(db.scalars(select(Delivery).where(Delivery.po_id == po.id)
                                     .order_by(Delivery.created_at)))
        if po.status in ("partly_received", "received"):
            a["ordered"] += sum(l.qty_milli for l in po.lines)
            a["received"] += sum(min(l.received_milli, l.qty_milli) for l in po.lines)
        a["damaged"] += sum(dl.damaged_milli for d in deliveries for dl in d.lines)
        if po.expected_date and deliveries:
            first = _aware(deliveries[0].created_at).astimezone(tz).date()
            a["with_date"] += 1
            a["on_time"] += int(first <= po.expected_date)
        elif po.expected_date and po.expected_date < today:
            a["with_date"] += 1                     # past its date and nothing has come: late

    def figures(v: dict) -> dict:
        return {"orders": v["orders"], "fill_rate": _ratio(v["received"], v["ordered"]),
                "on_time": _ratio(v["on_time"], v["with_date"]),
                "damaged_share": _ratio(v["damaged"], v["received"] + v["damaged"]),
                "unplanned_orders": v["unplanned"], "emergency_orders": v["emergency"]}
    rows = [{"supplier": k, **figures(v)} for k, v in sorted(agg.items())]
    total = {k: sum(v[k] for v in agg.values()) for k in
             ("orders", "ordered", "received", "damaged", "on_time", "with_date", "unplanned",
              "emergency")}
    return _out("Order performance by supplier", [
        ("supplier", "Supplier", "text"), ("orders", "Orders", "num"),
        ("fill_rate", "Fill rate", "pct"), ("on_time", "On time", "pct"),
        ("damaged_share", "Damaged", "pct"), ("unplanned_orders", "Made by hand", "num"),
        ("emergency_orders", "Emergency orders", "num")], rows, figures(total),
        ["Orders: approved in the period and not cancelled.",
         "Fill rate: good units received over units ordered, on orders that have arrived "
         "(in full or in part).",
         "On time: the first delivery came on or before the expected date. Orders with an "
         "expected date that has passed with nothing delivered count as late; orders with "
         "no expected date are not judged.",
         "Damaged: units refused at delivery (damaged, wrong, expired) over all units that "
         "arrived.",
         "Made by hand: orders not made from the forecast's suggestions.",
         "Emergency orders: approved on a day that is not one of the supplier's order "
         "days, or with an expected date sooner after approval than the supplier's lead "
         "time. Orders without a supplier are never counted as emergencies."])


def lost_sales(db, cid, store_ids, start, end) -> Report:
    """Units wanted on out-of-stock days, from each product's selling rate on the days
    it was on the shelf in the four weeks before, valued at margin."""
    st = _stores(db, store_ids)
    prods = {p.id: p for p in db.scalars(select(Product).where(Product.company_id == cid))}
    rows = []
    oos = list(db.scalars(select(DailySales).where(
        DailySales.company_id == cid, DailySales.store_id.in_(store_ids),
        DailySales.day >= start, DailySales.day <= end, DailySales.out_of_stock.is_(True))))
    agg: dict = defaultdict(lambda: [0, 0, 0])
    for r in oos:
        rate_rows = db.execute(select(func.sum(DailySales.units_milli), func.count()).where(
            DailySales.store_id == r.store_id, DailySales.product_id == r.product_id,
            DailySales.day >= r.day - timedelta(days=28), DailySales.day < r.day,
            DailySales.out_of_stock.is_(False))).one()
        units, days = int(rate_rows[0] or 0), int(rate_rows[1] or 0)
        rate = units // days if days else 0
        p = prods[r.product_id]
        margin = max((p.price_paise or 0) - (p.cost_paise or 0), 0)
        v = agg[(r.store_id, r.product_id)]
        v[0] += 1
        v[1] += rate
        v[2] += rate * margin // 1000
    for (sid, pid), (n, u, m) in agg.items():
        rows.append({"store": st[sid].name, "sku": prods[pid].sku, "name": prods[pid].name,
                     "days_out": n, "units_milli": u, "margin_paise": m})
    rows.sort(key=lambda x: -x["margin_paise"])
    return _out("Lost sales estimate", [("store", "Store", "text"), ("sku", "SKU", "text"),
                                        ("name", "Product", "text"), ("days_out", "Days out", "num"),
                                        ("units_milli", "Units wanted", "qty"),
                                        ("margin_paise", "Margin lost", "money")], rows,
                {"margin_paise": sum(r["margin_paise"] for r in rows)},
                ["Only days the ledger saw an empty shelf are counted."])


def profit_comparison(db, cid, store_ids, start, end) -> Report:
    from .forecast.jobs import active_version
    mv = active_version(db, cid)
    rows, notes = [], []
    pv = (mv.backtest or {}).get("profit_vs_blanket") if mv else None
    if pv:
        rows = [{"plan": "Stock targets from each product's costs", "margin": pv["targets"]},
                {"plan": "One 95% service level for everything", "margin": pv["blanket_95"]}]
        notes.append("Simulated margin in rupees over the backtest's evaluation weeks, judged "
                     "against what actually sold.")
    else:
        notes.append("The comparison comes with the first full training (about 10 months of "
                     "sales). Early forecasts do not have it yet.")
    return _out("Profit comparison", [("plan", "Plan", "text"), ("margin", "Margin (Rs)", "num")],
                rows, notes=notes)


def accounting(db, cid, store_ids, start, end) -> Report:
    days: dict = defaultdict(lambda: {"sales_paise": 0, "gst_paise": 0, "purchases_paise": 0})
    for d, total, tax in db.execute(select(Bill.business_day, func.sum(Bill.total_paise),
                                           func.sum(Bill.tax_paise)).where(
            Bill.company_id == cid, Bill.store_id.in_(store_ids),
            Bill.state.in_(("paid", "refund")), Bill.business_day >= start,
            Bill.business_day <= end).group_by(Bill.business_day)):
        days[d.isoformat()]["sales_paise"] += int(total or 0)
        days[d.isoformat()]["gst_paise"] += int(tax or 0)
    for d, rev in db.execute(select(DailySales.day, func.sum(DailySales.revenue_paise)).where(
            DailySales.company_id == cid, DailySales.store_id.in_(store_ids),
            DailySales.source.in_(("upload", "entry")), DailySales.day >= start,
            DailySales.day <= end).group_by(DailySales.day)):
        days[d.isoformat()]["sales_paise"] += int(rev or 0)
    for d in db.scalars(select(Delivery).where(Delivery.company_id == cid,
                                               Delivery.store_id.in_(store_ids))):
        day = d.created_at.date()
        if start <= day <= end:
            days[day.isoformat()]["purchases_paise"] += sum(
                l.received_milli * (l.unit_cost_paise or 0) // 1000 for l in d.lines)
    rows = [{"day": k, **v} for k, v in sorted(days.items())]
    return _out("Sales and purchases by day", [("day", "Day", "date"),
                                               ("sales_paise", "Sales incl. GST", "money"),
                                               ("gst_paise", "GST in till sales", "money"),
                                               ("purchases_paise", "Purchases excl. GST", "money")],
                rows, {"sales_paise": sum(r["sales_paise"] for r in rows),
                       "purchases_paise": sum(r["purchases_paise"] for r in rows)},
                ["GST is shown for bills made in Checkout; uploaded sales carry no tax detail."])


REPORTS = {"sales": sales, "sales_by_category": sales_by_category, "best_worst": best_worst,
           "stock_value": stock_value, "stock_turn": stock_turn,
           "slow_dead_stock": slow_dead_stock, "shrinkage": shrinkage, "wastage": wastage,
           "count_compliance": count_compliance, "forecast_accuracy": forecast_accuracy,
           "supplier_performance": supplier_performance, "lost_sales": lost_sales,
           "profit_comparison": profit_comparison, "accounting": accounting}


# ===================================================================== exports

def _cell(v, kind):
    if v is None:
        return None
    if kind == "qty":
        return v / 1000
    if kind == "money":
        return round(v / 100, 2)
    return v


def _sheet_name(title: str, used: set[str]) -> str:
    """Excel sheet names: at most 31 characters, none of the characters Excel forbids,
    and unique."""
    base = "".join(" " if ch in "[]:*?/" + chr(92) else ch for ch in title).strip()[:31] or "Report"
    name, n = base, 2
    while name.lower() in used:
        suffix = f" ({n})"
        name, n = base[:31 - len(suffix)] + suffix, n + 1
    used.add(name.lower())
    return name


def _fill_sheet(ws, rep: Report, heading: str | None = None) -> None:
    from openpyxl.styles import Font
    first = 1
    if heading:
        ws.append([heading])
        ws[1][0].font = Font(bold=True, size=12)
        ws.append([])
        first = 3
    ws.append([c["label"] or "" for c in rep["columns"]])
    for cell in ws[first]:
        cell.font = Font(bold=True)
    for r in rep["rows"]:
        ws.append([_cell(r.get(c["key"]), c["kind"]) for c in rep["columns"]])
    if rep["totals"]:
        ws.append([("Total" if i == 0 else _cell(rep["totals"].get(c["key"]), c["kind"]))
                   for i, c in enumerate(rep["columns"])])
        for cell in ws[ws.max_row]:
            cell.font = Font(bold=True)
    for i, c in enumerate(rep["columns"], start=1):
        ws.column_dimensions[ws.cell(first, i).column_letter].width = max(12, len(c["label"] or "") + 4)
        fmt = {"money": "#,##,##0.00", "qty": "#,##0.###", "pct": "0.0%"}.get(c["kind"])
        if fmt:
            for row in ws.iter_rows(min_row=first + 1, min_col=i, max_col=i):
                row[0].number_format = fmt
    ws.freeze_panes = ws.cell(first + 1, 1)
    for n in rep["notes"]:
        ws.append([])
        ws.append([n])


def to_xlsx(rep: Report) -> bytes:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = _sheet_name(rep["title"], set())
    _fill_sheet(ws, rep)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def to_xlsx_many(reps: list[Report], period: str) -> bytes:
    """Every report in one workbook, a sheet each, with a contents sheet first."""
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    toc = wb.active
    toc.title = "Contents"
    toc.append(["DQ-Stack reports"])
    toc[1][0].font = Font(bold=True, size=14)
    toc.append([period])
    toc.append([])
    toc.append(["Report", "Rows"])
    for cell in toc[4]:
        cell.font = Font(bold=True)
    toc.column_dimensions["A"].width = 36
    used = {"contents"}
    for rep in reps:
        name = _sheet_name(rep["title"], used)
        _fill_sheet(wb.create_sheet(name), rep, heading=f"{rep['title']} · {period}")
        toc.append([rep["title"], len(rep["rows"])])
        toc.cell(toc.max_row, 1).hyperlink = f"#'{name}'!A1"
        toc.cell(toc.max_row, 1).style = "Hyperlink"
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def to_csv(rep: Report) -> bytes:
    """Plain CSV in rupees and units, UTF-8 with a byte-order mark so Excel opens the
    rupee sign and Indian names correctly."""
    import csv
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([c["label"] or "" for c in rep["columns"]])
    for r in rep["rows"]:
        w.writerow(["" if (v := _cell(r.get(c["key"]), c["kind"])) is None else v
                    for c in rep["columns"]])
    if rep["totals"]:
        w.writerow([("Total" if i == 0 else ("" if (v := _cell(rep["totals"].get(c["key"]),
                                                                 c["kind"])) is None else v))
                    for i, c in enumerate(rep["columns"])])
    return buf.getvalue().encode("utf-8-sig")


def to_pdf(rep: Report, subtitle: str = "") -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    from .formats import group_indian
    st = getSampleStyleSheet()

    def show(v, kind):
        if v is None:
            return ""
        if kind == "money":
            whole, frac = divmod(abs(int(v)), 100)
            return ("-" if v < 0 else "") + f"Rs. {group_indian(whole)}.{frac:02d}"
        if kind == "qty":
            return f"{v / 1000:,.3f}".rstrip("0").rstrip(".")
        if kind == "pct":
            return f"{100 * v:.1f}%"
        return str(v)
    data = [[c["label"] for c in rep["columns"]]]
    for r in rep["rows"][:2000]:
        data.append([show(r.get(c["key"]), c["kind"]) for c in rep["columns"]])
    if rep["totals"]:
        data.append([("Total" if i == 0 else show(rep["totals"].get(c["key"]), c["kind"]))
                     for i, c in enumerate(rep["columns"])])
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm,
                            topMargin=12 * mm, bottomMargin=12 * mm, title=rep["title"])
    t = Table(data, repeatRows=1)
    t.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                           ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EDE9FE")),
                           ("FONTSIZE", (0, 0), (-1, -1), 8),
                           ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.HexColor("#6D28D9"))]))
    parts = [Paragraph(rep["title"], st["Title"])]
    if subtitle:
        parts.append(Paragraph(subtitle, st["Normal"]))
    parts += [Spacer(1, 4 * mm), t] + [Paragraph(n, st["Normal"]) for n in rep["notes"]]
    doc.build(parts)
    return buf.getvalue()
