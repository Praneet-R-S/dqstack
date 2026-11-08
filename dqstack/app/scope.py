"""Company separation and store-level access, in one place.

Every route receives a `Ctx` for the signed-in person. Reads go through `scoped()`
and single-record fetches through `get_or_404()`, both of which filter by the
person's company -- and, for records that belong to a store, by the stores the
person may see. A record from another company is therefore indistinguishable from
one that does not exist: the answer is "not found" either way, which is acceptance
scenario 17. Returning "forbidden" instead would confirm the id exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import HTTPException
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from . import permissions
from .models import AuthSession, Person, Store


@dataclass
class Ctx:
    person: Person
    session: AuthSession | None
    company_id: str
    role: str
    store_ids: set[str] = field(default_factory=set)
    all_stores: bool = False
    ip: str | None = None
    device: str | None = None

    def can(self, action: str, store_id: str | None = None) -> bool:
        return permissions.allowed(self.role, action, store_id, self.store_ids)

    def require(self, action: str, store_id: str | None = None) -> None:
        if not self.can(action, store_id):
            raise HTTPException(status_code=403, detail=_refusal(action))

    def visible_store_ids(self, db: Session) -> list[str]:
        if self.all_stores:
            return list(db.scalars(select(Store.id).where(Store.company_id == self.company_id)))
        return sorted(self.store_ids)


def _refusal(action: str) -> str:
    return ("Your role does not allow this. Ask a store manager or an administrator "
            f"if you need it done. ({action})")


def build_ctx(db: Session, person: Person, sess: AuthSession | None,
              ip: str | None = None, device: str | None = None) -> Ctx:
    if person.company_id is None:
        raise HTTPException(status_code=403, detail="Platform accounts do not belong to a company.")
    all_stores = permissions.sees_all_stores(person.role)
    store_ids = {s.id for s in person.stores if s.company_id == person.company_id}
    return Ctx(person=person, session=sess, company_id=person.company_id, role=person.role,
               store_ids=store_ids, all_stores=all_stores, ip=ip, device=device)


def scoped(ctx: Ctx, model, *, store_attr: str | None = "store_id") -> Select:
    """A SELECT already filtered to the person's company and visible stores."""
    q = select(model).where(model.company_id == ctx.company_id)
    if store_attr and hasattr(model, store_attr) and not ctx.all_stores:
        q = q.where(getattr(model, store_attr).in_(ctx.store_ids or {"__none__"}))
    return q


def get_or_404(db: Session, ctx: Ctx, model, record_id: str, *,
               store_attr: str | None = "store_id", what: str = "That record"):
    obj = db.get(model, record_id)
    if obj is None or getattr(obj, "company_id", None) != ctx.company_id:
        raise HTTPException(status_code=404, detail=f"{what} was not found.")
    if store_attr and hasattr(obj, store_attr) and not ctx.all_stores:
        sid = getattr(obj, store_attr)
        if sid is not None and sid not in ctx.store_ids:
            raise HTTPException(status_code=404, detail=f"{what} was not found.")
    return obj


def store_or_404(db: Session, ctx: Ctx, store_id: str) -> Store:
    st = db.get(Store, store_id)
    if st is None or st.company_id != ctx.company_id:
        raise HTTPException(status_code=404, detail="That store was not found.")
    if not ctx.all_stores and st.id not in ctx.store_ids:
        raise HTTPException(status_code=404, detail="That store was not found.")
    return st
