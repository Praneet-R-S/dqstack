"""Build step 3 -- Imports.

Done when: a 5,000-row product file with 20 bad rows imports 4,980 rows;
re-uploading it changes nothing; undo removes it.

Also acceptance scenario 1 (onboarding with 3 bad rows, then the corrected file
creates no duplicates), column matching remembered per source, the error file,
undo that refuses to destroy what has been used since, and the other kinds.
"""

from __future__ import annotations

import csv
import io

from sqlalchemy import func, select

from dqstack.app import db as dbmod
from dqstack.app.imports import parsing as P
from dqstack.app.models import Product, StockMovement

from .conftest import invite_and_join

HEAD = ["SKU", "Name", "Category", "Unit", "MRP", "Selling price", "Cost", "GST rate", "HSN",
        "Shelf life days", "Barcodes", "Case pack"]


def ean13(n: int) -> str:
    body = f"890{n:09d}"
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(body))
    return body + str((10 - total % 10) % 10)


def good_row(i: int) -> list[str]:
    return [f"SKU{i:05d}", f"Product {i}", ["Staples", "Dairy", "Snacks"][i % 3], "each",
            "120", "110", "80", "5", "19012000", "180", ean13(i), "12"]


def csv_bytes(rows: list[list[str]], head=HEAD) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(head)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def products_file_with_20_bad_rows() -> tuple[bytes, dict[int, str]]:
    """5,000 data rows. Returns the file and {spreadsheet row: expected problem}."""
    rows = [good_row(i) for i in range(5000)]
    bad: dict[int, str] = {}

    def spoil(i: int, col: str, value: str, problem: str):
        rows[i][HEAD.index(col)] = value
        bad[i + 2] = problem

    for i in (10, 20, 30):
        spoil(i, "SKU", "", "SKU is missing")
    for i in (40, 50):
        spoil(i, "Name", "", "name is missing")
    for i in (60, 70, 80):
        spoil(i, "Selling price", "150", "higher than MRP")
    for i in (90, 100):
        spoil(i, "Cost", "130", "cost is higher than MRP")
    for i in (110, 120):
        spoil(i, "GST rate", "15", "is not a GST rate")
    for i in (130, 140):
        spoil(i, "SKU", "SKU00001", "appears again")
    for i in (150, 160):
        spoil(i, "Barcodes", ean13(i)[:-1] + str((int(ean13(i)[-1]) + 1) % 10), "check digit")
    spoil(170, "Barcodes", ean13(5), "is also on row 7")
    spoil(180, "Unit", "dozen", "must be each, kg or litre")
    spoil(190, "HSN", "12AB", "must be 4 to 8 digits")
    spoil(200, "Shelf life days", "-5", "Shelf life days must be between")
    assert len(bad) == 20
    return csv_bytes(rows), bad


def _count_products() -> int:
    with dbmod.SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(Product))


def test_done_when_5000_rows_20_bad_reupload_changes_nothing_undo_removes(client):
    client.signup()
    content, bad = products_file_with_20_bad_rows()

    r = client.upload("products", content, "products.csv")
    assert r.status_code == 201, r.text
    job = r.json()
    assert job["status"] == "checked"
    assert (job["rows_read"], job["rows_new"], job["rows_failed"]) == (5000, 4980, 20)
    reported = {e["row"]: " ".join(e["problems"]) for e in job["errors"]}
    assert set(reported) == set(bad)
    for row, problem in bad.items():
        assert problem in reported[row], (row, reported[row])

    first = client.post(f"/api/imports/{job['id']}/commit").json()
    assert first["status"] == "imported" and first["rows_new"] == 4980
    assert _count_products() == 4980

    again = client.import_file("products", content, "products.csv")
    assert (again["rows_new"], again["rows_updated"], again["rows_unchanged"],
            again["rows_failed"]) == (0, 0, 4980, 20)
    assert _count_products() == 4980

    undone = client.post(f"/api/imports/{first['id']}/undo").json()
    assert undone["status"] == "undone" and undone["undo_report"]["kept"] == 0
    assert _count_products() == 0
    assert client.post(f"/api/imports/{first['id']}/undo").status_code == 409


def test_scenario_1_onboarding_with_three_bad_rows(client):
    client.signup()
    rows = [good_row(i) for i in range(50)]
    rows[4][HEAD.index("Cost")] = "200"                 # cost above MRP
    rows[9][HEAD.index("Name")] = ""                    # no name
    rows[19][HEAD.index("MRP")] = "12,3x"               # not an amount
    job = client.import_file("products", csv_bytes(rows))
    assert job["rows_new"] == 47
    assert [(e["row"], e["problems"][0]) for e in job["errors"]] == [
        (6, "cost is higher than MRP"), (11, "name is missing"), (21, "MRP '12,3x' is not an amount")]

    fixed = [good_row(i) for i in range(50)]
    again = client.import_file("products", csv_bytes(fixed))
    assert (again["rows_new"], again["rows_unchanged"], again["rows_failed"]) == (3, 47, 0)
    assert _count_products() == 50


def test_the_error_file_holds_every_bad_row_with_its_problem(client):
    client.signup()
    rows = [good_row(i) for i in range(5)]
    rows[2][HEAD.index("GST rate")] = "15"
    job = client.upload("products", csv_bytes(rows)).json()
    r = client.get(f"/api/imports/{job['id']}/errors.csv")
    assert r.status_code == 200
    got = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert got[0] == ["Row", *HEAD, "Problem"]
    assert len(got) == 2 and got[1][0] == "4" and "GST rate 15" in got[1][-1]


def test_columns_are_matched_and_remembered_per_source(client):
    client.signup()
    head = ["Item Code", "Item Description", "M.R.P.", "Sale Price", "Purchase Rate", "Tax %"]
    body = csv_bytes([["A1", "Atta 5 kg", "Rs. 1,250", "₹1,199.50", "1,000", "5%"]], head)
    job = client.upload("products", body).json()
    assert job["status"] == "checked", job
    assert job["mapping"] == {"sku": "Item Code", "name": "Item Description", "mrp": "M.R.P.",
                              "price": "Sale Price", "cost": "Purchase Rate", "gst": "Tax %"}

    odd = ["Ref", "Item Description"]
    job2 = client.upload("products", csv_bytes([["B1", "Besan"]], odd)).json()
    assert job2["status"] == "needs_mapping" and "SKU, which is required" in job2["message"]
    fixed = client.put(f"/api/imports/{job2['id']}/mapping",
                       {"mapping": {"sku": "Ref", "name": "Item Description"}}).json()
    assert fixed["status"] == "checked"
    client.post(f"/api/imports/{job2['id']}/commit")
    # Next file from the same source: the matching is remembered.
    job3 = client.upload("products", csv_bytes([["C1", "Chana"]], odd)).json()
    assert job3["status"] == "checked" and job3["mapping"]["sku"] == "Ref"


def test_indian_amounts_and_dates():
    assert P.money_paise("Rs. 1,23,456.50", "x") == 12345650
    assert P.money_paise("₹ 99", "x") == 9900
    assert P.day("03/04/2026").isoformat() == "2026-04-03"
    assert P.day("05 Nov 2026").isoformat() == "2026-11-05"
    assert P.day("2026-11-05").isoformat() == "2026-11-05"
    assert P.gst_bp("18%") == 1800 and P.gst_bp("0.05") == 500 and P.gst_bp("0.25") == 25
    assert P.quantity_milli("1.250", "q") == 1250
    assert P.weekdays("Mon, Thu", "d") == [0, 3]
    # 8,9,0,1,2,3,4,5,6,7,8,9 weighted 1,3,1,3... sums to 130, so the check digit is 0.
    assert P.gtin_ok("8901234567890") and not P.gtin_ok("8901234567893")


def test_xlsx_template_round_trip(client):
    client.signup()
    r = client.get("/api/imports/templates/products.xlsx")
    assert r.status_code == 200
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(r.content))
    assert [s.title for s in wb.worksheets] == ["Products", "Instructions", "Example"]
    ws = wb["Products"]
    titles = [c.value for c in ws[1]]
    row = {t: "" for t in titles}
    row.update({"SKU": "X1", "Name": "Ghee 1 L", "Unit": "litre", "MRP": 650, "GST rate": 12})
    ws.append([row[t] for t in titles])
    out = io.BytesIO()
    wb.save(out)
    job = client.import_file("products", out.getvalue(), "filled.xlsx")
    assert job["rows_new"] == 1
    assert client.get("/api/imports/templates/products.csv").status_code == 200


def test_undo_keeps_a_product_that_has_been_used_since(client):
    client.signup()
    store = client.post("/api/stores", {"code": "S1", "name": "One"}).json()
    job = client.import_file("products", csv_bytes([good_row(1), good_row(2)]))
    with dbmod.SessionLocal() as db:
        p = db.scalar(select(Product).where(Product.sku == "SKU00001"))
        db.add(StockMovement(company_id=p.company_id, store_id=store["id"], product_id=p.id,
                             qty_milli=5000, type="opening"))
        db.commit()
    rep = client.post(f"/api/imports/{job['id']}/undo").json()["undo_report"]
    assert rep["kept"] == 1 and "SKU00001 has stock" in rep["reasons"][0]
    assert _count_products() == 1


def test_undo_of_an_update_restores_old_values_unless_changed_again(client):
    client.signup()
    client.import_file("products", csv_bytes([good_row(1), good_row(2)]))
    cheaper = [good_row(1), good_row(2)]
    for r in cheaper:
        r[HEAD.index("Selling price")] = "99"
    upd = client.import_file("products", csv_bytes(cheaper))
    assert upd["rows_updated"] == 2
    # Someone changes SKU00002 again by a later file.
    later = [good_row(2)]
    later[0][HEAD.index("Selling price")] = "105"
    client.import_file("products", csv_bytes(later))
    rep = client.post(f"/api/imports/{upd['id']}/undo").json()["undo_report"]
    assert rep["reverted"] == 1 and rep["kept"] == 1
    with dbmod.SessionLocal() as db:
        prices = dict(db.execute(select(Product.sku, Product.price_paise)).all())
    assert prices == {"SKU00001": 11000, "SKU00002": 10500}


def test_a_barcode_on_two_products_names_both(client):
    client.signup()
    client.import_file("products", csv_bytes([good_row(1)]))
    clash = good_row(2)
    clash[HEAD.index("Barcodes")] = ean13(1)
    job = client.upload("products", csv_bytes([clash])).json()
    assert "already belongs to SKU SKU00001 (Product 1)" in job["errors"][0]["problems"][0]


def test_blank_cells_keep_stored_values(client):
    client.signup()
    client.import_file("products", csv_bytes([good_row(1)]))
    only_price = csv_bytes([["SKU00001", "", "", "", "", "105", "", "", "", "", "", ""]])
    job = client.import_file("products", only_price)
    assert job["rows_updated"] == 1
    with dbmod.SessionLocal() as db:
        p = db.scalar(select(Product))
        assert (p.name, p.price_paise, p.cost_paise, p.case_pack) == ("Product 1", 10500, 8000, 12)


def test_stores_suppliers_prices_calendar(client):
    client.signup()
    stores = ("Store code,Store name,State,Day close time,Weekly off,Mode\n"
              "PUN1,Pune Kothrud,maharashtra,21:30,Tue,connected\n"
              "KOC1,Kochi,Kerala,22:00,,standalone\n"
              "BAD1,Nowhere,Atlantis,22:00,,\n")
    sj = client.import_file("stores", stores)
    assert sj["rows_new"] == 2 and "Atlantis" in sj["errors"][0]["problems"][0]
    codes = {s["code"]: s for s in client.get("/api/stores").json()}
    assert codes["PUN1"]["state"] == "Maharashtra" and codes["PUN1"]["weekly_off"] == [1]

    client.import_file("products", csv_bytes([good_row(1), good_row(2)]))
    sup = ("Supplier,Lead time days,Minimum order value,Order days,SKU,Cost,Case pack\n"
           "Gupta Traders,2,\"5,000\",\"Mon, Thu\",SKU00001,78,12\n"
           "Gupta Traders,2,\"5,000\",\"Mon, Thu\",SKU00002,79,6\n"
           "Gupta Traders,3,,,,,\n"
           "Rao Agencies,1,0,,SKU99999,,\n")
    supj = client.import_file("suppliers", sup)
    probs = {e["row"]: e["problems"][0] for e in supj["errors"]}
    assert supj["rows_new"] == 2
    assert probs[4] == "lead time days differs from row 2 for the same supplier"
    assert "SKU99999 is not in your products" in probs[5]

    prices = ("SKU,Store code,Price,Start date,End date,Promotion\n"
              "SKU00001,,105,01-12-2026,07-12-2026,discount\n"
              "SKU00002,PUN1,99,01-12-2026,,\n"
              "SKU00002,,130,01-12-2026,,\n")
    pj = client.import_file("prices", prices)
    assert pj["rows_new"] == 2 and "higher than MRP" in pj["errors"][0]["problems"][0]
    again = client.import_file("prices", prices)
    assert again["rows_unchanged"] == 2

    cal = ("Name,Date,Kind,Store codes\n"
           "Temple fair,05-12-2026,local_event,KOC1\n"
           "Stock take closure,31-03-2027,closure,PUN1|KOC1\n")
    cj = client.import_file("calendar", cal)
    assert cj["rows_new"] == 2
    events = client.get("/api/calendar?from=2026-12-01&to=2026-12-31").json()
    assert any(e["name"] == "Temple fair" and e["source"] == "company" for e in events)


def test_who_may_upload_what(make_client):
    owner = make_client()
    owner.signup()
    s1 = owner.post("/api/stores", {"code": "S1", "name": "One"}).json()
    mgr, buyer = make_client(), make_client()
    invite_and_join(owner, mgr, "Manoj Manager", "store_manager", [s1["id"]])
    invite_and_join(owner, buyer, "Puja Purchaser", "purchaser", [s1["id"]])
    assert mgr.upload("products", csv_bytes([good_row(1)])).status_code == 403
    assert mgr.upload("stores", "Store code,Store name\nX,Y\n").status_code == 403
    owner.import_file("products", csv_bytes([good_row(1)]))
    r = buyer.upload("suppliers", "Supplier,SKU\nGupta,SKU00001\n")
    assert r.status_code == 201 and r.json()["status"] == "checked"


def test_another_companys_import_is_not_found(make_client):
    a, b = make_client(), make_client()
    a.signup(company="Alpha", email="a@alpha.example")
    b.signup(company="Beta", email="b@beta.example")
    job = a.upload("products", csv_bytes([good_row(1)])).json()
    for url in (f"/api/imports/{job['id']}", f"/api/imports/{job['id']}/errors.csv"):
        assert b.get(url).status_code == 404
    assert b.post(f"/api/imports/{job['id']}/commit").status_code == 404
    assert b.get("/api/imports").json() == []


def test_unreadable_files_are_refused_plainly(client):
    client.signup()
    r = client.upload("products", b"\x00\x01", "old.xls")
    assert r.status_code == 422 and "Save As" in r.json()["error"]
    r = client.upload("products", b"", "empty.csv")
    assert r.status_code == 422
