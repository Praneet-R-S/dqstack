"""Build step 8 -- Decisions and orders.

Done when: a suggested order for a supplier with a Rs 5,000 minimum and 12-unit
cases is correct by hand calculation. That is acceptance scenario 12: target 300,
on hand 110, on order 60, case pack 12, supplier minimum Rs 5,000 -> 132 (130
rounded up to whole cases), raised if the order total is below Rs 5,000, with the
reason shown.

The forecast here is written directly, so the arithmetic is the only thing under
test; step 7's tests cover where the forecast comes from.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select

from dqstack.app import db as dbmod
from dqstack.app import ledger
from dqstack.app.mailer import outbox
from dqstack.app.models import ForecastRun, Product, Recommendation, Supplier
from dqstack.app.ordering import Line, line_quantity, next_order_day, raise_to_minimum

from .conftest import invite_and_join


# ------------------------------------------------------------------- by hand

def test_scenario_12_by_hand():
    # 300 - 110 - 60 = 130, rounded up to whole cases of 12 = 132.
    assert line_quantity(300_000, 110_000, 60_000, 12_000) == 132_000
    assert line_quantity(100_000, 110_000, 0, 12_000) == 0          # covered: nothing
    assert line_quantity(10_000, 0, 0, 12_000, moq=24_000) == 24_000

    # At Rs 40 a unit, 132 cost Rs 5,280: the minimum is met, nothing changes.
    met = [Line("p", "A", "A", 4_000, 12_000, 300_000, 110_000, 60_000, 130_000, 132_000)]
    assert raise_to_minimum(met, 500_000) is None and met[0].qty_milli == 132_000
    # At Rs 35 a unit, 132 cost Rs 4,620: one more case (12 x 35 = 420) makes Rs 5,040.
    short = [Line("p", "A", "A", 3_500, 12_000, 300_000, 110_000, 60_000, 130_000, 132_000)]
    reason = raise_to_minimum(short, 500_000)
    assert short[0].qty_milli == 144_000
    assert reason == "Raised from ₹4,620 to ₹5,040 to reach the ₹5,000 minimum order"


def test_the_raise_goes_to_what_runs_out_first():
    fast = Line("f", "F", "Fast", 1_000, 10_000, 0, 5_000, 0, 0, 10_000, weekly_milli=50_000)
    slow = Line("s", "S", "Slow", 1_000, 10_000, 0, 50_000, 0, 0, 10_000, weekly_milli=5_000)
    raise_to_minimum([fast, slow], 30_000)              # needs Rs 100 more: 1 case of 10
    assert (fast.qty_milli, slow.qty_milli) == (20_000, 10_000)


def test_next_order_day():
    mon = date(2026, 11, 9)
    assert next_order_day([0, 3], mon) == date(2026, 11, 12)       # Thursday
    assert next_order_day([0], mon) == date(2026, 11, 16)          # next Monday
    assert next_order_day([], mon) is None


# ------------------------------------------------------------------- through the app

def _world(make_client, unit_cost="35"):
    owner = make_client()
    owner.signup()
    s1 = owner.post("/api/stores", {"code": "S1", "name": "Andheri"}).json()
    owner.import_file("products", "SKU,Name,Unit,MRP,Selling price,Cost,Case pack\n"
                                  f"DAL,Toor dal 1 kg,each,60,55,{unit_cost},12\n"
                                  "RICE,Rice 5 kg,each,500,450,380,4\n"
                                  "OIL,Oil 1 L,each,160,150,130,12\n")
    owner.import_file("suppliers", "Supplier,Email,Phone,Lead time days,Minimum order value,"
                                   "Order days,SKU,Case pack\n"
                                   "Gupta Traders,orders@gupta.example,98220 12345,2,5000,"
                                   "\"Mon, Thu\",DAL,12\n")
    with dbmod.SessionLocal() as db:
        pid = {p.sku: p.id for p in db.scalars(select(Product))}
        sup = db.scalar(select(Supplier)).id
    cid = owner.me["company"]["id"]
    with dbmod.SessionLocal() as db:
        ledger.post(db, company_id=cid, store_id=s1["id"], product_id=pid["DAL"],
                    qty_milli=110_000, type="opening")
        ledger.post(db, company_id=cid, store_id=s1["id"], product_id=pid["RICE"],
                    qty_milli=10_000, type="opening")
        db.commit()
    # 60 already on order with the same supplier.
    po = owner.post("/api/orders", {"store_id": s1["id"], "supplier_id": sup, "lines": [
        {"product_id": pid["DAL"], "qty_milli": 60_000}]}).json()
    owner.post(f"/api/orders/{po['id']}/approve")
    return owner, cid, s1, pid, sup


def _forecast(cid, store_id, sup, pid, rows):
    """A succeeded forecast with one recommendation per (sku, target, on hand, on order,
    known, weekly)."""
    with dbmod.SessionLocal() as db:
        run = ForecastRun(company_id=cid, kind="nightly", status="succeeded",
                          data_cutoff=date.today() - timedelta(days=1))
        db.add(run)
        db.flush()
        ids = {}
        for sku, target, on_hand, on_order, known, weekly in rows:
            r = Recommendation(company_id=cid, run_id=run.id, store_id=store_id,
                               product_id=pid[sku], target_milli=target, on_hand_milli=on_hand,
                               on_order_milli=on_order, stock_known=known,
                               supplier_id=sup if sku == "DAL" else None,
                               week1_mid_milli=weekly, flagged=True,
                               money_at_risk_paise=10_000 if sku == "DAL" else 100)
            db.add(r)
            db.flush()
            ids[sku] = r.id
        db.commit()
        return run.id, ids


def test_done_when_scenario_12_through_the_orders_screen(make_client):
    owner, cid, s1, pid, sup = _world(make_client, unit_cost="35")
    _forecast(cid, s1["id"], sup, pid, [("DAL", 300_000, 110_000, 60_000, True, 70_000)])
    out = owner.get(f"/api/orders/suggestions?store_id={s1['id']}").json()
    g = out["groups"][0]
    assert g["supplier"] == "Gupta Traders" and g["min_order_paise"] == 500_000
    line = g["lines"][0]
    assert (line["need_milli"], line["case_milli"], line["qty_milli"]) == (130_000, 12_000, 144_000)
    assert g["value_before_minimum_paise"] == 132 * 3_500           # Rs 4,620 for 132
    assert g["total_paise"] == 144 * 3_500                          # Rs 5,040 for 144
    assert g["raised"] == "Raised from ₹4,620 to ₹5,040 to reach the ₹5,000 minimum order"
    assert g["alternative"].startswith("or wait and combine it with the order on")
    assert "rounded up to whole cases of 12" in line["reasons"]
    assert "raised to reach the supplier minimum" in line["reasons"]


def test_scenario_12_when_the_minimum_is_already_met(make_client):
    owner, cid, s1, pid, sup = _world(make_client, unit_cost="40")
    _forecast(cid, s1["id"], sup, pid, [("DAL", 300_000, 110_000, 60_000, True, 70_000)])
    g = owner.get(f"/api/orders/suggestions?store_id={s1['id']}").json()["groups"][0]
    assert g["lines"][0]["qty_milli"] == 132_000 and g["raised"] is None
    assert g["total_paise"] == 132 * 4_000


def test_decisions_change_what_is_ordered(make_client):
    owner, cid, s1, pid, sup = _world(make_client, unit_cost="40")
    run, ids = _forecast(cid, s1["id"], sup, pid, [
        ("DAL", 300_000, 110_000, 60_000, True, 70_000),
        ("RICE", 30_000, 10_000, 0, True, 10_000),
        ("OIL", 50_000, 0, 0, False, 10_000)])
    out = owner.get(f"/api/orders/suggestions?store_id={s1['id']}").json()
    assert out["stock_unknown"] == ["OIL"]
    bad = owner.post("/api/decisions", {"recommendation_id": ids["DAL"], "action": "change",
                                        "value_milli": 400_000})
    assert bad.status_code == 422                                  # a change needs a reason
    d = owner.post("/api/decisions", {"recommendation_id": ids["DAL"], "action": "change",
                                      "value_milli": 400_000, "reason": "local_event",
                                      "note": "Temple fair"}).json()
    g = owner.get(f"/api/orders/suggestions?store_id={s1['id']}").json()["groups"]
    dal = next(l for grp in g for l in grp["lines"] if l["sku"] == "DAL")
    assert dal["qty_milli"] == 240_000                             # 400-110-60 = 230 -> 240
    owner.post("/api/decisions", {"recommendation_id": ids["RICE"], "action": "skip"})
    out = owner.get(f"/api/orders/suggestions?store_id={s1['id']}").json()
    assert out["skipped"] == ["RICE"]
    owner.post(f"/api/decisions/{d['id']}/undo")
    g = owner.get(f"/api/orders/suggestions?store_id={s1['id']}").json()["groups"]
    assert next(l for grp in g for l in grp["lines"] if l["sku"] == "DAL")["qty_milli"] == 132_000
    today = owner.get(f"/api/today?store_id={s1['id']}").json()
    assert today["rows"][0]["sku"] == "DAL" and today["rows"][0]["decision"] is None
    assert any(r["decision"] and r["decision"]["action"] == "skip" for r in today["rows"])


def test_bulk_approve_small_items_and_who_may_decide(make_client):
    owner, cid, s1, pid, sup = _world(make_client)
    run, ids = _forecast(cid, s1["id"], sup, pid, [
        ("DAL", 300_000, 110_000, 60_000, True, 70_000), ("RICE", 30_000, 10_000, 0, True, 1)])
    viewer = make_client()
    invite_and_join(owner, viewer, "Vani Viewer", "viewer", [s1["id"]])
    assert viewer.get(f"/api/today?store_id={s1['id']}").json()["can_decide"] is False
    assert viewer.post("/api/decisions", {"recommendation_id": ids["DAL"],
                                          "action": "approve"}).status_code == 403
    r = owner.post("/api/decisions/bulk-approve", {"store_id": s1["id"],
                                                   "max_value_paise": 1_000}).json()
    assert r["approved"] == 1                                      # RICE only: Rs 1 at risk
    # A newer forecast replaces the old one: deciding on the old one is refused.
    _forecast(cid, s1["id"], sup, pid, [("DAL", 300_000, 110_000, 60_000, True, 70_000)])
    stale = owner.post("/api/decisions", {"recommendation_id": ids["DAL"], "action": "approve"})
    assert stale.status_code == 409


def test_from_suggestion_to_sent_order(make_client):
    owner, cid, s1, pid, sup = _world(make_client, unit_cost="35")
    _forecast(cid, s1["id"], sup, pid, [("DAL", 300_000, 110_000, 60_000, True, 70_000),
                                        ("RICE", 30_000, 10_000, 0, True, 10_000)])
    made = owner.post("/api/orders/from-suggestions", {"store_id": s1["id"]}).json()
    by_sup = {o["supplier"]: o for o in made}
    gupta = by_sup["Gupta Traders"]
    assert gupta["origin"] == "suggested" and gupta["lines"][0]["qty_milli"] == 144_000
    assert gupta["notes"].startswith("Raised from")
    again = owner.post("/api/orders/from-suggestions", {"store_id": s1["id"]}).json()
    assert {o["id"] for o in again} == {o["id"] for o in made}     # refreshed, not doubled
    assert owner.post(f"/api/orders/{gupta['id']}/send", {"channel": "email"}).status_code == 409
    owner.post(f"/api/orders/{gupta['id']}/approve")
    pdf = owner.get(f"/api/orders/{gupta['id']}/pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    sent = owner.post(f"/api/orders/{gupta['id']}/send", {"channel": "email"}).json()
    assert sent["status"] == "sent" and sent["sent_to"] == "orders@gupta.example"
    mail = outbox[-1]
    assert mail.attachments[0][0] == f"{gupta['number']}.pdf"
    assert mail.attachments[0][1].startswith(b"%PDF")
    wa = owner.post(f"/api/orders/{gupta['id']}/send", {"channel": "whatsapp"}).json()
    assert wa["whatsapp_link"].startswith("https://wa.me/919822012345?text=Purchase%20order")
    # Now on order: the next suggestion counts it.
    stock = {r["sku"]: r for r in owner.get(f"/api/stock?store_id={s1['id']}").json()["rows"]}
    assert stock["DAL"]["on_order_milli"] == 60_000 + 144_000


def test_no_forecast_yet_teaches(make_client):
    owner = make_client()
    owner.signup()
    s1 = owner.post("/api/stores", {"code": "S1", "name": "One"}).json()
    out = owner.get(f"/api/orders/suggestions?store_id={s1['id']}").json()
    assert out["groups"] == [] and "first forecast" in out["message"]
