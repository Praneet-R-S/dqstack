"""Indian formats (interface rule 9): rupees in lakh and crore with Indian digit
grouping, DD MMM YYYY dates, and IST.

Indian grouping puts the first comma after three digits and every comma after that
after two: 12,34,56,789 rather than 123,456,789. Getting this wrong is the fastest
way for an Indian retail user to conclude software was not built for them.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def group_indian(n: int) -> str:
    s = str(abs(n))
    if len(s) <= 3:
        out = s
    else:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        out = ",".join(parts + [tail])
    return ("-" if n < 0 else "") + out


def rupees(paise: int | None, *, decimals: bool = True) -> str:
    """₹12,34,567.50"""
    if paise is None:
        return "—"
    neg = paise < 0
    p = abs(int(paise))
    whole, frac = divmod(p, 100)
    s = "₹" + group_indian(whole) + (f".{frac:02d}" if decimals else "")
    return ("-" if neg else "") + s


def rupees_short(paise: int | None) -> str:
    """₹4.2 L, ₹1.35 Cr, ₹8,450 -- the scale a person reads a total at."""
    if paise is None:
        return "—"
    r = paise / 100
    a = abs(r)
    sign = "-" if r < 0 else ""
    if a >= 1e7:
        return f"{sign}₹{a / 1e7:.2f} Cr"
    if a >= 1e5:
        return f"{sign}₹{a / 1e5:.2f} L"
    return sign + "₹" + group_indian(int(round(a)))


def qty(milli: int | None, unit: str = "each") -> str:
    if milli is None:
        return "—"
    if unit == "each":
        return group_indian(int(round(milli / 1000)))
    return f"{milli / 1000:.3f}".rstrip("0").rstrip(".") + f" {unit}"


def day(d: date | datetime | None) -> str:
    """08 Nov 2026"""
    if d is None:
        return "—"
    if isinstance(d, datetime):
        d = d.astimezone(IST).date() if d.tzinfo else d.date()
    return d.strftime("%d %b %Y")


def ist(dt: datetime | None) -> str:
    """08 Nov 2026, 14:32 IST"""
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        from datetime import timezone
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime("%d %b %Y, %H:%M IST")


def to_milli(x) -> int:
    """Parse a quantity a person typed or a file contained into thousandths."""
    if x is None or x == "":
        raise ValueError("a quantity is required")
    if isinstance(x, int):
        return x * 1000
    s = str(x).strip().replace(",", "")
    v = float(s)
    return int(round(v * 1000))


def to_paise(x) -> int:
    """Parse rupees ('1,234.50', '₹99', 45) into paise."""
    if x is None or x == "":
        raise ValueError("an amount is required")
    if isinstance(x, int):
        return x * 100
    s = str(x).strip().replace("₹", "").replace("Rs.", "").replace("Rs", "").replace(",", "")
    return int(round(float(s) * 100))
