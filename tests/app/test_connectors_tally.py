"""The Tally connector: an import file for Tally made from DQ-Stack's own records, and
Tally's own exports read into DQ-Stack through the Imports pipeline.

What these tests prove, and what they cannot:

* The export is well-formed XML in Tally's envelope; every voucher balances to the
  paisa; every stock item, ledger, godown and unit a voucher names is in the masters
  file; and the totals equal the bills, refunds, uploaded sales and deliveries they
  came from. Reading our own export back gives the same sales per product.
* Tally-shaped fixtures (written from Tally's documented XML: synthetic, no client
  data) import as products and sales with the pipeline's checks, a repeat changes
  nothing, and Undo removes them.
* Not proven here: that TallyPrime itself accepts the file. That needs a trial
  import into a real Tally company (deploy/README.md lists the checks).
"""

from __future__ import annotations

import io
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from dqstack.app import db as dbmod
from dqstack.app.connectors import tally as T
from dqstack.app.connectors import tally_xml as X
from dqstack.app.models import Bill, DailySales, Product, Store
from dqstack.app.sales import local_today, tz_of

from .conftest import invite_and_join

FIX = Path(__file__).parent / "fixtures" / "tally"


def _upload(client, kind, content: bytes, store_id=None, filename="tally.xml"):
    data = {"kind": kind}
    if store_id:
        data["store_id"] = store_id
    return client.http.post("/api/connectors/tally/import", data=data,
                            files={"file": (filename, content, "application/xml")},
                            headers=client._h())


def _company(make_client):
    owner = make_client()
    owner.signup()
    pun = owner.post("/api/stores", {"code": "PUN1", "name": "Pune, Kothrud",
                                     "state": "Maharashtra", "mode": "connected"}).json()
    koc = owner.post("/api/stores", {"code": "KOC1", "name": "Kochi",
                                     "state": "Kerala"}).json()
    return owner, pun, koc


def _products() -> dict[str, Product]:
    with dbmod.SessionLocal() as db:
        return {p.sku: p for p in db.scalars(select(Product))}


# =================================================================== import

def test_stock_items_import_as_products_repeat_safe_and_undoable(make_client):
    owner, pun, koc = _company(make_client)
    # Tally writes its exports in UTF-16 with a byte-order mark; read that as it comes.
    content = (FIX / "stock_items.xml").read_text(encoding="utf-8").encode("utf-16")
    r = _upload(owner, "products", content, filename="StockItems.xml")
    assert r.status_code == 201, r.text
    job = r.json()
    assert job["kind"] == "products" and job["status"] == "checked", job
    assert (job["rows_read"], job["rows_new"], job["rows_failed"]) == (4, 4, 0)
    done = owner.post(f"/api/imports/{job['id']}/commit").json()
    assert done["status"] == "imported"
    p = _products()
    rice = p["RICE-BAS-5KG"]                       # SKU from the stock item's alias
    assert (rice.name, rice.category, rice.unit, rice.hsn, rice.gst_rate_bp) == \
        ("Basmati Rice 5kg", "Rice and Grains", "each", "10063020", 500)
    # MRP as given; the standard selling price (before GST, latest dated) becomes the
    # shelf price with GST: 800 x 1.05; the standard cost stays before GST.
    assert (rice.mrp_paise, rice.price_paise, rice.cost_paise) == (89_900, 84_000, 72_000)
    dal = p["DAL-TOOR-LOOSE"]                      # SKU from the part number
    assert (dal.unit, dal.gst_rate_bp, dal.hsn, dal.category) == ("kg", 0, "07134000", "Pulses")
    soap = p["SOAP-100"]
    # The later GST entry (18% from 22 Sep 2025) wins over the old 28%; "Primary" is no
    # category; a price with GST above MRP is held at MRP.
    assert (soap.gst_rate_bp, soap.category, soap.price_paise, soap.mrp_paise) == \
        (1800, None, 4_500, 4_500)
    assert (p["OIL-MUST-1L"].unit, p["OIL-MUST-1L"].gst_rate_bp) == ("each", 500)  # Btl, CGST+SGST

    again = _upload(owner, "products", content).json()
    assert (again["rows_new"], again["rows_updated"], again["rows_unchanged"]) == (0, 0, 4)
    owner.post(f"/api/imports/{again['id']}/commit")
    assert len(_products()) == 4

    undone = owner.post(f"/api/imports/{job['id']}/undo").json()
    assert undone["status"] == "undone" and undone["undo_report"]["reverted"] == 4
    assert _products() == {}


def test_day_book_import_as_sales_matches_the_vouchers(make_client):
    owner, pun, koc = _company(make_client)
    items = (FIX / "stock_items.xml").read_bytes()
    owner.post(f"/api/imports/{_upload(owner, 'products', items).json()['id']}/commit")
    # Godowns: one named in the settings, one that is a store code, and lines without a
    # godown go to the store chosen on upload.
    r = owner.put("/api/connectors/tally/settings", {"godown_map": {"Kothrud Godown": "PUN1"}})
    assert r.status_code == 200, r.text
    book = (FIX / "daybook.xml").read_bytes()
    r = _upload(owner, "sales_history", book, store_id=koc["id"], filename="DayBook.xml")
    assert r.status_code == 201, r.text
    job = r.json()
    assert job["status"] == "checked" and job["rows_failed"] == 0, job
    notes = job["tally"]
    assert notes["vouchers"] == 4 and notes["cancelled"] == 1
    assert notes["other_types"] == {"Purchase": 1, "Receipt": 1, "Sales Order": 1}
    owner.post(f"/api/imports/{job['id']}/commit")

    p = _products()
    code = {pun["id"]: "PUN1", koc["id"]: "KOC1"}
    sku = {x.id: s for s, x in p.items()}
    with dbmod.SessionLocal() as db:
        got = {(code[r.store_id], r.day.isoformat(), sku[r.product_id]):
               (r.units_milli, r.revenue_paise) for r in db.scalars(select(DailySales))}
    # Voucher 1 (1,815.00 paid, 0.02 rounded off) shares 1,714.42 of goods and 100.60 of
    # GST by value with tax; the credit note takes one rice back at 840.00.
    assert got == {
        ("PUN1", "2026-11-02", "RICE-BAS-5KG"): (2_000 - 1_000, 168_000 - 84_000),
        ("PUN1", "2026-11-02", "SOAP-100"): (3_000, 13_502),
        ("KOC1", "2026-11-02", "DAL-TOOR-LOOSE"): (1_500, 24_750),
        ("PUN1", "2026-11-03", "OIL-MUST-1L"): (2_000, 42_000),
        ("KOC1", "2026-11-03", "OIL-MUST-1L"): (1_000, 21_000),
    }
    again = _upload(owner, "sales_history", book, store_id=koc["id"]).json()
    assert again["rows_new"] == 0 and again["rows_updated"] == 0, again
    owner.post(f"/api/imports/{again['id']}/commit")
    owner.post(f"/api/imports/{job['id']}/undo")
    with dbmod.SessionLocal() as db:
        left = [r for r in db.scalars(select(DailySales)) if r.units_milli or r.revenue_paise]
    assert left == []


def test_bad_tally_files_are_refused_in_plain_words(make_client):
    owner, pun, koc = _company(make_client)
    assert "not readable XML" in _upload(owner, "products", b"<ENVELOPE><BODY>").json()["error"]
    assert "not a Tally export" in _upload(owner, "products", b"<html></html>").json()["error"]
    bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><ENVELOPE>&a;</ENVELOPE>'
    assert "DTD" in _upload(owner, "products", bomb).json()["error"]
    book = (FIX / "daybook.xml").read_bytes()
    assert "no stock items" in _upload(owner, "products", book).json()["error"]
    # The ordinary upload says where a Tally file goes instead of reading it as a table.
    r = owner.upload("products", (FIX / "stock_items.xml").read_bytes(), "items.xml")
    assert r.status_code == 422 and "Connectors" in r.json()["error"]
    # Only an Owner or Admin.
    mgr = make_client()
    invite_and_join(owner, mgr, "Meera Manager", "store_manager", [pun["id"]])
    assert _upload(mgr, "products", book).status_code == 403
    assert mgr.get("/api/connectors/tally/export?start=2026-11-01&end=2026-11-02").status_code == 403


# =================================================================== export

def _shop(make_client):
    """A connected store with bills (a discount, a misc item, a refund), a standalone
    store with an uploaded day, and two deliveries: one from another state."""
    owner = make_client()
    owner.signup()
    s1 = owner.post("/api/stores", {"code": "S1", "name": "Andheri", "state": "Maharashtra",
                                    "mode": "connected"}).json()
    s2 = owner.post("/api/stores", {"code": "S2", "name": "Thane", "state": "Maharashtra"}).json()
    owner.import_file("products", "SKU,Name,Category,Unit,MRP,Selling price,Cost,GST rate,HSN\n"
                                  "RICE,Rice 5 kg,Grains,each,500,450,380,5,10063020\n"
                                  "SOAP,Bath soap,Personal care,each,45,45,32,18,34011110\n"
                                  "ONION,Onions,Vegetables,kg,40,40,30,0,07031010\n"
                                  "RICE2,Rice 5 kg,Grains,each,520,480,400,5,10063020\n")
    pid = {p.sku: p.id for p in _products().values()}
    owner.post("/api/people/me/manager-pin", {"pin": "4321"})

    def bill(number, lines, extra=None):
        total = sum(q * pr // 1000 - d for _, q, pr, d in lines)
        body = {"store_id": s1["id"], "bill_number": number, "device_id": "till-1",
                "lines": [{"product_id": p, "qty_milli": q, "unit_price_paise": pr,
                           "discount_paise": d, **({"override_reason": "Damaged pack"} if d else {})}
                          for p, q, pr, d in lines] + (extra or []),
                "payments": [{"method": "cash", "amount_paise": total + sum(
                    e["qty_milli"] * e["unit_price_paise"] // 1000 for e in (extra or []))}]}
        r = owner.post("/api/checkout/bills", body)
        assert r.status_code == 201, r.text
        return r.json()

    bill("B-1", [(pid["RICE"], 2000, 45_000, 0), (pid["SOAP"], 3000, 4_500, 0)])
    bill("B-2", [(pid["ONION"], 1_250, 4_000, 0), (pid["SOAP"], 1000, 4_500, 500)],
         extra=[{"description": "Carry bag", "qty_milli": 1000, "unit_price_paise": 500}])
    bill("B-3", [(pid["RICE2"], 1000, 48_000, 0), (pid["RICE"], 1000, 45_000, 0)])
    r = owner.post("/api/checkout/refunds", {
        "store_id": s1["id"], "refund_number": "R-1", "original_bill_number": "B-1",
        "reason": "Wrong fragrance", "manager_pin": "4321",
        "lines": [{"product_id": pid["SOAP"], "qty_milli": 1000, "resaleable": True}]})
    assert r.status_code == 201, r.text
    with dbmod.SessionLocal() as db:
        cid = owner.me["company"]["id"]
        today = local_today(tz_of(db, cid))
    owner.import_file("daily_sales", f"Date,Store code,SKU,Units,Revenue\n"
                                     f"{today:%d-%m-%Y},S2,RICE,3,1350\n"
                                     f"{today:%d-%m-%Y},S2,SOAP,2,90\n")
    local = owner.post("/api/suppliers", {"name": "Gupta Traders",
                                          "gstin": "27AAPFU0939F1ZV"}).json()
    other = owner.post("/api/suppliers", {"name": "Bengaluru Foods",
                                          "gstin": "29AAGCB7383J1Z4"}).json()
    for sup, inv, lines in ((local, "GT/88", [("RICE", 10_000, 38_000), ("SOAP", 12_000, 3_200)]),
                            (other, "BF-12", [("ONION", 25_500, 3_000), ("RICE2", 5_000, 40_000)])):
        r = owner.post("/api/deliveries", {"store_id": s1["id"], "supplier_id": sup["id"],
                                           "invoice_number": inv, "lines": [
            {"product_id": pid[s], "received_milli": q, "unit_cost_paise": c}
            for s, q, c in lines]})
        assert r.status_code == 201, r.text
    return owner, s1, s2, pid, today


def _zip(owner, today, stores=None):
    q = f"/api/connectors/tally/export?start={today}&end={today}" + \
        (f"&stores={stores}" if stores else "")
    r = owner.get(q)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert sorted(z.namelist()) == ["1-masters.xml", "2-vouchers.xml", "README.txt"]
    return z.read("1-masters.xml"), z.read("2-vouchers.xml"), z.read("README.txt").decode()


def _paise(text: str) -> int:
    return int((Decimal(text.strip()) * 100).quantize(Decimal("1")))


def test_export_balances_matches_the_bills_and_reads_back(make_client):
    owner, s1, s2, pid, today = _shop(make_client)
    masters, vouchers, readme = _zip(owner, today)
    # Plain ASCII, with Tally's own &#4; markers where Tally writes them.
    masters.decode("ascii"), vouchers.decode("ascii")
    assert b"&#4; Applicable" in masters
    mroot, vroot = X.root_of(masters), X.root_of(vouchers)
    assert mroot.find("BODY/IMPORTDATA/REQUESTDESC/REPORTNAME").text == "All Masters"
    assert vroot.find("BODY/IMPORTDATA/REQUESTDESC/REPORTNAME").text == "Vouchers"
    assert all(len(m) == 1 for m in vroot.iter("TALLYMESSAGE"))

    names = {kind: {e.get("NAME") for e in mroot.iter(kind)}
             for kind in ("LEDGER", "STOCKITEM", "GODOWN", "UNIT", "STOCKGROUP")}
    assert names["GODOWN"] == {"Andheri", "Thane"}
    assert names["UNIT"] == {"Nos", "kg"}
    assert names["STOCKGROUP"] == {"Grains", "Personal care", "Vegetables"}
    # Two products share a name: the second carries its SKU, and every item its alias.
    assert names["STOCKITEM"] == {"Rice 5 kg", "Rice 5 kg (RICE2)", "Bath soap", "Onions"}
    soap = next(e for e in mroot.iter("STOCKITEM") if e.get("NAME") == "Bath soap")
    assert [n.text for n in soap.iter("NAME")] == ["Bath soap", "SOAP"]
    assert soap.find("GSTDETAILS.LIST/HSNCODE").text == "34011110"
    rates = {r.find("GSTRATEDUTYHEAD").text: r.findtext("GSTRATE")
             for r in soap.iter("RATEDETAILS.LIST")}
    assert rates == {"CGST": "9", "SGST/UTGST": "9", "IGST": "18", "Cess": None}

    with dbmod.SessionLocal() as db:
        bills = list(db.scalars(select(Bill).where(Bill.state.in_(("paid", "refund")))))
        uploads = list(db.scalars(select(DailySales).where(DailySales.source == "upload")))
    paid = [b for b in bills if b.state == "paid"]
    refunds = [b for b in bills if b.state == "refund"]

    by_type = defaultdict(list)
    for v in vroot.iter("VOUCHER"):
        by_type[v.findtext("VOUCHERTYPENAME")].append(v)
        # Debit equals credit, to the paisa, counting every ledger and inventory line.
        total = sum(_paise(e.findtext("AMOUNT")) for e in v.findall("LEDGERENTRIES.LIST")) + \
            sum(_paise(e.findtext("AMOUNT")) for e in v.findall("ALLINVENTORYENTRIES.LIST"))
        assert total == 0, v.findtext("VOUCHERNUMBER")
        used = [e.findtext("LEDGERNAME") for e in v.findall("LEDGERENTRIES.LIST")]
        assert len(used) == len(set(used)), used     # two rates, one "Output CGST": one line
        for e in v.findall("LEDGERENTRIES.LIST"):
            assert (e.findtext("ISDEEMEDPOSITIVE") == "Yes") == (_paise(e.findtext("AMOUNT")) < 0)
            assert e.findtext("LEDGERNAME") in names["LEDGER"] | {"Cash"}
        for ie in v.findall("ALLINVENTORYENTRIES.LIST"):
            assert ie.findtext("STOCKITEMNAME") in names["STOCKITEM"]
            assert ie.findtext("BATCHALLOCATIONS.LIST/GODOWNNAME") in names["GODOWN"]
            assert ie.findtext("ACCOUNTINGALLOCATIONS.LIST/LEDGERNAME") in names["LEDGER"]
            assert ie.findtext("ACTUALQTY").split()[-1] in names["UNIT"]
    assert {k: len(v) for k, v in by_type.items()} == {"Sales": 2, "Credit Note": 1, "Purchase": 2}

    def party(v):
        return next(_paise(e.findtext("AMOUNT")) for e in v.findall("LEDGERENTRIES.LIST")
                    if e.findtext("ISPARTYLEDGER") == "Yes")

    def tax(v, word):
        return sum(_paise(e.findtext("AMOUNT")) for e in v.findall("LEDGERENTRIES.LIST")
                   if word in e.findtext("LEDGERNAME"))

    sales = {v.findtext("VOUCHERNUMBER"): v for v in by_type["Sales"]}
    ymd = today.strftime("%Y%m%d")
    till, entered = sales[f"S1/{ymd}"], sales[f"S2/{ymd}"]
    # One voucher for the till's day: what customers paid, and the GST inside it.
    assert -party(till) == sum(b.total_paise for b in paid)
    assert tax(till, "CGST") + tax(till, "SGST") == sum(b.tax_paise for b in paid)
    assert tax(till, "IGST") == 0                      # a sale in the store is intra-state
    misc = next(e for e in till.findall("LEDGERENTRIES.LIST")
                if e.findtext("LEDGERNAME") == "Sales - Misc items")
    assert _paise(misc.findtext("AMOUNT")) == 500
    # The uploaded day at the standalone store, tax worked out from each GST rate.
    assert -party(entered) == sum(r.revenue_paise for r in uploads) == 1350_00 + 90_00
    assert tax(entered, "GST") == round(135_000 * 500 / 10_500) + round(9_000 * 1800 / 11_800)
    cn = by_type["Credit Note"][0]
    assert party(cn) == -sum(b.total_paise for b in refunds) == 4_500
    # A refund bill keeps its tax on its lines.
    assert tax(cn, "GST") == -sum(ln.tax_paise for b in refunds for ln in b.lines) != 0

    purchases = {v.findtext("REFERENCE"): v for v in by_type["Purchase"]}
    gt, bf = purchases["GT/88"], purchases["BF-12"]
    assert party(gt) == 10 * 38_000 + round(380_000 * 0.05) + 12 * 3_200 + round(38_400 * 0.18)
    assert tax(gt, "IGST") == 0 and tax(gt, "Input CGST") < 0
    # Karnataka supplier, Maharashtra store: IGST only.
    assert tax(bf, "CGST") == 0 and tax(bf, "SGST") == 0
    assert -tax(bf, "IGST") == round(5 * 40_000 * 0.05)          # onions are 0%
    assert party(bf) == 25_500 * 3_000 // 1000 + 5 * 40_000 + 10_000
    assert gt.find("LEDGERENTRIES.LIST/BILLALLOCATIONS.LIST/NAME").text == "GT/88"
    sup = next(e for e in mroot.iter("LEDGER") if e.get("NAME") == "Bengaluru Foods")
    assert (sup.findtext("PARENT"), sup.findtext("PARTYGSTIN"), sup.findtext("LEDSTATENAME")) == \
        ("Sundry Creditors", "29AAGCB7383J1Z4", "Karnataka")
    assert "Import > Masters" in readme and "1-masters.xml" in readme

    # Read our own vouchers back with the Tally reader: per store and product, units and
    # money equal the bills less the refund, and the uploaded day.
    with dbmod.SessionLocal() as db:
        cid = owner.me["company"]["id"]
        items = X.stock_items(mroot)
        rows, notes = T.sales_rows(db, cid, X.vouchers(vroot), items, T.DEFAULTS, None)
    back = defaultdict(lambda: [Decimal(0), 0])
    for day, store, sku, units, rev in rows:
        back[(store, sku)][0] += Decimal(units)
        back[(store, sku)][1] += _paise(rev)
    want = defaultdict(lambda: [Decimal(0), 0])
    sku_of = {v: k for k, v in pid.items()}
    for b in bills:
        sign = 1 if b.state == "paid" else -1
        for ln in b.lines:
            if ln.product_id:
                w = want[("S1", sku_of[ln.product_id])]
                w[0] += sign * Decimal(ln.qty_milli) / 1000
                w[1] += sign * (ln.qty_milli * ln.unit_price_paise // 1000 - ln.discount_paise)
    for r in uploads:
        w = want[("S2", sku_of[r.product_id])]
        w[0] += Decimal(r.units_milli) / 1000
        w[1] += r.revenue_paise
    assert {k: v[0] for k, v in back.items()} == {k: v[0] for k, v in want.items()}
    # Money: totals exact; each product within a paisa per bill line (GST is rounded
    # once per line when billed and once per product in the voucher).
    assert sum(v[1] for v in back.values()) == sum(v[1] for v in want.values())
    for k in want:
        assert abs(back[k][1] - want[k][1]) <= 3, (k, back[k], want[k])


def test_export_settings_per_bill_and_ledger_names(make_client):
    owner, s1, s2, pid, today = _shop(make_client)
    r = owner.put("/api/connectors/tally/settings", {
        "voucher_per": "bill", "sales_ledger": "Sales @ {rate}%", "output_cgst": "CGST Output {rate}%",
        "counter_party": "Counter Sales", "tally_company": "Sharma Stores 2026-27",
        "godowns": False})
    assert r.status_code == 200, r.text
    assert owner.put("/api/connectors/tally/settings", {"round_off": ""}).status_code == 422
    assert owner.put("/api/connectors/tally/settings", {"sales_ledger": "A<B"}).status_code == 422
    masters, vouchers, _ = _zip(owner, today, stores=s1["id"])
    vroot, mroot = X.root_of(vouchers), X.root_of(masters)
    assert vroot.findtext("BODY/IMPORTDATA/REQUESTDESC/STATICVARIABLES/SVCURRENTCOMPANY") == \
        "Sharma Stores 2026-27"
    sales = [v for v in vroot.iter("VOUCHER") if v.findtext("VOUCHERTYPENAME") == "Sales"]
    assert sorted(v.findtext("VOUCHERNUMBER") for v in sales) == ["S1/B-1", "S1/B-2", "S1/B-3"]
    assert len({v.get("REMOTEID") for v in vroot.iter("VOUCHER")}) == len(list(vroot.iter("VOUCHER")))
    ledgers = {e.get("NAME"): e.findtext("PARENT") for e in mroot.iter("LEDGER")}
    assert ledgers["Sales @ 5%"] == "Sales Accounts" and ledgers["Sales @ 18%"] == "Sales Accounts"
    assert ledgers["CGST Output 5%"] == "Duties & Taxes"
    assert ledgers["Counter Sales"] == "Cash-in-Hand"
    assert all(v.findtext("PARTYLEDGERNAME") == "Counter Sales" for v in sales)
    assert not list(mroot.iter("GODOWN")) and b"GODOWNNAME" not in vouchers
    # The same period exported twice is the same file: REMOTEIDs and numbers are stable.
    assert _zip(owner, today, stores=s1["id"])[1] == vouchers
    runs = owner.get("/api/connectors/runs?kind=tally").json()
    assert runs[0]["trigger"] == "export" and runs[0]["summary"]["sales_vouchers"] == 3


def test_ledger_and_tax_split_helpers():
    assert X.pct(1800) == "18" and X.pct(250) == "2.5" and X.pct(25) == "0.25" and X.pct(0) == "0"
    assert X.ledger_name("Sales GST {rate}%", 1200) == "Sales GST 12%"
    assert X.money(-181502) == "-1815.02" and X.money(5) == "0.05"
    assert X.qty_text(1250, "kg", 3) == "1.250 kg" and X.qty_text(3000, "Nos", 0) == "3 Nos"
    assert T._share(100, [Decimal(1), Decimal(1), Decimal(1)]) == [34, 33, 33]
    assert sum(T._share(-1001, [Decimal(3), Decimal(7)])) == -1001
    assert X.base_type("Sales - POS") == "sale" and X.base_type("Sales Return") == "return"
    assert X.base_type("Purchase Return") == "other" and X.base_type("Sales Order") == "other"
    assert T.our_unit("KGS") == "kg" and T.our_unit("Pcs") == "each" and T.our_unit("gm") == ""
    raw = ET.fromstring("<A><AMOUNT>-1,234.50</AMOUNT></A>")
    assert X.number(raw.findtext("AMOUNT")) == Decimal("-1234.50")
    assert X.number("Rs. 99.00/Nos") == Decimal("99.00")
    assert date(2026, 11, 2).strftime("%Y%m%d") == X.tally_date(date(2026, 11, 2))


def test_one_company_cannot_reach_anothers_connectors(make_client):
    owner, pun, koc = _company(make_client)
    owner.put("/api/connectors/tally/settings", {"counter_party": "Counter A"})
    other = make_client()
    other.signup(company="Beta Mart", name="Bina Owner")
    # Another company's store is "not found", as everywhere else.
    r = other.get(f"/api/connectors/tally/export?start=2026-11-01&end=2026-11-02&stores={pun['id']}")
    assert r.status_code == 404
    r = _upload(other, "sales_history", (FIX / "daybook.xml").read_bytes(), store_id=koc["id"])
    assert r.status_code == 404
    mine = other.get("/api/connectors").json()
    assert mine["tally"]["settings"]["counter_party"] == "Cash" and mine["tally"]["runs"] == []
    assert other.post("/api/connectors/zoho/sync", {}).status_code == 409
