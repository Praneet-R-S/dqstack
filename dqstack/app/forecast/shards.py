"""Training a large company in shards (specification section 10: "A 50-store chain
with 5,000 products is 250,000 pairs, so training must be split by category or store
group and run in parallel; overnight forecasting from saved models stays within
minutes").

When a company has more series (product-store pairs) than DQ_TRAIN_SHARD_SERIES
(default 20,000), `jobs.train` does not train one engine on all of them:

  1. `plan` splits the series by category. A category larger than the limit is split
     further by store group (whole stores, balanced by series count); the smaller
     categories are packed together, first-fit decreasing, up to the limit. A single
     store's category is the smallest unit, so a shard can exceed the limit only when
     one store carries more than the limit in one category.
  2. Each shard is trained by the ordinary engine in its own process
     (DQ_TRAIN_WORKERS at a time, default one fewer than the CPUs), each process with
     its own scratch folders and its share of the CPU threads.
  3. One model version is saved whose payload holds every shard's saved model and the
     assignment rule, so a product or store added after training goes to a shard
     deterministically: its category's shard; in a split category, its store's group
     or, for a new store, the smallest group; a new category goes to the smallest shard.
  4. The nightly run forecasts each shard in its own process, the same way, and
     merges (`ShardedModel.forecast`). The promotion rule's scores are made shard by
     shard in the same pool and added up (`recent_scores`). A sharded model can also
     be scored on the whole panel like any other model (`panel_quantiles`, through
     `live.forecast_panel`), in one process: correct, but hours at 250,000 series.

At or below the limit nothing here runs: `plan_for` returns None and training is the
single-engine path, unchanged.

Every engine import is inside a function: a training process sets its scratch
folders in the environment (`_child_init`) before `dqstack.config` is first imported,
because the engine reads them once, at import.
"""

from __future__ import annotations

import gzip
import io
import math
import multiprocessing as mp
import os
import pickle
import shutil
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass

import numpy as np
import pandas as pd

FORMAT = "dq-sharded-1"
DEFAULT_SHARD_SERIES = 20_000


def shard_limit() -> int:
    return max(1, int(os.environ.get("DQ_TRAIN_SHARD_SERIES") or DEFAULT_SHARD_SERIES))


def train_workers() -> int:
    v = os.environ.get("DQ_TRAIN_WORKERS")
    return max(1, int(v) if v else (os.cpu_count() or 2) - 1)


# ===================================================================== the plan

def _cat_of(tables: dict) -> dict[str, str]:
    items = tables["items"]
    return dict(zip(items["item_id"].astype(str), items["cat_id"].astype(str)))


def series_pairs(tables: dict) -> pd.DataFrame:
    """One row per series (every carried pair has a weekly price), with its category."""
    p = tables["prices"][["item_id", "store_id"]].astype(str).drop_duplicates()
    p["cat_id"] = p["item_id"].map(_cat_of(tables)).fillna("General")
    return p.reset_index(drop=True)


def plan(pairs: pd.DataFrame, limit: int) -> dict:
    """Assign every (category, store) to a shard. `pairs` has cat_id and store_id,
    one row per series."""
    counts = pairs.groupby("cat_id").size()
    order = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    names: list[str] = []
    sizes: list[int] = []
    by_cat: dict[str, int] = {}
    by_cat_store: dict[str, dict[str, int]] = {}
    cat_default: dict[str, int] = {}
    small: list[tuple[str, int]] = []
    for cat, n in order:
        n = int(n)
        if n <= limit:
            small.append((cat, n))
            continue
        per_store = pairs[pairs["cat_id"] == cat].groupby("store_id").size()
        k = math.ceil(n / limit)
        loads, members = [0] * k, [[] for _ in range(k)]
        for sid, m in sorted(per_store.items(), key=lambda kv: (-kv[1], kv[0])):
            j = int(np.argmin(loads))          # largest stores first, to the lightest group
            loads[j] += int(m)
            members[j].append(sid)
        idxs = []
        for j in range(k):
            if not members[j]:
                continue
            idxs.append(len(names))
            names.append(f"{cat}, stores group {len(idxs)}")
            sizes.append(loads[j])
            for sid in members[j]:
                by_cat_store.setdefault(cat, {})[sid] = idxs[-1]
        cat_default[cat] = min(idxs, key=lambda i: (sizes[i], i))
    bins: list[list] = []                       # first-fit decreasing: [size, [cats]]
    for cat, n in small:
        for b in bins:
            if b[0] + n <= limit:
                b[0] += n
                b[1].append(cat)
                break
        else:
            bins.append([n, [cat]])
    for size, cats in bins:
        for cat in cats:
            by_cat[cat] = len(names)
        names.append(" + ".join(cats) if len(cats) <= 3 else f"{cats[0]} and {len(cats) - 1} more")
        sizes.append(size)
    return {"names": names, "sizes": sizes, "by_cat": by_cat, "by_cat_store": by_cat_store,
            "cat_default": cat_default,
            "default": min(range(len(names)), key=lambda i: (sizes[i], i)), "limit": limit}


def plan_for(tables: dict, limit: int | None = None) -> dict | None:
    """The shard plan, or None when the company is small enough for one engine."""
    limit = limit or shard_limit()
    pairs = series_pairs(tables)
    if len(pairs) <= limit:
        return None
    return plan(pairs, limit)


def shard_of(cat: str, store: str, a: dict) -> int:
    if cat in a["by_cat"]:
        return a["by_cat"][cat]
    if cat in a["by_cat_store"]:
        return a["by_cat_store"][cat].get(store, a["cat_default"][cat])
    return a["default"]


def _shard_index(df: pd.DataFrame, cat_of: dict, a: dict) -> np.ndarray:
    cats = df["item_id"].astype(str).map(cat_of).fillna("General").to_numpy()
    combo = pd.MultiIndex.from_arrays([cats, df["store_id"].astype(str).to_numpy()])
    uniq = combo.unique()
    look = np.array([shard_of(c, s, a) for c, s in uniq], dtype="int64")
    return look[uniq.get_indexer(combo)] if len(uniq) else np.zeros(0, dtype="int64")


def split_tables(tables: dict, a: dict) -> list[dict | None]:
    """The engine's five feeds, one set per shard (None for a shard with no series)."""
    cat_of = _cat_of(tables)
    sales, prices = tables["sales"], tables["prices"]
    si, pi = _shard_index(sales, cat_of, a), _shard_index(prices, cat_of, a)
    out: list[dict | None] = []
    for i in range(len(a["names"])):
        s = sales[si == i].reset_index(drop=True)
        p = prices[pi == i].reset_index(drop=True)
        if p.empty and s.empty:
            out.append(None)
            continue
        items = set(p["item_id"].astype(str)) | set(s["item_id"].astype(str))
        stores = set(p["store_id"].astype(str)) | set(s["store_id"].astype(str))
        out.append({"sales": s, "prices": p, "calendar": tables["calendar"],
                    "items": tables["items"][tables["items"]["item_id"].astype(str).isin(items)]
                    .reset_index(drop=True),
                    "stores": tables["stores"][tables["stores"]["store_id"].astype(str)
                                               .isin(stores)].reset_index(drop=True),
                    "_declared_rows": {"sales": len(s)}})
    return out


# ===================================================================== training

def _child_init(threads: int) -> None:
    """In each training process, before any engine import: private scratch folders
    and a fair share of the CPU threads."""
    root = tempfile.mkdtemp(prefix="dq-shard-")
    for name in ("ARTIFACTS", "INTERIM", "PROCESSED"):
        path = os.path.join(root, name.lower())
        os.makedirs(path, exist_ok=True)
        os.environ[f"DQ_{name}_DIR"] = path
    for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
        os.environ[var] = str(threads)
    os.environ["DQ_SHARD_THREADS"] = str(threads)
    from multiprocessing import util
    util.Finalize(None, shutil.rmtree, args=(root,), kwargs={"ignore_errors": True},
                  exitpriority=0)


def _pool(n: int) -> ProcessPoolExecutor:
    """n fresh processes ("spawn": no inherited threads or open database handles),
    each with its own scratch folders and a fair share of the CPU threads."""
    threads = max(1, (os.cpu_count() or 2) // n)
    return ProcessPoolExecutor(max_workers=n, mp_context=mp.get_context("spawn"),
                               initializer=_child_init, initargs=(threads,))


def _limit_threads() -> None:
    threads = int(os.environ.get("DQ_SHARD_THREADS") or 0)
    if not threads:
        return
    from ...config import LGBM_QUANTILE, LGBM_TWEEDIE
    LGBM_TWEEDIE["num_threads"] = threads              # this process's copy only
    LGBM_QUANTILE["num_threads"] = threads
    try:
        import torch
        torch.set_num_threads(threads)
    except Exception:  # noqa: BLE001 -- no torch, no neural candidate threads to set
        pass


def _forecast_one(blob: bytes, tables: dict, cfg, issue: str = "blend") -> dict:
    """One shard's nightly forecast, in its own process. Only what the nightly run
    reads goes back: the series frame and the arrays, not the panel's matrices."""
    _limit_threads()
    from ... import live
    from . import jobs
    with jobs.scratch():
        r = live.forecast(live.issued_as(live.load_model(blob), issue), tables, cfg)
    return _parts_of(r)


def _parts_of(r) -> dict:
    return {"series": r.P.series, "origin": r.origin, "quantiles": r.quantiles,
            "planning_class": r.planning_class, "history_days": r.history_days,
            "feed_runs": r.feed_runs}


def _score_one(blobs: dict[str, tuple[bytes, str] | str], tables: dict, cfg,
               n_origins: int) -> dict:
    """One shard's promotion scores, in its own process: every model given, as
    (saved bytes, what it issues), on the shard's own panel (live.score_sums). One
    shard model issued two ways is loaded once."""
    _limit_threads()
    from ... import live
    from . import jobs
    loaded: dict[int, dict] = {}
    models = {}
    for name, b in blobs.items():
        if isinstance(b, str):
            models[name] = b
            continue
        blob, issue = b
        if id(blob) not in loaded:
            loaded[id(blob)] = live.load_model(blob)
        models[name] = live.issued_as(loaded[id(blob)], issue)
    with jobs.scratch():
        return live.score_sums(models, tables, cfg, n_origins)


def _add_sums(parts: list[dict]) -> dict:
    """Score sums from separate parts of the series, added: per model, origin by
    origin (matched on date); fill rate from the added sales and demand."""
    out: dict[str, dict] = {}
    for part in parts:
        for name, s in part.items():
            o = out.setdefault(name, {"by_date": {}, "abs_err": 0.0, "actual": 0.0,
                                      "inside": 0.0, "cells": 0.0})
            for k in ("abs_err", "actual", "inside", "cells"):
                o[k] += s[k]
            for d, m, a, dem in zip(s["dates"], s["margin"], s["sales"], s["demand"]):
                v = o["by_date"].setdefault(d, [0.0, 0.0, 0.0])
                v[0] += m
                v[1] += a
                v[2] += dem
    for o in out.values():
        days = sorted(o.pop("by_date").items(), reverse=True)     # latest origin first
        o["dates"] = [d for d, _ in days]
        o["margin"] = [v[0] for _, v in days]
        o["sales"] = [v[1] for _, v in days]
        o["demand"] = [v[2] for _, v in days]
        o["fill"] = [a / max(dem, 1e-9) for a, dem in zip(o["sales"], o["demand"])]
    return out


def recent_scores(models: dict, tables: dict, cfg, n_origins: int = 4,
                  workers: int | None = None) -> dict[str, dict[str, float]]:
    """The promotion rule's scores (live.recent_scores) when a model was trained in
    shards. With none, this is live.recent_scores itself. Otherwise each sharded model
    is scored shard by shard in parallel processes, on the same shard panels it is
    trained and forecast on; models sharing one shard plan (and the early forecast,
    which forecasts each series on its own) are scored together on each shard, so
    they are compared on exactly the same series, weeks and sales. The sums are added
    (margin, error, sales and demand are sums over series). A single-engine model is
    scored on the whole panel, as before. Whole-panel scoring in one process takes
    hours at 250,000 series; this is what keeps a sharded weekly retrain within its
    time limit."""
    from ... import live
    sharded = {n: m for n, m in models.items() if isinstance(m, ShardedModel)}
    if not sharded:
        return live.recent_scores(models, tables, cfg, n_origins)
    groups: list[tuple[dict, dict]] = []
    for name, m in sharded.items():
        for assign, members in groups:
            if assign == m.assign:
                members[name] = m
                break
        else:
            groups.append((m.assign, {name: m}))
    starters = {n: m for n, m in models.items() if isinstance(m, str) and m == "starter"}
    groups[0][1].update(starters)
    rest = {n: m for n, m in models.items() if n not in sharded and n not in starters}
    parts: list[dict] = []
    for assign, members in groups:
        split = split_tables(tables, assign)
        todo = []
        for i, t in enumerate(split):
            if t is None:
                continue
            todo.append(({n: m if isinstance(m, str) else
                          (m.shards[m._by_index()[i]]["blob"], m.issue)
                          for n, m in members.items()}, t))
        n = max(1, min(workers or train_workers(), len(todo)))
        if n > 1:
            with _pool(n) as ex:
                futures = [ex.submit(_score_one, blobs, t, cfg, n_origins) for blobs, t in todo]
                parts += [f.result() for f in futures]
        else:
            parts += [_score_one(blobs, t, cfg, n_origins) for blobs, t in todo]
    if rest:
        parts.append(live.score_sums(rest, tables, cfg, n_origins))
    return live.finish_scores(_add_sums(parts))


def _train_one(cfg, quick: bool, tables: dict, splits: dict | None) -> dict:
    """One shard, in its own process: the ordinary engine on the shard's feeds."""
    _limit_threads()
    from ... import live
    from . import jobs
    t0 = time.perf_counter()
    with jobs.scratch():
        state = jobs.live_pipeline(cfg, quick, tables, splits)
        blob = live.saved_model(state, cfg)
        summary = jobs.engine_summary(state)
        n = int(state["P"].n_series)
        best = state.get("best_single")
    return {"blob": blob, "summary": summary, "n_series": n, "best_single": best,
            "seconds": round(time.perf_counter() - t0, 1)}


def train(cfg, quick: bool, tables: dict, splits: dict | None, assign: dict,
          workers: int | None = None) -> tuple["ShardedModel", bytes, dict, int]:
    """Train every shard in parallel processes. Returns the loaded model, the bytes
    to save, the combined backtest summary and the number of series. Any shard
    failing fails the whole training, so the model in use is left as it is."""
    parts = split_tables(tables, assign)
    todo = [(i, t) for i, t in enumerate(parts) if t is not None]
    n = max(1, min(workers or train_workers(), len(todo)))
    results: dict[int, dict] = {}
    with _pool(n) as ex:
        futures = {ex.submit(_train_one, cfg, quick, t, splits): i for i, t in todo}
        for f in as_completed(futures):
            results[futures[f]] = f.result()
    shards = [{"index": i, "name": assign["names"][i], **results[i]} for i, _ in todo]
    payload = {"format": FORMAT, "assign": assign, "cfg": cfg, "shards": shards}
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=1) as f:
        pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
    summary = combine_summaries(shards)
    summary["workers"] = n
    return ShardedModel(payload), buf.getvalue(), summary, sum(s["n_series"] for s in shards)


def combine_summaries(shards: list[dict]) -> dict:
    """The backtest of a sharded model: rates weighted by each shard's series, money
    summed, the shortest backtest's weeks. Each shard's own figures are kept."""
    w = np.array([s["n_series"] for s in shards], dtype="float64")
    sums = [s["summary"] for s in shards]

    def wmean(k):
        v = [(x.get(k), wi) for x, wi in zip(sums, w) if x.get(k) is not None]
        return round(sum(a * b for a, b in v) / sum(b for _, b in v), 4) if v else None

    def total(k):
        v = [x.get(k) for x in sums]
        return round(sum(v), 4) if v and all(x is not None for x in v) else None

    out = {"weeks": min(x.get("weeks") or 0 for x in sums),
           **{k: wmean(k) for k in ("wape", "inside_range", "fill_rate", "bias", "naive_wape")},
           "margin": total("margin")}
    if out["wape"] is not None and out["naive_wape"]:
        out["better_than_naive_pct"] = round(100 * (1 - out["wape"] / out["naive_wape"]), 1)
    pv = [x.get("profit_vs_blanket") for x in sums]
    if pv and all(pv) and all(p.get("targets") is not None and p.get("blanket_95") is not None
                              for p in pv):
        out["profit_vs_blanket"] = {"targets": round(sum(p["targets"] for p in pv), 4),
                                    "blanket_95": round(sum(p["blanket_95"] for p in pv), 4)}
    out["shards"] = [{"name": s["name"], "series": s["n_series"], "seconds": s["seconds"],
                      "wape": s["summary"].get("wape"), "margin": s["summary"].get("margin")}
                     for s in shards]
    return out


# ===================================================================== using it

@dataclass
class SeriesView:
    """What the nightly run reads from a merged forecast: the series frame."""
    series: pd.DataFrame

    @property
    def n_series(self) -> int:
        return len(self.series)


def is_payload(obj) -> bool:
    return isinstance(obj, dict) and obj.get("format") == FORMAT


class ShardedModel:
    """A saved sharded model, used where a single saved model would be."""

    def __init__(self, payload: dict, issue: str = "blend"):
        self.payload = payload
        self.assign = payload["assign"]
        self.issue = issue                  # every shard issues its blend, or its single best
        self._models: dict[int, dict] = {}
        self._tables: dict | None = None
        self._parts: list | None = None
        self._panels: dict[int, tuple] = {}

    @property
    def shards(self) -> list[dict]:
        return self.payload["shards"]

    def model(self, k: int) -> dict:
        """The k-th trained shard (position in `shards`), loaded on first use."""
        if k not in self._models:
            from ... import live
            self._models[k] = live.issued_as(live.load_model(self.shards[k]["blob"]), self.issue)
        return self._models[k]

    def issued_as(self, issue: str) -> "ShardedModel":
        """The same shards, each issuing its blend or its own single best method
        (live.issued_as). Shares the saved bytes; loads and binds on its own."""
        return ShardedModel(self.payload, issue)

    def can_issue_single(self) -> bool:
        return bool(self.shards) and all(s.get("best_single") for s in self.shards)

    def _by_index(self) -> dict[int, int]:
        return {s["index"]: k for k, s in enumerate(self.shards)}

    def bind(self, tables: dict) -> "ShardedModel":
        """The company's feeds, for scoring on its whole panel (`panel_quantiles`)."""
        self._tables, self._parts, self._panels = tables, None, {}
        return self

    def forecast(self, tables: dict, cfg=None, workers: int | None = None):
        """Tonight's forecast, shard by shard -- in parallel processes when there are
        several shards and workers (DQ_TRAIN_WORKERS) -- merged. Every series is in
        exactly one shard."""
        from ... import live
        parts = split_tables(tables, self.assign)
        index = self._by_index()
        stray = [i for i, t in enumerate(parts) if t is not None and i not in index]
        if stray:                                   # cannot happen with a plan from training
            raise RuntimeError(f"series assigned to shards with no model: {stray}")
        todo = [(i, k) for i, k in sorted(index.items()) if parts[i] is not None]
        n = max(1, min(workers or train_workers(), len(todo)))
        if n > 1:
            with _pool(n) as ex:
                futures = [ex.submit(_forecast_one, self.shards[k]["blob"], parts[i], cfg,
                                     self.issue) for i, k in todo]
                out = [f.result() for f in futures]
        else:
            out = [_parts_of(live.forecast(self.model(k), parts[i], cfg)) for i, k in todo]
        return live.LiveResult(
            P=SeriesView(pd.concat([r["series"] for r in out], ignore_index=True)),
            origin=max(r["origin"] for r in out),
            quantiles=np.concatenate([r["quantiles"] for r in out], axis=0),
            planning_class=np.concatenate([r["planning_class"] for r in out]),
            history_days=np.concatenate([r["history_days"] for r in out]),
            feed_runs=[f for r in out for f in r["feed_runs"]], tier="engine")

    def panel_quantiles(self, P, diag, O: int, cfg) -> np.ndarray:
        """The distribution at origin O for every series of the whole panel P: each
        shard forecasts its own series from its own panel, as it was trained."""
        from ... import live
        if self._tables is None:
            raise RuntimeError("bind the company's tables before scoring a sharded model")
        if self._parts is None:
            self._parts = split_tables(self._tables, self.assign)
        row = {s: r for r, s in enumerate(P.series["series_id"].astype(str))}
        out = None
        day = P.dates[O]
        for i, k in sorted(self._by_index().items()):
            t = self._parts[i]
            if t is None:
                continue
            if i not in self._panels:
                Ps, diag_s, _ = live._prepare(t, cfg)
                self._panels[i] = (Ps, diag_s)
            Ps, diag_s = self._panels[i]
            try:
                Os = Ps.index_of(day)
            except KeyError:
                continue
            qs = live.forecast_panel(self.model(k), Ps, diag_s, Os, cfg)
            if out is None:
                out = np.full((P.n_series, *qs.shape[1:]), np.nan, dtype="float64")
            idx = np.array([row.get(s, -1) for s in Ps.series["series_id"].astype(str)])
            ok = idx >= 0
            out[idx[ok]] = qs[ok]
        missing = np.ones(P.n_series, bool) if out is None else np.isnan(out).any(axis=(1, 2))
        if missing.any():                           # never expected; scored, not dropped
            fill = live.starter_panel(P, O)
            if out is None:
                return fill
            out[missing] = fill[missing]
        return out


def bind(models: dict, tables: dict) -> None:
    for m in models.values():
        if isinstance(m, ShardedModel):
            m.bind(tables)
