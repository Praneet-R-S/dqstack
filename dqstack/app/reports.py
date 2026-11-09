"""The report set of section 14 (build step 11), each exportable to Excel and PDF.

Every report returns the same shape -- a title, columns, rows, totals and notes --
so one renderer serves them all. Quantities are thousandths and money paise in the
rows; the exports show units and rupees.

    sales                 by day and store: units, revenue
    sales_by_category     revenue and units per category
    best_worst            best and worst sellers by revenue
    stock_value           value at cost by store and category
    slow_dead_stock       stock with no sale in 60 days (dead) or over 90 days of cover (slow)
    shrinkage             count losses and gains, at cost
    wastage               write-offs by reason, at cost (expiry losses are the "expired" rows)
    forecast_accuracy     last night's forecasts scored against sales, by day and category
    supplier_performance  fill rate, on-time delivery, damaged share, unplanned orders
    lost_sales            units the forecast says were wanted on out-of-stock days, at margin
    profit_comparison     stock targets from each product's costs vs one 95% service level
    accounting            sales, GST and purchases by day, for the accountant
"""

from __future__ import annotations

import io
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import rules as rules_mod
from . import stock as stock_mod
from .models import (Bill, Company, DailySales, Delivery, DeliveryLine, ForecastScore, Product,
                     PurchaseOrder, PurchaseOrderLine, StockMovement, StockOnHand, Store,
                     Supplier)

# column: (key, label, kind) with kind in text | qty | money | pct | num | date
Report = dict


def _out(title: str, columns: list, rows: list, totals: dict | None = None,
         notes: list | None = None) -> Report:
    return {"title": title, "columns": [{"key": k, "label": l, "kind": t} for k, l, t in columns],
            "rows": rows, "totals": totals or {}, "notes": notes or []}


def _stores(db: Session, store_ids: list[str]) -> dict[str, Store]:
    return {s.id: s for s in db.scalars(select(Store).where(Store.id.in_(store_ids)))}


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
    from .sales import tz_of
    from datetime import datetime, time
    tz = tz_of(db, cid)
    lo = datetime.combine(start, time(0, 0), tzinfo=tz)
    hi = datetime.combine(end + timedelta(days=1), time(0, 0), tzinfo=tz)
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
    rows = [{"store": s, "reason": r, "units_milli": u, "value_paise": v}
            for (s, r), (u, v) in sorted(agg.items())]
    return _out("Wastage, damage and expiry", [("store", "Store", "text"),
                                               ("reason", "Reason", "text"),
                                               ("units_milli", "Units", "qty"),
                                               ("value_paise", "At cost", "money")], rows,
                {"value_paise": sum(r["value_paise"] for r in rows),
                 "expiry_paise": sum(r["value_paise"] for r in rows if r["reason"] == "expired")},
                ["Expiry losses are the rows with the reason expired."])


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


def supplier_performance(db, cid, store_ids, start, end) -> Report:
    sups = {s.id: s.name for s in db.scalars(select(Supplier).where(Supplier.company_id == cid))}
    agg: dict = defaultdict(lambda: {"orders": 0, "ordered": 0, "received": 0, "damaged": 0,
                                     "on_time": 0, "with_date": 0, "unplanned": 0})
    for po in db.scalars(select(PurchaseOrder).where(
            PurchaseOrder.company_id == cid, PurchaseOrder.store_id.in_(store_ids),
            PurchaseOrder.status.in_(("partly_received", "received")),
            func.date(PurchaseOrder.created_at) >= start,
            func.date(PurchaseOrder.created_at) <= end)):
        a = agg[sups.get(po.supplier_id, "No supplier")]
        a["orders"] += 1
        a["unplanned"] += int(po.origin != "suggested")
        a["ordered"] += sum(l.qty_milli for l in po.lines)
        a["received"] += sum(min(l.received_milli, l.qty_milli) for l in po.lines)
        deliveries = list(db.scalars(select(Delivery).where(Delivery.po_id == po.id)
                                     .order_by(Delivery.created_at)))
        a["damaged"] += sum(dl.damaged_milli for d in deliveries for dl in d.lines)
        if po.expected_date and deliveries:
            a["with_date"] += 1
            a["on_time"] += int(deliveries[0].created_at.date() <= po.expected_date)
    rows = [{"supplier": k, "orders": v["orders"],
             "fill_rate": round(v["received"] / v["ordered"], 4) if v["ordered"] else None,
             "on_time": round(v["on_time"] / v["with_date"], 4) if v["with_date"] else None,
             "damaged_share": round(v["damaged"] / (v["received"] + v["damaged"]), 4)
             if v["received"] + v["damaged"] else None,
             "unplanned_orders": v["unplanned"]} for k, v in sorted(agg.items())]
    return _out("Supplier performance", [("supplier", "Supplier", "text"),
                                         ("orders", "Orders", "num"),
                                         ("fill_rate", "Fill rate", "pct"),
                                         ("on_time", "On time", "pct"),
                                         ("damaged_share", "Damaged", "pct"),
                                         ("unplanned_orders", "Unplanned orders", "num")], rows,
                notes=["Fill rate: good units received over units ordered, on orders that "
                       "have arrived. Unplanned: orders made by hand, not from suggestions."])


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
           "stock_value": stock_value, "slow_dead_stock": slow_dead_stock,
           "shrinkage": shrinkage, "wastage": wastage, "forecast_accuracy": forecast_accuracy,
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
