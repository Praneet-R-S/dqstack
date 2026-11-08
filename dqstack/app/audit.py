"""The activity log: who did what, when, from which device, before and after.

Append-only. The model refuses updates and deletes (models.py), and the PostgreSQL
migration adds triggers that refuse them at the database too, so a log entry cannot
be quietly rewritten by a later bug or a direct query.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from .models import ActivityLog


def _jsonable(v: Any) -> Any:
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_jsonable(x) for x in v]
    return v


SECRET_FIELDS = {"password_hash", "manager_pin_hash", "token_hash", "key_hash", "csrf_token"}


def snapshot(obj, fields: list[str] | None = None) -> dict:
    """The fields of a record worth logging, with secrets left out."""
    if obj is None:
        return {}
    cols = fields or [c.key for c in obj.__table__.columns]
    return {c: _jsonable(getattr(obj, c, None)) for c in cols if c not in SECRET_FIELDS}


def log(db: Session, ctx, action: str, entity: str | None = None, entity_id: str | None = None,
        before: dict | None = None, after: dict | None = None,
        store_id: str | None = None) -> ActivityLog:
    entry = ActivityLog(
        company_id=getattr(ctx, "company_id", None),
        person_id=getattr(getattr(ctx, "person", None), "id", None),
        device=getattr(ctx, "device", None),
        ip=getattr(ctx, "ip", None),
        store_id=store_id, action=action, entity=entity, entity_id=entity_id,
        before=_jsonable(before) if before else None,
        after=_jsonable(after) if after else None,
    )
    db.add(entry)
    return entry
