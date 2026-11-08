"""Business rules: sensible defaults the Owner can change (section 5, step 10).

A company stores only the values it has changed; everything else falls back to
these. That way a default improved in a later release reaches every company that
never touched it, instead of each company being frozen at whatever the default was
on the day it signed up.
"""

from __future__ import annotations

DEFAULTS: dict = {
    # Service level bounds applied to every product's computed target (decide.py).
    "service_level_floor": 0.50,
    "service_level_ceiling": 0.995,
    # Daily holding cost as a fraction of unit cost.
    "holding_rate_per_day": 0.0022,
    # Wastage policy: perishables over this many days of cover are flagged.
    "perishable_cover_days": 7,
    # Cycle counting intervals in days, by value class (section 9).
    "count_interval_days": {"A": 7, "B": 28, "C": 91},
    # Count differences at or below both limits post automatically; larger ones wait
    # for a manager (section 9: "for example 5 units or Rs 500").
    "count_auto_post_units": 5,
    "count_auto_post_paise": 50_000,
    # Supplier defaults until suppliers are loaded (section 5, step 5).
    "default_lead_time_days": 2,
    "default_review_period_days": 7,
    # Alerts.
    "overstock_cover_days": 60,
    "expiry_warning_days": 3,
    "dead_stock_days": 60,
    # Day close: closes automatically at this local time if nobody does it.
    "auto_close_time": "23:59",
}


def effective(company_rules: dict | None) -> dict:
    out = {**DEFAULTS}
    for k, v in (company_rules or {}).items():
        if k in DEFAULTS:
            if isinstance(DEFAULTS[k], dict) and isinstance(v, dict):
                out[k] = {**DEFAULTS[k], **v}
            else:
                out[k] = v
    return out


def validate(changes: dict) -> list[str]:
    """Plain-language problems with a proposed change, empty when acceptable."""
    problems = []
    for k, v in changes.items():
        if k not in DEFAULTS:
            problems.append(f"'{k}' is not a business rule.")
            continue
        if k in ("service_level_floor", "service_level_ceiling"):
            if not isinstance(v, (int, float)) or not 0.5 <= v <= 0.999:
                problems.append("Service levels must be between 50% and 99.9%.")
        elif k == "count_interval_days":
            if not isinstance(v, dict) or not all(
                    isinstance(v.get(c, 1), int) and v.get(c, 1) >= 1 for c in "ABC"):
                problems.append("Count intervals must be whole numbers of days, 1 or more.")
        elif isinstance(DEFAULTS[k], int) and not isinstance(DEFAULTS[k], bool):
            if not isinstance(v, int) or v < 0:
                problems.append(f"'{k}' must be a whole number, 0 or more.")
    floor = changes.get("service_level_floor")
    ceil = changes.get("service_level_ceiling")
    if isinstance(floor, (int, float)) and isinstance(ceil, (int, float)) and floor >= ceil:
        problems.append("The service level floor must be below the ceiling.")
    return problems
