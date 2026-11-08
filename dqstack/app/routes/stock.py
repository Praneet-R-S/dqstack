"""The Stock screen and its actions (section 9).

    GET  /api/stock?store_id=                 the stock list, with status and filters
    GET  /api/stock/export.csv?store_id=      the same list as a spreadsheet
    GET  /api/stock/history?store_id&product_id   every movement with running balance
    GET  /api/stock/valuation                 stock value at cost by store and category
    POST /api/stock/opening                   set one product's opening stock
    POST /api/stock/adjust                    a correction with a reason (managers)
    GET  /api/stock/verify                    the ledger consistency check
"""

from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit, ledger
from .. import stock as S
from ..db import get_db, utcnow
from ..deps import ctx
from ..formats import qty as fmt_qty
from ..models import Product, StockOnHand, Store
from ..scope import Ctx, get_or_404, store_or_404

router = APIRouter(prefix="/api/stock", tags=["stock"])


def _store(db: Session, c: Ctx, store_id: str | None) -> Store:
    if store_id:
        return store_or_404(db, c, store_id)
    ids = c.visible_store_ids(db)
    if not ids:
        raise HTTPException(404, "There are no stores yet. Add a store first.")
    return db.get(Store, ids[0])


@router.get("")
def stock_list(c: Ctx = Depends(ctx), db: Session = Depends(get_db),
               store_id: str | None = None, category: str | None = None,
               supplier_id: str | None = None, status: str | None = None,
               q: str | None = Query(None, max_length=100),
               limit: int = Query(500, ge=1, le=20000), offset: int = Query(0, ge=0)):
    c.require("data.read")
    store = _store(db, c, store_id)
    if status and status not in S.STATUS_ORDER:
        raise HTTPException(422, f"Status must be one of: {', '.join(S.STATUS_ORDER)}.")
    rows = S.stock_rows(db, c.company_id, store, category=category, supplier_id=supplier_id,
                        status=status, q=q)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"store": {"id": store.id, "code": store.code, "name": store.name},
            "as_of": utcnow().isoformat(), "total": len(rows), "status_counts": counts,
            "rows": rows[offset:offset + limit]}


@router.get("/export.csv")
def export(c: Ctx = Depends(ctx), db: Session = Depends(get_db), store_id: str | None = None):
    c.require("data.read")
    store = _store(db, c, store_id)
    rows = S.stock_rows(db, c.company_id, store)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["SKU", "Name", "Category", "Unit", "On hand", "In open bills", "On order",
                "In transit", "Days of cover", "Last counted", "Value at cost (Rs)", "Status"])
    for r in rows:
        w.writerow([r["sku"], r["name"], r["category"] or "", r["unit"],
                    r["on_hand_milli"] / 1000, r["in_open_bills_milli"] / 1000,
                    r["on_order_milli"] / 1000, r["in_transit_milli"] / 1000,
                    "" if r["days_of_cover"] is None else r["days_of_cover"],
                    r["last_counted_at"] or "", f"{r['value_paise'] / 100:.2f}", r["status"]])
    return Response("﻿" + buf.getvalue(), media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="stock-{store.code}.csv"'})


@router.get("/history")
def product_history(store_id: str, product_id: str, c: Ctx = Depends(ctx),
                    db: Session = Depends(get_db), limit: int = Query(200, ge=1, le=5000)):
    c.require("data.read")
    store = store_or_404(db, c, store_id)
    p = get_or_404(db, c, Product, product_id, store_attr=None, what="That product")
    row = db.get(StockOnHand, {"store_id": store.id, "product_id": p.id})
    return {"product": {"id": p.id, "sku": p.sku, "name": p.name, "unit": p.unit},
            "store": {"id": store.id, "code": store.code, "name": store.name},
            "on_hand_milli": row.on_hand_milli if row else 0, "known": bool(row and row.known),
            "movements": S.history(db, c.company_id, store.id, p.id, limit)}


@router.get("/valuation")
def valuation(c: Ctx = Depends(ctx), db: Session = Depends(get_db), store_id: str | None = None):
    c.require("reports.view")
    ids = [store_or_404(db, c, store_id).id] if store_id else c.visible_store_ids(db)
    return S.valuation(db, c.company_id, ids)


class OpeningIn(BaseModel):
    store_id: str
    product_id: str
    qty_milli: int = Field(ge=0)
    unit_cost_paise: int | None = Field(default=None, ge=0)
    key: str | None = Field(default=None, max_length=128)


@router.post("/opening")
def set_opening(body: OpeningIn, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    store = store_or_404(db, c, body.store_id)
    c.require("stock.opening", store.id)
    p = get_or_404(db, c, Product, body.product_id, store_attr=None, what="That product")
    row = db.get(StockOnHand, {"store_id": store.id, "product_id": p.id})
    if row is not None and (row.opening_milli is not None or row.known):
        if row.opening_milli == body.qty_milli:
            return {"on_hand_milli": row.on_hand_milli, "duplicate": True}
        raise HTTPException(409, f"Opening stock for {p.name} in {store.name} is already set. "
                                 "Count the product to correct it.")
    if body.qty_milli == 0:
        row = ledger._soh(db, c.company_id, store.id, p.id)
        now = utcnow()
        ledger.mark_known(row, now)
        ledger.baselined(row, now)
        row.opening_milli = 0
        audit.log(db, c, "stock.opening", "product", p.id, after={"qty_milli": 0},
                  store_id=store.id)
        db.commit()
        return {"on_hand_milli": row.on_hand_milli, "duplicate": False}
    res = ledger.post(db, company_id=c.company_id, store_id=store.id, product_id=p.id,
                      qty_milli=body.qty_milli, type="opening", reason="Opening stock",
                      source_type="manual", person_id=c.person.id, device=c.device,
                      unit_cost_paise=body.unit_cost_paise if body.unit_cost_paise is not None
                      else p.cost_paise, idempotency_key=body.key)
    audit.log(db, c, "stock.opening", "product", p.id, after={"qty_milli": body.qty_milli},
              store_id=store.id)
    db.commit()
    return {"on_hand_milli": res.on_hand_milli, "duplicate": res.duplicate}


class AdjustIn(BaseModel):
    store_id: str
    product_id: str
    qty_milli: int                   # signed change
    reason: str = Field(min_length=3, max_length=200)
    key: str | None = Field(default=None, max_length=128)


@router.post("/adjust")
def adjust(body: AdjustIn, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    """A correction outside a count, by someone who may approve count differences.
    Posted as a count adjustment with its reason; the original movements stay."""
    store = store_or_404(db, c, body.store_id)
    c.require("stock.approve_diff", store.id)
    p = get_or_404(db, c, Product, body.product_id, store_attr=None, what="That product")
    if body.qty_milli == 0:
        raise HTTPException(422, "An adjustment must change stock.")
    if p.unit == "each" and body.qty_milli % 1000:
        raise HTTPException(422, f"{p.name} is sold each, so the change must be whole units.")
    before = ledger.on_hand(db, store.id, p.id)
    res = ledger.post(db, company_id=c.company_id, store_id=store.id, product_id=p.id,
                      qty_milli=body.qty_milli, type="count_adjustment", reason=body.reason,
                      source_type="adjustment", person_id=c.person.id, device=c.device,
                      idempotency_key=body.key, makes_known=False)
    if not res.duplicate:
        audit.log(db, c, "stock.adjusted", "product", p.id,
                  {"on_hand": fmt_qty(before, p.unit)},
                  {"on_hand": fmt_qty(res.on_hand_milli, p.unit), "reason": body.reason},
                  store_id=store.id)
    db.commit()
    return {"on_hand_milli": res.on_hand_milli, "duplicate": res.duplicate,
            "movement_id": res.movement.id}


@router.get("/verify")
def verify(c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    c.require("company.settings")
    bad = ledger.verify(db, c.company_id)
    return {"consistent": not bad, "problems": bad[:200], "count": len(bad)}
