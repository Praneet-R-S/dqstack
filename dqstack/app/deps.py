"""Request dependencies: who is asking, and are they allowed to change things.

`ctx` resolves the session cookie to a person and builds their scope. For any method
that can change data it also checks the CSRF header, so a page on another site that
submits a form at this server gets refused even though the browser sends the cookie.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .scope import Ctx, build_ctx
from .security import csrf_ok, session_for

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CSRF_HEADER = "X-CSRF-Token"


def client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def device_of(request: Request) -> str | None:
    dev = request.headers.get("x-dq-device")
    ua = request.headers.get("user-agent", "")
    return (dev or ua)[:255] or None


def ctx(request: Request, db: Session = Depends(get_db)) -> Ctx:
    token = request.cookies.get(settings.cookie_name)
    found = session_for(db, token)
    if found is None:
        raise HTTPException(status_code=401, detail="Please sign in to continue.")
    sess, person = found
    if request.method not in SAFE_METHODS and not csrf_ok(sess, request.headers.get(CSRF_HEADER)):
        raise HTTPException(status_code=403,
                            detail="This request could not be verified. Reload the page and try again.")
    c = build_ctx(db, person, sess, ip=client_ip(request), device=device_of(request))
    request.state.ctx = c
    return c


def optional_ctx(request: Request, db: Session = Depends(get_db)) -> Ctx | None:
    try:
        return ctx(request, db)
    except HTTPException:
        return None
