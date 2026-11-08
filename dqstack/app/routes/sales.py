"""The Sales screen and Day close (build step 6).

    GET  /api/sales?store_id&from&to       daily sales by product and day
    GET  /api/sales/days?store_id&days     totals per day, with close status
    PUT  /api/sales/entry                   type a day's totals (standalone stores)
    GET  /api/day-close?store_id&day        what still needs doing before closing
    POST /api/day-close                     close the day
    GET  /api/day-close/history?store_id    past closes, automatic ones flagged
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit
from .. import sales as S
from ..db import get_db
from ..deps import ctx
from ..models import DailySales, DayClose, Person, Product, Store
from ..scope import Ctx, get_or_404, store_or_404

router = APIRouter(prefix="/api", tags=["sales"])


@router.get("/sales")
def sales_grid(store_id: str, c: Ctx = Depends(ctx), db: Session = Depends(get_db),
               start: date | None = Query(None, alias="from"),
               end: date | None = Query(None, alias="to"),
               product_id: str | None = None, q: str | None = Query(None, max_length=100)):
    store = store_or_404(db, c, store_id)
    c.require("data.read", store.id)
    tz = S.tz_of(db, c.company_id)
    end = end or S.local_today(tz)
    start = start or end - timedelta(days=13)
    if (end - start).days > 400:
        raise HTTPException(422, "Choose at most 400 days at a time.")
    qy = select(DailySales, Product).join(Product, Product.id == DailySales.product_id).where(
        DailySales.store_id == store.id, DailySales.day >= start, DailySales.day <= end)
    if product_id:
        qy = qy.where(DailySales.product_id == product_id)
    if q:
        like = f"%{q.strip()}%"
        qy = qy.where((Product.name.ilike(like)) | (Product.sku.ilike(like)))
    rows: dict = {}
    per_day: dict = defaultdict(lambda: {"units_milli": 0, "revenue_paise": 0})
    for ds, p in db.execute(qy):
        r = rows.setdefault(p.id, {"product_id": p.id, "sku": p.sku, "name": p.name,
                                   "unit": p.unit, "units_milli": 0, "revenue_paise": 0,
                                   "days": {}})
        r["days"][ds.day.isoformat()] = {"units_milli": ds.units_milli,
                                         "revenue_paise": ds.revenue_paise,
                                         "out_of_stock": ds.out_of_stock,
                                         "oos_source": ds.oos_source, "source": ds.source}
        r["units_milli"] += ds.units_milli
        r["revenue_paise"] += ds.revenue_paise
        t = per_day[ds.day.isoformat()]
        t["units_milli"] += ds.units_milli
        t["revenue_paise"] += ds.revenue_paise
    days = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
    return {"store": {"id": store.id, "code": store.code, "name": store.name,
                      "mode": store.mode},
            "from": start.isoformat(), "to": end.isoformat(), "days": days,
            "totals": {d: per_day.get(d, {"units_milli": 0, "revenue_paise": 0}) for d in days},
            "rows": sorted(rows.values(), key=lambda r: -r["revenue_paise"])}


@router.get("/sales/days")
def sales_days(store_id: str, c: Ctx = Depends(ctx), db: Session = Depends(get_db),
               days: int = Query(14, ge=1, le=120)):
    store = store_or_404(db, c, store_id)
    c.require("data.read", store.id)
    tz = S.tz_of(db, c.company_id)
    end = S.local_today(tz)
    start = end - timedelta(days=days - 1)
    totals = {d: (u, r, n) for d, u, r, n in db.execute(
        select(DailySales.day, func.sum(DailySales.units_milli), func.sum(DailySales.revenue_paise),
               func.count()).where(DailySales.store_id == store.id, DailySales.day >= start,
                                   DailySales.day <= end,
                                   DailySales.source != "ledger").group_by(DailySales.day))}
    closes = {dc.day: dc for dc in db.scalars(select(DayClose).where(
        DayClose.store_id == store.id, DayClose.day >= start, DayClose.day <= end))}
    out = []
    for i in range(days):
        d = end - timedelta(days=i)
        u, r, n = totals.get(d, (0, 0, 0))
        dc = closes.get(d)
        out.append({"day": d.isoformat(), "units_milli": int(u or 0), "revenue_paise": int(r or 0),
                    "products": n, "closed": dc is not None,
                    "closed_automatically": bool(dc and dc.automatic),
                    "late_data": bool(dc and dc.late_data_at)})
    return out


class EntryLine(BaseModel):
    product_id: str
    units_milli: int = Field(ge=0)
    revenue_paise: int | None = Field(default=None, ge=0)


class EntryIn(BaseModel):
    store_id: str
    day: date
    lines: list[EntryLine] = Field(min_length=1, max_length=20000)


@router.put("/sales/entry")
def sales_entry(body: EntryIn, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    """The Sales entry grid: a standalone store types the day's totals. Each product
    entered replaces that product's total for the day; others are left alone."""
    store = store_or_404(db, c, body.store_id)
    c.require("sales.enter", store.id)
    if store.mode != "standalone":
        raise HTTPException(409, f"{store.name} is connected: its sales come from the tills.")
    if body.day > S.local_today(S.tz_of(db, c.company_id)):
        raise HTTPException(422, "That date is in the future.")
    totals = {}
    for ln in body.lines:
        p = get_or_404(db, c, Product, ln.product_id, store_attr=None, what="A product")
        if p.unit == "each" and ln.units_milli % 1000:
            raise HTTPException(422, f"{p.name} is sold in whole units.")
        if p.id in totals:
            raise HTTPException(422, f"{p.name} is entered twice.")
        rev = ln.revenue_paise if ln.revenue_paise is not None else \
            ln.units_milli * (p.price_paise or 0) // 1000
        totals[p.id] = (ln.units_milli, rev)
    out = S.apply_day_totals(db, c, store, body.day, totals, replace_day=False, source="entry",
                             post_stock=True)
    audit.log(db, c, "sales.entered", "store", store.id, after={
        "day": body.day.isoformat(), "products": len(totals), **out}, store_id=store.id)
    db.commit()
    return {"day": body.day.isoformat(), **out}


# ===================================================================== day close

def _day(db: Session, c: Ctx, day: date | None) -> date:
    return day or S.local_today(S.tz_of(db, c.company_id))


@router.get("/day-close")
def day_close_status(store_id: str, c: Ctx = Depends(ctx), db: Session = Depends(get_db),
                     day: date | None = None):
    store = store_or_404(db, c, store_id)
    c.require("data.read", store.id)
    return S.day_close_status(db, c, store, _day(db, c, day))


class CloseIn(BaseModel):
    store_id: str
    day: date | None = None
    cash_counted_paise: int | None = Field(default=None, ge=0)
    void_open_bills: bool = False
    no_sales: bool = False
    store_was_closed: bool = False
    note: str | None = Field(default=None, max_length=300)


@router.post("/day-close", status_code=201)
def close_day(body: CloseIn, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    store = store_or_404(db, c, body.store_id)
    c.require("day.close", store.id)
    d = _day(db, c, body.day)
    if d > S.local_today(S.tz_of(db, c.company_id)):
        raise HTTPException(422, "A day cannot be closed before it starts.")
    S.close_day(db, c, store, d, cash_counted_paise=body.cash_counted_paise,
                void_open_bills=body.void_open_bills, no_sales=body.no_sales,
                store_was_closed=body.store_was_closed, note=body.note)
    db.commit()
    return S.day_close_status(db, c, store, d)


@router.get("/day-close/history")
def history(store_id: str, c: Ctx = Depends(ctx), db: Session = Depends(get_db),
            days: int = Query(30, ge=1, le=366)):
    store = store_or_404(db, c, store_id)
    c.require("data.read", store.id)
    since = S.local_today(S.tz_of(db, c.company_id)) - timedelta(days=days)
    names = {p.id: p.name for p in db.scalars(select(Person).where(
        Person.company_id == c.company_id))}
    return [{"day": dc.day.isoformat(), "automatic": dc.automatic,
             "closed_by": names.get(dc.closed_by), "store_was_closed": dc.store_was_closed,
             "cash_expected_paise": dc.cash_expected_paise,
             "cash_counted_paise": dc.cash_counted_paise,
             "late_data": dc.late_data_at is not None, "note": dc.note, "report": dc.report}
            for dc in db.scalars(select(DayClose).where(DayClose.store_id == store.id,
                                                        DayClose.day >= since)
                                 .order_by(DayClose.day.desc()))]
