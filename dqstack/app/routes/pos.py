"""The connector for an existing billing system (section 14, build step 10).

A POS sends one message per completed sale, one per refund and one per void, each
with its own id. A repeated id is recognised and ignored, so a POS may resend its
queue as often as it likes. Only completed, paid sales are accepted (the paid-bill
rule); voids and refunds arrive as their own messages.

    Authorization: Bearer dqk_...          an API key made under Settings > Integrations

    POST /api/pos/v1/messages              up to 500 messages per request
    POST /api/pos/v1/files                 the nightly file drop, for systems that
                                           cannot call an API (CSV, one row per line)
    GET  /api/pos/v1/ping                  check the key works

    POST /api/integrations/api-keys        make a key (shown once)
    GET  /api/integrations/api-keys
    POST /api/integrations/api-keys/{id}/revoke

A message:

    {"id": "pos-7781-000123", "type": "sale", "store_code": "PUN1",
     "bill_number": "000123", "occurred_at": "2026-11-09T18:42:10+05:30",
     "lines": [{"sku": "RICE-BAS-5KG", "qty": 2, "price": 849.00, "discount": 0}],
     "payment_method": "upi"}

    refund: "original_bill_number" and lines with "resaleable"
    void:   "original_bill_number"; a paid bill is reversed in full
"""

from __future__ import annotations

import csv
import hashlib
import io
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import audit
from .. import checkout as K
from ..db import get_db, utcnow
from ..deps import ctx
from ..models import ApiKey, Barcode, Bill, PosMessage, Product, Store
from ..ratelimit import limiter
from ..scope import Ctx, get_or_404, store_or_404
from ..security import new_api_key

router = APIRouter(prefix="/api", tags=["pos"])


# ===================================================================== keys

class KeyIn(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    store_id: str | None = None


@router.post("/integrations/api-keys", status_code=201)
def make_key(body: KeyIn, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    c.require("integrations.manage")
    if body.store_id:
        store_or_404(db, c, body.store_id)
    raw, prefix = new_api_key()
    k = ApiKey(company_id=c.company_id, name=body.name.strip(), prefix=prefix,
               key_hash=hashlib.sha256(raw.encode()).hexdigest(), store_id=body.store_id)
    db.add(k)
    db.flush()
    audit.log(db, c, "integration.key_created", "api_key", k.id,
              after={"name": k.name, "prefix": prefix, "store_id": body.store_id})
    db.commit()
    return {"id": k.id, "name": k.name, "prefix": prefix, "store_id": k.store_id, "key": raw,
            "note": "Copy this key now. It is not shown again."}


@router.get("/integrations/api-keys")
def list_keys(c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    c.require("integrations.manage")
    return [{"id": k.id, "name": k.name, "prefix": k.prefix, "store_id": k.store_id,
             "revoked": k.revoked_at is not None,
             "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None}
            for k in db.scalars(select(ApiKey).where(ApiKey.company_id == c.company_id)
                                .order_by(ApiKey.created_at))]


@router.post("/integrations/api-keys/{key_id}/revoke")
def revoke_key(key_id: str, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    c.require("integrations.manage")
    k = get_or_404(db, c, ApiKey, key_id, store_attr=None, what="That key")
    k.revoked_at = k.revoked_at or utcnow()
    audit.log(db, c, "integration.key_revoked", "api_key", k.id)
    db.commit()
    return {"ok": True}


_TOUCHED: dict[str, float] = {}


def _touch(key_id: str) -> None:
    """Note when a key was last used, at most once a minute and in its own short
    transaction. Writing it inside the request's transaction would lock the key's row
    until the bill is saved, so every request on one key would wait for the one before.
    This process also remembers when it last wrote, so a busy till does not open a
    second connection on every request only to find the minute has not passed."""
    import time

    from sqlalchemy import or_, update

    from .. import db as dbmod
    mono = time.monotonic()
    if mono - _TOUCHED.get(key_id, -1e9) < 60:
        return
    _TOUCHED[key_id] = mono
    now = utcnow()
    with dbmod.SessionLocal() as s:
        s.execute(update(ApiKey).where(ApiKey.id == key_id, or_(
            ApiKey.last_used_at.is_(None), ApiKey.last_used_at < now - timedelta(minutes=1)))
            .values(last_used_at=now))
        s.commit()


def pos_ctx(request: Request, db: Session = Depends(get_db),
            authorization: str | None = Header(default=None),
            x_api_key: str | None = Header(default=None)):
    raw = x_api_key or (authorization[7:] if authorization and
                        authorization.lower().startswith("bearer ") else None)
    if not raw:
        raise HTTPException(401, "Send the API key in an Authorization: Bearer header.")
    k = db.scalar(select(ApiKey).where(ApiKey.key_hash == hashlib.sha256(raw.encode()).hexdigest()))
    if k is None or k.revoked_at is not None:
        raise HTTPException(401, "That API key is not valid, or has been revoked.")
    limiter.check(f"pos:{k.id}", limit=600, window_s=60)
    _touch(k.id)
    return key_ctx(k, f"pos:{k.prefix}", request.client.host if request.client else None)


def key_ctx(k: ApiKey, device: str, ip: str | None = None) -> SimpleNamespace:
    """Who is acting when a key sends: the key itself, for its own company only. Also
    used by the SFTP file drop (filedrop.py), which acts with the folder's key."""
    return SimpleNamespace(company_id=k.company_id, key=k, role="admin",
                           person=SimpleNamespace(id=None, name=f"POS: {k.name}"),
                           device=device, ip=ip, can=lambda *a, **kw: True)


# ===================================================================== messages

def _money(v) -> int:
    return int((Decimal(str(v)) * 100).quantize(Decimal("1")))


def _qty(v) -> int:
    return int((Decimal(str(v)) * 1000).quantize(Decimal("1")))


def _lines(db: Session, company_id: str, lines: list[dict]) -> list[dict]:
    out = []
    for ln in lines:
        code = str(ln.get("sku") or ln.get("barcode") or "").strip()
        p = db.scalar(select(Product).where(Product.company_id == company_id,
                                            func.upper(Product.sku) == code.upper()))
        per = 1
        if p is None and code:
            b = db.scalar(select(Barcode).where(Barcode.company_id == company_id,
                                                Barcode.code == code))
            if b is not None:
                p, per = db.get(Product, b.product_id), b.units_per_scan
        qty = _qty(ln.get("qty", 1)) * per
        price = _money(ln["price"]) // per if "price" in ln else None
        item = {"qty_milli": qty, "unit_price_paise": price,
                "discount_paise": _money(ln.get("discount", 0))}
        if p is not None:
            item["product_id"] = p.id
        else:
            item.update({"barcode": code or None,
                         "description": ln.get("name") or f"Item {code or 'without a code'}"})
        out.append(item)
    return out


def process(db: Session, kc, msg: dict) -> dict:
    mid = str(msg.get("id") or "").strip()
    kind = msg.get("type")
    if not mid or kind not in ("sale", "refund", "void"):
        return {"status": "refused", "error": "Each message needs an id and a type of sale, "
                                              "refund or void."}
    seen = db.scalar(select(PosMessage).where(PosMessage.company_id == kc.company_id,
                                              PosMessage.external_id == mid))
    if seen is not None:
        return {"id": mid, "status": "duplicate", "result": seen.result}
    store = db.scalar(select(Store).where(Store.company_id == kc.company_id,
                                          func.upper(Store.code) == str(msg.get("store_code", "")).upper()))
    if store is None or (kc.key.store_id and store.id != kc.key.store_id):
        return {"id": mid, "status": "refused", "error": "That store code is not one this key "
                                                          "may send for."}
    if store.mode != "connected":
        return {"id": mid, "status": "refused", "error": f"{store.code} is set to standalone; "
                                                          "switch it to connected first."}
    try:
        if kind == "sale":
            lines = _lines(db, kc.company_id, msg.get("lines") or [])
            total = sum(l["qty_milli"] * (l["unit_price_paise"] or 0) // 1000 - l["discount_paise"]
                        for l in lines)
            method = msg.get("payment_method") or "cash"
            bill, dup = K.pay(db, kc, store, {
                "bill_number": str(msg.get("bill_number") or mid), "device_id": "pos",
                "paid_at": msg.get("occurred_at"), "lines": lines,
                "payments": [{"method": method if method in K.METHODS else "card",
                              "amount_paise": total}]}, trusted=True)
            result = {"bill_id": bill.id, "duplicate_bill": dup}
        elif kind == "refund":
            lines = []
            for ln in _lines(db, kc.company_id, msg.get("lines") or []):
                if "product_id" in ln:
                    lines.append({"product_id": ln["product_id"], "qty_milli": ln["qty_milli"],
                                  "resaleable": True})
            src = msg.get("lines") or []
            for i, ln in enumerate(lines):
                if i < len(src) and src[i].get("resaleable") is False:
                    ln["resaleable"] = False
            bill, dup = K.refund(db, kc, store, {
                "refund_number": mid, "original_bill_number": str(msg.get("original_bill_number")),
                "lines": lines, "reason": msg.get("reason") or "Refund at the POS",
                "method": msg.get("payment_method") or "cash"}, require_pin=False)
            result = {"bill_id": bill.id}
        else:
            number = str(msg.get("original_bill_number") or msg.get("bill_number") or "")
            orig = db.scalar(select(Bill).where(Bill.store_id == store.id,
                                                Bill.bill_number == number))
            if orig is not None and orig.state == "paid":
                done: dict[str, int] = {}
                for r in db.scalars(select(Bill).where(Bill.refund_of == orig.id)):
                    for l in r.lines:
                        done[l.product_id] = done.get(l.product_id, 0) + l.qty_milli
                left: dict[str, int] = {}
                for l in orig.lines:
                    if l.product_id:
                        left[l.product_id] = left.get(l.product_id, 0) + l.qty_milli
                lines = [{"product_id": pid, "qty_milli": q - done.get(pid, 0), "resaleable": True}
                         for pid, q in left.items() if q - done.get(pid, 0) > 0]
                bill, _ = K.refund(db, kc, store, {
                    "refund_number": mid, "original_bill_number": number, "lines": lines,
                    "reason": msg.get("reason") or "Voided at the POS"}, require_pin=False)
                result = {"bill_id": bill.id, "reversed": True}
            else:
                bill = K.void_by_number(db, kc, store, number or mid,
                                        msg.get("reason") or "Voided at the POS", [])
                result = {"bill_id": bill.id, "reversed": False}
        db.add(PosMessage(company_id=kc.company_id, external_id=mid, kind=kind,
                          payload=msg, result=result))
        db.commit()
        return {"id": mid, "status": "accepted", "result": result}
    except HTTPException as e:
        db.rollback()
        return {"id": mid, "status": "refused", "error": e.detail}
    except IntegrityError:
        db.rollback()               # the same id arrived on another connection just now
        return {"id": mid, "status": "duplicate"}


class MessagesIn(BaseModel):
    messages: list[dict] = Field(max_length=500)


@router.get("/pos/v1/ping")
def ping(kc=Depends(pos_ctx), db: Session = Depends(get_db)):
    db.commit()
    return {"ok": True, "key": kc.key.prefix, "store_id": kc.key.store_id}


@router.post("/pos/v1/messages")
def messages(body: MessagesIn, kc=Depends(pos_ctx), db: Session = Depends(get_db)):
    results = [process(db, kc, m) for m in body.messages]
    db.commit()
    return {"results": results,
            "accepted": sum(r["status"] == "accepted" for r in results),
            "duplicates": sum(r["status"] == "duplicate" for r in results),
            "refused": sum(r["status"] == "refused" for r in results)}


FILE_LIMIT = 25 * 1024 * 1024


@router.post("/pos/v1/files")
async def file_drop(file: UploadFile = File(...), kc=Depends(pos_ctx),
                    db: Session = Depends(get_db)):
    """The nightly file: CSV with columns type, store_code, bill_number, occurred_at,
    sku, qty, price, discount, payment_method, original_bill_number. Rows of one bill
    are one message, identified by type, store and bill number, so a file sent twice
    changes nothing."""
    data = await file.read(FILE_LIMIT)
    # The same check as the SFTP drop: without a store and a bill number per row, every
    # row of a store would merge into one nameless bill.
    from ..filedrop import _header_problem
    problem = _header_problem(data)
    if problem:
        raise HTTPException(422, problem)
    return process_file(db, kc, data)


def process_file(db: Session, kc, data: bytes) -> dict:
    """One nightly file, by the API or from the SFTP drop (filedrop.py): parsed into
    bills, each recorded through `process` with its repeat protection. Commits."""
    text = data.decode("utf-8-sig", errors="replace")
    bills: dict[tuple, dict] = {}
    for row in csv.DictReader(io.StringIO(text)):
        row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        kind = row.get("type") or "sale"
        key = (kind, row.get("store_code", "").upper(), row.get("bill_number", ""))
        m = bills.setdefault(key, {"id": f"file:{kind}:{key[1]}:{key[2]}", "type": kind,
                                   "store_code": key[1], "bill_number": key[2],
                                   "occurred_at": row.get("occurred_at") or None,
                                   "payment_method": row.get("payment_method") or "cash",
                                   "original_bill_number": row.get("original_bill_number") or None,
                                   "lines": []})
        if row.get("sku"):
            m["lines"].append({"sku": row["sku"], "qty": row.get("qty") or "1",
                               "price": row.get("price") or "0",
                               "discount": row.get("discount") or "0"})
    results = [process(db, kc, m) for m in bills.values()]
    db.commit()
    return {"bills": len(results), "accepted": sum(r["status"] == "accepted" for r in results),
            "duplicates": sum(r["status"] == "duplicate" for r in results),
            "refused": [r for r in results if r["status"] == "refused"][:100]}
