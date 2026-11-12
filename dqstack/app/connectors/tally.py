"""Tally: an import file of DQ-Stack's sales, refunds and purchases for the books, and
Tally's own stock items and day book read into DQ-Stack.

Export (DQ-Stack -> Tally), for a period and a choice of stores:

* Sales: by default ONE sales voucher per store per day, with one line per product
  and the GST split by rate. A shop makes hundreds of small bills a day; posting each
  as a Tally invoice would copy the till into the books, while what the accounts and
  the GST return need for counter (B2C) sales is the day's taxable value and tax by
  rate (GSTR-1 reports B2C small sales consolidated by rate and place of supply
  anyway). Checkout does not record a customer's GSTIN, so there is no B2C/B2B split
  to keep. A company that wants every bill can choose one voucher per bill.
* Refunds: a credit note per store per day (or per refund bill), the mirror image.
* Uploaded or typed daily sales (stores without tills) join their day's voucher, the
  tax worked out from each product's GST rate, since those files carry no tax.
* Purchases: one purchase voucher per delivery received (the supplier's invoice),
  at the delivery's cost before GST, tax at the product's rate; IGST when the
  supplier's GSTIN is from another state than the store, else CGST and SGST.
* Sales at a store are intra-state (over-the-counter supply is in the store's
  state), so they carry CGST and SGST.
* Masters: units, stock groups (categories), godowns (stores), ledgers and stock
  items with HSN and GST rate, so the file imports into an empty company. Ledger names
  are the company's choice (`DEFAULTS`); "{rate}" in a name becomes the rate.

Two files, imported in order: masters (Import > Masters), then vouchers (Import >
Transactions). They come together in one zip with a note saying so.

Import (Tally -> DQ-Stack): the Stock Item master becomes a products file and the
sales vouchers of a day book become a sales file, each run through the Imports
pipeline (importing.py).
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..checkout import tax_inside
from ..db import utcnow
from ..models import (Bill, Company, Connector, DailySales, Delivery, Product, Store, Supplier)
from ..sales import tz_of
from . import tally_xml as X

DEFAULTS: dict = {
    "tally_company": "",                 # SVCURRENTCOMPANY; empty = the company open in Tally
    "voucher_per": "day",                # day | bill
    "sales_voucher_type": "Sales",
    "credit_note_voucher_type": "Credit Note",
    "purchase_voucher_type": "Purchase",
    "counter_party": "Cash",             # who the day's counter sales are made to
    "counter_party_group": "Cash-in-Hand",
    "sales_ledger": "Sales GST {rate}%",
    "purchase_ledger": "Purchase GST {rate}%",
    "output_cgst": "Output CGST",
    "output_sgst": "Output SGST",
    "output_igst": "Output IGST",
    "input_cgst": "Input CGST",
    "input_sgst": "Input SGST",
    "input_igst": "Input IGST",
    "round_off": "Round Off",
    "misc_sales": "Sales - Misc items",
    "unit_each": "Nos",
    "unit_kg": "kg",
    "unit_litre": "ltr",
    "godowns": True,                     # one godown per store, and allocations to it
    "item_name": "name",                 # name | sku: what the stock item is called
    "include_entered_sales": True,
    "zero_rate_taxability": "Exempt",
    # Reading Tally files: godown name -> store code.
    "godown_map": {},
}
LEDGER_KEYS = ("counter_party", "sales_ledger", "purchase_ledger", "output_cgst", "output_sgst",
               "output_igst", "input_cgst", "input_sgst", "input_igst", "round_off",
               "misc_sales", "sales_voucher_type", "credit_note_voucher_type",
               "purchase_voucher_type", "unit_each", "unit_kg", "unit_litre",
               "counter_party_group", "tally_company")

STATE_CODES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh",
    "05": "Uttarakhand", "06": "Haryana", "07": "Delhi", "08": "Rajasthan",
    "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim", "12": "Arunachal Pradesh",
    "13": "Nagaland", "14": "Manipur", "15": "Mizoram", "16": "Tripura", "17": "Meghalaya",
    "18": "Assam", "19": "West Bengal", "20": "Jharkhand", "21": "Odisha",
    "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra", "29": "Karnataka",
    "30": "Goa", "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "35": "Andaman and Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh",
    "38": "Ladakh"}
CODE_OF_STATE = {v: k for k, v in STATE_CODES.items()}


def settings_of(conn: Connector | None) -> dict:
    s = dict(DEFAULTS)
    s.update((conn.settings or {}) if conn else {})
    return s


def get_connector(db: Session, company_id: str, kind: str) -> Connector:
    conn = db.scalar(select(Connector).where(Connector.company_id == company_id,
                                             Connector.kind == kind))
    if conn is None:
        conn = Connector(company_id=company_id, kind=kind, settings={}, cursor={},
                         status="ready" if kind == "tally" else "not_connected")
        db.add(conn)
        db.flush()
    return conn


def clean_settings(body: dict) -> dict:
    """Validate an Admin's Tally settings; returns only known keys."""
    out = {}
    for k, v in body.items():
        if k not in DEFAULTS:
            continue
        if k in LEDGER_KEYS:
            v = str(v or "").strip()
            if k != "tally_company" and not v:
                raise HTTPException(422, "Every ledger and voucher type needs a name.")
            if len(v) > 100 or any(ch in v for ch in "<>&\"\x04"):
                raise HTTPException(422, f"'{v[:40]}' cannot be a Tally name: keep it under 100 "
                                         "characters, without < > & or quotes.")
        elif k == "voucher_per":
            if v not in ("day", "bill"):
                raise HTTPException(422, "Sales go to Tally one voucher per day or per bill.")
        elif k == "item_name":
            if v not in ("name", "sku"):
                raise HTTPException(422, "Stock items are named by product name or by SKU.")
        elif k in ("godowns", "include_entered_sales"):
            v = bool(v)
        elif k == "zero_rate_taxability":
            if v not in ("Exempt", "Nil Rated"):
                raise HTTPException(422, "0% items are Exempt or Nil Rated in Tally.")
        elif k == "godown_map":
            if not isinstance(v, dict):
                raise HTTPException(422, "The godown list must map godown names to store codes.")
            v = {str(a).strip()[:100]: str(b).strip().upper()[:32] for a, b in v.items()
                 if str(a).strip() and str(b).strip()}
        out[k] = v
    return out


# =================================================================== export

def _local_day(at: datetime, tz) -> date:
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at.astimezone(tz).date()


class _Builder:
    def __init__(self, db: Session, company_id: str, stores: list[Store], s: dict):
        self.db, self.cid, self.s = db, company_id, s
        self.stores = {st.id: st for st in stores}
        self.ledgers: dict[str, X.Ledger] = {}
        self.items: dict[str, X.Item] = {}
        self.item_names: set[str] = set()
        self.units: dict[str, int] = {}
        self.groups: set[str] = set()
        self.products: dict[str, Product] = {}
        self.warnings: list[str] = []
        names, used = {}, set()
        for st in sorted(stores, key=lambda x: x.code):
            n = st.name.strip() or st.code
            if n.casefold() in used:
                n = f"{n} ({st.code})"
            used.add(n.casefold())
            names[st.id] = n
        self.godown = names if s["godowns"] else {}

    # ------------------------------------------------------------ masters
    def unit(self, unit: str) -> tuple[str, int]:
        name = {"each": self.s["unit_each"], "kg": self.s["unit_kg"],
                "litre": self.s["unit_litre"]}.get(unit, self.s["unit_each"])
        dec = 0 if unit == "each" else 3
        self.units[name] = dec
        return name, dec

    def ledger(self, name: str, parent: str, fields=()) -> str:
        if name not in self.ledgers:
            self.ledgers[name] = X.Ledger(name, parent, list(fields))
        return name

    def tax_ledger(self, key: str, rate_bp: int) -> str:
        head = {"cgst": "Central Tax", "sgst": "State Tax", "igst": "Integrated Tax"}[key[-4:]]
        return self.ledger(X.ledger_name(self.s[key], rate_bp), "Duties & Taxes",
                           [("TAXTYPE", "GST"), ("GSTDUTYHEAD", head)])

    def product(self, pid: str) -> Product:
        if pid not in self.products:
            self.products[pid] = self.db.get(Product, pid)
        return self.products[pid]

    def item(self, p: Product) -> X.Item:
        if p.id in self.items:
            return self.items[p.id]
        base = p.sku if self.s["item_name"] == "sku" else (p.name or p.sku).strip()
        name = base
        if name.casefold() in self.item_names:
            name = f"{base} ({p.sku})"
        self.item_names.add(name.casefold())
        uname, dec = self.unit(p.unit)
        if p.category:
            self.groups.add(p.category.strip())
        rate = p.gst_rate_bp or 0
        price_ex = round(p.price_paise * 10_000 / (10_000 + rate)) if p.price_paise else None
        it = X.Item(name=name, sku=p.sku, unit_name=uname, decimals=dec,
                    group=p.category.strip() if p.category else None, hsn=p.hsn,
                    gst_bp=p.gst_rate_bp, mrp_paise=p.mrp_paise, price_paise=price_ex,
                    cost_paise=p.cost_paise, taxability_zero=self.s["zero_rate_taxability"])
        self.items[p.id] = it
        return it

    # ------------------------------------------------------------ vouchers
    def sales_voucher(self, base: str, store: Store, day: date, number: str, remote: str,
                      products: dict[str, list[int]], misc_gross: int, party_total: int,
                      narration: str) -> X.Voucher:
        """products: {product_id: [qty_milli, gross_paise, tax_paise]}, all positive."""
        s = self.s
        vtype = s["sales_voucher_type"] if base == "sales" else s["credit_note_voucher_type"]
        sign = 1 if base == "sales" else -1           # credit is positive in Tally
        party = s["counter_party"]
        if party.casefold() != "cash":
            self.ledger(party, s["counter_party_group"])
        v = X.Voucher(vtype=vtype, base=base, remote_id=remote, number=number, day=day,
                      party=party, narration=narration, state=store.state)
        tax_by_rate: dict[int, int] = defaultdict(int)
        goods = 0
        for pid in sorted(products, key=lambda x: self.item(self.product(x)).name):
            qty, gross, tax = products[pid]
            if qty <= 0 and gross <= 0:
                continue
            p = self.product(pid)
            it = self.item(p)
            rate = p.gst_rate_bp or 0
            led = self.ledger(X.ledger_name(s["sales_ledger"], rate), "Sales Accounts")
            v.lines.append(X.Line(item=it.name, unit_name=it.unit_name, decimals=it.decimals,
                                  qty_milli=qty, taxable_paise=gross - tax, ledger=led,
                                  godown=self.godown.get(store.id)))
            tax_by_rate[rate] += tax
            goods += gross
        entries = [X.Entry(party, -sign * party_total, party=True)]
        for rate in sorted(tax_by_rate):
            tax = tax_by_rate[rate]
            if not tax:
                continue
            c = tax // 2
            entries.append(X.Entry(self.tax_ledger("output_cgst", rate), sign * c))
            entries.append(X.Entry(self.tax_ledger("output_sgst", rate), sign * (tax - c)))
        if misc_gross:
            entries.append(X.Entry(self.ledger(s["misc_sales"], "Sales Accounts"),
                                   sign * misc_gross))
        diff = party_total - goods - misc_gross
        if diff:
            entries.append(X.Entry(self.ledger(s["round_off"], "Indirect Expenses"), sign * diff))
        v.entries = entries
        return v

    def purchase_voucher(self, d: Delivery, store: Store, day: date, supplier: Supplier | None,
                         skipped: list[str]) -> X.Voucher | None:
        s = self.s
        sup_state = STATE_CODES.get((supplier.gstin or "")[:2]) if supplier and supplier.gstin \
            else None
        inter = bool(sup_state and store.state and sup_state != store.state)
        if supplier is not None:
            fields = [("ISBILLWISEON", "Yes")]
            if supplier.gstin:
                fields += [("PARTYGSTIN", supplier.gstin), ("GSTREGISTRATIONTYPE", "Regular")]
            if sup_state:
                fields.append(("LEDSTATENAME", sup_state))
            party = self.ledger(supplier.name.strip(), "Sundry Creditors", fields)
        else:
            party = s["counter_party"]
            if party.casefold() != "cash":
                self.ledger(party, s["counter_party_group"])
        number = d.invoice_number or f"GRN-{d.id[:8]}"
        v = X.Voucher(vtype=s["purchase_voucher_type"], base="purchase",
                      remote_id=f"dqstack:{self.cid[:8]}:purchase:{d.id}", number=number,
                      day=day, party=party, reference=d.invoice_number,
                      narration=f"DQ-Stack delivery at {store.code}"
                                + (f", order {d.po_id[:8]}" if d.po_id else ""),
                      state=store.state)
        tax_by_rate: dict[int, int] = defaultdict(int)
        merged: dict[str, list[int]] = {}
        for ln in d.lines:
            if ln.received_milli <= 0:
                continue
            p = self.product(ln.product_id)
            cost = ln.unit_cost_paise if ln.unit_cost_paise is not None else p.cost_paise
            if cost is None:
                skipped.append(f"{p.sku} on delivery {number} has no cost, so it was left out")
                continue
            m = merged.setdefault(p.id, [0, 0])
            m[0] += ln.received_milli
            m[1] += ln.received_milli * cost // 1000
        total = 0
        for pid, (qty, taxable) in merged.items():
            p = self.product(pid)
            it = self.item(p)
            rate = p.gst_rate_bp or 0
            tax = round(taxable * rate / 10_000)
            led = self.ledger(X.ledger_name(s["purchase_ledger"], rate), "Purchase Accounts")
            v.lines.append(X.Line(item=it.name, unit_name=it.unit_name, decimals=it.decimals,
                                  qty_milli=qty, taxable_paise=taxable, ledger=led,
                                  godown=self.godown.get(store.id)))
            tax_by_rate[rate] += tax
            total += taxable + tax
        if not v.lines:
            return None
        v.entries = [X.Entry(party, total, party=True,
                             bill_ref=number if supplier is not None else None)]
        for rate in sorted(tax_by_rate):
            tax = tax_by_rate[rate]
            if not tax:
                continue
            if inter:
                v.entries.append(X.Entry(self.tax_ledger("input_igst", rate), -tax))
            else:
                c = tax // 2
                v.entries.append(X.Entry(self.tax_ledger("input_cgst", rate), -c))
                v.entries.append(X.Entry(self.tax_ledger("input_sgst", rate), -(tax - c)))
        return v


def build_export(db: Session, company_id: str, store_ids: list[str], start: date, end: date,
                 conn: Connector | None = None) -> dict:
    """{"masters": bytes, "vouchers": bytes, "summary": {...}} for the period."""
    s = settings_of(conn)
    tz = tz_of(db, company_id)
    stores = list(db.scalars(select(Store).where(Store.company_id == company_id,
                                                 Store.id.in_(store_ids))))
    b = _Builder(db, company_id, stores, s)
    cid8 = company_id[:8]
    vouchers: list[X.Voucher] = []
    summary = {"start": start.isoformat(), "end": end.isoformat(),
               "stores": [st.code for st in stores], "sales_vouchers": 0, "credit_notes": 0,
               "purchase_vouchers": 0, "bills": 0, "refund_bills": 0, "sales_paise": 0,
               "refunds_paise": 0, "purchases_paise": 0, "output_tax_paise": 0,
               "input_tax_paise": 0, "entered_days": 0, "skipped": [], "warnings": []}
    states = {st.state for st in stores if st.state}
    if len(states) > 1:
        summary["warnings"].append(
            "These stores are in more than one state. Each state is its own GST registration, "
            "and Tally usually keeps each in its own company: export one state at a time.")
    if not s["godowns"] and len(stores) > 1:
        summary["warnings"].append("Godowns are off, so Tally will not tell the stores apart.")

    # ---- sales and refunds from bills, by store and day (or one voucher per bill)
    groups: dict[tuple, dict] = {}
    bills = db.scalars(select(Bill).where(
        Bill.company_id == company_id, Bill.store_id.in_(store_ids),
        Bill.state.in_(("paid", "refund")), Bill.business_day >= start,
        Bill.business_day <= end).order_by(Bill.business_day, Bill.bill_number))
    for bill in bills:
        base = "sales" if bill.state == "paid" else "credit_note"
        key = (bill.store_id, bill.business_day, base) if s["voucher_per"] == "day" \
            else (bill.store_id, bill.business_day, base, bill.id)
        g = groups.setdefault(key, {"products": {}, "misc": 0, "party": 0, "bills": [],
                                    "entered": 0})
        g["bills"].append(bill.bill_number)
        g["party"] += abs(bill.total_paise)
        for ln in bill.lines:
            gross = ln.qty_milli * ln.unit_price_paise // 1000 - ln.discount_paise
            if ln.product_id is None:
                g["misc"] += gross
                continue
            v = g["products"].setdefault(ln.product_id, [0, 0, 0])
            v[0] += ln.qty_milli
            v[1] += gross
            v[2] += ln.tax_paise
        summary["bills" if base == "sales" else "refund_bills"] += 1

    # ---- daily sales typed or uploaded for stores without tills
    if s["include_entered_sales"]:
        for r in db.scalars(select(DailySales).where(
                DailySales.company_id == company_id, DailySales.store_id.in_(store_ids),
                DailySales.source.in_(("upload", "entry")), DailySales.day >= start,
                DailySales.day <= end, DailySales.units_milli > 0)):
            key = (r.store_id, r.day, "sales") if s["voucher_per"] == "day" \
                else (r.store_id, r.day, "sales", "entered")
            g = groups.setdefault(key, {"products": {}, "misc": 0, "party": 0, "bills": [],
                                        "entered": 0})
            p = b.product(r.product_id)
            v = g["products"].setdefault(r.product_id, [0, 0, 0])
            v[0] += r.units_milli
            v[1] += r.revenue_paise
            v[2] += tax_inside(r.revenue_paise, p.gst_rate_bp)
            g["party"] += r.revenue_paise
            g["entered"] += 1

    for key in sorted(groups, key=lambda k: (k[1], b.stores[k[0]].code, k[2], k[3:])):
        g = groups[key]
        store, day, base = b.stores[key[0]], key[1], key[2]
        code = store.code
        ymd = day.strftime("%Y%m%d")
        if s["voucher_per"] == "day":
            number = f"{code}/{ymd}" if base == "sales" else f"{code}/R/{ymd}"
            remote = f"dqstack:{cid8}:{base}:{code}:{ymd}"
        elif key[3] == "entered":
            number, remote = f"{code}/D/{ymd}", f"dqstack:{cid8}:entered:{code}:{ymd}"
        else:
            number = f"{code}/{g['bills'][0]}"
            remote = f"dqstack:{cid8}:{base}:{key[3]}"
        what = []
        if g["bills"]:
            n = len(g["bills"])
            what.append(f"{n} {'bill' if base == 'sales' else 'refund'}{'s' if n != 1 else ''}")
        if g["entered"]:
            what.append("daily sales entered or uploaded")
            summary["entered_days"] += 1
        narration = f"DQ-Stack {store.name} ({code}), {day:%d %b %Y}: {', '.join(what)}"
        v = b.sales_voucher(base, store, day, number, remote, g["products"], g["misc"],
                            g["party"], narration)
        if not v.lines and not g["misc"]:
            continue
        vouchers.append(v)
        tax = sum(p[2] for p in g["products"].values())
        if base == "sales":
            summary["sales_vouchers"] += 1
            summary["sales_paise"] += g["party"]
            summary["output_tax_paise"] += tax
        else:
            summary["credit_notes"] += 1
            summary["refunds_paise"] += g["party"]
            summary["output_tax_paise"] -= tax

    # ---- purchases: deliveries received in the period (local date)
    lo = datetime.combine(start, time(0, 0), tzinfo=tz).astimezone(timezone.utc)
    hi = datetime.combine(end + timedelta(days=1), time(0, 0), tzinfo=tz).astimezone(timezone.utc)
    sups = {x.id: x for x in db.scalars(select(Supplier).where(Supplier.company_id == company_id))}
    deliveries = db.scalars(select(Delivery).where(
        Delivery.company_id == company_id, Delivery.store_id.in_(store_ids),
        Delivery.created_at >= lo, Delivery.created_at < hi).order_by(Delivery.created_at))
    for d in deliveries:
        day = _local_day(d.created_at, tz)
        if not start <= day <= end:          # SQLite compares naive times; re-check locally
            continue
        v = b.purchase_voucher(d, b.stores[d.store_id], day, sups.get(d.supplier_id or ""),
                               summary["skipped"])
        if v is None:
            continue
        vouchers.append(v)
        summary["purchase_vouchers"] += 1
        party = next(e for e in v.entries if e.party)
        summary["purchases_paise"] += party.amount
        summary["input_tax_paise"] += -sum(e.amount for e in v.entries if not e.party)

    applicable = date(2017, 7, 1)          # GST's first day: one stable date for every item
    items = sorted(b.items.values(), key=lambda it: it.name.casefold())
    company = s["tally_company"] or None
    masters = X.masters_xml(company, sorted(b.units.items()), sorted(b.groups),
                            [b.godown[sid] for sid in sorted(b.godown, key=lambda x: b.stores[x].code)],
                            [b.ledgers[k] for k in sorted(b.ledgers)], items, applicable)
    summary["items"] = len(items)
    summary["ledgers"] = len(b.ledgers)
    summary["warnings"].extend(b.warnings)
    return {"masters": masters, "vouchers": X.vouchers_xml(company, vouchers), "summary": summary}


README = """DQ-Stack export for Tally ({period})

Import the two files in this order, in TallyPrime: Import > Masters, then
Import > Transactions (Tally.ERP 9: Gateway of Tally > Import of Data).

  1-masters.xml   units, stock groups, godowns, ledgers and stock items
  2-vouchers.xml  {counts}

Before the first import:
  * Make sure GST is enabled for the company (F11 features) and that credit notes may
    be made in invoice mode, if the file has credit notes.
  * Check the ledger names in DQ-Stack (Connectors > Tally) match this company's.
    A ledger that does not exist yet is created by the masters file.
  * Each voucher carries a REMOTEID from DQ-Stack. Import a period once; if you must
    import it again, delete the earlier vouchers first or choose Tally's option to
    replace duplicates.

Totals in this file: sales {sales}, refunds {refunds}, purchases {purchases}.
"""


def export_zip(db: Session, company_id: str, store_ids: list[str], start: date, end: date,
               conn: Connector | None = None) -> tuple[bytes, dict]:
    from ..formats import rupees
    out = build_export(db, company_id, store_ids, start, end, conn)
    sm = out["summary"]
    counts = (f"{sm['sales_vouchers']} sales, {sm['credit_notes']} credit notes, "
              f"{sm['purchase_vouchers']} purchases")
    note = README.format(period=f"{start:%d %b %Y} to {end:%d %b %Y}", counts=counts,
                         sales=rupees(sm["sales_paise"]).replace("₹", "Rs "),
                         refunds=rupees(sm["refunds_paise"]).replace("₹", "Rs "),
                         purchases=rupees(sm["purchases_paise"]).replace("₹", "Rs "))
    if sm["skipped"] or sm["warnings"]:
        note += "\nPlease note:\n" + "\n".join("  * " + x for x in sm["warnings"] + sm["skipped"]) + "\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("1-masters.xml", out["masters"])
        z.writestr("2-vouchers.xml", out["vouchers"])
        z.writestr("README.txt", note.replace("\n", "\r\n"))
    return buf.getvalue(), sm


# =================================================================== import

UNIT_WORDS = {
    "kg": ("kg", "kgs", "kilogram", "kilograms", "kilo", "kgs."),
    "litre": ("l", "ltr", "ltrs", "litre", "litres", "liter", "liters", "lt"),
    "each": ("nos", "no", "no.", "pcs", "pc", "piece", "pieces", "each", "ea", "unit", "units",
             "box", "boxes", "pkt", "pkts", "packet", "packets", "pack", "bag", "bags", "btl",
             "bottle", "bottles", "set", "sets", "pair", "pairs", "roll", "rolls", "tin", "tins",
             "jar", "jars", "can", "cans", "bundle", "bundles", "ctn", "carton", "cartons",
             "strip", "strips", "sachet", "sachets", "doz", "dozen", "tub", "pouch", "pouches"),
}
_UNIT = {w: u for u, words in UNIT_WORDS.items() for w in words}


def our_unit(raw: str | None) -> str:
    """Tally's unit as each, kg or litre; blank (keep what is stored, or each for a new
    product) when it is none of those, such as grams or a compound unit."""
    return _UNIT.get((raw or "").strip().lower(), "")


def _sku_for(it: X.StockItem) -> str:
    return (it.part_no or (it.aliases[0] if it.aliases else "") or it.name).strip()


def _num(d: Decimal | None) -> str:
    return "" if d is None else format(d.normalize(), "f")


PRODUCT_HEADERS = ["SKU", "Name", "Category", "Unit", "MRP", "Selling price", "Cost",
                   "GST rate", "HSN"]
SALES_HEADERS = ["Date", "Store code", "SKU", "Units", "Revenue"]


def product_rows(items: list[X.StockItem]) -> tuple[list[list], dict]:
    rows, notes = [], {"unknown_units": set()}
    for it in items:
        unit = our_unit(it.unit)
        if it.unit and not unit:
            notes["unknown_units"].add(it.unit)
        price = it.price
        if price is not None and it.gst_rate is not None:
            # Tally's standard selling price is before GST; DQ-Stack keeps the shelf price.
            price = (price * (100 + it.gst_rate) / 100).quantize(Decimal("0.01"))
            if it.mrp is not None and price > it.mrp:
                price = it.mrp
        rows.append([_sku_for(it), it.name, it.group or "", unit, _num(it.mrp), _num(price),
                     _num(it.cost), _num(it.gst_rate), it.hsn or ""])
    notes["unknown_units"] = sorted(notes["unknown_units"])
    return rows, notes


def _resolver(db: Session, company_id: str, items: list[X.StockItem]):
    by_sku, by_name, dup = {}, {}, set()
    for p in db.scalars(select(Product).where(Product.company_id == company_id)):
        by_sku[p.sku.casefold()] = p
        k = (p.name or "").strip().casefold()
        if k in by_name:
            dup.add(k)
        by_name[k] = p
    file_sku = {it.name.casefold(): _sku_for(it) for it in items}
    file_rate = {it.name.casefold(): it.gst_rate for it in items}

    def sku_rate(name: str) -> tuple[str, Decimal | None]:
        k = name.strip().casefold()
        sku = file_sku.get(k)
        p = by_sku.get((sku or name).casefold()) if (sku or name) else None
        if p is None and k in by_name and k not in dup:
            p = by_name[k]
        rate = file_rate.get(k)
        if rate is None and p is not None and p.gst_rate_bp is not None:
            rate = Decimal(p.gst_rate_bp) / 100
        return (p.sku if p is not None else (sku or name.strip())), rate
    return sku_rate


def _store_resolver(db: Session, company_id: str, s: dict, default_store: Store | None):
    stores = list(db.scalars(select(Store).where(Store.company_id == company_id)))
    by_code = {st.code.casefold(): st.code for st in stores}
    by_name = {st.name.strip().casefold(): st.code for st in stores}
    mapping = {k.casefold(): v for k, v in (s.get("godown_map") or {}).items()}

    def code(godown: str | None) -> str:
        if godown:
            g = godown.strip().casefold()
            for table in (mapping, by_code, by_name):
                if g in table:
                    return table[g]
        if default_store is not None:
            return default_store.code
        return godown or ""
    return code


def _share(total: int, weights: list[Decimal]) -> list[int]:
    """Split `total` paise in proportion to weights, adding up exactly."""
    w = sum(weights)
    if not weights:
        return []
    if w == 0:
        return [0] * (len(weights) - 1) + [total]
    raw = [Decimal(total) * x / w for x in weights]
    out = [int(r) for r in raw]                   # toward zero
    rest = total - sum(out)
    order = sorted(range(len(raw)), key=lambda i: -(abs(raw[i] - out[i])))
    step = 1 if rest > 0 else -1
    for i in order[:abs(rest)]:
        out[i] += step
    return out


def sales_rows(db: Session, company_id: str, vouchers: list[X.ReadVoucher],
               items: list[X.StockItem], s: dict, default_store: Store | None
               ) -> tuple[list[list], dict]:
    """One row per voucher line: date, store, SKU, units and what the customer paid
    for it including GST. The goods' value plus the voucher's GST ledgers is shared
    over its lines by their value with tax at each item's rate, so the rows add up to
    exactly what was paid for the goods. Round-off and lines that are only a ledger
    (a misc sale, freight) are not product sales and are left out."""
    sku_rate = _resolver(db, company_id, items)
    store_code = _store_resolver(db, company_id, s, default_store)
    rows, notes = [], {"vouchers": 0, "cancelled": 0, "other_types": defaultdict(int)}
    for v in vouchers:
        if v.base == "other":
            notes["other_types"][v.vtype or "unnamed"] += 1
            continue
        if v.cancelled:
            notes["cancelled"] += 1
            continue
        if v.day is None or not v.lines:
            continue
        notes["vouchers"] += 1
        sign = 1 if v.base == "sale" else -1
        resolved = [sku_rate(ln.item) for ln in v.lines]
        goods = sum(ln.amount_paise for ln in v.lines)
        avg = Decimal(v.tax_paise) / goods if goods else Decimal(0)
        weights = [Decimal(ln.amount_paise) * (1 + (r / 100 if r is not None else avg))
                   for ln, (_, r) in zip(v.lines, resolved)]
        revenue = _share(goods + v.tax_paise, weights)
        for ln, (sku, _), rev in zip(v.lines, resolved, revenue):
            rows.append([v.day.isoformat(), store_code(ln.godown), sku,
                         _num(sign * ln.qty), f"{Decimal(sign * rev) / 100:.2f}"])
    notes["other_types"] = dict(notes["other_types"])
    return rows, notes


def convert(db: Session, company_id: str, content: bytes, kind: str, conn: Connector | None,
            default_store: Store | None) -> tuple[list[str], list[list], dict]:
    """A Tally XML file as rows of the products or sales template."""
    try:
        root = X.root_of(content)
    except X.TallyFileError as e:
        raise HTTPException(422, str(e)) from None
    items = X.stock_items(root)
    if kind == "products":
        if not items:
            raise HTTPException(422, "This Tally file has no stock items in it. In Tally, "
                                     "export the Stock Items list (Masters) as XML.")
        rows, notes = product_rows(items)
        notes["items"] = len(items)
        return PRODUCT_HEADERS, rows, notes
    vs = X.vouchers(root)
    if not vs:
        raise HTTPException(422, "This Tally file has no vouchers in it. In Tally, export the "
                                 "Day Book or the Sales Register as XML (Data Interchange).")
    rows, notes = sales_rows(db, company_id, vs, items, settings_of(conn), default_store)
    if not rows:
        raise HTTPException(422, "This Tally file has no sales or sales returns in it "
                                 f"(found: {', '.join(sorted(notes['other_types'])) or 'none'}).")
    return SALES_HEADERS, rows, notes


def rows_to_csv(headers: list[str], rows: list[list]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    w.writerows(rows)
    return buf.getvalue()


def stamp(conn: Connector) -> None:
    conn.updated_at = utcnow()
