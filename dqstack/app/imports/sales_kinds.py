"""Sales files: history for onboarding, and the daily file for standalone stores.

Both accept day totals or bill-level exports, which are added up by day (section 5
step 7), and both replace what is stored for each store and day the file contains:
a corrected file for a day is the truth for that day. Matching is on store, product
and date, so the same file twice changes nothing.

* Sales history fills the daily sales table only. It never moves stock.
* Daily sales also move stock, by the difference from what was posted before, and
  only for standalone stores: a connected store's sales come from its tills.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from fastapi import HTTPException
from sqlalchemy import select

from .. import sales as S
from ..models import Barcode, DailySales, ImportChange, Product, Store
from . import parsing as P
from .kinds import Column, Kind, _Row, register

TILL_SOURCES = ("checkout", "pos")


class _Sales(Kind):
    natural_key = "store, product and date"
    store_scoped = True
    post_stock = False
    source = "history"
    columns = (
        Column("day", "Date", True, ("date", "billdate", "saledate", "invoicedate", "day",
                                     "transactiondate", "txndate"), "DD-MM-YYYY.", "07-11-2026"),
        Column("store_code", "Store code", True, ("store", "storecode", "branch", "outlet",
                                                  "location"), "", "PUN1"),
        Column("sku", "SKU", True, ("itemcode", "productcode", "article", "skucode", "barcode",
                                    "ean", "item"),
               "Your SKU, or the product's barcode.", "RICE-BAS-5KG"),
        Column("units", "Units", True, ("qty", "quantity", "unitssold", "qtysold", "salesqty",
                                        "soldqty", "nos"),
               "Units sold; returns may be negative on bill-level files.", "6"),
        Column("revenue", "Revenue", False, ("amount", "sales", "salesvalue", "value",
                                             "netamount", "totalamount", "netsales", "turnover"),
               "Rupees taken, including GST.", "5094"),
        Column("price", "Price", False, ("rate", "sellingprice", "unitprice", "mrp"),
               "Used for revenue when there is no revenue column.", "849"),
    )

    def authorise(self, c):
        if not c.can("imports.upload"):
            raise HTTPException(403, "Only an Owner, Admin or Store manager can upload sales.")

    def begin(self, db, c):
        stores = {s.code.upper(): s for s in db.scalars(
            select(Store).where(Store.company_id == c.company_id))}
        products = {p.sku.upper(): p for p in db.scalars(
            select(Product).where(Product.company_id == c.company_id))}
        by_id = {p.id: p for p in products.values()}
        codes = {b.code: b for b in db.scalars(
            select(Barcode).where(Barcode.company_id == c.company_id))}
        return {"c": c, "stores": stores, "products": products, "by_id": by_id, "codes": codes,
                "today": S.local_today(S.tz_of(db, c.company_id))}

    def check_row(self, v, n, st):
        r = _Row(v)
        c = st["c"]
        code = r.raw("store_code").upper()
        store = st["stores"].get(code) if code else None
        if not code:
            r.errors.append("store code is missing")
        elif store is None or (not c.all_stores and store.id not in c.store_ids) \
                or not c.can("imports.upload", store.id):
            r.errors.append(f"store code {code} is not one of your stores")
            store = None
        elif self.post_stock and store.mode != "standalone":
            r.errors.append(f"{store.code} is a connected store; its sales come from the tills")
        sku = r.raw("sku")
        product = None
        units_per = 1
        if not sku:
            r.errors.append("SKU is missing")
        else:
            product = st["products"].get(sku.upper())
            if product is None and sku in st["codes"]:
                b = st["codes"][sku]
                product = st["by_id"].get(b.product_id)
                units_per = b.units_per_scan
            if product is None:
                r.errors.append(f"SKU or barcode {sku} is not in your products")
        d = r.take("day", P.day, "date")
        if not r.raw("day"):
            r.errors.append("date is missing")
        elif d is not None and d > st["today"]:
            r.errors.append(f"date {d:%d %b %Y} is in the future")
        units = r.take("units", P.quantity_milli, "Units", allow_negative=True)
        if not r.raw("units"):
            r.errors.append("units are missing")
        revenue = r.take("revenue", P.money_paise, "Revenue", allow_negative=True)
        price = r.take("price", P.money_paise, "Price")
        if r.errors:
            return None, r.errors, r.warnings
        if revenue is None:
            # A price on the row is per row unit (a case, for a case barcode); otherwise
            # the catalogue price per single unit.
            if price is not None:
                revenue = units * price // 1000
            else:
                revenue = units * units_per * (product.price_paise or 0) // 1000
        units *= units_per
        return {"store_id": store.id, "store_code": store.code, "product_id": product.id,
                "sku": product.sku, "day": d, "units": units, "revenue": revenue,
                "unit": product.unit}, [], r.warnings

    def aggregate(self, db, items, st):
        """Add bill-level rows up by store, product and day, then compare with what is
        stored. Every store-day in the file is replaced, so a product sold that day
        before but missing from the file goes to zero."""
        totals: dict = defaultdict(lambda: [0, 0, []])
        for n, clean, _ in items:
            t = totals[(clean["store_id"], clean["product_id"], clean["day"])]
            t[0] += clean["units"]
            t[1] += clean["revenue"]
            t[2].append(n)
        errors = []
        good = {}
        for key, (units, revenue, rows) in totals.items():
            if units < 0:
                p = st["by_id"][key[1]]
                errors.append({"row": rows[0], "problems": [
                    f"units for {p.sku} on {key[2]:%d %b %Y} add up to less than zero"]})
                continue
            p = st["by_id"][key[1]]
            if p.unit == "each" and units % 1000:
                errors.append({"row": rows[0], "problems": [
                    f"units for {p.sku} on {key[2]:%d %b %Y} must add up to a whole number"]})
                continue
            good[key] = (units, revenue, rows[0])
        store_days = {(s, d) for s, _, d in good}
        existing: dict = {}
        if store_days:
            stores = {s for s, _ in store_days}
            lo = min(d for _, d in store_days)
            hi = max(d for _, d in store_days)
            for row in db.scalars(select(DailySales).where(
                    DailySales.store_id.in_(stores), DailySales.day >= lo, DailySales.day <= hi)):
                if (row.store_id, row.day) in store_days:
                    existing[(row.store_id, row.product_id, row.day)] = row
        anchors: dict = {}
        if store_days and self.post_stock:
            from ..models import StockOnHand
            from ..security import _aware
            for soh in db.scalars(select(StockOnHand).where(
                    StockOnHand.store_id.in_({s for s, _ in store_days}),
                    StockOnHand.baseline_at.is_not(None))):
                anchors[(soh.store_id, soh.product_id)] = _aware(soh.baseline_at)
        tz = S.tz_of(db, st["c"].company_id)

        def absorbed(key) -> bool:
            """Sales before the product's last count do not move stock (sales.py)."""
            anchor = anchors.get((key[0], key[1]))
            if anchor is None:
                return False
            return S.close_instant(db.get(Store, key[0]), tz, key[2]) < anchor

        out = []
        for key, (units, revenue, first_row) in good.items():
            row = existing.get(key)
            if row is not None and row.source in TILL_SOURCES:
                errors.append({"row": first_row, "problems": [
                    "these sales already came from the store's tills"]})
                continue
            if row is None:
                status = "new"
            elif row.units_milli == units and row.revenue_paise == revenue and \
                    (not self.post_stock or row.posted_milli == units or absorbed(key)):
                status = "same"
            else:
                status = "update"
            out.append((first_row, {"store_id": key[0], "product_id": key[1], "day": key[2],
                                    "units": units, "revenue": revenue}, status))
        for key, row in existing.items():
            if key not in good and row.source not in TILL_SOURCES and row.source != "ledger" \
                    and (row.units_milli or row.revenue_paise):
                out.append((0, {"store_id": key[0], "product_id": key[1], "day": key[2],
                                "units": 0, "revenue": 0}, "update"))
        return out, errors

    def classify(self, clean, st):          # unused: aggregate() classifies
        return "new"

    def apply(self, db, c, items, st, rec):
        by_day: dict = defaultdict(dict)
        for clean in items:
            by_day[(clean["store_id"], clean["day"])][clean["product_id"]] = (clean["units"],
                                                                               clean["revenue"])
        for (store_id, d), totals in sorted(by_day.items(), key=lambda kv: kv[0][1]):
            store = db.get(Store, store_id)
            S.apply_day_totals(db, c, store, d, totals, replace_day=False, source=self.source,
                               post_stock=self.post_stock, job_id=rec.job_id, rec=rec)
        db.flush()

    def undo_batch(self, db, changes, c=None):
        reverted, kept = 0, []
        for ch in changes:
            reason = S.undo_daily_sales(db, c, ch)
            if reason:
                kept.append(reason)
            else:
                reverted += 1
            db.flush()
        return reverted, kept

    def undo(self, db, ch: ImportChange):     # pragma: no cover - undo_batch is used
        raise NotImplementedError


class SalesHistory(_Sales):
    key = "sales_history"
    title = "Sales history"
    post_stock = False
    source = "history"


class DailySalesFile(_Sales):
    key = "daily_sales"
    title = "Daily sales"
    post_stock = True
    source = "upload"


register(SalesHistory())
register(DailySalesFile())
