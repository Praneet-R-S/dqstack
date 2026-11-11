"""Forecasting gaps against the product specification.

A. Short history: from 2 weeks of sales the early forecast runs, every line labelled
   "estimate, little history"; a new store in a company with history elsewhere is
   forecast from the same products in its other stores, scaled by store size; a new
   product borrows from its category until 8 weeks of its own sales; a company with
   no sales anywhere is told plainly what is needed and gets no invented numbers.
B. Festivals not yet in a company's sales get the national prior (a documented
   table derived from the demo generator's assumptions), flagged lower confidence.
C. A company above DQ_TRAIN_SHARD_SERIES series is trained in shards, in parallel
   processes; the forecast covers every series exactly once and promotion works.
"""

from __future__ import annotations

import gzip
import io
import pickle
import random
import time
from datetime import date, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import delete, select

from dqstack import live
from dqstack.app import db as dbmod
from dqstack.app import worker
from dqstack.app.forecast import coldstart, jobs, priors, shards, source
from dqstack.app.models import (Alert, CalendarEvent, DailySales, ForecastRun, ModelArtifact,
                                ModelVersion, Product, Recommendation, Store)

from .test_step7_forecast import PRODUCTS, WEEKLY, _company, _poisson

MH = (("PUN1", "Maharashtra"), ("PUN2", "Maharashtra"))


def _recs(cid: str) -> list[Recommendation]:
    with dbmod.SessionLocal() as db:
        run = jobs.current_run(db, cid)
        return list(db.scalars(select(Recommendation).where(Recommendation.run_id == run.id)))


def _estimate(rec: Recommendation) -> dict | None:
    return next((r for r in rec.reasons if r["key"] == "estimate"), None)


def _set_sizes(cid: str, sizes: dict[str, str]) -> None:
    with dbmod.SessionLocal() as db:
        for st in db.scalars(select(Store).where(Store.company_id == cid)):
            if st.code in sizes:
                st.size = sizes[st.code]
        db.commit()


def _add_sales(cid: str, store_id: str, pid: dict, days: int, factor: float,
               skus=None, seed: int = 11) -> None:
    rng = random.Random(seed)
    end = date.today() - timedelta(days=1)
    with dbmod.SessionLocal() as db:
        for sku, _, _, _, _, level in PRODUCTS:
            if skus is not None and sku not in skus:
                continue
            for d in range(days):
                day = end - timedelta(days=days - 1 - d)
                units = _poisson(rng, level * factor * WEEKLY[day.weekday()])
                db.add(DailySales(company_id=cid, store_id=store_id, product_id=pid[sku], day=day,
                                  units_milli=units * 1000, revenue_paise=units * 100,
                                  posted_milli=0, source="upload"))
        db.commit()


# ===================================================================== A. short history

def test_two_weeks_of_sales_give_a_labelled_estimate(make_client):
    owner = make_client()
    cid, stores, pid = _company(owner, days=20)
    ran = worker.tick()                                    # 20 days: the scheduler starts it
    assert [(k, s) for _, k, s in ran] == [("train", "succeeded")]
    st = owner.get("/api/forecast/status").json()
    assert st["model"]["tier"] == "starter" and st["days_needed"] == 0
    assert st["days_to_full"] == 36 and "8 weeks" in st["needs"]
    recs = _recs(cid)
    assert len(recs) == len(PRODUCTS) * 2
    for r in recs:
        e = _estimate(r)
        assert r.estimate and e is not None and e["basis"] == "little_history"
        assert e["text"].startswith("Estimate, little history: ")
        assert "days of sales so far" in e["text"] and r.history_days <= 20
        assert r.week1_mid_milli > 0
    api = owner.get(f"/api/recommendations?store_id={stores[0]['id']}").json()
    assert all(row["estimate"] for row in api["rows"])
    assert "estimates" in api["run"]["message"]


def test_two_weeks_means_two_weeks_of_recorded_days(make_client):
    """A closed day is not an observation: 14 calendar days with one closure is 13."""
    owner = make_client()
    cid, stores, pid = _company(owner, days=14)
    with dbmod.SessionLocal() as db:
        db.add(CalendarEvent(company_id=cid, name="Closed", kind="closure",
                             day=date.today() - timedelta(days=5)))
        db.commit()
    r = owner.post("/api/forecast/run", {"kind": "train"}).json()
    assert r["status"] == "skipped" and "No product has 2 weeks" in r["message"]


def test_no_sales_anywhere_says_what_is_needed_and_invents_nothing(make_client):
    owner = make_client()
    owner.signup()
    owner.post("/api/stores", {"code": "S1", "name": "One", "state": "Goa"})
    owner.import_file("products", "SKU,Name,Category,Unit,MRP,Selling price,Cost\n"
                                  "RICE,Rice,Staples,each,100,100,80\n")
    assert worker.tick() == []
    st = owner.get("/api/forecast/status").json()
    assert st["history_days"] == 0 and st["days_needed"] == 14 and st["current"] is None
    assert "nothing of your own to forecast from" in st["needs"]
    ready = owner.get("/api/setup/readiness").json()
    assert ready["stores"][0]["forecast_mode"] == "none"
    assert any("No sales history" in m["what"] and "2 weeks" in m["what"]
               for m in ready["missing"])
    r = owner.post("/api/forecast/run", {"kind": "train"}).json()
    assert r["status"] == "skipped" and "No sales are recorded yet" in r["message"]
    assert owner.get("/api/recommendations").json()["total"] == 0
    with dbmod.SessionLocal() as db:
        assert db.scalar(select(Alert).where(Alert.kind == "forecast_train_failed")) is None


def test_another_companys_sales_are_never_used(make_client):
    """Company separation: a company with history trains; one with none gets nothing,
    not an estimate from the other."""
    rich, poor = make_client(), make_client()
    _company(rich, days=60)
    poor.signup(company="Poor Stores", email="p@poor.example")
    poor.post("/api/stores", {"code": "P1", "name": "P1", "state": "Maharashtra"})
    poor.import_file("products", "SKU,Name,Category,Unit,MRP,Selling price,Cost\n"
                                 "RICE,Rice 5 kg,Staples,each,450,450,380\n")
    ran = worker.tick()
    assert [(k, s) for _, k, s in ran] == [("train", "succeeded")]
    assert poor.get("/api/forecast/status").json()["current"] is None
    assert poor.post("/api/forecast/run", {"kind": "train"}).json()["status"] == "skipped"
    assert poor.get("/api/recommendations").json()["total"] == 0


def test_a_new_store_is_forecast_from_the_companys_other_stores(make_client):
    owner = make_client()
    cid, stores, pid = _company(owner, days=120, stores=MH)
    _set_sizes(cid, {"PUN1": "large", "PUN2": "large"})
    new = owner.post("/api/stores", {"code": "PUN3", "name": "PUN3", "state": "Maharashtra",
                                     "size": "medium"}).json()
    worker.tick()
    with dbmod.SessionLocal() as db:
        data = source.load(db, cid)
    assert data.new_stores == {new["id"]} and data.donor_stores == {s["id"] for s in stores}
    recs = _recs(cid)
    by = {(r.store_id, r.product_id): r for r in recs}
    assert len(recs) == len(PRODUCTS) * 3                  # the new store gets every product
    ratio = coldstart.SIZE_FACTOR["medium"] / coldstart.SIZE_FACTOR["large"]
    for sku in pid:
        r = by[(new["id"], pid[sku])]
        e = _estimate(r)
        assert r.estimate and e["basis"] == "no_history"
        assert "your other stores" in e["text"] and "store size" in e["text"]
        donors = [by[(s["id"], pid[sku])] for s in stores]
        for day in range(len(r.daily)):                 # each day's median, scaled
            want = ratio * sum(d.daily[day][1] for d in donors) / 2
            assert abs(r.daily[day][1] - want) <= 2, (sku, day, r.daily[day][1], want)
    assert all(_estimate(by[(s["id"], p)]) is None for s in stores for p in pid.values())
    ready = owner.get("/api/setup/readiness").json()
    modes = {s["code"]: s["forecast_mode"] for s in ready["stores"]}
    assert modes == {"PUN1": "own", "PUN2": "own", "PUN3": "from_other_stores"}


def test_a_new_store_with_three_weeks_blends_towards_its_own_forecast(make_client):
    owner = make_client()
    cid, stores, pid = _company(owner, days=120, stores=MH)
    new = owner.post("/api/stores", {"code": "PUN3", "name": "PUN3",
                                     "state": "Maharashtra"}).json()
    _add_sales(cid, new["id"], pid, days=21, factor=0.5)
    worker.tick()
    with dbmod.SessionLocal() as db:
        data = source.load(db, cid)
        assert data.store_days[new["id"]] == 21 and new["id"] in data.new_stores
        by_cat, r_all, how = coldstart.store_ratio(
            data.tables["sales"], dict(zip(data.tables["items"]["item_id"],
                                           data.tables["items"]["cat_id"])),
            new["id"], data.donor_stores, data.store_first[new["id"]], 21,
            {sid: s.size for sid, s in data.stores.items()})
    # The store sells about half of an established store (they are 1.0 and 1.2).
    assert 0.3 < r_all < 0.6 and "first 21 days" in how
    for r in _recs(cid):
        if r.store_id == new["id"]:
            e = _estimate(r)
            assert e["basis"] == "little_history" and "21 days" in e["text"]


def test_a_new_product_borrows_from_its_category_then_hands_over(make_client):
    owner = make_client()
    cid, stores, pid = _company(owner, days=120, stores=MH)
    with dbmod.SessionLocal() as db:
        fresh = Product(company_id=cid, sku="NEWSNK", name="New snack", category="Snacks",
                        price_paise=4000, cost_paise=3000)
        half = Product(company_id=cid, sku="HALF", name="Four weeks old", category="Snacks",
                       price_paise=4000, cost_paise=3000)
        db.add_all([fresh, half])
        db.commit()
        fresh_id, half_id = fresh.id, half.id
    rng = random.Random(3)
    with dbmod.SessionLocal() as db:                       # HALF: 28 days of its own sales
        end = date.today() - timedelta(days=1)
        for s in stores:
            for d in range(28):
                units = max(1, _poisson(rng, 6.0))
                db.add(DailySales(company_id=cid, store_id=s["id"], product_id=half_id,
                                  day=end - timedelta(days=d), units_milli=units * 1000,
                                  revenue_paise=0, posted_milli=0, source="upload"))
        db.commit()
    worker.tick()
    by = {(r.store_id, r.product_id): r for r in _recs(cid)}
    for s in stores:
        r = by[(s["id"], fresh_id)]
        e = _estimate(r)
        assert e["basis"] == "no_history" and "similar products in Snacks" in e["text"]
        peers = [by[(s["id"], pid[k])] for k in ("BISC", "NAMK")]
        for day in range(len(r.daily)):                    # the median of two is the mean
            want = sum(p.daily[day][1] for p in peers) / 2
            assert abs(r.daily[day][1] - want) <= 2
        h = _estimate(by[(s["id"], half_id)])
        assert h["basis"] == "little_history" and "28 days of its own sales" in h["text"]
    with dbmod.SessionLocal() as db:
        data = source.load(db, cid)
        with jobs.scratch():
            res = live.starter(data.tables, jobs.company_cfg(db, cid))
    labels = coldstart.apply(res, data, "starter")
    item = res.P.series["item_id"].astype(str).to_numpy()
    weights = {item[r]: v["own_weight"] for r, v in labels.items()}
    assert weights[fresh_id] == 0.0 and weights[half_id] == 0.5     # 28 of 56 days
    assert not any(item[r] in pid.values() for r in labels)          # 120 days: its own


# ===================================================================== B. festivals

def test_the_festival_prior_is_the_generators_assumptions_shrunk():
    assert priors.PRIOR == {"major": {"festival_goods": 3.0, "festive": 1.3, "everyday": 1.1},
                            "minor": {"festival_goods": 1.075, "festive": 1.075,
                                      "everyday": 1.025}}
    g = priors.group_of
    assert [g(c) for c in ("Snacks", "Oils", "Cooking oil", "Sweets and mithai", "Festival",
                           "Pooja items", "Toiletries", "Dairy", None)] == [
        "festive", "festive", "festive", "festive", "festival_goods", "festival_goods",
        "everyday", "everyday", "everyday"]
    assert priors.multiplier("Diwali", "Snacks") == 1.3
    assert priors.multiplier("Guru Nanak Jayanti", "Dairy") == 1.025
    assert max(v for k in priors.PRIOR.values() for v in k.values()) <= priors.CAP
    assert {"Diwali", "Holi", "Onam", "Christmas"} <= priors.NATIONAL_FESTIVALS
    assert "Republic Day" not in priors.NATIONAL_FESTIVALS          # a holiday, no lift


def test_an_unseen_festival_gets_the_national_prior_flagged_lower_confidence(make_client):
    owner = make_client()
    cid, stores, pid = _company(owner, days=120, stores=MH)
    cutoff = date.today() - timedelta(days=1)
    with dbmod.SessionLocal() as db:                      # a calendar this test controls
        db.execute(delete(CalendarEvent).where(CalendarEvent.company_id == cid))
        db.add_all([CalendarEvent(company_id=cid, name="Diwali", kind="festival",
                                  day=cutoff + timedelta(days=10)),
                    CalendarEvent(company_id=cid, name="Holi", kind="festival",
                                  day=cutoff + timedelta(days=20)),
                    CalendarEvent(company_id=cid, name="Holi", kind="festival",
                                  day=cutoff - timedelta(days=60))])
        db.commit()
        data = source.load(db, cid)
        cfg = jobs.company_cfg(db, cid)
        with jobs.scratch():
            base = live.starter(data.tables, cfg)
        cat = dict(zip(data.tables["items"]["item_id"], data.tables["items"]["cat_id"]))
        item = base.P.series["item_id"].astype(str).to_numpy()
        for tier in ("engine", "starter"):
            res = live.LiveResult(**{**base.__dict__, "quantiles": base.quantiles.copy()})
            notes = priors.apply(db, cid, res, data, tier)
            ratio = res.quantiles[..., 3] / np.maximum(base.quantiles[..., 3], 1e-9)
            for r in range(len(item)):
                m_d = priors.multiplier("Diwali", cat[item[r]])
                m_h = priors.multiplier("Holi", cat[item[r]])
                ok = base.quantiles[r, :, 3] > 0
                assert np.allclose(ratio[r, 3:10][ok[3:10]], m_d, rtol=1e-4)     # days 4-10
                assert np.allclose(ratio[r, 10:13][ok[10:13]], 1.0)              # between
                holi = ratio[r, 13:20][ok[13:20]]                                # days 14-20
                assert np.allclose(holi, 1.0 if tier == "engine" else m_h, rtol=1e-4)
            want = {(cutoff + timedelta(days=10), "Diwali", "first")}
            if tier == "starter":                     # the early forecast learns no festival
                want.add((cutoff + timedelta(days=20), "Holi", "national"))
            assert {sid: set(v) for sid, v in notes.items()} == {s["id"]: want for s in stores}
    worker.tick()                                       # the early tier, end to end
    for r in _recs(cid):
        fest = [x for x in r.reasons if x["key"] == "festival"]
        assert fest[0]["confidence"] == "lower" and "first Diwali in your data" in fest[0]["text"]
        assert "national festival pattern" in fest[1]["text"]


# ===================================================================== C. shards

def test_the_shard_plan_splits_by_category_then_store_group():
    rows = ([("A", f"s{k}") for k in range(3) for _ in range(10)] + [("B", "s0")] * 8
            + [("C", "s1")] * 5 + [("D", "s2")] * 3)
    pairs = pd.DataFrame(rows, columns=["cat_id", "store_id"])
    a = shards.plan(pairs, limit=12)
    assert len(a["names"]) == 5 and max(a["sizes"]) <= 12
    assert sorted(a["sizes"]) == [5, 10, 10, 10, 11]                # A by store; B+D; C
    assert a["by_cat"]["B"] == a["by_cat"]["D"] != a["by_cat"]["C"]
    assert len(set(a["by_cat_store"]["A"].values())) == 3
    idx = [shards.shard_of(c, s, a) for c, s in rows]
    assert sum(a["sizes"]) == len(rows) and np.bincount(idx).tolist() == a["sizes"]
    assert shards.shard_of("A", "a new store", a) == a["cat_default"]["A"]
    assert shards.shard_of("Never seen", "s0", a) == a["default"]
    one = shards.plan(pairs, limit=len(rows))
    assert one["sizes"] == [len(rows)] and set(one["by_cat"]) == {"A", "B", "C", "D"}


def test_at_or_below_the_limit_nothing_is_sharded(make_client, monkeypatch):
    owner = make_client()
    cid, stores, pid = _company(owner, days=30)
    with dbmod.SessionLocal() as db:
        tables = source.load(db, cid).tables
    n = len(shards.series_pairs(tables))
    assert n == len(PRODUCTS) * 2
    monkeypatch.setenv("DQ_TRAIN_SHARD_SERIES", str(n))
    assert shards.plan_for(tables) is None
    monkeypatch.delenv("DQ_TRAIN_SHARD_SERIES")
    assert shards.shard_limit() == 20_000 and shards.plan_for(tables) is None
    monkeypatch.setenv("DQ_TRAIN_WORKERS", "0")
    assert shards.train_workers() == 1


def test_a_large_company_trains_in_shards_in_parallel(make_client, monkeypatch):
    """16 series with a limit of 6: Staples (8) splits by store, Personal care and
    Snacks (4 each) cannot share, so four shards, trained by four processes."""
    owner = make_client()
    cid, stores, pid = _company(owner, days=330)
    monkeypatch.setenv("DQ_TRAIN_SHARD_SERIES", "6")
    monkeypatch.setenv("DQ_TRAIN_WORKERS", "4")
    t0 = time.perf_counter()
    with dbmod.SessionLocal() as db:
        run = jobs.train(db, cid, trigger="onboarding")
        assert run.status == "succeeded", run.error
        train_seconds = time.perf_counter() - t0
        assert run.timings["shards"] == 4 and run.timings["workers"] == 4
        assert "in 4 parts" in run.message
        mv = db.scalar(select(ModelVersion))
        assert (mv.tier, mv.status, mv.n_series) == ("engine", "active", 16)
        bt = mv.backtest
        assert bt["weeks"] >= 4 and 0 < bt["wape"] < 1 and len(bt["shards"]) == 4
        assert sum(s["series"] for s in bt["shards"]) == 16
        assert bt["recent"]["new"]["origins"] >= 1 and mv.promotion["decision"] == "promoted"
        art = db.get(ModelArtifact, mv.id)
        with gzip.GzipFile(fileobj=io.BytesIO(art.content)) as f:
            payload = pickle.load(f)
        assert shards.is_payload(payload) and len(payload["shards"]) == 4
        series = [set(live.load_model(s["blob"])["series"]) for s in payload["shards"]]
        assert sum(len(s) for s in series) == len(set().union(*series)) == 16   # disjoint
        night = db.scalar(select(ForecastRun).where(ForecastRun.kind == "nightly"))
        assert night.status == "succeeded" and night.timings["shards"] == 4
        recs = list(db.scalars(select(Recommendation).where(Recommendation.run_id == night.id)))
        pairs = [(r.store_id, r.product_id) for r in recs]
        assert len(pairs) == len(set(pairs)) == 16                      # every series once
        assert all(r.week1_lo_milli <= r.week1_mid_milli <= r.week1_hi_milli for r in recs)
        nightly_seconds = night.timings["seconds"]          # from the saved shards

        # The promotion rule with a sharded model on both sides: the weekly retrain's
        # candidate (here the same shards again, to keep the test short) is scored
        # against the active sharded model on the same recent weeks.
        model = jobs._load(db, mv)
        blob = art.content
        monkeypatch.setattr(shards, "train", lambda *a, **k: (
            shards.ShardedModel(payload), blob, dict(bt), 16))
        monkeypatch.setattr(jobs, "nightly", lambda *a, **k: None)   # tested above
        retrain = jobs.train(db, cid, trigger="weekly")
        assert retrain.status == "succeeded", retrain.error
        versions = {m.version: m for m in db.scalars(select(ModelVersion))}
        promo = versions["v2"].promotion
        assert promo["current_version"] == "v1" and promo["origins"] >= 1
        assert promo["new_margin"] == promo["current_margin"]          # same shards, same weeks
        assert versions["v2"].status == "active" and versions["v1"].status == "retired"
        # Scores made shard by shard and added up are the scores of the whole panel: the
        # per-shard path loses nothing. (In this process, so both sides use the same
        # CPU threads; the parallel path is the one the retrain above went through.)
        data = source.load(db, cid)
        cfg = jobs.company_cfg(db, cid)
        t2 = time.perf_counter()
        parts = shards.recent_scores({"m": model, "s": "starter"}, data.tables, cfg, workers=1)
        score_seconds = time.perf_counter() - t2
        with jobs.scratch():
            whole = live.recent_scores({"m": model.bind(data.tables), "s": "starter"},
                                       data.tables, cfg)
        for name in ("m", "s"):
            assert parts[name]["origins"] == whole[name]["origins"] >= 1
            for k in ("margin", "wape", "inside_range", "fill_rate"):
                assert np.isclose(parts[name][k], whole[name][k], rtol=1e-9), (name, k)
    print(f"\n  sharded train+first forecast {train_seconds:.0f}s, nightly {nightly_seconds:.1f}s, "
          f"scores by shard {score_seconds:.1f}s")
