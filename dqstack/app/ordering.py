"""Suggested orders (section 9 step 3e, build step 8).

    suggested = stock target - on hand - already on order
                rounded up to whole cases (and at least the supplier's minimum
                quantity for that product)
                then, per supplier, raised in whole cases until the order reaches the
                supplier's minimum order value -- with the reason shown, and the
                next order day offered as the alternative

The arithmetic is in small pure functions (`line_quantity`, `raise_to_minimum`) so
a person can check it by hand, which is the step's acceptance test.

What a manager decided on the Today screen wins: "change" sets the product's stock
target to their number, "skip" leaves it off this order, "approve" keeps the
system's target. Products whose stock is not known get no suggestion: an order on
a guess is worse than no order (section 5 step 6).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .formats import rupees
from .models import Decision, Product, Recommendation, Supplier, SupplierProduct


def line_quantity(target: int, on_hand: int, on_order: int, case: int, moq: int = 0) -> int:
    """All in the same unit (thousandths). 0 when stock already covers the target."""
    need = target - on_hand - on_order
    if need <= 0:
        return 0
    need = max(need, moq)
    case = max(case, 1)
    return math.ceil(need / case) * case


@dataclass
class Line:
    product_id: str
    sku: str
    name: str
    unit_cost_paise: int                 # per stock unit, excluding GST
    case_milli: int
    target_milli: int
    on_hand_milli: int
    on_order_milli: int
    need_milli: int                      # target - on hand - on order, before rounding
    qty_milli: int
    weekly_milli: int = 0                # forecast demand next 7 days (raise priority)
    reasons: list[str] = field(default_factory=list)
    recommendation_id: str | None = None
    unit: str = "each"

    @property
    def value_paise(self) -> int:
        return self.qty_milli * self.unit_cost_paise // 1000


def raise_to_minimum(lines: list[Line], min_paise: int) -> str | None:
    """Add whole cases until the order reaches the supplier's minimum value. Each
    case goes to the product that will run out soonest after this order (lowest
    cover), so the extra stock is the stock most likely to sell. Returns the reason
    to show, or None when the minimum is already met."""
    total = sum(l.value_paise for l in lines)
    if min_paise <= 0 or total >= min_paise or not lines:
        return None
    start = total
    candidates = [l for l in lines if l.unit_cost_paise > 0]
    if not candidates:
        return None
    while total < min_paise:
        def cover(l: Line) -> float:
            stock = l.on_hand_milli + l.on_order_milli + l.qty_milli
            return stock / max(l.weekly_milli, 1)
        pick = min(candidates, key=cover)
        pick.qty_milli += pick.case_milli
        total += pick.case_milli * pick.unit_cost_paise // 1000
        if "raised" not in " ".join(pick.reasons):
            pick.reasons.append("raised to reach the supplier minimum")
    return (f"Raised from {rupees(start, decimals=False)} to {rupees(total, decimals=False)} "
            f"to reach the {rupees(min_paise, decimals=False)} minimum order")


def next_order_day(order_days: list[int], after: date) -> date | None:
    """The first allowed order day strictly after `after`; None means any day."""
    if not order_days:
        return None
    for k in range(1, 8):
        d = after + timedelta(days=k)
        if d.weekday() in order_days:
            return d
    return None


def latest_decisions(db: Session, run_id: str) -> dict[tuple[str, str], Decision]:
    """The standing decision per store and product on this forecast: undone ones
    do not count, and a later decision replaces an earlier one."""
    out: dict[tuple[str, str], Decision] = {}
    rows = db.scalars(select(Decision).join(Recommendation, Recommendation.id ==
                                            Decision.recommendation_id).where(
        Recommendation.run_id == run_id, Decision.undone_at.is_(None))
        .order_by(Decision.created_at))
    for d in rows:
        out[(d.store_id, d.product_id)] = d
    return out


def suggestions(db: Session, company_id: str, run_id: str, store_id: str,
                today: date | None = None) -> dict:
    """One draft order per supplier for one store, from the current forecast."""
    today = today or date.today()
    recs = list(db.scalars(select(Recommendation).where(Recommendation.run_id == run_id,
                                                        Recommendation.store_id == store_id)))
    if not recs:
        return {"groups": [], "skipped": [], "stock_unknown": []}
    decided = latest_decisions(db, run_id)
    products = {p.id: p for p in db.scalars(select(Product).where(
        Product.id.in_([r.product_id for r in recs])))}
    links = {(l.supplier_id, l.product_id): l for l in db.scalars(select(SupplierProduct).where(
        SupplierProduct.company_id == company_id))}
    suppliers = {s.id: s for s in db.scalars(select(Supplier).where(
        Supplier.company_id == company_id))}
    groups: dict[str | None, list[Line]] = {}
    skipped, unknown = [], []
    for r in recs:
        p = products.get(r.product_id)
        if p is None or p.status == "discontinued":
            continue
        d = decided.get((store_id, r.product_id))
        if d is not None and d.action == "skip":
            skipped.append(p.sku)
            continue
        if not r.stock_known:
            unknown.append(p.sku)
            continue
        target = r.target_milli or 0
        reasons = []
        if d is not None and d.action == "change" and d.value_milli is not None:
            target = d.value_milli
            reasons.append(f"stock target set by a manager ({d.reason or 'changed'})")
        link = links.get((r.supplier_id, p.id)) if r.supplier_id else None
        case = (link.case_pack if link and link.case_pack else p.case_pack or 1) * 1000
        moq = (link.min_order_qty_milli or 0) if link else 0
        qty = line_quantity(target, r.on_hand_milli, r.on_order_milli, case, moq)
        if qty <= 0:
            continue
        need = target - r.on_hand_milli - r.on_order_milli
        if qty > need:
            reasons.append(f"rounded up to whole cases of {case // 1000}")
        cost = (link.cost_paise if link and link.cost_paise is not None else p.cost_paise) or 0
        groups.setdefault(r.supplier_id, []).append(Line(
            product_id=p.id, sku=p.sku, name=p.name, unit_cost_paise=cost, case_milli=case,
            target_milli=target, on_hand_milli=r.on_hand_milli, on_order_milli=r.on_order_milli,
            need_milli=need, qty_milli=qty, weekly_milli=r.week1_mid_milli, reasons=reasons,
            recommendation_id=r.id, unit=p.unit or "each"))
    out = []
    for sid, lines in groups.items():
        sup = suppliers.get(sid) if sid else None
        min_paise = sup.min_order_paise if sup else 0
        before = sum(l.value_paise for l in lines)
        raised = raise_to_minimum(lines, min_paise)
        nxt = next_order_day(sup.order_days if sup else [], today)
        alt = None
        if raised and nxt:
            alt = f"or wait and combine it with the order on {nxt:%a %d %b}"
        out.append({
            "supplier_id": sid, "supplier": sup.name if sup else "No supplier set",
            "min_order_paise": min_paise, "lead_time_days": sup.lead_time_days if sup else None,
            "can_order_today": not sup or not sup.order_days or today.weekday() in sup.order_days,
            "next_order_day": nxt.isoformat() if nxt else None,
            "value_before_minimum_paise": before,
            "total_paise": sum(l.value_paise for l in lines),
            "raised": raised, "alternative": alt,
            "lines": [{"product_id": l.product_id, "sku": l.sku, "name": l.name, "unit": l.unit,
                       "target_milli": l.target_milli, "on_hand_milli": l.on_hand_milli,
                       "on_order_milli": l.on_order_milli, "need_milli": l.need_milli,
                       "qty_milli": l.qty_milli, "case_milli": l.case_milli,
                       "unit_cost_paise": l.unit_cost_paise, "value_paise": l.value_paise,
                       "reasons": l.reasons, "recommendation_id": l.recommendation_id}
                      for l in sorted(lines, key=lambda x: x.name)]})
    out.sort(key=lambda g: (g["supplier_id"] is None, g["supplier"]))
    return {"groups": out, "skipped": sorted(skipped), "stock_unknown": sorted(unknown)}
