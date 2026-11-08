"""The company calendar: festivals, holidays, closures and local events (section 5
step 9, section 13).

Reading is open to everyone in the company, filtered to what applies to the stores
they can see. Company-wide changes need company settings rights. A store manager may
add or change a local event or closure for their own stores, because the
specification's answer to "festival or local event not in calendar" is that the
manager adds it and tomorrow's forecast includes it.
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..deps import ctx
from ..models import CalendarEvent, Store
from ..scope import Ctx, get_or_404, store_or_404
from .org import STATE_NAMES

router = APIRouter(prefix="/api/calendar", tags=["calendar"])

KINDS = ("festival", "holiday", "closure", "local_event")


def event_dict(e: CalendarEvent) -> dict:
    return {"id": e.id, "name": e.name, "day": e.day.isoformat(), "kind": e.kind,
            "states": e.states, "store_ids": e.store_ids, "approximate": e.approximate,
            "source": e.source}


def applies_to(e: CalendarEvent, store: Store) -> bool:
    if e.store_ids:
        return store.id in e.store_ids
    if e.states:
        return bool(store.state) and store.state in e.states
    return True


@router.get("")
def list_events(c: Ctx = Depends(ctx), db: Session = Depends(get_db),
                start: date | None = Query(None, alias="from"),
                end: date | None = Query(None, alias="to"),
                store_id: str | None = None):
    start = start or date.today() - timedelta(days=30)
    end = end or start + timedelta(days=400)
    q = (select(CalendarEvent).where(CalendarEvent.company_id == c.company_id,
                                     CalendarEvent.day >= start, CalendarEvent.day <= end)
         .order_by(CalendarEvent.day, CalendarEvent.name))
    events = list(db.scalars(q))
    if store_id:
        store = store_or_404(db, c, store_id)
        events = [e for e in events if applies_to(e, store)]
    elif not c.all_stores:
        visible = [s for s in db.scalars(select(Store).where(Store.company_id == c.company_id))
                   if s.id in c.store_ids]
        events = [e for e in events if any(applies_to(e, s) for s in visible)]
    return [event_dict(e) for e in events]


class EventIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    day: date
    kind: str = "local_event"
    states: list[str] = Field(default_factory=list)
    store_ids: list[str] = Field(default_factory=list)


class EventPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    day: date | None = None
    kind: str | None = None
    states: list[str] | None = None
    store_ids: list[str] | None = None


def _authorise(c: Ctx, db: Session, kind: str, states: list[str], store_ids: list[str]) -> None:
    if kind not in KINDS:
        raise HTTPException(422, "Kind must be festival, holiday, closure or local_event.")
    bad = [s for s in states if s not in STATE_NAMES]
    if bad:
        raise HTTPException(422, f"'{bad[0]}' is not an Indian state or union territory.")
    for sid in store_ids:
        store_or_404(db, c, sid)           # 404 for another company's or an unseen store
    if c.can("company.settings"):
        return
    # Store-level change by a manager: only local events or closures, only for stores
    # they run, and never company-wide.
    if not store_ids or kind not in ("local_event", "closure"):
        raise HTTPException(403, "Only an Owner or Admin can change company-wide calendar "
                                 "entries. Add a local event or closure for your store instead.")
    for sid in store_ids:
        c.require("forecast.review", sid)


@router.post("", status_code=201)
def add_event(body: EventIn, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    _authorise(c, db, body.kind, body.states, body.store_ids)
    e = CalendarEvent(company_id=c.company_id, name=body.name.strip(), day=body.day,
                      kind=body.kind, states=body.states, store_ids=body.store_ids,
                      approximate=False, source="company")
    db.add(e)
    db.flush()
    audit.log(db, c, "calendar.added", "calendar_event", e.id, after=event_dict(e),
              store_id=body.store_ids[0] if len(body.store_ids) == 1 else None)
    db.commit()
    return event_dict(e)


@router.patch("/{event_id}")
def change_event(event_id: str, body: EventPatch, c: Ctx = Depends(ctx),
                 db: Session = Depends(get_db)):
    e = get_or_404(db, c, CalendarEvent, event_id, store_attr=None, what="That calendar entry")
    merged = {"kind": body.kind or e.kind,
              "states": e.states if body.states is None else body.states,
              "store_ids": e.store_ids if body.store_ids is None else body.store_ids}
    _authorise(c, db, merged["kind"], merged["states"], merged["store_ids"])
    if not c.can("company.settings"):
        _authorise(c, db, e.kind, e.states, e.store_ids)   # and over the entry as it was
    before = event_dict(e)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(e, k, v.strip() if isinstance(v, str) and k == "name" else v)
    if body.day is not None:
        e.approximate = False              # a person confirmed or corrected the date
    audit.log(db, c, "calendar.changed", "calendar_event", e.id, before, event_dict(e))
    db.commit()
    return event_dict(e)


@router.delete("/{event_id}")
def remove_event(event_id: str, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    e = get_or_404(db, c, CalendarEvent, event_id, store_attr=None, what="That calendar entry")
    _authorise(c, db, e.kind, e.states, e.store_ids)
    before = event_dict(e)
    db.delete(e)
    audit.log(db, c, "calendar.removed", "calendar_event", event_id, before, None)
    db.commit()
    return {"ok": True}
