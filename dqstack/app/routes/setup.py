"""The setup checklist and the readiness page (section 5).

The checklist sits on the Home screen until every step is done. Each step's state
is worked out from the data wherever the data can say it ("3 stores", "38 weeks of
sales"), and recorded by hand only where it cannot ("I have checked the calendar").
A step can be left for later only where the specification says it can wait.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db, utcnow
from ..deps import ctx
from ..formats import group_indian
from ..models import (Barcode, CalendarEvent, Company, DailySales, ForecastRun, Person, PriceRecord,
                      Product, StockOnHand, Store, Supplier)
from ..scope import Ctx

router = APIRouter(prefix="/api/setup", tags=["setup"])

# key, title, what it involves, minutes (None = depends on data), where to continue,
# optional (may be left for later)
STEPS: list[tuple[str, str, str, int | None, str, bool]] = [
    ("company", "Create the company",
     "Company name, GSTIN, time zone and financial year start.", 2, "#/settings/company", False),
    ("stores", "Add stores",
     "Name, address, state, opening hours, day close time and weekly off day.", 5,
     "#/settings/stores", False),
    ("people", "Invite people",
     "Email or phone, role and stores. Each person sets their own password.", 5,
     "#/settings/people", False),
    ("products", "Load products",
     "Download the template, fill it in, upload it and read the check report.", 30,
     "#/imports/new/products", False),
    ("suppliers", "Load suppliers",
     "Who supplies what, lead time, order days, case pack and minimum order. This can wait: "
     "until then a 2 day lead time and weekly ordering are used.", 20,
     "#/imports/new/suppliers", True),
    ("opening_stock", "Set opening stock",
     "Upload a stock file, count in the app aisle by aisle, or start with stock unknown. "
     "Order suggestions appear only for products whose stock is known.", 30,
     "#/stock/opening", False),
    ("sales_history", "Load sales history",
     "Daily units sold per store and product. At least 8 weeks to start; 12 months so "
     "festivals are learned; 2 to 3 years is best.", 20, "#/imports/new/sales_history", False),
    ("prices", "Prices and promotions",
     "Optional. Past prices improve accuracy, and planned promotions are forecast in "
     "advance.", 15, "#/imports/new/prices", True),
    ("calendar", "Check the calendar",
     "National and state festivals are filled in. Confirm the dates and add local events "
     "and store closures.", 10, "#/settings/calendar", False),
    ("rules", "Business rules",
     "Service level, holding cost, wastage policy, how often to count, and the count "
     "difference that needs approval.", 10, "#/settings/rules", False),
    ("readiness", "Review the readiness page",
     "What is loaded, what is missing, and a data quality score for each store.", 5,
     "#/setup/readiness", False),
    ("first_forecast", "First forecast",
     "The engine trains on your own history and tests itself on your last 8 weeks. You "
     "get an email when it is ready.", None, "#/setup/forecast", False),
    ("go_live", "Go live",
     "Choose a daily mode for each store: standalone (upload or type the day's sales) or "
     "connected (scanner or existing billing system).", 5, "#/settings/stores", False),
]
KEYS = [s[0] for s in STEPS]

# Which states a person may set by hand, per step.
MANUAL = {
    "people": {"done"},             # "nobody else needs an account yet"
    "suppliers": {"later"},
    "opening_stock": {"unknown"},    # start with stock unknown
    "sales_history": {"none"},       # no history: new-store mode
    "prices": {"later"},
    "calendar": {"done"},
    "rules": {"done"},
    "readiness": {"done"},
    "go_live": {"done"},
}


def _weeks_of_sales(db: Session, company_id: str, store_id: str | None = None) -> float:
    q = select(func.min(DailySales.day), func.max(DailySales.day)).where(
        DailySales.company_id == company_id)
    if store_id:
        q = q.where(DailySales.store_id == store_id)
    lo, hi = db.execute(q).one()
    if not lo or not hi:
        return 0.0
    return ((hi - lo).days + 1) / 7.0


def _count(db: Session, model, company_id: str, *where) -> int:
    return db.scalar(select(func.count()).select_from(model).where(
        model.company_id == company_id, *where)) or 0


def checklist(db: Session, company_id: str) -> dict:
    co = db.get(Company, company_id)
    marks = dict(co.setup or {})
    n_stores = _count(db, Store, company_id, Store.active.is_(True))
    n_people = _count(db, Person, company_id) - 1          # everyone but the first Owner
    n_products = _count(db, Product, company_id, Product.status != "discontinued")
    n_suppliers = _count(db, Supplier, company_id)
    n_known = _count(db, StockOnHand, company_id, StockOnHand.known.is_(True))
    n_prices = _count(db, PriceRecord, company_id)
    weeks = _weeks_of_sales(db, company_id)
    last_run = db.scalar(select(ForecastRun).where(ForecastRun.company_id == company_id)
                         .order_by(ForecastRun.created_at.desc()).limit(1))
    ok_run = db.scalar(select(func.count()).select_from(ForecastRun).where(
        ForecastRun.company_id == company_id, ForecastRun.status == "succeeded")) or 0

    def st(key: str) -> tuple[str, str]:
        m = marks.get(key)
        if key == "company":
            return "done", co.name
        if key == "stores":
            return ("done", f"{n_stores} store{'s' * (n_stores != 1)}") if n_stores else ("todo", "")
        if key == "people":
            if n_people > 0:
                return "done", f"{n_people} invited"
            return ("done", "Nobody else yet") if m == "done" else ("todo", "")
        if key == "products":
            return ("done", f"{group_indian(n_products)} products") if n_products else ("todo", "")
        if key == "suppliers":
            if n_suppliers:
                return "done", f"{n_suppliers} supplier{'s' * (n_suppliers != 1)}"
            return ("later", "Company defaults in use") if m == "later" else ("todo", "")
        if key == "opening_stock":
            if m == "unknown" and not n_known:
                return "done", "Starting with stock unknown"
            pairs = n_products * max(n_stores, 1)
            if n_known and pairs and n_known >= pairs:
                return "done", "Stock known for every product"
            if n_known:
                return "in_progress", f"Known for {group_indian(n_known)} of {group_indian(pairs)}"
            return "todo", ""
        if key == "sales_history":
            if weeks >= 8:
                return "done", f"{int(weeks)} weeks of sales"
            if weeks > 0:
                return "in_progress", f"{int(weeks)} weeks loaded; 8 needed to start"
            if m == "none":
                return "done", "No history: new store mode"
            return "todo", ""
        if key == "prices":
            if n_prices:
                return "done", f"{group_indian(n_prices)} price records"
            return ("later", "Skipped for now") if m == "later" else ("todo", "")
        if key == "first_forecast":
            if ok_run:
                return "done", "Forecast ready"
            if last_run and last_run.status in ("queued", "running"):
                return "in_progress", "Running now"
            if last_run and last_run.status == "failed":
                return "todo", "The last attempt failed; see Model performance"
            return "todo", ""
        if key == "go_live":
            return ("done", "Live") if m == "done" else ("todo", "")
        return ("done", "") if m else ("todo", "")

    steps = []
    for i, (key, title, what, minutes, route, optional) in enumerate(STEPS, start=1):
        state, detail = st(key)
        steps.append({"n": i, "key": key, "title": title, "what": what, "minutes": minutes,
                      "route": route, "optional": optional, "state": state, "detail": detail,
                      "manual": sorted(MANUAL.get(key, set()))})
    finished = sum(1 for s in steps if s["state"] in ("done", "later"))
    return {"steps": steps, "done": finished, "total": len(steps),
            "complete": finished == len(steps)}


@router.get("")
def get_checklist(c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    return checklist(db, c.company_id)


class MarkIn(BaseModel):
    state: str                       # one of MANUAL[key], or "reopen"
    modes: dict[str, str] | None = None   # go_live: store id -> standalone | connected


@router.post("/{key}")
def mark(key: str, body: MarkIn, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    c.require("company.settings")
    if key not in KEYS:
        raise HTTPException(404, "That setup step does not exist.")
    allowed = MANUAL.get(key, set())
    if body.state != "reopen" and body.state not in allowed:
        if not allowed:
            raise HTTPException(409, "This step completes itself when its data is loaded.")
        raise HTTPException(422, f"This step can be marked: {', '.join(sorted(allowed))}.")
    co = db.get(Company, c.company_id)
    before = dict(co.setup or {})
    marks = dict(before)
    if key == "go_live" and body.state == "done":
        stores = {s.id: s for s in db.scalars(select(Store).where(
            Store.company_id == c.company_id, Store.active.is_(True)))}
        if not stores:
            raise HTTPException(409, "Add at least one store before going live.")
        for sid, mode in (body.modes or {}).items():
            if sid not in stores:
                raise HTTPException(404, "That store was not found.")
            if mode not in ("standalone", "connected"):
                raise HTTPException(422, "Mode must be standalone or connected.")
            stores[sid].mode = mode
    if body.state == "reopen":
        marks.pop(key, None)
    else:
        marks[key] = body.state if body.state != "done" else utcnow().isoformat()
    co.setup = marks
    audit.log(db, c, "setup.marked", "company", co.id, {"setup": before}, {"setup": marks})
    db.commit()
    return checklist(db, c.company_id)


@router.get("/readiness")
def readiness(c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    """What is loaded, what is missing, a data-quality score per store, and how long
    the first forecast should take (section 5 step 11)."""
    cid = c.company_id
    n_products = _count(db, Product, cid, Product.status != "discontinued")
    priced = _count(db, Product, cid, Product.status != "discontinued",
                    Product.price_paise.is_not(None), Product.cost_paise.is_not(None),
                    Product.gst_rate_bp.is_not(None))
    with_barcode = db.scalar(select(func.count(func.distinct(Barcode.product_id))).where(
        Barcode.company_id == cid)) or 0
    catalogue = priced / n_products if n_products else 0.0
    today = date.today()
    events = _count(db, CalendarEvent, cid, CalendarEvent.day >= today)
    stores_out = []
    pairs_total = 0
    for s in db.scalars(select(Store).where(Store.company_id == cid, Store.active.is_(True))
                        .order_by(Store.code)):
        weeks = _weeks_of_sales(db, cid, s.id)
        known = _count(db, StockOnHand, cid, StockOnHand.store_id == s.id,
                       StockOnHand.known.is_(True))
        pairs = db.scalar(select(func.count(func.distinct(DailySales.product_id))).where(
            DailySales.company_id == cid, DailySales.store_id == s.id)) or 0
        pairs_total += pairs
        history = min(weeks / 52.0, 1.0)
        stock = known / n_products if n_products else 0.0
        score = round(100 * (0.4 * history + 0.3 * catalogue + 0.3 * stock))
        stores_out.append({
            "id": s.id, "code": s.code, "name": s.name, "state": s.state, "mode": s.mode,
            "weeks_of_sales": round(weeks, 1), "products_with_sales": pairs,
            "stock_known": known, "score": score,
            "score_parts": {"history": round(100 * history), "catalogue": round(100 * catalogue),
                            "stock": round(100 * stock)}})
    missing = []
    if not stores_out:
        missing.append({"what": "No stores yet", "route": "#/settings/stores"})
    if not n_products:
        missing.append({"what": "No products yet", "route": "#/imports/new/products"})
    elif priced < n_products:
        missing.append({"what": f"{group_indian(n_products - priced)} products have no price, "
                                "cost or GST rate", "route": "#/imports/new/products"})
    if not pairs_total:
        missing.append({"what": "No sales history: forecasts start in new store mode",
                        "route": "#/imports/new/sales_history"})
    if not _count(db, Supplier, cid):
        missing.append({"what": "No suppliers: company defaults (2 day lead time, weekly "
                                "ordering) will be used", "route": "#/imports/new/suppliers"})
    # Section 5 step 12: about 20 to 60 minutes per 1,000 product-store pairs on 2 CPUs.
    lo = max(1, round(pairs_total / 1000 * 20))
    hi = max(2, round(pairs_total / 1000 * 60))
    return {"products": n_products, "products_priced": priced,
            "products_with_barcode": with_barcode, "upcoming_calendar_events": events,
            "stores": stores_out, "missing": missing, "product_store_pairs": pairs_total,
            "first_forecast_minutes": {"low": lo, "high": hi} if pairs_total else None,
            "score_explained": "40% sales history (a full year scores 100), 30% products "
                               "with price, cost and GST rate, 30% products with known stock."}
