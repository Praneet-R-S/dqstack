"""The inventory ledger (section 9): the one place stock changes.

    on hand = opening + received + customer returns + transfers in
              - sold - transfers out - wastage - supplier returns +/- count adjustments

Every change to stock is a movement posted through `post()`, which writes the
movement and updates the store's stock-on-hand row in the same transaction. Nothing
else writes `StockOnHand.on_hand_milli`. Movements are never edited or deleted, so
the running total can always be rebuilt from them, and `verify()` does exactly that.

Quantities are integer thousandths and costs integer paise, so the sum is exact
rather than approximately right (see models.py).

Repeats are harmless: a movement carrying an idempotency key that has been seen
before returns the original movement and changes nothing. That is what makes an
offline device's resend, a double press or a network retry safe (principle 5).

On PostgreSQL the stock row is locked for the length of the transaction, so two
tills selling the same product at the same moment cannot lose an update.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import new_id, utcnow
from .models import Product, StockMovement, StockOnHand

# type -> required sign of the quantity: +1 in, -1 out, 0 either way
TYPES: dict[str, int] = {
    "opening": 0,             # positive normally; negative only to reverse an opening
    "sale": -1,
    "customer_return": +1,
    "delivery": +1,           # delivery received
    "supplier_return": -1,
    "transfer_out": -1,
    "transfer_in": +1,
    "count_adjustment": 0,
    "wastage": -1,
    "damage": -1,
}
LABELS = {
    "opening": "Opening stock", "sale": "Sale", "customer_return": "Customer return",
    "delivery": "Delivery received", "supplier_return": "Returned to supplier",
    "transfer_out": "Transfer out", "transfer_in": "Transfer in",
    "count_adjustment": "Count adjustment", "wastage": "Wastage", "damage": "Damage",
}
# Movements that bring stock in at a known cost move the average cost.
COSTED_IN = {"opening", "delivery", "transfer_in"}


class LedgerError(ValueError):
    """A movement that breaks a ledger rule. The message is safe to show."""


@dataclass
class Posted:
    movement: StockMovement
    duplicate: bool          # True when the idempotency key had been seen before
    on_hand_milli: int


def _pg(db: Session) -> bool:
    return db.get_bind().dialect.name == "postgresql"


def _soh(db: Session, company_id: str, store_id: str, product_id: str) -> StockOnHand:
    q = select(StockOnHand).where(StockOnHand.store_id == store_id,
                                  StockOnHand.product_id == product_id)
    pg = _pg(db)
    if pg:
        q = q.with_for_update()
    row = db.scalar(q)
    if row is None:
        row = StockOnHand(company_id=company_id, store_id=store_id, product_id=product_id,
                          on_hand_milli=0, known=False)
        db.add(row)
        if pg:
            # SQLite serialises writers, so only PostgreSQL can race here.
            try:
                with db.begin_nested():
                    db.flush([row])
            except IntegrityError:
                db.expunge(row)
                row = db.scalar(q)        # another transaction created it; lock theirs
        else:
            db.flush([row])
    return row


def post(db: Session, *, company_id: str, store_id: str, product_id: str, qty_milli: int,
         type: str, reason: str | None = None, source_type: str | None = None,
         source_id: str | None = None, person_id: str | None = None,
         device: str | None = None, unit_cost_paise: int | None = None,
         batch: str | None = None, expiry: date | None = None,
         idempotency_key: str | None = None, at: datetime | None = None,
         makes_known: bool | None = None) -> Posted:
    """Post one movement and update stock on hand. The caller commits."""
    if type not in TYPES:
        raise LedgerError(f"'{type}' is not a kind of stock movement.")
    if not isinstance(qty_milli, int):
        raise LedgerError("Quantities are whole thousandths.")
    sign = TYPES[type]
    if qty_milli == 0 and type != "count_adjustment":
        raise LedgerError("A movement must change stock.")
    correction = type == "sale" and source_type == "sales_correction"
    if (sign > 0 and qty_milli < 0 or sign < 0 and qty_milli > 0) and not correction:
        # The one exception: a corrected sales file that says fewer were sold than an
        # earlier file for the same day gives the difference back as a sale correction.
        direction = "add to" if sign > 0 else "take from"
        raise LedgerError(f"{LABELS[type]} can only {direction} stock.")
    if type == "opening" and qty_milli < 0 and source_type != "import_undo":
        raise LedgerError("Opening stock cannot be negative.")
    if idempotency_key:
        seen = db.scalar(select(StockMovement).where(
            StockMovement.company_id == company_id,
            StockMovement.idempotency_key == idempotency_key))
        if seen is not None:
            row = _soh(db, company_id, seen.store_id, seen.product_id)
            return Posted(seen, True, row.on_hand_milli)

    row = _soh(db, company_id, store_id, product_id)
    now = at or utcnow()
    if unit_cost_paise is None and (type in COSTED_IN or row.avg_cost_paise is None):
        # No cost given and no average yet: the catalogue cost is the best estimate, so
        # write-offs and transfers of stock that arrived without a cost are not valued
        # at zero.
        product = db.get(Product, product_id)
        catalogue = product.cost_paise if product is not None else None
        if type in COSTED_IN:
            unit_cost_paise = catalogue
        elif row.avg_cost_paise is None:
            row.avg_cost_paise = catalogue
    if type in COSTED_IN and unit_cost_paise is not None and qty_milli > 0:
        held = max(row.on_hand_milli, 0)
        if held > 0 and row.avg_cost_paise is not None:
            row.avg_cost_paise = round((held * row.avg_cost_paise + qty_milli * unit_cost_paise)
                                       / (held + qty_milli))
        else:
            row.avg_cost_paise = unit_cost_paise
    elif unit_cost_paise is None:
        unit_cost_paise = row.avg_cost_paise         # outbound movements are valued at cost

    mv = StockMovement(id=new_id(), company_id=company_id, store_id=store_id,
                       product_id=product_id, qty_milli=qty_milli, type=type, reason=reason,
                       source_type=source_type, source_id=source_id, person_id=person_id,
                       device=device, unit_cost_paise=unit_cost_paise, batch=batch,
                       expiry=expiry, idempotency_key=idempotency_key, at=now)
    if idempotency_key and _pg(db):
        db.add(mv)
        try:
            with db.begin_nested():
                db.flush([mv])
        except IntegrityError:
            # The same key arrived on another connection between our check and insert.
            seen = db.scalar(select(StockMovement).where(
                StockMovement.company_id == company_id,
                StockMovement.idempotency_key == idempotency_key))
            return Posted(seen, True, row.on_hand_milli)
    elif idempotency_key:
        # Flushed now so a repeat of this key later in the same transaction is seen by
        # the check above (the session does not autoflush).
        db.add(mv)
        db.flush([mv])
    else:
        db.add(mv)

    row.on_hand_milli += qty_milli
    row.last_movement_at = now
    if type == "sale":
        row.last_sale_at = now
    if type == "opening":
        row.opening_milli = None if source_type == "import_undo" else \
            (row.opening_milli or 0) + qty_milli
    if makes_known is True or (makes_known is None and type in ("opening", "count_adjustment")
                               and source_type != "import_undo"):
        row.known = True
    if type == "opening" and source_type == "import_undo":
        others = db.scalar(select(func.count()).select_from(StockMovement).where(
            StockMovement.store_id == store_id, StockMovement.product_id == product_id,
            StockMovement.type != "opening"))
        if not others:
            row.known = False
    return Posted(mv, False, row.on_hand_milli)


def on_hand(db: Session, store_id: str, product_id: str) -> int:
    v = db.scalar(select(StockOnHand.on_hand_milli).where(
        StockOnHand.store_id == store_id, StockOnHand.product_id == product_id))
    return v or 0


def verify(db: Session, company_id: str) -> list[dict]:
    """Every store-product whose stock on hand differs from the sum of its movements.
    Empty means the ledger is consistent. Run nightly and by the tests."""
    sums = dict(((s, p), q) for s, p, q in db.execute(
        select(StockMovement.store_id, StockMovement.product_id,
               func.coalesce(func.sum(StockMovement.qty_milli), 0))
        .where(StockMovement.company_id == company_id)
        .group_by(StockMovement.store_id, StockMovement.product_id)))
    bad = []
    seen = set()
    for row in db.scalars(select(StockOnHand).where(StockOnHand.company_id == company_id)):
        key = (row.store_id, row.product_id)
        seen.add(key)
        expected = int(sums.get(key, 0))
        if row.on_hand_milli != expected:
            bad.append({"store_id": row.store_id, "product_id": row.product_id,
                        "on_hand_milli": row.on_hand_milli, "movements_milli": expected})
    for key, q in sums.items():
        if key not in seen and q:
            bad.append({"store_id": key[0], "product_id": key[1], "on_hand_milli": 0,
                        "movements_milli": int(q)})
    return bad
