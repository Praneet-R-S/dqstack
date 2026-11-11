"""Build step 7 -- the forecast engine on real data.

Done when: a test company's first forecast and backtest complete unattended; the
nightly run finishes in minutes and a failure keeps yesterday's forecast.

The engine here is the real one (dqstack.pipeline and dqstack.live), run on a small
synthetic company through the same scheduler production uses. Training takes about
a minute, so the end-to-end scenario is one test; the other behaviours use the
early-forecast tier, which needs no training.
"""

from __future__ import annotations

import math
import random
import time
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import func, select

from dqstack.app import db as dbmod
from dqstack.app import ledger, worker
from dqstack.app.forecast import jobs, source
from dqstack.app.models import (Alert, CalendarEvent, DailySales, ForecastRun, ForecastScore,
                                ModelArtifact, ModelVersion, Product, Recommendation, Store)

from .conftest import invite_and_join

PRODUCTS = [("RICE", "Rice 5 kg", "Staples", 450, 380, 9.0),
            ("DAL", "Toor dal 1 kg", "Staples", 180, 150, 6.0),
            ("OIL", "Sunflower oil 1 L", "Staples", 160, 135, 5.0),
            ("ATTA", "Atta 10 kg", "Staples", 520, 450, 3.0),
            ("SOAP", "Bath soap", "Personal care", 45, 32, 12.0),
            ("PASTE", "Toothpaste", "Personal care", 95, 70, 4.0),
            ("BISC", "Biscuits", "Snacks", 30, 22, 18.0),
            ("NAMK", "Namkeen 200 g", "Snacks", 60, 42, 7.0)]
WEEKLY = [1.0, 0.9, 0.95, 1.0, 1.1, 1.35, 1.3]      # Monday .. Sunday


def _company(client, days: int, products=PRODUCTS, stores=(("PUN1", "Maharashtra"),
                                                           ("BLR1", "Karnataka"))):
    """A company with `days` of synthetic daily sales ending yesterday."""
    client.signup()
    st = [client.post("/api/stores", {"code": c, "name": c, "state": s}).json()
          for c, s in stores]
    rows = "\n".join(f"{sku},{name},{cat},each,{mrp},{mrp},{cost}"
                     for sku, name, cat, mrp, cost, _ in products)
    client.import_file("products", "SKU,Name,Category,Unit,MRP,Selling price,Cost\n" + rows)
    client.import_file("suppliers", "Supplier,Lead time days,Order days,SKU,Case pack\n"
                                    + "".join(f"Gupta Traders,3,\"Mon, Thu\",{p[0]},6\n"
                                              for p in products[:4]))
    cid = client.me["company"]["id"]
    with dbmod.SessionLocal() as db:
        pid = {p.sku: p.id for p in db.scalars(select(Product))}
        rng = random.Random(7)
        end = date.today() - timedelta(days=1)
        for s_i, s in enumerate(st):
            for sku, _, _, _, _, level in products:
                for d in range(days):
                    day = end - timedelta(days=days - 1 - d)
                    mu = level * (1 + 0.2 * s_i) * WEEKLY[day.weekday()]
                    units = _poisson(rng, mu)
                    db.add(DailySales(company_id=cid, store_id=s["id"], product_id=pid[sku],
                                      day=day, units_milli=units * 1000,
                                      revenue_paise=units * 100, posted_milli=0,
                                      source="history"))
        db.commit()
    return cid, st, pid


def _poisson(rng: random.Random, mu: float) -> int:
    L, k, p = math.exp(-mu), 0, 1.0
    while True:
        p *= rng.random()
        if p <= L:
            return k
        k += 1


def _open_stock(client, store_id, product_id, units):
    cid = client.me["company"]["id"]
    with dbmod.SessionLocal() as db:
        ledger.post(db, company_id=cid, store_id=store_id, product_id=product_id,
                    qty_milli=units * 1000, type="opening",
                    at=datetime.now(timezone.utc) - timedelta(days=3))
        db.commit()


def test_done_when_first_forecast_and_backtest_run_unattended(make_client, monkeypatch):
    owner = make_client()
    cid, stores, pid = _company(owner, days=330)
    _open_stock(owner, stores[0]["id"], pid["RICE"], 12)

    # Nobody presses anything: the scheduler sees 300 days of sales and no model.
    t0 = time.perf_counter()
    ran = worker.tick()
    assert [(k, s) for _, k, s in ran] == [("train", "succeeded")]
    train_seconds = time.perf_counter() - t0
    with dbmod.SessionLocal() as db:
        mv = db.scalar(select(ModelVersion))
        assert (mv.version, mv.tier, mv.status) == ("v1", "engine", "active")
        assert db.get(ModelArtifact, mv.id).size > 10_000
        bt = mv.backtest
        assert bt["weeks"] >= 4 and 0 < bt["wape"] < 1.0 and 0 <= bt["inside_range"] <= 1
        # Stage 4: the blend and the single best method were scored on the same weeks,
        # the one that earned more is issued, and the nightly run loads it that way.
        p = mv.promotion
        from dqstack.pipeline import ENSEMBLE
        assert p["issue"] in ("blend", "single") and p["single_method"] in ENSEMBLE
        assert p["new_margin"] == max(p["blend_margin"], p["single_margin"])
        assert (p["issue"] == "single") == (p["single_margin"] > p["blend_margin"])
        assert jobs._load(db, mv)["issue"] == p["issue"]
        nightly_runs = list(db.scalars(select(ForecastRun).where(ForecastRun.kind == "nightly")))
        assert [r.status for r in nightly_runs] == ["succeeded"]
        first = nightly_runs[0]
        assert first.data_cutoff == date.today() - timedelta(days=1)
        n_recs = db.scalar(select(func.count()).select_from(Recommendation).where(
            Recommendation.run_id == first.id))
        assert n_recs == len(PRODUCTS) * len(stores)

    status = owner.get("/api/forecast/status").json()
    assert status["current"]["id"] == first.id and status["stale"] is False
    assert status["model"]["backtest"]["wape"] == bt["wape"]
    recs = owner.get(f"/api/recommendations?store_id={stores[0]['id']}").json()
    rice = next(r for r in recs["rows"] if r["sku"] == "RICE")
    assert rice["week"]["lo_milli"] <= rice["week"]["mid_milli"] <= rice["week"]["hi_milli"]
    assert rice["target_milli"] > 0 and rice["lead_time_days"] == 3 and rice["risk_days"] == 7
    assert rice["stock_known"] and rice["suggested_milli"] % 6000 == 0      # whole cases of 6
    assert len(rice["daily"]) == 28 and len(rice["risk_q"]) == 9
    assert sum(r["flagged"] for r in recs["rows"]) <= jobs.FLAG_PER_STORE

    # The next night: forecast only, from the saved model -- seconds, not minutes.
    t1 = time.perf_counter()
    again = owner.post("/api/forecast/run", {"kind": "nightly"})
    assert again.status_code == 202 and again.json()["status"] == "succeeded"
    nightly_seconds = time.perf_counter() - t1
    assert nightly_seconds < 120, nightly_seconds

    # A night that fails keeps yesterday's forecast, says so, and alerts the Admin.
    good = owner.get("/api/forecast/status").json()["current"]["id"]

    def broken(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr("dqstack.live.forecast", broken)
    failed = owner.post("/api/forecast/run", {"kind": "nightly"}).json()
    assert failed["status"] == "failed" and "Yesterday's forecast is still" in failed["message"]
    status = owner.get("/api/forecast/status").json()
    assert status["current"]["id"] == good and status["latest"]["status"] == "failed"
    still = owner.get(f"/api/recommendations?store_id={stores[0]['id']}").json()
    assert still["run"]["id"] == good and still["total"] == len(PRODUCTS)
    with dbmod.SessionLocal() as db:
        alert = db.scalar(select(Alert).where(Alert.kind == "forecast_nightly_failed"))
        assert alert is not None and alert.severity == "risk"
    print(f"\n  train+first forecast {train_seconds:.0f}s, nightly {nightly_seconds:.1f}s")


def test_a_failed_data_check_also_keeps_yesterdays_forecast(make_client, monkeypatch):
    owner = make_client()
    cid, stores, pid = _company(owner, days=90)
    worker.tick()                                           # early forecast tier
    good = owner.get("/api/forecast/status").json()["current"]["id"]
    real = source.load

    def bad_feed(db, company_id, as_of=None):
        data = real(db, company_id, as_of)
        data.tables["sales"].loc[0, "sales"] = -5           # a negative sale: Stage 0 fails
        return data
    monkeypatch.setattr(source, "load", bad_feed)
    r = owner.post("/api/forecast/run", {"kind": "nightly"}).json()
    assert r["status"] == "failed" and "did not pass its checks" in r["message"]
    assert owner.get("/api/forecast/status").json()["current"]["id"] == good


def test_early_forecast_for_two_to_six_months(make_client):
    owner = make_client()
    cid, stores, pid = _company(owner, days=90)
    ran = worker.tick()
    assert [(k, s) for _, k, s in ran] == [("train", "succeeded")]
    with dbmod.SessionLocal() as db:
        mv = db.scalar(select(ModelVersion))
        assert mv.tier == "starter" and mv.status == "active"
        assert db.get(ModelArtifact, mv.id) is None          # nothing to save
        assert mv.backtest["weeks"] >= 1 and mv.backtest["wape"] is not None
    recs = owner.get("/api/recommendations").json()
    assert recs["total"] == len(PRODUCTS) * 2 and recs["run"]["tier"] == "starter"


def test_too_little_history_says_how_much_more_is_needed(make_client):
    owner = make_client()
    _company(owner, days=10)
    assert worker.tick() == []                               # nothing to train yet
    st = owner.get("/api/forecast/status").json()
    assert st["history_days"] == 10 and st["days_needed"] == 4 and st["current"] is None
    assert st["days_to_full"] == 46 and "2 weeks" in st["needs"]
    r = owner.post("/api/forecast/run", {"kind": "train"}).json()
    assert r["status"] == "skipped" and "2 weeks" in r["message"]


def test_the_promotion_rule_keeps_the_model_that_earns_more(make_client, monkeypatch):
    owner = make_client()
    _company(owner, days=90)
    worker.tick()                                            # v1, promoted
    monkeypatch.setattr("dqstack.live.recent_scores", lambda models, *a, **k: {
        "new": {"margin": 100.0, "wape": 0.3, "inside_range": 0.9, "fill_rate": 0.9, "origins": 4},
        "current": {"margin": 150.0, "wape": 0.3, "inside_range": 0.9, "fill_rate": 0.9,
                    "origins": 4}})
    r = owner.post("/api/forecast/run", {"kind": "train"}).json()
    assert r["status"] == "succeeded" and "kept v1" in r["message"]
    vs = {m["version"]: m for m in owner.get("/api/forecast/models").json()}
    assert vs["v1"]["status"] == "active" and vs["v2"]["status"] == "rejected"
    assert vs["v2"]["promotion"] == {"decision": "kept the current model", "new_margin": 100.0,
                                     "current_margin": 150.0, "current_version": "v1",
                                     "origins": 4}


@pytest.mark.parametrize("single, issue", [(160.0, "single"), (90.0, "blend")])
def test_training_issues_the_single_best_method_only_when_it_earns_more(
        make_client, monkeypatch, single, issue):
    owner = make_client()
    _company(owner, days=90)
    worker.tick()                                            # v1, promoted
    monkeypatch.setattr("dqstack.live.can_issue_single", lambda m: True)
    monkeypatch.setattr("dqstack.live.recent_scores", lambda models, *a, **k: {
        "new": {"margin": 100.0, "wape": 0.3, "inside_range": 0.9, "fill_rate": 0.9, "origins": 4},
        "new_single": {"margin": single, "wape": 0.28, "inside_range": 0.88, "fill_rate": 0.92,
                       "origins": 4},
        "current": {"margin": 95.0, "wape": 0.3, "inside_range": 0.9, "fill_rate": 0.9,
                    "origins": 4}})
    r = owner.post("/api/forecast/run", {"kind": "train"}).json()
    assert r["status"] == "succeeded"
    assert ("single best forecasting method" in r["message"]) == (issue == "single")
    vs = {m["version"]: m for m in owner.get("/api/forecast/models").json()}
    assert vs["v2"]["status"] == "active" and vs["v1"]["status"] == "retired"
    p = vs["v2"]["promotion"]
    assert (p["issue"], p["blend_margin"], p["single_margin"]) == (issue, 100.0, single)
    assert p["new_margin"] == max(100.0, single) and p["current_margin"] == 95.0
    bt = vs["v2"]["backtest"]
    if issue == "single":       # its own figures, from the weeks it was scored on
        assert (bt["margin"], bt["fill_rate"], bt["weeks"]) == (160.0, 0.92, 4)
        assert "blend_backtest" in bt
    else:
        assert "blend_backtest" not in bt
    with dbmod.SessionLocal() as db:
        assert jobs.issue_of(db.scalar(select(ModelVersion).where(
            ModelVersion.version == "v2"))) == issue
        assert jobs.issue_of(db.scalar(select(ModelVersion).where(
            ModelVersion.version == "v1"))) == "blend"        # trained before the comparison


def test_monitoring_scores_last_nights_forecast(make_client):
    owner = make_client()
    cid, stores, pid = _company(owner, days=90)
    with dbmod.SessionLocal() as db:                         # hold back yesterday
        y = date.today() - timedelta(days=1)
        held = [(r.store_id, r.product_id, r.units_milli) for r in db.scalars(
            select(DailySales).where(DailySales.day == y))]
        for r in db.scalars(select(DailySales).where(DailySales.day == y)):
            db.delete(r)
        db.commit()
    worker.tick()                                             # trains, forecasts from day before
    with dbmod.SessionLocal() as db:                          # yesterday's sales arrive
        for s, p, u in held:
            db.add(DailySales(company_id=cid, store_id=s, product_id=p, day=y, units_milli=u,
                              revenue_paise=0, posted_milli=0, source="upload"))
        db.commit()
    owner.post("/api/forecast/run", {"kind": "nightly"})
    with dbmod.SessionLocal() as db:
        scores = list(db.scalars(select(ForecastScore)))
    assert {s.day for s in scores} == {y} and sum(s.n for s in scores) == len(PRODUCTS) * 2
    mon = owner.get("/api/forecast/monitoring").json()
    assert mon["by_day"][0]["day"] == y.isoformat() and mon["by_day"][0]["wape"] is not None
    assert {c["category"] for c in mon["by_category"]} == {"Staples", "Personal care", "Snacks"}


def test_the_engine_sees_what_the_database_means(make_client):
    """Closures and known stock-outs are missing, not zero; a store's late upload is
    missing, not zero; kg products are forecast in 100 g steps; festivals by state."""
    owner = make_client()
    prods = PRODUCTS[:2] + [("ONION", "Onions", "Vegetables", 40, 30, 20.0)]
    cid, stores, pid = _company(owner, days=70, products=prods)
    with dbmod.SessionLocal() as db:
        onion = db.get(Product, pid["ONION"])
        onion.unit = "kg"
        y = date.today() - timedelta(days=1)
        for r in db.scalars(select(DailySales).where(DailySales.store_id == stores[1]["id"],
                                                     DailySales.day == y)):
            db.delete(r)                                       # BLR1 has not uploaded yesterday
        two = date.today() - timedelta(days=2)
        db.add(CalendarEvent(company_id=cid, name="PUN1 closed", day=two, kind="closure",
                             store_ids=[stores[0]["id"]]))
        r = db.scalar(select(DailySales).where(DailySales.store_id == stores[1]["id"],
                                               DailySales.product_id == pid["DAL"],
                                               DailySales.day == two))
        r.units_milli, r.out_of_stock, r.oos_source = 0, True, "ledger"
        db.add(Product(company_id=cid, sku="NEW", name="New launch", category="Snacks",
                       price_paise=5000, cost_paise=3500))
        db.commit()
        data = source.load(db, cid)
    s = data.tables["sales"]
    key = lambda st, sku, d: ((s.store_id == stores[st]["id"]) & (s.item_id == pid[sku])
                              & (s.date == pd.Timestamp(d)))
    assert not key(0, "RICE", two).any()                       # closed: missing
    assert not key(1, "DAL", two).any()                        # empty shelf: missing
    assert not key(1, "RICE", y).any() and key(0, "RICE", y).any()   # BLR1's upload pending
    assert s["sales"].dtype.kind == "i" and (s["sales"] >= 0).all()
    with dbmod.SessionLocal() as db:
        raw = db.scalar(select(DailySales.units_milli).where(
            DailySales.store_id == stores[0]["id"], DailySales.product_id == pid["ONION"],
            DailySales.day == y))
    assert int(s[key(0, "ONION", y)]["sales"].iloc[0]) == round(raw / 100)
    cal = data.tables["calendar"]
    assert {"snap_MH", "snap_KA"} <= set(cal.columns)
    new_id = next(k for k, v in data.products.items() if v.sku == "NEW")
    assert new_id in set(data.tables["prices"]["item_id"]) and new_id not in set(s["item_id"])
    from dqstack import ingest
    with jobs.scratch():
        ing = ingest.run(tables=data.tables)
    assert not ing.rejected


def test_who_may_see_and_start_forecasts(make_client):
    owner = make_client()
    cid, stores, pid = _company(owner, days=60)
    viewer, other = make_client(), make_client()
    invite_and_join(owner, viewer, "Vani Viewer", "viewer", [stores[0]["id"]])
    assert viewer.get("/api/forecast/status").status_code == 200
    assert viewer.post("/api/forecast/run", {"kind": "nightly"}).status_code == 403
    other.signup(company="Other", email="o@other.example")
    assert other.get(f"/api/recommendations?store_id={stores[0]['id']}").status_code == 404
    assert other.get("/api/forecast/status").json()["history_days"] == 0


def test_the_schedule(make_client):
    owner = make_client()
    cid, stores, pid = _company(owner, days=90)
    from zoneinfo import ZoneInfo
    ist = ZoneInfo("Asia/Kolkata")
    with dbmod.SessionLocal() as db:
        from dqstack.app.models import Company
        co = db.get(Company, cid)
        assert worker.due(db, co, datetime.now(timezone.utc)) == [("train", "onboarding", None)]
    worker.tick()
    with dbmod.SessionLocal() as db:
        co = db.get(Company, cid)
        tomorrow = date.today() + timedelta(days=1)
        while tomorrow.weekday() == 6:
            tomorrow += timedelta(days=1)
        at = lambda h: datetime.combine(tomorrow, datetime.min.time(), tzinfo=ist).replace(
            hour=h).astimezone(timezone.utc)
        assert worker.due(db, co, at(0)) == []                     # before 01:00: wait
        assert worker.due(db, co, at(1)) == [("nightly", "schedule", None)]
        sunday = tomorrow + timedelta(days=(6 - tomorrow.weekday()) + 7)
        sun2 = datetime.combine(sunday, datetime.min.time(), tzinfo=ist).replace(
            hour=2).astimezone(timezone.utc)
        assert worker.due(db, co, sun2) == [("train", "weekly", None)]
        co.setup = {**co.setup, "retrain_requested": datetime.now(timezone.utc).isoformat()}
        db.commit()
        assert worker.due(db, co, at(1)) == [("train", "early_retrain", None)]


@pytest.mark.parametrize("days", [302, 330, 357, 358, 500, 666, 667, 1100])
def test_every_tier_gives_the_engine_enough_training_origins(days):
    """The tiers come from make_splits' arithmetic; check them against make_splits
    itself, including the pipeline's early-stopping split (3 validation origins and a
    4-origin purge must leave at least one origin to fit on)."""
    from types import SimpleNamespace

    from dqstack.features import make_splits
    tier, splits, quick = jobs.plan(days)
    assert tier == "engine"
    args = {"n_train_origins": 12 if quick else 32, "n_eval_origins": 4, **splits}
    P = SimpleNamespace(n_history=days, dates=pd.date_range("2024-01-01", periods=days + 28))
    sp = make_splits(P, horizon=28, **args)
    fit = sp.train[: -(3 + 4)]
    assert len(sp.train) >= jobs.MIN_TRAIN_ORIGINS and fit, (days, sp.train)
    assert sp.train[-1] + 1 > jobs.NN_REGION          # NHITS has its windows


@pytest.mark.parametrize("days,tier", [(7, None), (13, None), (14, "starter"), (30, "starter"),
                                       (56, "starter"), (238, "starter"), (301, "starter")])
def test_short_histories(days, tier):
    assert jobs.plan(days)[0] == tier
