"""The Today screen: the forecasts worth a human look, and what was decided.

    GET  /api/today?store_id=           flagged products, most money at risk first
    POST /api/decisions                 approve, change (with a reason) or skip
    POST /api/decisions/bulk-approve    approve the small items in one go
    POST /api/decisions/{id}/undo       take a decision back

A decision is a record, not an edit: undoing one marks it undone, and the next
decision replaces it. Orders read the standing decision (ordering.py).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db, utcnow
from ..deps import ctx
from ..forecast import jobs
from ..models import Decision, Product, Recommendation, Store, Supplier
from ..ordering import latest_decisions
from ..scope import Ctx, get_or_404, store_or_404
from .forecast import rec_dict, run_dict

router = APIRouter(prefix="/api", tags=["today"])

CHANGE_REASONS = ("local_event", "promotion", "supplier_issue", "new_product", "known_order",
                  "weather", "other")


@router.get("/today")
def today(store_id: str, c: Ctx = Depends(ctx), db: Session = Depends(get_db),
          include_decided: bool = True, limit: int = Query(50, ge=1, le=500)):
    store = store_or_404(db, c, store_id)
    c.require("data.read", store.id)
    cur = jobs.current_run(db, c.company_id)
    if cur is None:
        return {"run": None, "rows": [], "decided": 0, "to_review": 0}
    decided = latest_decisions(db, cur.id)
    rows = db.execute(select(Recommendation, Product).join(
        Product, Product.id == Recommendation.product_id).where(
        Recommendation.run_id == cur.id, Recommendation.store_id == store.id,
        Recommendation.flagged.is_(True)).order_by(
        Recommendation.money_at_risk_paise.desc(), Product.name).limit(limit)).all()
    sups = {s.id: s.name for s in db.scalars(select(Supplier).where(
        Supplier.company_id == c.company_id))}
    out = []
    for r, p in rows:
        d = decided.get((r.store_id, r.product_id))
        if d is not None and not include_decided:
            continue
        row = rec_dict(r, p, {store.id: store.name}, sups)
        row["decision"] = None if d is None else {
            "id": d.id, "action": d.action, "value_milli": d.value_milli, "reason": d.reason,
            "note": d.note, "at": d.created_at.isoformat() if d.created_at else None}
        out.append(row)
    n_dec = sum(1 for row in out if row["decision"])
    return {"run": run_dict(cur), "rows": out, "decided": n_dec, "to_review": len(out) - n_dec,
            "change_reasons": CHANGE_REASONS,
            "can_decide": c.can("forecast.review", store.id)}


class DecisionIn(BaseModel):
    recommendation_id: str
    action: str
    value_milli: int | None = Field(default=None, ge=0)
    reason: str | None = None
    note: str | None = Field(default=None, max_length=500)


def _decide(db: Session, c: Ctx, rec: Recommendation, body: DecisionIn) -> Decision:
    c.require("forecast.review", rec.store_id)
    if body.action not in ("approve", "change", "skip"):
        raise HTTPException(422, "Action must be approve, change or skip.")
    if body.action == "change":
        if body.value_milli is None:
            raise HTTPException(422, "Give the stock target you want instead.")
        if body.reason not in CHANGE_REASONS:
            raise HTTPException(422, "Choose a reason for the change, so the forecast can "
                                     "learn from it.")
    cur = jobs.current_run(db, c.company_id)
    if cur is None or rec.run_id != cur.id:
        raise HTTPException(409, "This forecast has been replaced by a newer one. Reload the "
                                 "page and decide on the current one.")
    d = Decision(company_id=c.company_id, recommendation_id=rec.id, store_id=rec.store_id,
                 product_id=rec.product_id, person_id=c.person.id, action=body.action,
                 value_milli=body.value_milli if body.action == "change" else None,
                 reason=body.reason if body.action == "change" else body.reason,
                 note=body.note)
    db.add(d)
    db.flush()
    audit.log(db, c, f"forecast.{body.action}", "recommendation", rec.id, after={
        "product_id": rec.product_id, "target_milli": rec.target_milli,
        "value_milli": d.value_milli, "reason": d.reason, "note": d.note},
        store_id=rec.store_id)
    return d


@router.post("/decisions", status_code=201)
def decide(body: DecisionIn, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    rec = get_or_404(db, c, Recommendation, body.recommendation_id, what="That forecast")
    d = _decide(db, c, rec, body)
    db.commit()
    return {"id": d.id, "action": d.action, "value_milli": d.value_milli,
            "undo_seconds": 10}


class BulkIn(BaseModel):
    store_id: str
    max_value_paise: int = Field(default=50_000, ge=0)   # "small items"


@router.post("/decisions/bulk-approve")
def bulk_approve(body: BulkIn, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    store = store_or_404(db, c, body.store_id)
    c.require("forecast.review", store.id)
    cur = jobs.current_run(db, c.company_id)
    if cur is None:
        raise HTTPException(409, "There is no forecast to approve yet.")
    decided = latest_decisions(db, cur.id)
    n = 0
    for r in db.scalars(select(Recommendation).where(
            Recommendation.run_id == cur.id, Recommendation.store_id == store.id,
            Recommendation.flagged.is_(True),
            Recommendation.money_at_risk_paise <= body.max_value_paise)):
        if (r.store_id, r.product_id) in decided:
            continue
        _decide(db, c, r, DecisionIn(recommendation_id=r.id, action="approve"))
        n += 1
    db.commit()
    return {"approved": n}


@router.post("/decisions/{decision_id}/undo")
def undo(decision_id: str, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    d = get_or_404(db, c, Decision, decision_id, what="That decision")
    c.require("forecast.review", d.store_id)
    if d.undone_at is not None:
        return {"ok": True}
    d.undone_at = utcnow()
    audit.log(db, c, "forecast.undone", "decision", d.id, store_id=d.store_id)
    db.commit()
    return {"ok": True}
