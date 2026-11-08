"""Import kinds that post to the stock ledger.

Opening stock is the first movement of each product in each store. It is set once:
a file that later says a different number is refused for that row, because stock
is never typed over (principle 2) -- a wrong opening figure is corrected by
counting. Undoing an opening-stock import posts reversing movements; nothing is
deleted from the ledger.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import func, select

from .. import ledger
from ..db import utcnow
from ..formats import qty as fmt_qty
from ..models import ImportChange, Product, StockMovement, StockOnHand, Store
from . import parsing as P
from .kinds import Column, Kind, _Row, register


class OpeningStock(Kind):
    key = "opening_stock"
    title = "Opening stock"
    natural_key = "store code and SKU"
    store_scoped = True
    columns = (
        Column("store_code", "Store code", True, ("store", "storecode", "branch", "outlet"),
               "", "PUN1"),
        Column("sku", "SKU", True, ("itemcode", "productcode", "article", "skucode"), "",
               "RICE-BAS-5KG"),
        Column("quantity", "Quantity", True, ("qty", "stock", "onhand", "closingstock",
                                              "balance", "quantityonhand", "stockqty"),
               "Units on hand; up to 3 decimals for kg and litre.", "24"),
        Column("unit_cost", "Unit cost", False, ("cost", "rate", "costprice", "value per unit"),
               "Cost per unit in rupees, excluding GST.", "715"),
        Column("batch", "Batch", False, ("batchno", "lot", "lotno"), "", "B2611"),
        Column("expiry", "Expiry date", False, ("expirydate", "bestbefore", "exp", "useby"),
               "DD-MM-YYYY.", "30-06-2027"),
    )

    def authorise(self, c):
        if not c.can("stock.opening"):
            raise HTTPException(403, "Only an Owner, Admin or Store manager can set opening stock.")

    def begin(self, db, c):
        stores = {s.code.upper(): s for s in db.scalars(
            select(Store).where(Store.company_id == c.company_id))}
        products = {p.sku.upper(): p for p in db.scalars(
            select(Product).where(Product.company_id == c.company_id))}
        soh = {(r.store_id, r.product_id): r for r in db.scalars(
            select(StockOnHand).where(StockOnHand.company_id == c.company_id))}
        return {"c": c, "stores": stores, "products": products, "soh": soh, "seen": {}}

    def check_row(self, v, n, st):
        r = _Row(v)
        c = st["c"]
        code = r.raw("store_code").upper()
        store = st["stores"].get(code) if code else None
        if not code:
            r.errors.append("store code is missing")
        elif store is None or not c.can("stock.opening", store.id) or \
                (not c.all_stores and store.id not in c.store_ids):
            r.errors.append(f"store code {code} is not one of your stores")
            store = None
        sku = r.raw("sku")
        product = st["products"].get(sku.upper()) if sku else None
        if not sku:
            r.errors.append("SKU is missing")
        elif product is None:
            r.errors.append(f"SKU {sku} is not in your products")
        q = r.take("quantity", P.quantity_milli, "Quantity")
        if not r.raw("quantity"):
            r.errors.append("quantity is missing")
        if q is not None and product is not None and product.unit == "each" and q % 1000:
            r.errors.append(f"quantity {r.raw('quantity')} must be a whole number for a "
                            "product sold each")
        cost = r.take("unit_cost", P.money_paise, "Unit cost")
        r.text("batch", "Batch", 64)
        expiry = r.take("expiry", P.day, "expiry date")
        if store is not None and product is not None:
            key = (store.id, product.id)
            if key in st["seen"]:
                r.errors.append(f"SKU {product.sku} in {store.code} is also on row {st['seen'][key]}")
            else:
                st["seen"][key] = n
            row = st["soh"].get(key)
            if row is not None and row.opening_milli is not None and q is not None \
                    and row.opening_milli != q:
                r.errors.append(f"opening stock for SKU {product.sku} in {store.code} is already "
                                f"{fmt_qty(row.opening_milli, product.unit)}; count the product "
                                "to correct it")
            elif row is not None and row.opening_milli is None and row.known and q is not None:
                r.errors.append(f"SKU {product.sku} in {store.code} has been counted already; "
                                "opening stock can no longer be set")
        if r.errors:
            return None, r.errors, r.warnings
        return {"store_id": store.id, "store_code": store.code, "product_id": product.id,
                "sku": product.sku, "qty_milli": q, "unit_cost_paise": cost,
                "batch": r.out.get("batch"), "expiry": expiry}, [], r.warnings

    def classify(self, clean, st):
        row = st["soh"].get((clean["store_id"], clean["product_id"]))
        if row is not None and row.opening_milli is not None:
            return "same"
        return "new"

    def apply(self, db, c, items, st, rec):
        for clean in items:
            if clean["qty_milli"] == 0:
                # Zero stock is still a fact worth recording: the product is known.
                row = ledger._soh(db, c.company_id, clean["store_id"], clean["product_id"])
                now = utcnow()
                ledger.mark_known(row, now)
                ledger.baselined(row, now)
                row.opening_milli = 0
                rec.add("opening_zero", f"{clean['store_id']}:{clean['product_id']}", "insert",
                        after={"store": clean["store_code"], "sku": clean["sku"]})
                continue
            res = ledger.post(db, company_id=c.company_id, store_id=clean["store_id"],
                              product_id=clean["product_id"], qty_milli=clean["qty_milli"],
                              type="opening", reason="Opening stock file", source_type="import",
                              source_id=rec.job_id, person_id=c.person.id, device=c.device,
                              unit_cost_paise=clean["unit_cost_paise"], batch=clean["batch"],
                              expiry=clean["expiry"],
                              idempotency_key=f"import:{rec.job_id}:{clean['store_id']}:"
                                              f"{clean['product_id']}")
            rec.add("opening", res.movement.id, "insert",
                    after={"store": clean["store_code"], "sku": clean["sku"],
                           "store_id": clean["store_id"], "product_id": clean["product_id"],
                           "qty_milli": clean["qty_milli"]})
        db.flush()

    def undo(self, db, ch: ImportChange):
        a = ch.after or {}
        label = f"SKU {a.get('sku')} in {a.get('store')}"
        if ch.entity == "opening_zero":
            store_id, product_id = ch.entity_id.split(":")
            row = db.get(StockOnHand, {"store_id": store_id, "product_id": product_id})
            moved = db.scalar(select(func.count()).select_from(StockMovement).where(
                StockMovement.store_id == store_id, StockMovement.product_id == product_id))
            if row is not None and not moved:
                row.known = False
                row.known_since = None
                row.baseline_at = None
                row.opening_milli = None
                return None
            return f"{label} has moved since; count it to correct the opening stock"
        mv = db.get(StockMovement, ch.entity_id)
        if mv is None:
            return f"{label}: the opening movement is missing"
        later = db.scalar(select(func.count()).select_from(StockMovement).where(
            StockMovement.store_id == mv.store_id, StockMovement.product_id == mv.product_id,
            StockMovement.id != mv.id, StockMovement.source_type != "import_undo"))
        if later:
            return f"{label} has moved since; count it to correct the opening stock"
        ledger.post(db, company_id=mv.company_id, store_id=mv.store_id,
                    product_id=mv.product_id, qty_milli=-mv.qty_milli, type="opening",
                    reason="Opening stock import undone", source_type="import_undo",
                    source_id=ch.job_id, idempotency_key=f"undo:{mv.id}")
        return None


register(OpeningStock())
