"""What each kind of file holds, how its rows are checked, and how they are applied.

Every kind follows the same contract, which is what makes uploads repeatable
(section 5, last paragraph):

* A row is identified by its natural key (SKU for products, store code for stores,
  supplier name, and so on). A row whose key already exists updates that record; a
  row identical to what is stored is "unchanged" and writes nothing. Uploading the
  same file twice therefore changes nothing.
* A blank cell means "keep what is there", never "erase it". That is what lets a
  file holding only SKU and price update prices without touching anything else.
* Every record created, changed or removed is written to ImportChange with its
  before and after values, so undo can put back exactly what was there -- and can
  refuse, with a reason, where something has happened to the record since.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import HTTPException
from sqlalchemy import delete, exists, or_, select
from sqlalchemy.orm import Session

from .. import permissions
from ..db import new_id
from ..formats import rupees
from ..models import (Barcode, BillLine, CalendarEvent, DailySales, ImportChange, PriceRecord,
                      Product, PurchaseOrderLine, StockMovement, Store, Supplier, SupplierProduct)
from . import parsing as P

PROMO_TYPES = ("none", "discount", "multibuy", "display", "clearance", "other")
STATE_NAMES: tuple[str, ...] = ()           # filled from routes.org on first use


def _states() -> tuple[str, ...]:
    global STATE_NAMES
    if not STATE_NAMES:
        from ..routes.org import STATE_NAMES as S
        STATE_NAMES = S
    return STATE_NAMES


@dataclass(frozen=True)
class Column:
    key: str
    title: str
    required: bool = False
    aliases: tuple[str, ...] = ()
    help: str = ""
    example: str = ""


@dataclass
class Recorder:
    """Writes ImportChange rows in order."""
    db: Session
    company_id: str
    job_id: str
    seq: int = 0
    rows: list = field(default_factory=list)

    def add(self, entity: str, entity_id: str, op: str, before=None, after=None) -> None:
        self.seq += 1
        self.rows.append(ImportChange(id=new_id(), company_id=self.company_id,
                                      job_id=self.job_id, seq=self.seq, entity=entity,
                                      entity_id=entity_id, op=op, before=before, after=after))

    def flush(self) -> None:
        self.db.add_all(self.rows)
        self.rows = []


class Kind:
    key = ""
    title = ""
    natural_key = ""
    columns: tuple[Column, ...] = ()
    store_scoped = False

    def authorise(self, c) -> None:
        if permissions.level(c.role, "imports.upload") != permissions.YES:
            raise HTTPException(403, f"Only an Owner or Admin can upload {self.title.lower()}.")

    def begin(self, db: Session, c) -> dict:
        return {}

    def check_row(self, v: dict[str, str], n: int, st: dict) -> tuple[dict | None, list, list]:
        raise NotImplementedError

    def classify(self, clean: dict, st: dict) -> str:
        raise NotImplementedError

    def apply(self, db: Session, c, items: list[dict], st: dict, rec: Recorder) -> None:
        raise NotImplementedError

    def undo(self, db: Session, ch: ImportChange) -> str | None:
        raise NotImplementedError

    def undo_batch(self, db: Session, changes: list[ImportChange]) -> tuple[int, list[str]]:
        """Undo changes newest first. Kinds with large files override this."""
        reverted, kept = 0, []
        for ch in changes:
            reason = self.undo(db, ch)
            if reason:
                kept.append(reason)
            else:
                reverted += 1
            db.flush()
        return reverted, kept


def _chunks(seq: list, n: int = 500):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


class _Row:
    """Collects every problem in a row instead of stopping at the first."""

    def __init__(self, v: dict[str, str]):
        self.v = v
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.out: dict = {}

    def raw(self, key: str) -> str:
        return (self.v.get(key) or "").strip()

    def take(self, key: str, fn, *args, **kw):
        s = self.raw(key)
        if not s:
            return None
        try:
            val = fn(s, *args, **kw)
        except P.CellError as e:
            self.errors.append(str(e))
            return None
        self.out[key] = val
        return val

    def text(self, key: str, what: str, max_len: int):
        s = self.raw(key)
        if not s:
            return None
        if len(s) > max_len:
            self.errors.append(f"{what} is longer than {max_len} characters")
            return None
        self.out[key] = s
        return s


def _changed(obj, fields: dict) -> dict:
    return {k: v for k, v in fields.items() if getattr(obj, k) != v}


def _jsonable(d: dict) -> dict:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in d.items()}


def _restore_value(model, key: str, v):
    from datetime import date
    col = model.__table__.c[key]
    if v is not None and col.type.python_type is date and isinstance(v, str):
        return date.fromisoformat(v)
    return v


def _undo_update(db: Session, model, ch: ImportChange, label: str) -> str | None:
    obj = db.get(model, ch.entity_id)
    if obj is None:
        return f"{label} no longer exists"
    now = {k: getattr(obj, k) for k in (ch.after or {})}
    if _jsonable(now) != (ch.after or {}):
        return f"{label} was changed again after this import; left as it is"
    for k, v in (ch.before or {}).items():
        setattr(obj, k, _restore_value(model, k, v))
    return None


# =================================================================== products

class Products(Kind):
    key = "products"
    title = "Products"
    natural_key = "SKU"
    columns = (
        Column("sku", "SKU", True, ("itemcode", "productcode", "code", "article", "articleno",
                                    "articlecode", "itemno", "skucode", "productid"),
               "Your code for the product. Each SKU appears once.", "RICE-BAS-5KG"),
        Column("name", "Name", True, ("productname", "itemname", "description", "item",
                                      "itemdescription", "product"),
               "What the product is called on the shelf.", "Basmati rice 5 kg"),
        Column("brand", "Brand", False, ("make", "manufacturer"), "", "India Gate"),
        Column("category", "Category", False, ("cat", "group", "productgroup", "subcategory"),
               "", "Rice and grains"),
        Column("department", "Department", False, ("dept", "division"), "", "Staples"),
        Column("pack_size", "Pack size", False, ("size", "packsize", "pack", "weight",
                                                 "netweight", "volume"), "", "5 kg"),
        Column("unit", "Unit", False, ("uom", "unitofmeasure", "measure"),
               "each, kg or litre. Loose items sold by weight use kg.", "each"),
        Column("mrp", "MRP", False, ("maxretailprice", "mrprs", "mrp₹", "mrpinr"),
               "Maximum retail price in rupees.", "899"),
        Column("price", "Selling price", False, ("sellingprice", "sp", "saleprice", "rate",
                                                 "retailprice", "salesprice", "price"),
               "What the customer pays, including GST.", "849"),
        Column("cost", "Cost", False, ("costprice", "cp", "purchaseprice", "landedcost",
                                       "buyingprice", "purchaserate"),
               "What you pay the supplier, excluding GST.", "720"),
        Column("gst", "GST rate", False, ("gst", "gst%", "gstrate", "gstpercent", "tax", "tax%",
                                          "taxrate", "taxpercent"),
               "Percent: 0, 5, 12, 18, 28 or 40.", "5"),
        Column("hsn", "HSN", False, ("hsncode", "hsnsac", "hsnsaccode"), "4 to 8 digits.",
               "10063020"),
        Column("shelf_life_days", "Shelf life days", False, ("shelflife", "shelflifedays",
                                                             "expirydays", "bestbeforedays"),
               "Days from receipt to expiry.", "365"),
        Column("perishable", "Perishable", False, ("isperishable",), "yes or no.", "no"),
        Column("barcodes", "Barcodes", False, ("barcode", "ean", "upc", "gtin", "eancode",
                                               "barcodes"),
               "One or more, separated by |. Needed only for scanning.", "8901234567890"),
        Column("case_pack", "Case pack", False, ("casesize", "casepack", "packof",
                                                 "unitspercase", "caseqty", "innerpack"),
               "Units in one case from the supplier.", "4"),
        Column("status", "Status", False, ("productstatus", "active"),
               "new, active or discontinued.", "active"),
    )
    FIELDS = ("name", "brand", "category", "department", "pack_size", "unit", "mrp_paise",
              "price_paise", "cost_paise", "gst_rate_bp", "hsn", "shelf_life_days",
              "perishable", "case_pack", "status")

    def begin(self, db, c):
        products = {p.sku.upper(): p for p in db.scalars(
            select(Product).where(Product.company_id == c.company_id))}
        by_id = {p.id: p for p in products.values()}
        codes = {}
        per_product: dict[str, set[str]] = {}
        for b in db.scalars(select(Barcode).where(Barcode.company_id == c.company_id)):
            codes[b.code] = b
            per_product.setdefault(b.product_id, set()).add(b.code)
        return {"products": products, "by_id": by_id, "codes": codes, "per_product": per_product,
                "seen": {}, "seen_codes": {}}

    def check_row(self, v, n, st):
        r = _Row(v)
        sku = r.raw("sku")
        if not sku:
            r.errors.append("SKU is missing")
        elif len(sku) > 64:
            r.errors.append("SKU is longer than 64 characters")
        elif sku.upper() in st["seen"]:
            r.errors.append(f"SKU {sku} appears again (first on row {st['seen'][sku.upper()]})")
        existing = st["products"].get(sku.upper()) if sku else None
        name = r.text("name", "Name", 300)
        if not name and not existing:
            r.errors.append("name is missing")
        r.text("brand", "Brand", 120)
        r.text("category", "Category", 120)
        r.text("department", "Department", 120)
        r.text("pack_size", "Pack size", 64)
        r.take("unit", P.unit)
        mrp = r.take("mrp", P.money_paise, "MRP")
        price = r.take("price", P.money_paise, "Selling price")
        cost = r.take("cost", P.money_paise, "Cost")
        r.take("gst", P.gst_bp)
        hsn = r.raw("hsn")
        if hsn:
            if not hsn.isdigit() or not 4 <= len(hsn) <= 8:
                r.errors.append(f"HSN '{hsn}' must be 4 to 8 digits")
            else:
                r.out["hsn"] = hsn
        r.take("shelf_life_days", P.whole, "Shelf life days", lo=0, hi=3650)
        r.take("perishable", P.yes_no, "Perishable")
        r.take("case_pack", P.whole, "Case pack", lo=1, hi=100000)
        status = r.raw("status").lower()
        if status:
            if status in ("yes", "true"):
                status = "active"
            if status not in ("new", "active", "discontinued"):
                r.errors.append(f"status '{r.raw('status')}' must be new, active or discontinued")
            else:
                r.out["status"] = status
        # Prices against each other, using stored values where the file leaves a gap.
        eff_mrp = mrp if mrp is not None else (existing.mrp_paise if existing else None)
        eff_price = price if price is not None else (existing.price_paise if existing else None)
        eff_cost = cost if cost is not None else (existing.cost_paise if existing else None)
        if eff_mrp is not None and eff_price is not None and eff_price > eff_mrp:
            r.errors.append(f"selling price {rupees(eff_price)} is higher than MRP {rupees(eff_mrp)}")
        if eff_mrp is not None and eff_cost is not None and eff_cost > eff_mrp:
            r.errors.append("cost is higher than MRP")
        elif eff_price is not None and eff_cost is not None and eff_cost > eff_price:
            r.warnings.append("cost is higher than the selling price, so each sale loses money")
        codes = P.split_list(r.raw("barcodes"))
        if codes:
            ok_codes = []
            for code in codes:
                if not code.isalnum() or not 4 <= len(code) <= 64:
                    r.errors.append(f"barcode '{code}' must be 4 to 64 letters or digits")
                elif not P.gtin_ok(code):
                    r.errors.append(f"barcode {code} has a wrong check digit; check it was "
                                    "typed correctly")
                elif code in st["seen_codes"]:
                    first_row, first_sku = st["seen_codes"][code]
                    r.errors.append(f"barcode {code} is also on row {first_row} (SKU {first_sku})")
                else:
                    owner = st["codes"].get(code)
                    if owner is not None and (existing is None or owner.product_id != existing.id):
                        other = st["by_id"].get(owner.product_id)
                        label = f"SKU {other.sku} ({other.name})" if other else "another product"
                        r.errors.append(f"barcode {code} already belongs to {label}; one of "
                                        "them must change")
                    else:
                        ok_codes.append(code)
            r.out["barcodes"] = ok_codes
        if sku and sku.upper() not in st["seen"]:
            st["seen"][sku.upper()] = n
        if r.errors:
            return None, r.errors, r.warnings
        for code in r.out.get("barcodes", []):
            st["seen_codes"][code] = (n, sku)
        clean = {"sku": sku}
        mapping = {"name": "name", "brand": "brand", "category": "category",
                   "department": "department", "pack_size": "pack_size", "unit": "unit",
                   "mrp": "mrp_paise", "price": "price_paise", "cost": "cost_paise",
                   "gst": "gst_rate_bp", "hsn": "hsn", "shelf_life_days": "shelf_life_days",
                   "perishable": "perishable", "case_pack": "case_pack", "status": "status"}
        for k, f in mapping.items():
            if k in r.out:
                clean[f] = r.out[k]
        if "barcodes" in r.out:
            clean["barcodes"] = r.out["barcodes"]
        return clean, [], r.warnings

    def classify(self, clean, st):
        p = st["products"].get(clean["sku"].upper())
        if p is None:
            return "new"
        fields = {k: v for k, v in clean.items() if k in self.FIELDS}
        if _changed(p, fields):
            return "update"
        if "barcodes" in clean and set(clean["barcodes"]) != st["per_product"].get(p.id, set()):
            return "update"
        return "same"

    def apply(self, db, c, items, st, rec):
        for clean in items:
            fields = {k: v for k, v in clean.items() if k in self.FIELDS}
            p = st["products"].get(clean["sku"].upper())
            if p is None:
                p = Product(id=new_id(), company_id=c.company_id, sku=clean["sku"],
                            **{"unit": "each", "case_pack": 1, "status": "active",
                               "perishable": False, **fields})
                db.add(p)
                st["products"][p.sku.upper()] = p
                st["by_id"][p.id] = p
                rec.add("product", p.id, "insert",
                        after=_jsonable({k: getattr(p, k) for k in ("sku",) + self.FIELDS}))
            else:
                diff = _changed(p, fields)
                if diff:
                    before = {k: getattr(p, k) for k in diff}
                    for k, v in diff.items():
                        setattr(p, k, v)
                    rec.add("product", p.id, "update", before=_jsonable(before),
                            after=_jsonable(diff))
        # Products first: barcodes reference them, and without an ORM relationship the
        # unit of work does not know to insert them in that order.
        db.flush()
        for clean in items:
            p = st["products"][clean["sku"].upper()]
            if "barcodes" in clean:
                have = st["per_product"].get(p.id, set())
                want = set(clean["barcodes"])
                for code in sorted(have - want):
                    b = st["codes"].pop(code)
                    rec.add("barcode", b.id, "delete", before={
                        "code": b.code, "product_id": b.product_id,
                        "units_per_scan": b.units_per_scan})
                    db.delete(b)
                for code in sorted(want - have):
                    b = Barcode(id=new_id(), company_id=c.company_id, product_id=p.id, code=code)
                    db.add(b)
                    st["codes"][code] = b
                    rec.add("barcode", b.id, "insert", after={
                        "code": code, "product_id": p.id, "units_per_scan": 1})
                st["per_product"][p.id] = want
        db.flush()

    def undo_batch(self, db, changes):
        """Bulk version for files of thousands of products. Same rules as `undo`:
        barcodes this import added go; barcodes it removed come back unless taken;
        updates are reversed unless changed again; products it created are deleted
        unless stock, sales or orders now refer to them."""
        reverted, kept = 0, []
        inserted = [ch for ch in changes if ch.entity == "product" and ch.op == "insert"]
        updated = [ch for ch in changes if ch.entity == "product" and ch.op == "update"]
        bc_ins = [ch.entity_id for ch in changes if ch.entity == "barcode" and ch.op == "insert"]
        bc_del = [ch for ch in changes if ch.entity == "barcode" and ch.op == "delete"]
        for part in _chunks(bc_ins):
            db.execute(delete(Barcode).where(Barcode.id.in_(part)))
        reverted += len(bc_ins)
        for ch in bc_del:
            reason = self.undo(db, ch)
            kept.append(reason) if reason else None
            reverted += 0 if reason else 1
        ids = [ch.entity_id for ch in updated]
        for part in _chunks(ids):
            list(db.scalars(select(Product).where(Product.id.in_(part))))   # load once
        for ch in updated:
            reason = _undo_update(db, Product, ch, f"SKU {ch.entity_id}")
            if reason:
                p = db.get(Product, ch.entity_id)
                kept.append(reason.replace(f"SKU {ch.entity_id}", f"SKU {p.sku}") if p else reason)
            else:
                reverted += 1
        db.flush()
        new_ids = [ch.entity_id for ch in inserted]
        used: set[str] = set()
        for part in _chunks(new_ids):
            for model in (StockMovement, DailySales, BillLine, PurchaseOrderLine):
                used.update(db.scalars(select(model.product_id).where(
                    model.product_id.in_(part)).distinct()))
        skus = dict(db.execute(select(Product.id, Product.sku).where(
            Product.id.in_(list(used)))).all()) if used else {}
        for pid in new_ids:
            if pid in used:
                kept.append(f"SKU {skus.get(pid, pid)} has stock, sales or orders recorded "
                            "since; kept")
        gone = [pid for pid in new_ids if pid not in used]
        for part in _chunks(gone):
            db.execute(delete(Barcode).where(Barcode.product_id.in_(part)))
            db.execute(delete(SupplierProduct).where(SupplierProduct.product_id.in_(part)))
            db.execute(delete(PriceRecord).where(PriceRecord.product_id.in_(part)))
            db.execute(delete(Product).where(Product.id.in_(part)))
        reverted += len(gone)
        db.expire_all()
        return reverted, kept

    def undo(self, db, ch):
        if ch.entity == "barcode":
            if ch.op == "insert":
                b = db.get(Barcode, ch.entity_id)
                if b is not None:
                    db.delete(b)
                return None
            if ch.op == "delete":
                taken = db.scalar(select(Barcode.id).where(
                    Barcode.company_id == ch.company_id, Barcode.code == ch.before["code"]))
                if taken:
                    return f"barcode {ch.before['code']} is now used by another product"
                db.add(Barcode(id=ch.entity_id, company_id=ch.company_id, **ch.before))
                return None
        if ch.entity == "product":
            sku = (ch.after or {}).get("sku") or ch.entity_id
            if ch.op == "update":
                return _undo_update(db, Product, ch, f"SKU {sku}")
            if ch.op == "insert":
                p = db.get(Product, ch.entity_id)
                if p is None:
                    return None
                used = db.scalar(select(
                    exists().where(StockMovement.product_id == p.id)
                    | exists().where(DailySales.product_id == p.id)
                    | exists().where(BillLine.product_id == p.id)
                    | exists().where(PurchaseOrderLine.product_id == p.id)))
                if used:
                    return f"SKU {p.sku} has stock, sales or orders recorded since; kept"
                db.execute(delete(Barcode).where(Barcode.product_id == p.id))
                db.execute(delete(SupplierProduct).where(SupplierProduct.product_id == p.id))
                db.execute(delete(PriceRecord).where(PriceRecord.product_id == p.id))
                db.delete(p)
                return None
        return f"unknown change {ch.entity}/{ch.op}"


# =================================================================== stores

class Stores(Kind):
    key = "stores"
    title = "Stores"
    natural_key = "store code"
    columns = (
        Column("code", "Store code", True, ("storecode", "code", "storeid", "branchcode",
                                            "outletcode", "storeno"), "Short code, unique.", "PUN1"),
        Column("name", "Store name", True, ("storename", "name", "branch", "outlet",
                                            "branchname", "outletname"), "", "Pune Kothrud"),
        Column("address", "Address", False, ("storeaddress", "location"), "",
               "12 Karve Road, Kothrud, Pune 411038"),
        Column("state", "State", False, ("statename", "region"),
               "Decides which festival calendar applies.", "Maharashtra"),
        Column("opening_hours", "Opening hours", False, ("hours", "timings", "storehours"),
               "", "08:00-22:00"),
        Column("day_close_time", "Day close time", False, ("closetime", "closingtime",
                                                           "dayclose", "dayclosetime"),
               "24 hour time.", "22:00"),
        Column("weekly_off", "Weekly off", False, ("weeklyoff", "offday", "holiday",
                                                   "weeklyholiday", "closedon"),
               "Days the store is closed, for example Tue.", ""),
        Column("size", "Size", False, ("storesize", "format", "sizeclass"),
               "small, medium or large.", "medium"),
        Column("mode", "Mode", False, ("storemode", "dailymode"),
               "standalone or connected.", "standalone"),
    )
    FIELDS = ("name", "address", "state", "opening_hours", "day_close_time", "weekly_off",
              "size", "mode")

    def authorise(self, c):
        c.require("company.settings")

    def begin(self, db, c):
        return {"stores": {s.code.upper(): s for s in db.scalars(
            select(Store).where(Store.company_id == c.company_id))}, "seen": {}}

    def check_row(self, v, n, st):
        r = _Row(v)
        code = r.raw("code").upper()
        if not code:
            r.errors.append("store code is missing")
        elif len(code) > 32:
            r.errors.append("store code is longer than 32 characters")
        elif code in st["seen"]:
            r.errors.append(f"store code {code} appears again (first on row {st['seen'][code]})")
        if code and code not in st["seen"]:
            st["seen"][code] = n
        existing = st["stores"].get(code)
        if not r.text("name", "Store name", 200) and not existing:
            r.errors.append("store name is missing")
        r.text("address", "Address", 2000)
        state = r.raw("state")
        if state:
            match = next((s for s in _states() if s.lower() == state.lower()), None)
            if match:
                r.out["state"] = match
            else:
                r.errors.append(f"'{state}' is not an Indian state or union territory")
        r.text("opening_hours", "Opening hours", 64)
        t = r.raw("day_close_time")
        if t:
            if P.TIME_RE.match(t):
                hh, mm = t.split(":")
                r.out["day_close_time"] = f"{int(hh):02d}:{mm}"
            else:
                r.errors.append(f"day close time '{t}' must look like 22:00")
        r.take("weekly_off", P.weekdays, "Weekly off")
        size = r.raw("size").lower()
        if size:
            if size not in ("small", "medium", "large"):
                r.errors.append("size must be small, medium or large")
            else:
                r.out["size"] = size
        mode = r.raw("mode").lower()
        if mode:
            if mode not in ("standalone", "connected"):
                r.errors.append("mode must be standalone or connected")
            else:
                r.out["mode"] = mode
        if r.errors:
            return None, r.errors, r.warnings
        return {"code": code, **{k: r.out[k] for k in self.FIELDS if k in r.out}}, [], r.warnings

    def classify(self, clean, st):
        s = st["stores"].get(clean["code"])
        if s is None:
            return "new"
        return "update" if _changed(s, {k: v for k, v in clean.items() if k != "code"}) else "same"

    def apply(self, db, c, items, st, rec):
        for clean in items:
            fields = {k: v for k, v in clean.items() if k != "code"}
            s = st["stores"].get(clean["code"])
            if s is None:
                s = Store(id=new_id(), company_id=c.company_id, code=clean["code"],
                          **{"day_close_time": "22:00", "weekly_off": [], "mode": "standalone",
                             **fields})
                db.add(s)
                st["stores"][s.code] = s
                rec.add("store", s.id, "insert",
                        after=_jsonable({k: getattr(s, k) for k in ("code",) + self.FIELDS}))
            else:
                diff = _changed(s, fields)
                if diff:
                    rec.add("store", s.id, "update",
                            before=_jsonable({k: getattr(s, k) for k in diff}),
                            after=_jsonable(diff))
                    for k, v in diff.items():
                        setattr(s, k, v)
        db.flush()

    def undo(self, db, ch):
        code = (ch.after or {}).get("code", "")
        if ch.op == "update":
            return _undo_update(db, Store, ch, f"store {code}".strip())
        s = db.get(Store, ch.entity_id)
        if s is None:
            return None
        used = db.scalar(select(exists().where(StockMovement.store_id == s.id)
                                | exists().where(DailySales.store_id == s.id)))
        if used:
            return f"store {s.code} has stock or sales recorded since; kept"
        from ..models import PersonStore
        db.execute(delete(PersonStore).where(PersonStore.store_id == s.id))
        db.delete(s)
        return None


# =================================================================== suppliers

class Suppliers(Kind):
    """One row per supplier, or one row per supplier and product (the supplier's
    columns repeated). Rows without a SKU only describe the supplier."""
    key = "suppliers"
    title = "Suppliers"
    natural_key = "supplier name, and SKU for each product they supply"
    columns = (
        Column("supplier", "Supplier", True, ("suppliername", "vendor", "vendorname",
                                              "distributor", "party", "partyname"),
               "Supplier name; rows with the same name are one supplier.", "Gupta Traders"),
        Column("gstin", "Supplier GSTIN", False, ("gstin", "suppliergstin", "gstno", "gst number"),
               "", "27AAPFU0939F1ZV"),
        Column("contact", "Contact person", False, ("contact", "contactperson", "contactname"),
               "", "Anil Gupta"),
        Column("phone", "Phone", False, ("mobile", "phoneno", "contactnumber", "whatsapp"),
               "", "9822012345"),
        Column("email", "Email", False, ("emailid", "mail"), "Where orders are emailed.",
               "orders@guptatraders.in"),
        Column("order_days", "Order days", False, ("orderdays", "orderingdays", "visitdays",
                                                   "deliverydays"),
               "Days you can order, for example Mon, Thu. Blank means any day.", "Mon, Thu"),
        Column("lead_time_days", "Lead time days", False, ("leadtime", "leadtimedays",
                                                           "deliverytime", "leaddays"),
               "Days from order to delivery.", "2"),
        Column("min_order", "Minimum order value", False, ("minorder", "minimumorder", "moq value",
                                                           "minordervalue", "minimumordervalue"),
               "In rupees, per order.", "5000"),
        Column("sku", "SKU", False, ("itemcode", "productcode", "article", "skucode"),
               "A product this supplier supplies.", "RICE-BAS-5KG"),
        Column("supplier_code", "Supplier product code", False, ("suppliercode", "vendorcode",
                                                                 "suppliersku", "vendorsku"),
               "", "GT-1182"),
        Column("cost", "Cost", False, ("costprice", "purchaseprice", "rate", "buyingprice"),
               "What this supplier charges, excluding GST.", "715"),
        Column("case_pack", "Case pack", False, ("casepack", "casesize", "unitspercase"),
               "", "4"),
        Column("min_order_qty", "Minimum order quantity", False, ("moq", "minorderqty",
                                                                  "minimumorderquantity"),
               "In units.", "8"),
    )
    SFIELDS = ("gstin", "contact", "phone", "email", "order_days", "lead_time_days",
               "min_order_paise")
    LFIELDS = ("supplier_code", "cost_paise", "case_pack", "min_order_qty_milli")

    def authorise(self, c):
        if not (permissions.level(c.role, "imports.upload") == permissions.YES
                or c.can("suppliers.manage")):
            raise HTTPException(403, "Only an Owner, Admin or Purchaser can upload suppliers.")

    def begin(self, db, c):
        suppliers = {s.name.upper(): s for s in db.scalars(
            select(Supplier).where(Supplier.company_id == c.company_id))}
        products = {p.sku.upper(): p for p in db.scalars(
            select(Product).where(Product.company_id == c.company_id))}
        links = {(l.supplier_id, l.product_id): l for l in db.scalars(
            select(SupplierProduct).where(SupplierProduct.company_id == c.company_id))}
        return {"suppliers": suppliers, "products": products, "links": links,
                "supplier_rows": {}, "seen_links": {}}

    def check_row(self, v, n, st):
        r = _Row(v)
        name = r.raw("supplier")
        if not name:
            r.errors.append("supplier name is missing")
        elif len(name) > 200:
            r.errors.append("supplier name is longer than 200 characters")
        g = r.raw("gstin").upper()
        if g:
            if P.GSTIN_RE.match(g):
                r.out["gstin"] = g
            else:
                r.errors.append(f"GSTIN '{g}' is not in the right format")
        r.text("contact", "Contact person", 200)
        r.text("phone", "Phone", 20)
        e = r.raw("email").lower()
        if e:
            if P.EMAIL_RE.match(e):
                r.out["email"] = e
            else:
                r.errors.append(f"email '{e}' is not an email address")
        r.take("order_days", P.weekdays, "Order days")
        r.take("lead_time_days", P.whole, "Lead time days", lo=0, hi=120)
        r.take("min_order", P.money_paise, "Minimum order value")
        sup = {"gstin": r.out.get("gstin"), "contact": r.out.get("contact"),
               "phone": r.out.get("phone"), "email": r.out.get("email"),
               "order_days": r.out.get("order_days"),
               "lead_time_days": r.out.get("lead_time_days"),
               "min_order_paise": r.out.get("min_order")}
        sup = {k: val for k, val in sup.items() if val is not None}
        if name:
            first = st["supplier_rows"].get(name.upper())
            if first:
                first_row, first_vals = first
                for k, val in sup.items():
                    if k in first_vals and first_vals[k] != val:
                        label = k.replace("_paise", "").replace("_", " ")
                        r.errors.append(f"{label} differs from row {first_row} for the same "
                                        "supplier")
        sku = r.raw("sku")
        product = None
        if sku:
            product = st["products"].get(sku.upper())
            if product is None:
                r.errors.append(f"SKU {sku} is not in your products; load products first")
            elif name and (name.upper(), sku.upper()) in st["seen_links"]:
                r.errors.append(f"SKU {sku} is listed again for this supplier (first on row "
                                f"{st['seen_links'][(name.upper(), sku.upper())]})")
        r.text("supplier_code", "Supplier product code", 64)
        r.take("cost", P.money_paise, "Cost")
        r.take("case_pack", P.whole, "Case pack", lo=1, hi=100000)
        r.take("min_order_qty", P.quantity_milli, "Minimum order quantity")
        link = {"supplier_code": r.out.get("supplier_code"), "cost_paise": r.out.get("cost"),
                "case_pack": r.out.get("case_pack"),
                "min_order_qty_milli": r.out.get("min_order_qty")}
        link = {k: val for k, val in link.items() if val is not None}
        if link and not sku:
            r.errors.append("product columns are filled but SKU is missing")
        if r.errors:
            return None, r.errors, r.warnings
        prev = st["supplier_rows"].setdefault(name.upper(), (n, {}))[1]
        for k, val in sup.items():
            prev.setdefault(k, val)
        if product is not None:
            st["seen_links"][(name.upper(), sku.upper())] = n
        return {"name": name, "supplier": sup, "product_id": product.id if product else None,
                "sku": sku, "link": link}, [], r.warnings

    def classify(self, clean, st):
        s = st["suppliers"].get(clean["name"].upper())
        if s is None:
            return "new"
        changed = bool(_changed(s, clean["supplier"]))
        if clean["product_id"]:
            l = st["links"].get((s.id, clean["product_id"]))
            if l is None:
                return "new"
            changed = changed or bool(_changed(l, clean["link"]))
        return "update" if changed else "same"

    def apply(self, db, c, items, st, rec):
        for clean in items:
            s = st["suppliers"].get(clean["name"].upper())
            if s is None:
                s = Supplier(id=new_id(), company_id=c.company_id, name=clean["name"],
                             **{"order_days": [], "lead_time_days": 2, "min_order_paise": 0,
                                **clean["supplier"]})
                db.add(s)
                st["suppliers"][s.name.upper()] = s
                rec.add("supplier", s.id, "insert",
                        after=_jsonable({k: getattr(s, k) for k in ("name",) + self.SFIELDS}))
            else:
                diff = _changed(s, clean["supplier"])
                if diff:
                    rec.add("supplier", s.id, "update",
                            before=_jsonable({k: getattr(s, k) for k in diff}),
                            after=_jsonable(diff))
                    for k, val in diff.items():
                        setattr(s, k, val)
        db.flush()                      # suppliers before the product links that point at them
        for clean in items:
            s = st["suppliers"][clean["name"].upper()]
            if clean["product_id"]:
                l = st["links"].get((s.id, clean["product_id"]))
                if l is None:
                    l = SupplierProduct(id=new_id(), company_id=c.company_id, supplier_id=s.id,
                                        product_id=clean["product_id"], **clean["link"])
                    db.add(l)
                    st["links"][(s.id, clean["product_id"])] = l
                    rec.add("supplier_product", l.id, "insert",
                            after={"supplier": s.name, "sku": clean["sku"], **clean["link"]})
                else:
                    diff = _changed(l, clean["link"])
                    if diff:
                        rec.add("supplier_product", l.id, "update",
                                before={k: getattr(l, k) for k in diff}, after=diff)
                        for k, val in diff.items():
                            setattr(l, k, val)
        db.flush()

    def undo(self, db, ch):
        if ch.entity == "supplier_product":
            if ch.op == "update":
                return _undo_update(db, SupplierProduct, ch, "a supplier's product terms")
            l = db.get(SupplierProduct, ch.entity_id)
            if l is not None:
                db.delete(l)
            return None
        name = (ch.after or {}).get("name", "")
        if ch.op == "update":
            return _undo_update(db, Supplier, ch, f"supplier {name}".strip())
        s = db.get(Supplier, ch.entity_id)
        if s is None:
            return None
        from ..models import PurchaseOrder
        if db.scalar(select(exists().where(PurchaseOrder.supplier_id == s.id))):
            return f"supplier {s.name} has purchase orders since; kept"
        db.execute(delete(SupplierProduct).where(SupplierProduct.supplier_id == s.id))
        db.delete(s)
        return None


# =================================================================== prices

class Prices(Kind):
    key = "prices"
    title = "Prices and promotions"
    natural_key = "SKU, store, start date and promotion"
    columns = (
        Column("sku", "SKU", True, ("itemcode", "productcode", "article", "skucode"), "",
               "RICE-BAS-5KG"),
        Column("store_code", "Store code", False, ("store", "storecode", "branch", "outlet"),
               "Blank means every store.", ""),
        Column("price", "Price", True, ("sellingprice", "promoprice", "offerprice", "rate", "sp"),
               "Price to the customer in rupees, including GST.", "799"),
        Column("start_date", "Start date", True, ("from", "fromdate", "startdate", "validfrom",
                                                  "effectivedate", "start"),
               "DD-MM-YYYY.", "01-12-2026"),
        Column("end_date", "End date", False, ("to", "todate", "enddate", "validto", "validtill",
                                               "end"),
               "Blank means until the next price.", "07-12-2026"),
        Column("promotion", "Promotion", False, ("promo", "promotype", "offer", "scheme",
                                                 "promotiontype"),
               "none, discount, multibuy, display, clearance or other.", "discount"),
    )

    def begin(self, db, c):
        products = {p.sku.upper(): p for p in db.scalars(
            select(Product).where(Product.company_id == c.company_id))}
        stores = {s.code.upper(): s for s in db.scalars(
            select(Store).where(Store.company_id == c.company_id))}
        existing = {(r.product_id, r.store_id, r.start_date, r.promo_type or "none"): r
                    for r in db.scalars(select(PriceRecord).where(
                        PriceRecord.company_id == c.company_id))}
        return {"products": products, "stores": stores, "existing": existing, "seen": {}}

    def check_row(self, v, n, st):
        r = _Row(v)
        sku = r.raw("sku")
        product = st["products"].get(sku.upper()) if sku else None
        if not sku:
            r.errors.append("SKU is missing")
        elif product is None:
            r.errors.append(f"SKU {sku} is not in your products")
        code = r.raw("store_code").upper()
        store = None
        if code:
            store = st["stores"].get(code)
            if store is None:
                r.errors.append(f"store code {code} is not one of your stores")
        price = r.take("price", P.money_paise, "Price")
        if not r.raw("price"):
            r.errors.append("price is missing")
        start = r.take("start_date", P.day, "start date")
        if not r.raw("start_date"):
            r.errors.append("start date is missing")
        end = r.take("end_date", P.day, "end date")
        if start and end and end < start:
            r.errors.append("end date is before the start date")
        promo = (r.raw("promotion") or "none").lower()
        if promo not in PROMO_TYPES:
            r.errors.append(f"promotion '{r.raw('promotion')}' must be one of "
                            f"{', '.join(PROMO_TYPES)}")
        if product is not None and price is not None and product.mrp_paise is not None \
                and price > product.mrp_paise:
            r.errors.append(f"price {rupees(price)} is higher than MRP {rupees(product.mrp_paise)}")
        if r.errors:
            return None, r.errors, r.warnings
        key = (product.id, store.id if store else None, start, promo)
        if key in st["seen"]:
            return None, [f"the same SKU, store, start date and promotion is on row "
                          f"{st['seen'][key]}"], []
        st["seen"][key] = n
        return {"key": key, "sku": sku, "store_code": code or None, "price_paise": price,
                "end_date": end}, [], r.warnings

    def classify(self, clean, st):
        rec = st["existing"].get(clean["key"])
        if rec is None:
            return "new"
        return "update" if (rec.price_paise, rec.end_date) != (clean["price_paise"],
                                                               clean["end_date"]) else "same"

    def apply(self, db, c, items, st, rec):
        for clean in items:
            pid, sid, start, promo = clean["key"]
            pr = st["existing"].get(clean["key"])
            if pr is None:
                pr = PriceRecord(id=new_id(), company_id=c.company_id, product_id=pid,
                                 store_id=sid, price_paise=clean["price_paise"],
                                 start_date=start, end_date=clean["end_date"],
                                 promo_type=None if promo == "none" else promo)
                db.add(pr)
                st["existing"][clean["key"]] = pr
                rec.add("price", pr.id, "insert", after=_jsonable({
                    "sku": clean["sku"], "store": clean["store_code"],
                    "price_paise": pr.price_paise, "start_date": start,
                    "end_date": pr.end_date, "promo_type": pr.promo_type}))
            else:
                diff = _changed(pr, {"price_paise": clean["price_paise"],
                                     "end_date": clean["end_date"]})
                if diff:
                    rec.add("price", pr.id, "update",
                            before=_jsonable({k: getattr(pr, k) for k in diff}),
                            after=_jsonable(diff))
                    for k, val in diff.items():
                        setattr(pr, k, val)
        db.flush()

    def undo(self, db, ch):
        if ch.op == "update":
            return _undo_update(db, PriceRecord, ch, "a price")
        pr = db.get(PriceRecord, ch.entity_id)
        if pr is not None:
            db.delete(pr)
        return None


# =================================================================== calendar

class Calendar(Kind):
    key = "calendar"
    title = "Calendar"
    natural_key = "name and date"
    columns = (
        Column("name", "Name", True, ("event", "eventname", "festival", "occasion", "holiday"),
               "", "Temple fair"),
        Column("date", "Date", True, ("day", "eventdate", "on"), "DD-MM-YYYY.", "05-12-2026"),
        Column("kind", "Kind", False, ("type", "eventtype", "category"),
               "festival, holiday, closure or local_event.", "local_event"),
        Column("states", "States", False, ("state", "region"),
               "Blank for national. Several separated by |.", ""),
        Column("store_codes", "Store codes", False, ("stores", "store", "storecode"),
               "Blank for every store. Several separated by |.", "PUN1"),
    )

    def authorise(self, c):
        c.require("company.settings")

    def begin(self, db, c):
        stores = {s.code.upper(): s for s in db.scalars(
            select(Store).where(Store.company_id == c.company_id))}
        existing = {(e.name.upper(), e.day): e for e in db.scalars(select(CalendarEvent).where(
            CalendarEvent.company_id == c.company_id))}
        return {"stores": stores, "existing": existing, "seen": {}}

    def check_row(self, v, n, st):
        r = _Row(v)
        name = r.text("name", "Name", 200)
        if not r.raw("name"):
            r.errors.append("name is missing")
        d = r.take("date", P.day, "date")
        if not r.raw("date"):
            r.errors.append("date is missing")
        kind = (r.raw("kind") or "local_event").lower().replace(" ", "_")
        if kind == "local":
            kind = "local_event"
        if kind not in ("festival", "holiday", "closure", "local_event"):
            r.errors.append("kind must be festival, holiday, closure or local_event")
        states = []
        for s in P.split_list(r.raw("states")):
            match = next((x for x in _states() if x.lower() == s.lower()), None)
            if match:
                states.append(match)
            else:
                r.errors.append(f"'{s}' is not an Indian state or union territory")
        store_ids = []
        for code in P.split_list(r.raw("store_codes")):
            s = st["stores"].get(code.upper())
            if s is None:
                r.errors.append(f"store code {code.upper()} is not one of your stores")
            else:
                store_ids.append(s.id)
        if r.errors:
            return None, r.errors, r.warnings
        key = (name.upper(), d)
        if key in st["seen"]:
            return None, [f"the same name and date is on row {st['seen'][key]}"], []
        st["seen"][key] = n
        return {"key": key, "name": name, "day": d, "kind": kind, "states": states,
                "store_ids": store_ids}, [], r.warnings

    def classify(self, clean, st):
        e = st["existing"].get(clean["key"])
        if e is None:
            return "new"
        return "update" if _changed(e, {"kind": clean["kind"], "states": clean["states"],
                                        "store_ids": clean["store_ids"]}) else "same"

    def apply(self, db, c, items, st, rec):
        for clean in items:
            e = st["existing"].get(clean["key"])
            fields = {"kind": clean["kind"], "states": clean["states"],
                      "store_ids": clean["store_ids"]}
            if e is None:
                e = CalendarEvent(id=new_id(), company_id=c.company_id, name=clean["name"],
                                  day=clean["day"], approximate=False, source="company", **fields)
                db.add(e)
                st["existing"][clean["key"]] = e
                rec.add("calendar_event", e.id, "insert",
                        after=_jsonable({"name": e.name, "day": e.day, **fields}))
            else:
                diff = _changed(e, fields)
                if diff:
                    rec.add("calendar_event", e.id, "update",
                            before={k: getattr(e, k) for k in diff}, after=diff)
                    for k, val in diff.items():
                        setattr(e, k, val)
        db.flush()

    def undo(self, db, ch):
        if ch.op == "update":
            return _undo_update(db, CalendarEvent, ch, "a calendar entry")
        e = db.get(CalendarEvent, ch.entity_id)
        if e is not None:
            db.delete(e)
        return None


KINDS: dict[str, Kind] = {k.key: k for k in (Products(), Stores(), Suppliers(), Prices(),
                                             Calendar())}


def register(kind: Kind) -> None:
    """Later build steps add their kinds (opening stock, sales) here."""
    KINDS[kind.key] = kind
