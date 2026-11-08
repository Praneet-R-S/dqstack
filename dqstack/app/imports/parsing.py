"""Reading uploaded files and the values in their cells.

Every parser here either returns a clean value or raises `CellError` with a sentence
a shop manager can act on ("MRP '12,3x' is not an amount"). The check report is
built from those sentences, so they are written for the person fixing the file, not
for a developer.

Indian conventions: amounts may carry Rs, Rs., INR or the rupee sign and Indian digit
grouping (1,23,456.50). Dates are read day first (03/04/2026 is 3 April), never
month first; ISO dates and real Excel dates are also understood.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

MAX_BYTES = 25 * 1024 * 1024
MAX_ROWS = 1_000_000


class CellError(ValueError):
    pass


class FileError(ValueError):
    pass


# ------------------------------------------------------------------ files

def read_table(content: bytes, filename: str) -> tuple[list[str], list[list[str]]]:
    """Return (headers, rows) with every cell as a stripped string."""
    if len(content) > MAX_BYTES:
        raise FileError("That file is larger than 25 MB. Split it into smaller files.")
    name = (filename or "").lower()
    if name.endswith(".xls"):
        raise FileError("Old .xls files are not supported. In Excel, use Save As and choose "
                        ".xlsx or CSV.")
    if name.endswith((".xlsx", ".xlsm")) or content[:2] == b"PK":
        headers, rows = _read_xlsx(content)
    else:
        headers, rows = _read_csv(content)
    headers = [h.strip() for h in headers]
    while headers and not headers[-1]:
        headers.pop()
    if not headers:
        raise FileError("The first row of the file is empty. It should hold the column names.")
    if len(rows) > MAX_ROWS:
        raise FileError(f"That file has {len(rows):,} rows; the limit is {MAX_ROWS:,}. "
                        "Split it into smaller files.")
    width = len(headers)
    rows = [(r + [""] * width)[:width] for r in rows]
    return headers, rows


def _decode(content: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("latin-1")


def _read_csv(content: bytes) -> tuple[list[str], list[list[str]]]:
    text = _decode(content)
    sample = text[:20000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    try:
        headers = next(reader)
    except StopIteration:
        raise FileError("The file is empty.") from None
    return headers, [[c.strip() for c in r] for r in reader]


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.date().isoformat() if (v.hour, v.minute, v.second) == (0, 0, 0) else v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(v)
    return str(v).strip()


def _read_xlsx(content: bytes) -> tuple[list[str], list[list[str]]]:
    from openpyxl import load_workbook
    try:
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception:  # noqa: BLE001 -- any parse failure is the same message
        raise FileError("This file could not be opened as an Excel workbook.") from None
    ws = next((w for w in wb.worksheets if w.sheet_state == "visible"
               and w.title.lower() not in ("instructions", "example")), wb.worksheets[0])
    it = ws.iter_rows(values_only=True)
    try:
        headers = [_cell(v) for v in next(it)]
    except StopIteration:
        raise FileError("The first sheet is empty.") from None
    rows = [[_cell(v) for v in r] for r in it]
    wb.close()
    return headers, rows


def norm(header: str) -> str:
    """'Selling Price (Rs)' -> 'sellingpricers'. Used to match columns."""
    return re.sub(r"[^a-z0-9%]", "", header.lower())


# ------------------------------------------------------------------ values

_MONEY_JUNK = re.compile(r"(rs\.?|inr|₹|\s|,)", re.IGNORECASE)


def money_paise(raw: str, what: str, *, allow_negative: bool = False) -> int:
    s = _MONEY_JUNK.sub("", raw)
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise CellError(f"{what} '{raw}' is not an amount") from None
    if d < 0 and not allow_negative:
        raise CellError(f"{what} cannot be negative")
    return int((d * 100).quantize(Decimal("1")))


def quantity_milli(raw: str, what: str, *, allow_negative: bool = False) -> int:
    s = raw.replace(",", "").strip()
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise CellError(f"{what} '{raw}' is not a number") from None
    if d < 0 and not allow_negative:
        raise CellError(f"{what} cannot be negative")
    milli = d * 1000
    if milli != milli.to_integral_value():
        raise CellError(f"{what} '{raw}' has more than 3 decimal places")
    return int(milli)


def whole(raw: str, what: str, *, lo: int = 0, hi: int | None = None) -> int:
    s = raw.replace(",", "").strip()
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise CellError(f"{what} '{raw}' is not a whole number") from None
    if d != d.to_integral_value():
        raise CellError(f"{what} '{raw}' must be a whole number")
    n = int(d)
    if n < lo or (hi is not None and n > hi):
        raise CellError(f"{what} must be between {lo} and {hi}" if hi is not None
                        else f"{what} must be at least {lo}")
    return n


# GST rates in use or in historical data (percent). 12 and 28 were folded into other
# slabs in September 2025 but still appear in older files.
GST_SLABS = (Decimal("0"), Decimal("0.1"), Decimal("0.25"), Decimal("1"), Decimal("1.5"),
             Decimal("3"), Decimal("5"), Decimal("6"), Decimal("7.5"), Decimal("12"),
             Decimal("18"), Decimal("28"), Decimal("40"))


def gst_bp(raw: str) -> int:
    s = raw.replace("%", "").strip()
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise CellError(f"GST rate '{raw}' is not a number") from None
    if d not in GST_SLABS and d < 1 and (d * 100) in GST_SLABS:
        d = d * 100                        # written as a fraction: 0.18 means 18%
    if d not in GST_SLABS:
        raise CellError(f"GST rate {raw} is not a GST rate (use 0, 5, 12, 18, 28 or 40, "
                        f"or a special rate)")
    return int(d * 100)


_UNITS = {"each": "each", "ea": "each", "pc": "each", "pcs": "each", "piece": "each",
          "pieces": "each", "nos": "each", "no": "each", "unit": "each", "units": "each",
          "kg": "kg", "kgs": "kg", "kilogram": "kg", "kilograms": "kg", "kilo": "kg",
          "litre": "litre", "liter": "litre", "litres": "litre", "liters": "litre",
          "l": "litre", "ltr": "litre", "ltrs": "litre", "lt": "litre"}


def unit(raw: str) -> str:
    u = _UNITS.get(raw.strip().lower().rstrip("."))
    if not u:
        raise CellError(f"unit '{raw}' must be each, kg or litre")
    return u


_TRUE = {"yes", "y", "true", "1", "t"}
_FALSE = {"no", "n", "false", "0", "f"}


def yes_no(raw: str, what: str) -> bool:
    v = raw.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise CellError(f"{what} '{raw}' must be yes or no")


_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def day(raw: str, what: str = "date") -> date:
    s = raw.strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ].*)?", s)
    try:
        if m:
            return date(int(m[1]), int(m[2]), int(m[3]))
        m = re.fullmatch(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2}|\d{4})", s)
        if m:
            y = int(m[3]) + (2000 if len(m[3]) == 2 else 0)
            return date(y, int(m[2]), int(m[1]))
        m = re.fullmatch(r"(\d{1,2})[ \-/]([A-Za-z]{3,9})[ \-/,]*(\d{2}|\d{4})", s)
        if m and m[2][:3].lower() in _MONTHS:
            y = int(m[3]) + (2000 if len(m[3]) == 2 else 0)
            return date(y, _MONTHS[m[2][:3].lower()], int(m[1]))
        if re.fullmatch(r"\d{5}(\.0+)?", s) and 20000 <= float(s) <= 80000:
            return date(1899, 12, 30) + timedelta(days=int(float(s)))   # Excel serial date
    except ValueError:
        raise CellError(f"{what} '{raw}' is not a real date") from None
    raise CellError(f"{what} '{raw}' is not a date (use DD-MM-YYYY or YYYY-MM-DD)")


_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def weekdays(raw: str, what: str) -> list[int]:
    s = raw.strip().lower()
    if s in ("", "any", "daily", "all", "every day", "everyday"):
        return []
    out = []
    for part in re.split(r"[\s,;/|&+]+|and", s):
        if not part:
            continue
        d = _DAYS.get(part[:3])
        if d is None:
            raise CellError(f"{what} '{raw}' should list days like Mon, Thu")
        if d not in out:
            out.append(d)
    return sorted(out)


def split_list(raw: str) -> list[str]:
    return [p.strip() for p in re.split(r"[|;,]", raw) if p.strip()]


def gtin_ok(code: str) -> bool:
    """Check digit of an EAN-8, UPC-A, EAN-13 or GTIN-14. Other codes pass."""
    if not code.isdigit() or len(code) not in (8, 12, 13, 14):
        return True
    digits = [int(c) for c in code]
    body, check = digits[:-1], digits[-1]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
