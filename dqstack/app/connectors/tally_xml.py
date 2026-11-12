"""Tally XML: writing an import file, and reading Tally's own exports.

Tally (TallyPrime, Tally.ERP 9) exchanges data as XML in one envelope:

    <ENVELOPE>
      <HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
      <BODY><IMPORTDATA>
        <REQUESTDESC><REPORTNAME>All Masters | Vouchers</REPORTNAME>
          <STATICVARIABLES><SVCURRENTCOMPANY>...</SVCURRENTCOMPANY></STATICVARIABLES>
        </REQUESTDESC>
        <REQUESTDATA>
          <TALLYMESSAGE xmlns:UDF="TallyUDF"> one master or voucher </TALLYMESSAGE> ...
        </REQUESTDATA>
      </IMPORTDATA></BODY>
    </ENVELOPE>

Its "Export > XML (Data Interchange)" of a Day Book or of the stock items writes the
same shape, so one reader handles both, and our own file can be read back to check
it (the round-trip tests).

Conventions this module follows, and the reader expects:

* Amounts: credit positive, debit negative, with ISDEEMEDPOSITIVE "Yes" on a debit.
  Every voucher written here balances to the paisa (`Voucher.check`).
* Dates YYYYMMDD; quantities "12 Nos" or "1.250 kg"; rates "849.00/Nos".
* Tally marks its built-in values with a control character, as in
  `<GSTAPPLICABLE>&#4; Applicable</GSTAPPLICABLE>` and `<STATENAME>&#4; Any</STATENAME>`.
  XML 1.0 does not allow &#4;, so a standard parser refuses Tally's own files; the
  reader removes such references first. The writer emits them where Tally does.
* Non-ASCII names (Hindi product names, the rupee sign) are written as numeric
  character references, so the file is plain ASCII and reads the same whatever
  encoding Tally assumes. Tally's exports are often UTF-16 with a byte-order mark;
  the reader accepts UTF-16 and UTF-8.

Pure functions only: no database. `tally.py` gathers the data and applies it.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

CTRL4 = "⁣DQ4⁣"          # placeholder for Tally's &#4; marker, swapped after writing
MAX_XML_BYTES = 50 * 1024 * 1024


class TallyFileError(ValueError):
    """A file that is not a Tally XML export, in words a person can act on."""


# =================================================================== numbers and words

def money(paise: int) -> str:
    sign = "-" if paise < 0 else ""
    whole, frac = divmod(abs(int(paise)), 100)
    return f"{sign}{whole}.{frac:02d}"


def qty_text(milli: int, unit_name: str, decimals: int) -> str:
    v = Decimal(abs(int(milli))) / 1000
    s = f"{v:.{decimals}f}" if decimals else str(int(v))
    return f"{s} {unit_name}"


def rate_text(taxable_paise: int, qty_milli: int, unit_name: str) -> str:
    if not qty_milli:
        return f"0.00/{unit_name}"
    per = (Decimal(abs(taxable_paise)) * 1000 / Decimal(abs(qty_milli)) / 100).quantize(Decimal("0.01"))
    return f"{per}/{unit_name}"


def pct(rate_bp: int | None) -> str:
    """1800 -> '18', 250 -> '2.5', 25 -> '0.25'."""
    d = (Decimal(rate_bp or 0) / 100).normalize()
    s = format(d, "f")
    return s


def ledger_name(template: str, rate_bp: int | None) -> str:
    return template.replace("{rate}", pct(rate_bp)).strip()


def tally_date(d: date) -> str:
    return d.strftime("%Y%m%d")


# =================================================================== what is written

@dataclass
class Item:
    name: str
    sku: str
    unit_name: str          # the Tally unit: Nos, kg, ltr
    decimals: int
    group: str | None = None
    hsn: str | None = None
    gst_bp: int | None = None
    mrp_paise: int | None = None
    price_paise: int | None = None      # selling price incl. GST, as DQ-Stack keeps it
    cost_paise: int | None = None
    taxability_zero: str = "Exempt"


@dataclass
class Line:
    """One stock item on a voucher. Amounts are positive; the voucher sets the sign."""
    item: str
    unit_name: str
    decimals: int
    qty_milli: int
    taxable_paise: int
    ledger: str             # the sales or purchase ledger it is allocated to
    godown: str | None = None


@dataclass
class Entry:
    """A ledger line. `amount` is signed the Tally way: credit positive."""
    ledger: str
    amount: int
    party: bool = False
    bill_ref: str | None = None


@dataclass
class Voucher:
    vtype: str              # the voucher type name in Tally
    base: str               # sales | credit_note | purchase
    remote_id: str
    number: str
    day: date
    party: str
    lines: list[Line] = field(default_factory=list)
    entries: list[Entry] = field(default_factory=list)   # party, taxes, round off, misc
    narration: str = ""
    reference: str | None = None
    state: str | None = None

    @property
    def inventory_sign(self) -> int:
        """Goods out (a sale) are a credit; goods in (a purchase, a return) a debit."""
        return 1 if self.base == "sales" else -1

    def check(self) -> None:
        total = sum(e.amount for e in self.entries) + \
            sum(self.inventory_sign * ln.taxable_paise for ln in self.lines)
        if total != 0:
            raise AssertionError(f"voucher {self.number} does not balance: {total} paise")


@dataclass
class Ledger:
    name: str
    parent: str
    fields: list[tuple[str, str]] = field(default_factory=list)


# =================================================================== writing

def _el(parent: ET.Element, tag: str, text=None, **attrs) -> ET.Element:
    e = ET.SubElement(parent, tag, {k: str(v) for k, v in attrs.items()})
    if text is not None:
        e.text = str(text)
    return e


def _names(parent: ET.Element, *names: str) -> None:
    """Tally keeps a master's name and its aliases in LANGUAGENAME.LIST."""
    lang = _el(parent, "LANGUAGENAME.LIST")
    nl = _el(lang, "NAME.LIST", TYPE="String")
    seen = set()
    for n in names:
        if n and n.casefold() not in seen:
            seen.add(n.casefold())
            _el(nl, "NAME", n)
    _el(lang, "LANGUAGEID", " 1033")


def envelope(report: str, company: str | None) -> tuple[ET.Element, ET.Element]:
    env = ET.Element("ENVELOPE")
    _el(_el(env, "HEADER"), "TALLYREQUEST", "Import Data")
    imp = _el(_el(env, "BODY"), "IMPORTDATA")
    desc = _el(imp, "REQUESTDESC")
    _el(desc, "REPORTNAME", report)
    sv = _el(desc, "STATICVARIABLES")
    if company:
        _el(sv, "SVCURRENTCOMPANY", company)
    return env, _el(imp, "REQUESTDATA")


def _message(data: ET.Element) -> ET.Element:
    return _el(data, "TALLYMESSAGE", **{"xmlns:UDF": "TallyUDF"})


def serialise(env: ET.Element) -> bytes:
    ET.indent(env, space=" ")
    text = ET.tostring(env, encoding="unicode", short_empty_elements=True)
    text = text.replace(CTRL4, "&#4;")
    return text.encode("ascii", "xmlcharrefreplace") + b"\r\n"


UQC = {"Nos": "NOS-NUMBERS", "kg": "KGS-KILOGRAMS", "ltr": "LTR-LITRES"}


def masters_xml(company: str | None, units: list[tuple[str, int]], groups: list[str],
                godowns: list[str], ledgers: list[Ledger], items: list[Item],
                applicable_from: date) -> bytes:
    """Everything the vouchers need, in the order Tally needs it: units and groups
    before the items that use them."""
    env, data = envelope("All Masters", company)
    for name, decimals in units:
        u = _el(_message(data), "UNIT", NAME=name)
        _el(u, "NAME", name)
        _el(u, "ISSIMPLEUNIT", "Yes")
        _el(u, "DECIMALPLACES", decimals)
        if name in UQC:
            _el(u, "GSTREPUOM", UQC[name])
    for g in groups:
        e = _el(_message(data), "STOCKGROUP", NAME=g)
        _el(e, "PARENT", "")
        _el(e, "ISADDABLE", "Yes")
        _names(e, g)
    for g in godowns:
        e = _el(_message(data), "GODOWN", NAME=g)
        _el(e, "PARENT", "")
        _names(e, g)
    for led in ledgers:
        e = _el(_message(data), "LEDGER", NAME=led.name)
        _el(e, "PARENT", led.parent)
        for tag, val in led.fields:
            _el(e, tag, val)
        _names(e, led.name)
    for it in items:
        e = _el(_message(data), "STOCKITEM", NAME=it.name)
        _el(e, "PARENT", it.group or "")
        _el(e, "BASEUNITS", it.unit_name)
        _el(e, "GSTAPPLICABLE", f"{CTRL4} Applicable")
        _el(e, "GSTTYPEOFSUPPLY", "Goods")
        if it.gst_bp is not None or it.hsn:
            g = _el(e, "GSTDETAILS.LIST")
            _el(g, "APPLICABLEFROM", tally_date(applicable_from))
            _el(g, "CALCULATIONTYPE", "On Value")
            if it.hsn:
                _el(g, "HSNCODE", it.hsn)
            _el(g, "TAXABILITY", "Taxable" if it.gst_bp else it.taxability_zero)
            sw = _el(g, "STATEWISEDETAILS.LIST")
            _el(sw, "STATENAME", f"{CTRL4} Any")
            full = Decimal(it.gst_bp or 0)
            for head, bp in (("CGST", full / 2), ("SGST/UTGST", full / 2), ("IGST", full)):
                r = _el(sw, "RATEDETAILS.LIST")
                _el(r, "GSTRATEDUTYHEAD", head)
                _el(r, "GSTRATEVALUATIONTYPE", "Based on Value")
                _el(r, "GSTRATE", pct(bp))
            r = _el(sw, "RATEDETAILS.LIST")
            _el(r, "GSTRATEDUTYHEAD", "Cess")
            _el(r, "GSTRATEVALUATIONTYPE", "Not Applicable")
        if it.mrp_paise:
            m = _el(e, "MRPDETAILS.LIST")
            _el(m, "FROMDATE", tally_date(applicable_from))
            mr = _el(m, "MRPRATEDETAILS.LIST")
            _el(mr, "STATENAME", f"{CTRL4} Any")
            _el(mr, "RATE", f"{money(it.mrp_paise)}/{it.unit_name}")
        if it.price_paise:
            p = _el(e, "STANDARDPRICELIST.LIST")
            _el(p, "DATE", tally_date(applicable_from))
            _el(p, "RATE", f"{money(it.price_paise)}/{it.unit_name}")
        if it.cost_paise:
            p = _el(e, "STANDARDCOSTLIST.LIST")
            _el(p, "DATE", tally_date(applicable_from))
            _el(p, "RATE", f"{money(it.cost_paise)}/{it.unit_name}")
        _names(e, it.name, it.sku)
    return serialise(env)


def _yes(neg: bool) -> str:
    return "Yes" if neg else "No"


def merged(entries: list[Entry]) -> list[Entry]:
    """One line per ledger: when two rates post to one tax ledger (an "Output CGST"
    without {rate} in its name), their amounts are added, as Tally shows them."""
    out: dict[tuple, Entry] = {}
    for en in entries:
        k = (en.ledger.casefold(), en.party)
        if k in out:
            out[k] = Entry(out[k].ledger, out[k].amount + en.amount, en.party, out[k].bill_ref)
        else:
            out[k] = Entry(en.ledger, en.amount, en.party, en.bill_ref)
    return [e for e in out.values() if e.amount or e.party]


def vouchers_xml(company: str | None, vouchers: list[Voucher]) -> bytes:
    env, data = envelope("Vouchers", company)
    for v in vouchers:
        v.entries = merged(v.entries)
        v.check()
        e = _el(_message(data), "VOUCHER", REMOTEID=v.remote_id, VCHTYPE=v.vtype,
                ACTION="Create", OBJVIEW="Invoice Voucher View")
        _el(e, "DATE", tally_date(v.day))
        _el(e, "EFFECTIVEDATE", tally_date(v.day))
        _el(e, "VOUCHERTYPENAME", v.vtype)
        _el(e, "VOUCHERNUMBER", v.number)
        if v.reference:
            _el(e, "REFERENCE", v.reference)
            _el(e, "REFERENCEDATE", tally_date(v.day))
        _el(e, "PARTYLEDGERNAME", v.party)
        _el(e, "BASICBASEPARTYNAME", v.party)
        if v.state:
            _el(e, "STATENAME", v.state)
            _el(e, "PLACEOFSUPPLY", v.state)
        _el(e, "PERSISTEDVIEW", "Invoice Voucher View")
        _el(e, "ISINVOICE", "Yes")
        if v.narration:
            _el(e, "NARRATION", v.narration)
        for en in v.entries:
            le = _el(e, "LEDGERENTRIES.LIST")
            _el(le, "LEDGERNAME", en.ledger)
            _el(le, "ISDEEMEDPOSITIVE", _yes(en.amount < 0))
            _el(le, "ISPARTYLEDGER", _yes(en.party))
            _el(le, "AMOUNT", money(en.amount))
            if en.bill_ref:
                b = _el(le, "BILLALLOCATIONS.LIST")
                _el(b, "NAME", en.bill_ref)
                _el(b, "BILLTYPE", "New Ref")
                _el(b, "AMOUNT", money(en.amount))
        for ln in v.lines:
            amt = v.inventory_sign * ln.taxable_paise
            ie = _el(e, "ALLINVENTORYENTRIES.LIST")
            _el(ie, "STOCKITEMNAME", ln.item)
            _el(ie, "ISDEEMEDPOSITIVE", _yes(amt < 0))
            _el(ie, "RATE", rate_text(ln.taxable_paise, ln.qty_milli, ln.unit_name))
            _el(ie, "AMOUNT", money(amt))
            q = qty_text(ln.qty_milli, ln.unit_name, ln.decimals)
            _el(ie, "ACTUALQTY", q)
            _el(ie, "BILLEDQTY", q)
            if ln.godown:
                b = _el(ie, "BATCHALLOCATIONS.LIST")
                _el(b, "GODOWNNAME", ln.godown)
                _el(b, "BATCHNAME", "Primary Batch")
                _el(b, "AMOUNT", money(amt))
                _el(b, "ACTUALQTY", q)
                _el(b, "BILLEDQTY", q)
            a = _el(ie, "ACCOUNTINGALLOCATIONS.LIST")
            _el(a, "LEDGERNAME", ln.ledger)
            _el(a, "ISDEEMEDPOSITIVE", _yes(amt < 0))
            _el(a, "AMOUNT", money(amt))
    return serialise(env)


# =================================================================== reading

_BAD_REF = re.compile(r"&#(x[0-9a-fA-F]+|\d+);")
_RAW_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _drop_bad_ref(m: re.Match) -> str:
    s = m.group(1)
    try:
        n = int(s[1:], 16) if s[0] in "xX" else int(s)
    except ValueError:
        return ""
    return m.group(0) if n in (9, 10, 13) or n >= 32 else ""


def decode(content: bytes) -> str:
    if content[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return content.decode("utf-16")
    if content[:3] == b"\xef\xbb\xbf":
        return content[3:].decode("utf-8", errors="replace")
    if len(content) > 1 and content[1:2] == b"\x00":
        return content.decode("utf-16-le", errors="replace")
    if content[:1] == b"\x00":
        return content.decode("utf-16-be", errors="replace")
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("cp1252", errors="replace")


def looks_like_xml(content: bytes) -> bool:
    head = decode(content[:400]).lstrip("﻿ \t\r\n")
    return head.startswith("<")


def root_of(content: bytes) -> ET.Element:
    if len(content) > MAX_XML_BYTES:
        raise TallyFileError("That file is larger than 50 MB. Export a shorter period from Tally.")
    text = decode(content).lstrip("﻿")
    if re.search(r"<!(DOCTYPE|ENTITY)", text, re.I):
        # Tally never writes a DTD; refusing one also refuses entity tricks.
        raise TallyFileError("This XML file declares a DTD, which Tally files never do. "
                             "Export it from Tally again.")
    text = re.sub(r"^<\?xml[^>]*\?>", "", text.strip())
    text = _BAD_REF.sub(_drop_bad_ref, text)
    text = _RAW_CTRL.sub("", text)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise TallyFileError(f"This file is not readable XML ({e}). Export it from Tally "
                             "again as XML (Data Interchange).") from None
    if root.tag.upper() != "ENVELOPE":
        raise TallyFileError("This XML file is not a Tally export: it does not start with "
                             "<ENVELOPE>.")
    return root


def _t(el: ET.Element | None, tag: str) -> str:
    if el is None:
        return ""
    c = el.find(tag)
    return (c.text or "").strip() if c is not None and c.text else ""


def number(raw: str) -> Decimal | None:
    """'849.00/Nos' -> 849.00, ' 12.500 kg' -> 12.500, '-1,234.50' -> -1234.50,
    'Rs. 99/Nos' -> 99. Empty -> None."""
    s = (raw or "").split("/")[0]
    s = s.replace(",", "")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return Decimal(m.group(0))
    except InvalidOperation:
        return None


def unit_of_qty(raw: str) -> str:
    m = re.search(r"[-\d.,]+\s*(.*)$", (raw or "").strip())
    return (m.group(1) if m else "").strip()


def to_paise(d: Decimal | None) -> int | None:
    return None if d is None else int((d * 100).quantize(Decimal("1")))


def to_milli(d: Decimal | None) -> int | None:
    return None if d is None else int((d * 1000).quantize(Decimal("1")))


def _latest(el: ET.Element, tag: str, date_tag: str) -> ET.Element | None:
    best, best_d = None, ""
    for c in el.findall(tag):
        d = _t(c, date_tag)
        if best is None or d >= best_d:
            best, best_d = c, d
    return best


def _names_of(el: ET.Element) -> list[str]:
    out = []
    for nl in el.iter("NAME.LIST"):
        for n in nl.findall("NAME"):
            if n.text and n.text.strip():
                out.append(n.text.strip())
    return out


def _gst_rate(block: ET.Element | None) -> Decimal | None:
    if block is None:
        return None
    if _t(block, "TAXABILITY").lower() in ("exempt", "nil rated", "non gst", "non-gst"):
        return Decimal(0)
    heads: dict[str, Decimal] = {}
    for r in block.iter("RATEDETAILS.LIST"):
        head = _t(r, "GSTRATEDUTYHEAD").lower()
        rate = number(_t(r, "GSTRATE"))
        if rate is not None:
            heads[head] = rate
    for k in ("igst", "integrated tax"):
        if k in heads:
            return heads[k]
    c = next((v for k, v in heads.items() if k in ("cgst", "central tax")), None)
    s = next((v for k, v in heads.items() if k.startswith("sgst") or k == "state tax"), None)
    if c is not None and s is not None:
        return c + s
    direct = number(_t(block, "IGSTRATE")) or number(_t(block, "GSTRATE"))
    return direct


@dataclass
class StockItem:
    name: str
    aliases: list[str]
    part_no: str | None
    group: str | None
    unit: str
    hsn: str | None
    gst_rate: Decimal | None
    mrp: Decimal | None
    price: Decimal | None
    cost: Decimal | None


def stock_items(root: ET.Element) -> list[StockItem]:
    out = []
    for e in root.iter("STOCKITEM"):
        names = _names_of(e)
        name = (e.get("NAME") or (names[0] if names else "")).strip()
        if not name:
            continue
        aliases = [n for n in names if n.casefold() != name.casefold()]
        part = _t(e, "PARTNO") or _t(e, "PARTNUMBER") or None
        group = _t(e, "PARENT")
        if group.lower().lstrip("\x04 ").strip() in ("", "primary"):
            group = None
        gst = _latest(e, "GSTDETAILS.LIST", "APPLICABLEFROM")
        hsn = _t(gst, "HSNCODE") if gst is not None else ""
        if not hsn:
            hd = _latest(e, "HSNDETAILS.LIST", "APPLICABLEFROM")
            hsn = _t(hd, "HSNCODE") if hd is not None else _t(e, "HSNCODE")
        mrp = None
        md = _latest(e, "MRPDETAILS.LIST", "FROMDATE")
        if md is not None:
            r = md.find("MRPRATEDETAILS.LIST")
            mrp = number(_t(r, "RATE")) if r is not None else None
        sp = _latest(e, "STANDARDPRICELIST.LIST", "DATE")
        sc = _latest(e, "STANDARDCOSTLIST.LIST", "DATE")
        out.append(StockItem(name=name, aliases=aliases, part_no=part, group=group,
                             unit=_t(e, "BASEUNITS"), hsn=re.sub(r"\s", "", hsn) or None,
                             gst_rate=_gst_rate(gst),
                             mrp=mrp, price=number(_t(sp, "RATE")) if sp is not None else None,
                             cost=number(_t(sc, "RATE")) if sc is not None else None))
    return out


@dataclass
class VoucherLine:
    item: str
    qty: Decimal
    amount_paise: int          # the taxable value, positive
    godown: str | None


@dataclass
class ReadVoucher:
    vtype: str
    base: str                  # sale | return | other
    number: str
    day: date | None
    party: str
    party_total_paise: int     # what the customer paid (or was refunded), incl. tax
    tax_paise: int             # the GST ledgers on the goods' side
    lines: list[VoucherLine]
    cancelled: bool


# A ledger carrying tax, by its name: Output CGST, SGST @ 9%, IGST Payable, Cess, VAT.
TAX_LEDGER = re.compile(r"gst|tax|cess|vat", re.I)


def base_type(vtype: str) -> str:
    """Tally lets a company name its own voucher types ("Sales - POS"), so the base
    is guessed from the name; anything that is not a sale or a sales return is left."""
    n = vtype.lower()
    if "order" in n or "quotation" in n or "delivery" in n or "purchase" in n:
        return "other"
    if "credit note" in n or "return" in n:
        return "return"
    if "sale" in n:
        return "sale"
    return "other"


def _amount(el: ET.Element) -> int:
    return to_paise(number(_t(el, "AMOUNT"))) or 0


def vouchers(root: ET.Element) -> list[ReadVoucher]:
    out = []
    for v in root.iter("VOUCHER"):
        vtype = _t(v, "VOUCHERTYPENAME") or v.get("VCHTYPE") or ""
        raw_day = _t(v, "DATE")
        try:
            day = date(int(raw_day[:4]), int(raw_day[4:6]), int(raw_day[6:8]))
        except (ValueError, IndexError):
            day = None
        party = _t(v, "PARTYLEDGERNAME")
        cancelled = _t(v, "ISCANCELLED").lower() == "yes" or \
            _t(v, "ISOPTIONAL").lower() == "yes" or _t(v, "ISDELETED").lower() == "yes"
        lines: list[VoucherLine] = []
        for tag in ("ALLINVENTORYENTRIES.LIST", "INVENTORYENTRIES.LIST"):
            for ie in v.findall(tag):
                item = _t(ie, "STOCKITEMNAME")
                if not item:
                    continue
                qty = number(_t(ie, "BILLEDQTY") or _t(ie, "ACTUALQTY")) or Decimal(0)
                amt = abs(_amount(ie))
                allocs = [b for b in ie.findall("BATCHALLOCATIONS.LIST") if _t(b, "GODOWNNAME")]
                if len(allocs) > 1:
                    # Split across godowns by each allocation's own quantity and value.
                    for b in allocs:
                        bq = number(_t(b, "BILLEDQTY") or _t(b, "ACTUALQTY")) or Decimal(0)
                        ba = abs(_amount(b)) if _t(b, "AMOUNT") else \
                            (int(amt * abs(bq) / abs(qty)) if qty else 0)
                        lines.append(VoucherLine(item, abs(bq), ba, _t(b, "GODOWNNAME")))
                else:
                    gd = _t(allocs[0], "GODOWNNAME") if allocs else None
                    lines.append(VoucherLine(item, abs(qty), amt, gd or None))
        party_total = None
        entries = v.findall("LEDGERENTRIES.LIST") + v.findall("ALLLEDGERENTRIES.LIST")
        raw = sum(_amount(ie) for tag in ("ALLINVENTORYENTRIES.LIST", "INVENTORYENTRIES.LIST")
                  for ie in v.findall(tag))
        side = 1 if raw >= 0 else -1          # goods out are a credit, goods in a debit
        tax = 0
        for le in entries:
            a = _amount(le)
            if _t(le, "ISPARTYLEDGER").lower() == "yes" or \
                    (party and _t(le, "LEDGERNAME") == party):
                party_total = (party_total or 0) + abs(a)
            elif a * side > 0 and TAX_LEDGER.search(_t(le, "LEDGERNAME")):
                tax += abs(a)
        if party_total is None:
            # No party line marked: the goods plus every ledger on the goods' side (the
            # taxes) is what the party paid, since a voucher balances.
            party_total = sum(x.amount_paise for x in lines) + \
                sum(abs(a) for a in (_amount(le) for le in entries) if a * side > 0)
        out.append(ReadVoucher(vtype=vtype, base=base_type(vtype),
                               number=_t(v, "VOUCHERNUMBER"), day=day, party=party,
                               party_total_paise=party_total, tax_paise=tax, lines=lines,
                               cancelled=cancelled))
    return out
