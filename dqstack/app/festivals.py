"""National and state festivals, pre-filled for every new company (section 5 step 9).

Each company gets its own copy, so it can correct a date, remove a festival it does
not observe, and add local events and closures without affecting anyone else.

The list covers 2024 to 2028: the years ahead for planning, and the years behind
because the forecasting engine learns festival effects from past sales, which needs
past festival dates as much as future ones.

Dates of festivals that follow the lunar calendar are the best available and can
shift by a day with local observance; they are marked `approximate`, and the setup
checklist's "Check the calendar" step asks the company to confirm them. Fixed dates
and Good Friday (computed from Easter, below) are exact.
"""

from __future__ import annotations

from datetime import date, timedelta

NATIONAL: tuple[str, ...] = ()

_SANKRANTI_STATES = ("Gujarat", "Maharashtra", "Karnataka", "Andhra Pradesh", "Telangana",
                     "Rajasthan", "Madhya Pradesh", "Uttar Pradesh", "Bihar", "Odisha")
_NORTH = ("Punjab", "Haryana", "Delhi", "Chandigarh", "Himachal Pradesh", "Jammu and Kashmir")
_EAST_PUJA = ("West Bengal", "Assam", "Odisha", "Tripura", "Bihar", "Jharkhand")
_GANESH = ("Maharashtra", "Karnataka", "Goa", "Telangana", "Andhra Pradesh", "Gujarat")
_KARVA = ("Punjab", "Haryana", "Rajasthan", "Uttar Pradesh", "Delhi", "Madhya Pradesh",
          "Chandigarh")
_CHHATH = ("Bihar", "Jharkhand", "Uttar Pradesh")

# name, states (empty = national), {year: (month, day)}
_LUNAR: list[tuple[str, tuple[str, ...], dict[int, tuple[int, int]]]] = [
    ("Makar Sankranti", _SANKRANTI_STATES,
     {2024: (1, 15), 2025: (1, 14), 2026: (1, 14), 2027: (1, 15), 2028: (1, 15)}),
    ("Pongal", ("Tamil Nadu", "Puducherry"),
     {2024: (1, 15), 2025: (1, 14), 2026: (1, 14), 2027: (1, 15), 2028: (1, 15)}),
    ("Lohri", _NORTH,
     {2024: (1, 14), 2025: (1, 13), 2026: (1, 13), 2027: (1, 14), 2028: (1, 14)}),
    ("Maha Shivaratri", NATIONAL,
     {2024: (3, 8), 2025: (2, 26), 2026: (2, 15), 2027: (3, 6), 2028: (2, 23)}),
    ("Holi", NATIONAL,
     {2024: (3, 25), 2025: (3, 14), 2026: (3, 4), 2027: (3, 22), 2028: (3, 11)}),
    ("Gudi Padwa", ("Maharashtra", "Goa"),
     {2024: (4, 9), 2025: (3, 30), 2026: (3, 19), 2027: (4, 7), 2028: (3, 27)}),
    ("Ugadi", ("Karnataka", "Andhra Pradesh", "Telangana"),
     {2024: (4, 9), 2025: (3, 30), 2026: (3, 19), 2027: (4, 7), 2028: (3, 27)}),
    ("Eid al-Fitr", NATIONAL,
     {2024: (4, 11), 2025: (3, 31), 2026: (3, 21), 2027: (3, 10), 2028: (2, 27)}),
    ("Ram Navami", NATIONAL,
     {2024: (4, 17), 2025: (4, 6), 2026: (3, 26), 2027: (4, 15), 2028: (4, 3)}),
    ("Baisakhi", ("Punjab", "Haryana", "Chandigarh"),
     {2024: (4, 13), 2025: (4, 13), 2026: (4, 14), 2027: (4, 14), 2028: (4, 13)}),
    ("Tamil New Year", ("Tamil Nadu", "Puducherry"),
     {2024: (4, 14), 2025: (4, 14), 2026: (4, 14), 2027: (4, 14), 2028: (4, 14)}),
    ("Vishu", ("Kerala",),
     {2024: (4, 14), 2025: (4, 14), 2026: (4, 14), 2027: (4, 15), 2028: (4, 14)}),
    ("Bohag Bihu", ("Assam",),
     {2024: (4, 14), 2025: (4, 14), 2026: (4, 14), 2027: (4, 14), 2028: (4, 14)}),
    ("Pohela Boishakh", ("West Bengal", "Tripura"),
     {2024: (4, 14), 2025: (4, 15), 2026: (4, 15), 2027: (4, 15), 2028: (4, 14)}),
    ("Eid al-Adha", NATIONAL,
     {2024: (6, 17), 2025: (6, 7), 2026: (5, 27), 2027: (5, 17), 2028: (5, 5)}),
    ("Raksha Bandhan", NATIONAL,
     {2024: (8, 19), 2025: (8, 9), 2026: (8, 28), 2027: (8, 17), 2028: (8, 5)}),
    ("Janmashtami", NATIONAL,
     {2024: (8, 26), 2025: (8, 16), 2026: (9, 4), 2027: (8, 25), 2028: (8, 13)}),
    ("Onam", ("Kerala",),
     {2024: (9, 15), 2025: (9, 5), 2026: (8, 26), 2027: (9, 12), 2028: (8, 31)}),
    ("Ganesh Chaturthi", _GANESH,
     {2024: (9, 7), 2025: (8, 27), 2026: (9, 14), 2027: (9, 4), 2028: (8, 23)}),
    ("Dussehra", NATIONAL,
     {2024: (10, 12), 2025: (10, 2), 2026: (10, 20), 2027: (10, 9), 2028: (9, 27)}),
    ("Karva Chauth", _KARVA,
     {2024: (10, 20), 2025: (10, 10), 2026: (10, 29), 2027: (10, 18), 2028: (10, 7)}),
    ("Dhanteras", NATIONAL,
     {2024: (10, 29), 2025: (10, 18), 2026: (11, 6), 2027: (10, 27), 2028: (10, 15)}),
    ("Diwali", NATIONAL,
     {2024: (11, 1), 2025: (10, 20), 2026: (11, 8), 2027: (10, 29), 2028: (10, 17)}),
    ("Chhath Puja", _CHHATH,
     {2024: (11, 7), 2025: (10, 27), 2026: (11, 14), 2027: (11, 4), 2028: (10, 23)}),
    ("Guru Nanak Jayanti", NATIONAL,
     {2024: (11, 15), 2025: (11, 5), 2026: (11, 24), 2027: (11, 14), 2028: (11, 2)}),
]

_FIXED: list[tuple[str, tuple[str, ...], tuple[int, int], str]] = [
    ("Republic Day", NATIONAL, (1, 26), "holiday"),
    ("Independence Day", NATIONAL, (8, 15), "holiday"),
    ("Gandhi Jayanti", NATIONAL, (10, 2), "holiday"),
    ("Christmas", NATIONAL, (12, 25), "festival"),
    ("New Year's Eve", NATIONAL, (12, 31), "festival"),
]

YEARS = (2024, 2025, 2026, 2027, 2028)


def easter(year: int) -> date:
    """Western Easter Sunday (anonymous Gregorian algorithm). Exact."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def events(years=YEARS) -> list[dict]:
    """Every pre-filled event as plain dicts, sorted by date."""
    out: list[dict] = []
    for y in years:
        for name, states, (m, d), kind in _FIXED:
            out.append({"name": name, "day": date(y, m, d), "kind": kind,
                        "states": list(states), "approximate": False})
        out.append({"name": "Good Friday", "day": easter(y) - timedelta(days=2),
                    "kind": "holiday", "states": [], "approximate": False})
        for name, states, by_year in _LUNAR:
            if y in by_year:
                m, d = by_year[y]
                out.append({"name": name, "day": date(y, m, d), "kind": "festival",
                            "states": list(states), "approximate": True})
    out.sort(key=lambda e: (e["day"], e["name"]))
    return out


def seed(db, company_id: str, years=YEARS) -> int:
    """Add the pre-filled events a company does not already have. Safe to run again
    (each year, or after an update to this list): matching is on name and date."""
    from sqlalchemy import select

    from .models import CalendarEvent
    have = {(n, d) for n, d in db.execute(
        select(CalendarEvent.name, CalendarEvent.day).where(
            CalendarEvent.company_id == company_id))}
    added = 0
    for e in events(years):
        if (e["name"], e["day"]) in have:
            continue
        db.add(CalendarEvent(company_id=company_id, name=e["name"], day=e["day"],
                             kind=e["kind"], states=e["states"], approximate=e["approximate"],
                             source="national list"))
        added += 1
    return added
