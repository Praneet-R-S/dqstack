"""Products and suppliers (the Product and Suppliers screens).

    GET   /api/products?q=&category=&status=     search by name, SKU or barcode
    GET   /api/products/lookup?code=              what a scanned barcode or SKU is
    GET   /api/products/categories
    POST  /api/products                           add one product
    GET   /api/products/{id}                      one product, its barcodes and suppliers
    PATCH /api/products/{id}
    GET   /api/products/{id}/overview?store_id=   everything the Product screen shows
    POST  /api/products/{id}/barcodes             add a barcode (a case barcode adds units)
    DELETE /api/products/{id}/barcodes/{code}
    GET/POST/PATCH /api/suppliers ...             suppliers and the products they supply
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import audit, permissions
from .. import stock as S
from ..db import get_db
from ..deps import ctx
from ..imports import parsing as P
from ..models import (Barcode, DailySales, ForecastRun, PriceRecord, Product, Recommendation,
                      StockOnHand, Store, Supplier, SupplierProduct)
from ..scope import Ctx, get_or_404, store_or_404

router = APIRouter(prefix="/api", tags=["catalogue"])

PRODUCT_FIELDS = ("sku", "name", "brand", "category", "department", "pack_size", "unit",
                  "mrp_paise", "price_paise", "cost_paise", "gst_rate_bp", "hsn",
                  "shelf_life_days", "perishable", "case_pack", "status", "abc_class")


def _catalogue_admin(c: Ctx) -> None:
    if permissions.level(c.role, "imports.upload") != permissions.YES:
        raise HTTPException(403, "Only an Owner or Admin can change the product catalogue.")


def product_dict(p: Product, codes: list[Barcode] | None = None) -> dict:
    d = {k: getattr(p, k) for k in PRODUCT_FIELDS}
    d["id"] = p.id
    if codes is not None:
        d["barcodes"] = [{"code": b.code, "units_per_scan": b.units_per_scan} for b in codes]
    return d


def _codes(db: Session, product_ids: list[str]) -> dict[str, list[Barcode]]:
    out: dict[str, list[Barcode]] = {}
    if not product_ids:
        return out
    for b in db.scalars(select(Barcode).where(Barcode.product_id.in_(product_ids))
                        .order_by(Barcode.code)):
        out.setdefault(b.product_id, []).append(b)
    return out


@router.get("/products")
def list_products(c: Ctx = Depends(ctx), db: Session = Depends(get_db),
                  q: str | None = Query(None, max_length=100), category: str | None = None,
                  status: str | None = None, limit: int = Query(100, ge=1, le=5000),
                  offset: int = Query(0, ge=0)):
    c.require("data.read")
    qy = select(Product).where(Product.company_id == c.company_id)
    if q:
        like = f"%{q.strip()}%"
        qy = qy.where(or_(Product.name.ilike(like), Product.sku.ilike(like),
                          Product.id.in_(select(Barcode.product_id).where(
                              Barcode.company_id == c.company_id, Barcode.code == q.strip()))))
    if category:
        qy = qy.where(Product.category == category)
    if status:
        qy = qy.where(Product.status == status)
    total = db.scalar(select(func.count()).select_from(qy.subquery()))
    rows = list(db.scalars(qy.order_by(Product.name).offset(offset).limit(limit)))
    codes = _codes(db, [p.id for p in rows])
    return {"total": total, "rows": [product_dict(p, codes.get(p.id, [])) for p in rows]}


@router.get("/products/lookup")
def lookup(code: str = Query(min_length=1, max_length=64), c: Ctx = Depends(ctx),
           db: Session = Depends(get_db)):
    """A scanned code: a barcode (a case barcode reports its units), else a SKU.
    Weight-embedded scale barcodes are read by Checkout (step 9)."""
    c.require("data.read")
    code = code.strip()
    b = db.scalar(select(Barcode).where(Barcode.company_id == c.company_id, Barcode.code == code))
    if b is not None:
        p = db.get(Product, b.product_id)
        return {"found": True, "product": product_dict(p), "units_per_scan": b.units_per_scan}
    p = db.scalar(select(Product).where(Product.company_id == c.company_id,
                                        func.upper(Product.sku) == code.upper()))
    if p is not None:
        return {"found": True, "product": product_dict(p), "units_per_scan": 1}
    return {"found": False, "message": f"Barcode {code} is not in your catalogue."}


@router.get("/products/categories")
def categories(c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    c.require("data.read")
    rows = db.execute(select(Product.category, func.count()).where(
        Product.company_id == c.company_id, Product.category.is_not(None))
        .group_by(Product.category).order_by(Product.category))
    return [{"category": cat, "products": n} for cat, n in rows]


class ProductIn(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=300)
    brand: str | None = Field(default=None, max_length=120)
    category: str | None = Field(default=None, max_length=120)
    department: str | None = Field(default=None, max_length=120)
    pack_size: str | None = Field(default=None, max_length=64)
    unit: str = "each"
    mrp_paise: int | None = Field(default=None, ge=0)
    price_paise: int | None = Field(default=None, ge=0)
    cost_paise: int | None = Field(default=None, ge=0)
    gst_rate_bp: int | None = Field(default=None, ge=0, le=4000)
    hsn: str | None = Field(default=None, max_length=16)
    shelf_life_days: int | None = Field(default=None, ge=0, le=3650)
    perishable: bool = False
    case_pack: int = Field(default=1, ge=1)
    status: str = "new"


class ProductPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=300)
    brand: str | None = None
    category: str | None = None
    department: str | None = None
    pack_size: str | None = None
    unit: str | None = None
    mrp_paise: int | None = Field(default=None, ge=0)
    price_paise: int | None = Field(default=None, ge=0)
    cost_paise: int | None = Field(default=None, ge=0)
    gst_rate_bp: int | None = Field(default=None, ge=0, le=4000)
    hsn: str | None = None
    shelf_life_days: int | None = Field(default=None, ge=0, le=3650)
    perishable: bool | None = None
    case_pack: int | None = Field(default=None, ge=1)
    status: str | None = None


def _check_product(d: dict) -> None:
    if d.get("unit") is not None and d["unit"] not in ("each", "kg", "litre"):
        raise HTTPException(422, "Unit must be each, kg or litre.")
    if d.get("status") is not None and d["status"] not in ("new", "active", "discontinued"):
        raise HTTPException(422, "Status must be new, active or discontinued.")
    if d.get("gst_rate_bp") is not None:
        try:
            P.gst_bp(str(d["gst_rate_bp"] / 100))
        except P.CellError:
            raise HTTPException(422, "That GST rate is not a GST rate.") from None
    mrp, price, cost = d.get("mrp_paise"), d.get("price_paise"), d.get("cost_paise")
    if mrp is not None and price is not None and price > mrp:
        raise HTTPException(422, "The selling price cannot be higher than MRP.")
    if mrp is not None and cost is not None and cost > mrp:
        raise HTTPException(422, "The cost cannot be higher than MRP.")
    hsn = d.get("hsn")
    if hsn and (not hsn.isdigit() or not 4 <= len(hsn) <= 8):
        raise HTTPException(422, "HSN must be 4 to 8 digits.")


@router.post("/products", status_code=201)
def create_product(body: ProductIn, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    _catalogue_admin(c)
    d = body.model_dump()
    d["sku"] = d["sku"].strip()
    _check_product(d)
    if db.scalar(select(Product.id).where(Product.company_id == c.company_id,
                                          func.upper(Product.sku) == d["sku"].upper())):
        raise HTTPException(409, f"A product with SKU {d['sku']} already exists.")
    p = Product(company_id=c.company_id, **d)
    db.add(p)
    db.flush()
    audit.log(db, c, "product.created", "product", p.id, after=product_dict(p))
    db.commit()
    return product_dict(p, [])


@router.get("/products/{product_id}")
def get_product(product_id: str, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    c.require("data.read")
    p = get_or_404(db, c, Product, product_id, store_attr=None, what="That product")
    d = product_dict(p, _codes(db, [p.id]).get(p.id, []))
    d["suppliers"] = [{"supplier_id": s.id, "supplier": s.name, "supplier_code": l.supplier_code,
                       "cost_paise": l.cost_paise, "case_pack": l.case_pack,
                       "min_order_qty_milli": l.min_order_qty_milli,
                       "lead_time_days": s.lead_time_days, "preferred": l.preferred}
                      for l, s in db.execute(select(SupplierProduct, Supplier).join(
                          Supplier, Supplier.id == SupplierProduct.supplier_id).where(
                          SupplierProduct.product_id == p.id))]
    return d


@router.patch("/products/{product_id}")
def patch_product(product_id: str, body: ProductPatch, c: Ctx = Depends(ctx),
                  db: Session = Depends(get_db)):
    _catalogue_admin(c)
    p = get_or_404(db, c, Product, product_id, store_attr=None, what="That product")
    changes = body.model_dump(exclude_unset=True)
    merged = {**product_dict(p), **changes}
    _check_product(merged)
    before = product_dict(p)
    for k, v in changes.items():
        setattr(p, k, v)
    audit.log(db, c, "product.updated", "product", p.id, before, product_dict(p))
    db.commit()
    return product_dict(p, _codes(db, [p.id]).get(p.id, []))


class BarcodeIn(BaseModel):
    code: str = Field(min_length=4, max_length=64)
    units_per_scan: int = Field(default=1, ge=1, le=100000)


@router.post("/products/{product_id}/barcodes", status_code=201)
def add_barcode(product_id: str, body: BarcodeIn, c: Ctx = Depends(ctx),
                db: Session = Depends(get_db)):
    _catalogue_admin(c)
    p = get_or_404(db, c, Product, product_id, store_attr=None, what="That product")
    code = body.code.strip()
    if not code.isalnum():
        raise HTTPException(422, "A barcode is letters and digits only.")
    if not P.gtin_ok(code):
        raise HTTPException(422, f"Barcode {code} has a wrong check digit; check it was typed "
                                 "correctly.")
    other = db.scalar(select(Barcode).where(Barcode.company_id == c.company_id,
                                            Barcode.code == code))
    if other is not None:
        owner = db.get(Product, other.product_id)
        raise HTTPException(409, f"Barcode {code} already belongs to SKU {owner.sku} "
                                 f"({owner.name}); one of them must change.")
    db.add(Barcode(company_id=c.company_id, product_id=p.id, code=code,
                   units_per_scan=body.units_per_scan))
    audit.log(db, c, "product.barcode_added", "product", p.id,
              after={"code": code, "units_per_scan": body.units_per_scan})
    db.commit()
    return product_dict(p, _codes(db, [p.id]).get(p.id, []))


@router.delete("/products/{product_id}/barcodes/{code}")
def remove_barcode(product_id: str, code: str, c: Ctx = Depends(ctx),
                   db: Session = Depends(get_db)):
    _catalogue_admin(c)
    p = get_or_404(db, c, Product, product_id, store_attr=None, what="That product")
    b = db.scalar(select(Barcode).where(Barcode.product_id == p.id, Barcode.code == code))
    if b is None:
        raise HTTPException(404, "That barcode was not found on this product.")
    db.delete(b)
    audit.log(db, c, "product.barcode_removed", "product", p.id, before={"code": code})
    db.commit()
    return product_dict(p, _codes(db, [p.id]).get(p.id, []))


@router.get("/products/{product_id}/overview")
def overview(product_id: str, c: Ctx = Depends(ctx), db: Session = Depends(get_db),
             store_id: str | None = None, days: int = Query(90, ge=7, le=1100)):
    """The Product screen: stock and history in this store, the same product in the
    other stores the person can see, sales, suppliers, prices and the forecast."""
    c.require("data.read")
    p = get_or_404(db, c, Product, product_id, store_attr=None, what="That product")
    visible = c.visible_store_ids(db)
    store = store_or_404(db, c, store_id) if store_id else (db.get(Store, visible[0])
                                                          if visible else None)
    detail = get_product(product_id, c, db)
    out: dict = {"product": detail, "store": None, "stock": None, "movements": [],
                 "other_stores": [], "sales": [], "prices": [], "forecast": None}
    if store is None:
        return out
    out["store"] = {"id": store.id, "code": store.code, "name": store.name}
    rows = S.stock_rows(db, c.company_id, store, product_id=p.id)
    out["stock"] = rows[0] if rows else None
    out["movements"] = S.history(db, c.company_id, store.id, p.id, 50)
    names = {s.id: s for s in db.scalars(select(Store).where(Store.id.in_(visible)))}
    for row in db.scalars(select(StockOnHand).where(StockOnHand.product_id == p.id,
                                                    StockOnHand.store_id.in_(visible))):
        if row.store_id != store.id:
            s = names[row.store_id]
            out["other_stores"].append({"store_id": s.id, "code": s.code, "name": s.name,
                                        "on_hand_milli": row.on_hand_milli, "known": row.known})
    since = date.today() - timedelta(days=days)
    out["sales"] = [{"day": d.day.isoformat(), "units_milli": d.units_milli,
                     "revenue_paise": d.revenue_paise, "out_of_stock": d.out_of_stock}
                    for d in db.scalars(select(DailySales).where(
                        DailySales.store_id == store.id, DailySales.product_id == p.id,
                        DailySales.day >= since).order_by(DailySales.day))]
    out["prices"] = [{"price_paise": r.price_paise, "start_date": r.start_date.isoformat(),
                      "end_date": r.end_date.isoformat() if r.end_date else None,
                      "promo_type": r.promo_type, "store_id": r.store_id}
                     for r in db.scalars(select(PriceRecord).where(
                         PriceRecord.product_id == p.id,
                         or_(PriceRecord.store_id.is_(None), PriceRecord.store_id == store.id))
                         .order_by(PriceRecord.start_date.desc()).limit(50))]
    run = db.scalar(select(ForecastRun).where(ForecastRun.company_id == c.company_id,
                                              ForecastRun.status == "succeeded")
                    .order_by(ForecastRun.created_at.desc()).limit(1))
    if run is not None:
        rec = db.scalar(select(Recommendation).where(Recommendation.run_id == run.id,
                                                     Recommendation.store_id == store.id,
                                                     Recommendation.product_id == p.id))
        if rec is not None:
            out["forecast"] = {"run_id": run.id, "data_cutoff": run.data_cutoff.isoformat()
                               if run.data_cutoff else None, "daily": rec.daily,
                               "week1_mid_milli": rec.week1_mid_milli,
                               "week1_lo_milli": rec.week1_lo_milli,
                               "week1_hi_milli": rec.week1_hi_milli,
                               "target_milli": rec.target_milli, "reasons": rec.reasons,
                               "money_at_risk_paise": rec.money_at_risk_paise}
    return out


# ------------------------------------------------------------------ suppliers

def supplier_dict(s: Supplier, n_products: int | None = None) -> dict:
    d = {"id": s.id, "name": s.name, "gstin": s.gstin, "contact": s.contact, "email": s.email,
         "phone": s.phone, "order_days": s.order_days, "lead_time_days": s.lead_time_days,
         "min_order_paise": s.min_order_paise}
    if n_products is not None:
        d["products"] = n_products
    return d


@router.get("/suppliers")
def list_suppliers(c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    c.require("data.read")
    counts = dict(db.execute(select(SupplierProduct.supplier_id, func.count()).where(
        SupplierProduct.company_id == c.company_id).group_by(SupplierProduct.supplier_id)).all())
    return [supplier_dict(s, counts.get(s.id, 0)) for s in db.scalars(
        select(Supplier).where(Supplier.company_id == c.company_id).order_by(Supplier.name))]


@router.get("/suppliers/{supplier_id}")
def get_supplier(supplier_id: str, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    c.require("data.read")
    s = get_or_404(db, c, Supplier, supplier_id, store_attr=None, what="That supplier")
    d = supplier_dict(s)
    d["product_list"] = [{"product_id": p.id, "sku": p.sku, "name": p.name,
                          "supplier_code": l.supplier_code, "cost_paise": l.cost_paise,
                          # The supplier's own terms as stored (None = the product's own);
                          # the effective case size is given separately.
                          "case_pack": l.case_pack, "product_case_pack": p.case_pack,
                          "effective_case_pack": l.case_pack or p.case_pack,
                          "unit": p.unit or "each", "preferred": l.preferred,
                          "min_order_qty_milli": l.min_order_qty_milli}
                         for l, p in db.execute(select(SupplierProduct, Product).join(
                             Product, Product.id == SupplierProduct.product_id).where(
                             SupplierProduct.supplier_id == s.id).order_by(Product.name))]
    return d


class SupplierIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    gstin: str | None = None
    contact: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=20)
    order_days: list[int] = Field(default_factory=list)
    lead_time_days: int = Field(default=2, ge=0, le=120)
    min_order_paise: int = Field(default=0, ge=0)


class SupplierPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    gstin: str | None = None
    contact: str | None = None
    email: str | None = None
    phone: str | None = None
    order_days: list[int] | None = None
    lead_time_days: int | None = Field(default=None, ge=0, le=120)
    min_order_paise: int | None = Field(default=None, ge=0)


def _check_supplier(d: dict) -> None:
    if d.get("gstin") and not P.GSTIN_RE.match(d["gstin"].upper()):
        raise HTTPException(422, "That GSTIN is not in the right format.")
    if d.get("email") and not P.EMAIL_RE.match(d["email"]):
        raise HTTPException(422, "Enter a valid email address.")
    if any(x not in range(7) for x in (d.get("order_days") or [])):
        raise HTTPException(422, "Order days are 0 (Monday) to 6 (Sunday).")


def _supplier_admin(c: Ctx) -> None:
    if not (permissions.level(c.role, "imports.upload") == permissions.YES
            or c.can("suppliers.manage")):
        raise HTTPException(403, "Only an Owner, Admin or Purchaser can change suppliers.")


@router.post("/suppliers", status_code=201)
def create_supplier(body: SupplierIn, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    _supplier_admin(c)
    d = body.model_dump()
    _check_supplier(d)
    if db.scalar(select(Supplier.id).where(Supplier.company_id == c.company_id,
                                           func.upper(Supplier.name) == d["name"].upper())):
        raise HTTPException(409, f"A supplier called {d['name']} already exists.")
    s = Supplier(company_id=c.company_id, **{**d, "gstin": (d["gstin"] or "").upper() or None})
    db.add(s)
    db.flush()
    audit.log(db, c, "supplier.created", "supplier", s.id, after=supplier_dict(s))
    db.commit()
    return supplier_dict(s, 0)


@router.patch("/suppliers/{supplier_id}")
def patch_supplier(supplier_id: str, body: SupplierPatch, c: Ctx = Depends(ctx),
                   db: Session = Depends(get_db)):
    _supplier_admin(c)
    s = get_or_404(db, c, Supplier, supplier_id, store_attr=None, what="That supplier")
    changes = body.model_dump(exclude_unset=True)
    _check_supplier(changes)
    before = supplier_dict(s)
    for k, v in changes.items():
        setattr(s, k, v.upper() if k == "gstin" and v else v)
    audit.log(db, c, "supplier.updated", "supplier", s.id, before, supplier_dict(s))
    db.commit()
    return supplier_dict(s)


class TermsIn(BaseModel):
    supplier_code: str | None = Field(default=None, max_length=64)
    cost_paise: int | None = Field(default=None, ge=0)
    case_pack: int | None = Field(default=None, ge=1)
    min_order_qty_milli: int | None = Field(default=None, ge=0)
    preferred: bool = True


@router.put("/suppliers/{supplier_id}/products/{product_id}")
def set_terms(supplier_id: str, product_id: str, body: TermsIn, c: Ctx = Depends(ctx),
              db: Session = Depends(get_db)):
    _supplier_admin(c)
    s = get_or_404(db, c, Supplier, supplier_id, store_attr=None, what="That supplier")
    p = get_or_404(db, c, Product, product_id, store_attr=None, what="That product")
    link = db.scalar(select(SupplierProduct).where(SupplierProduct.supplier_id == s.id,
                                                   SupplierProduct.product_id == p.id))
    before = None
    if link is None:
        link = SupplierProduct(company_id=c.company_id, supplier_id=s.id, product_id=p.id)
        db.add(link)
    else:
        before = {k: getattr(link, k) for k in body.model_dump()}
    for k, v in body.model_dump().items():
        setattr(link, k, v)
    audit.log(db, c, "supplier.terms", "supplier", s.id, before,
              {"sku": p.sku, **body.model_dump()})
    db.commit()
    return get_supplier(supplier_id, c, db)


@router.delete("/suppliers/{supplier_id}/products/{product_id}")
def remove_terms(supplier_id: str, product_id: str, c: Ctx = Depends(ctx),
                 db: Session = Depends(get_db)):
    _supplier_admin(c)
    s = get_or_404(db, c, Supplier, supplier_id, store_attr=None, what="That supplier")
    link = db.scalar(select(SupplierProduct).where(SupplierProduct.supplier_id == s.id,
                                                   SupplierProduct.product_id == product_id))
    if link is None:
        raise HTTPException(404, "This supplier does not supply that product.")
    db.delete(link)
    audit.log(db, c, "supplier.terms_removed", "supplier", s.id, before={"product_id": product_id})
    db.commit()
    return {"ok": True}
