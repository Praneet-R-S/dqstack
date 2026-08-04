"""Export the UI data bundle.

Written as `window.DQ_DATA = {...}` in a `.js` file rather than a `.json` fetched
at runtime, because `fetch()` against `file://` is blocked by the browser's
origin rules and the workspace has to open by double-clicking `index.html` with
no server. Production would serve Arrow IPC over HTTP per CON-1; this is the
capstone substitute and the shape of the payload is the same.

Rounding is deliberate. Quantile fans are stored to one decimal and history to
integers, which cuts the bundle by roughly two thirds with no visible difference
at chart resolution.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from . import combine as comb
from .config import ARTIFACTS, CFG, HORIZON, QUANTILES, UI_DATA
from .decide import quantile_moments
from .features import horizon_group_of

HISTORY_DAYS = 126
# Levels kept for the chart fan: three nested bands plus the median.
CHART_Q = (0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95)
CHART_IDX = [QUANTILES.index(q) for q in CHART_Q]


def _r(a, nd=1):
    return np.round(np.asarray(a, dtype="float64"), nd).tolist()


def _clean(o):
    """JSON-safe: numpy scalars, NaN and Inf all become plain Python."""
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        f = float(o)
        return None if (np.isnan(f) or np.isinf(f)) else round(f, 6)
    if isinstance(o, (np.bool_, bool)):
        return bool(o)
    if isinstance(o, (pd.Timestamp,)):
        return o.date().isoformat()
    if o is None or isinstance(o, (str, int)):
        return o
    return str(o)


# ------------------------------------------------------------------ prior version

def prior_forecast(state: dict, lag: int = 7) -> tuple[np.ndarray, list[str]]:
    """The forecast this system produced `lag` days ago, for today's target dates.

    Planners overwhelmingly ask "what changed since last cycle", and almost no
    tool shows it. Recomputing one extra origin is cheap and it turns the chart
    into a diff rather than a snapshot.
    """
    from .pipeline import candidate_quantiles

    P = state["P"]
    Ol = state["live"]["origin"]
    Op = Ol - lag
    horizons = tuple(range(1, HORIZON + 1))
    _, qs = candidate_quantiles(P, Op, horizons, state["trained"])
    cq = comb.combine_quantiles(qs, state["weights"], state["meta"].candidates)
    if CFG.enable_reconciliation:
        rc = state["reconciler"]
        cq = rc.reconcile_quantiles(cq, rc.base_forecasts(P.y, Op, range(1, HORIZON + 1)))
    pc = P.series["planning_class"].astype(str).to_numpy(dtype="U20")
    hg = np.array([horizon_group_of(h) for h in horizons], dtype="U12")
    pc2 = np.repeat(pc[:, None], len(horizons), axis=1)
    hg2 = np.broadcast_to(hg, (len(pc), len(horizons)))
    if CFG.enable_calibration:
        cq = state["conformal"].apply(cq, pc2, hg2)

    med = cq[..., len(QUANTILES) // 2]
    # Align: horizon h at the prior origin targets the same date as horizon
    # h - lag at the live origin.
    out = np.full((P.n_series, HORIZON), np.nan, dtype="float32")
    out[:, : HORIZON - lag] = med[:, lag:]
    return out, [str(P.dates[Op].date())]


# ------------------------------------------------------------------ worklist

def build_exceptions(
    diag: pd.DataFrame, fan: np.ndarray, prior: np.ndarray, ledger_by_series: pd.DataFrame,
    frac: dict[str, np.ndarray], series_order: np.ndarray,
) -> pd.DataFrame:
    """Score every series for the exception worklist.

    Planners do not browse thirty thousand SKUs, they work a queue. The queue is
    the primary navigation, so its ordering is a product decision, not a sort.
    Each reason is surfaced as its own tag so a planner can filter to the kind of
    problem they are in the mood to fix.
    """
    med = fan[..., len(QUANTILES) // 2]
    lo = fan[..., QUANTILES.index(0.10)]
    hi = fan[..., QUANTILES.index(0.90)]

    total = med.sum(axis=1)
    width = (hi - lo).sum(axis=1)
    rel_width = width / np.maximum(total, 1.0)

    with np.errstate(invalid="ignore", divide="ignore"):
        prior_tot = np.nansum(prior[:, : HORIZON - 7], axis=1)
        now_tot = med[:, : HORIZON - 7].sum(axis=1)
        swing = np.where(prior_tot > 1.0, (now_tot - prior_tot) / prior_tot, 0.0)

    # `fan`, `prior` and `frac` are all in matrix-row order, so the diagnostics
    # must be reindexed onto that order explicitly. They happen to agree today
    # (both are lexicographic by series_id), but relying on that coincidence
    # would silently mis-attribute every exception the moment either side's
    # ordering changed — and it would look like a plausible worklist, not a bug.
    d = diag.set_index("series_id").reindex(series_order)
    assert len(d) == fan.shape[0], f"diagnostics {len(d)} != forecast rows {fan.shape[0]}"
    health = d["health_score"].to_numpy()
    censored = d["censored_rate"].to_numpy()
    klass = d["planning_class"].to_numpy()
    revenue = np.nan_to_num(d["revenue"].to_numpy())

    lg = ledger_by_series.reindex(d.index)
    lost_margin = np.nan_to_num(lg["lost_margin"].to_numpy())
    fill = np.nan_to_num(lg["fill_rate"].to_numpy(), nan=1.0)

    reasons: list[list[str]] = [[] for _ in range(len(d))]
    for i in range(len(d)):
        if fill[i] < 0.95 or lost_margin[i] > 0:
            reasons[i].append("risk")
        if health[i] < 0.70:
            reasons[i].append("stale")
        if censored[i] > 0.04:
            reasons[i].append("censored")
        if rel_width[i] > 1.6:
            reasons[i].append("wide")
        if abs(swing[i]) > 0.25:
            reasons[i].append("swing")
        if klass[i] == "cold_start":
            reasons[i].append("cold-start")
        if klass[i] == "terminal":
            reasons[i].append("terminal")

    rev_rank = pd.Series(revenue).rank(pct=True).to_numpy()
    priority = (
        2.2 * np.clip(lost_margin / max(np.nanmax(lost_margin), 1.0), 0, 1)
        + 1.4 * (1.0 - health)
        + 1.0 * np.clip(np.abs(swing), 0, 1)
        + 0.8 * np.clip(rel_width / 3.0, 0, 1)
        + 0.9 * rev_rank
    )

    return pd.DataFrame(
        {
            "series_id": d.index,
            "reasons": reasons,
            "priority": priority,
            "swing": swing,
            "rel_width": rel_width,
            "forecast_total": total,
            "lost_margin": lost_margin,
            "fill_rate": fill,
        }
    )


# ------------------------------------------------------------------ main

def export_bundle(state: dict, out_dir: Path = UI_DATA, explain_top: int = 160) -> Path:
    P = state["P"]
    diag = state["diag"]
    metrics = state["metrics"]
    live = state["live"]
    ev = state["eval"]
    cp = state["cost"]

    fan = live["calibrated"]                       # (S, H, Q)
    frac = live["fractile"]
    sids = P.series["series_id"].to_numpy()
    Ol = live["origin"]

    print("  computing prior forecast version (origin - 7d)", flush=True)
    prior, prior_meta = prior_forecast(state, lag=7)

    # ---- per-series ledger rollup from the eval-origin simulation
    led = ev["sim"].ledger
    led_s = led.groupby("series_id", observed=True).agg(
        lost_margin=("lost_margin", "sum"),
        realized_margin=("realized_margin", "sum"),
        holding_cost=("holding_cost", "sum"),
        waste_cost=("waste_cost", "sum"),
        demand=("demand", "sum"),
        sales=("sales", "sum"),
        orders=("order_placed", "sum"),
    )
    led_s["fill_rate"] = led_s["sales"] / led_s["demand"].replace(0, np.nan)

    exc = build_exceptions(diag, fan, prior, led_s, frac, sids)
    exc = exc.set_index("series_id")

    # ---- explanations for the head of the worklist
    top_ids = exc.sort_values("priority", ascending=False).head(explain_top).index.tolist()
    print(f"  computing TreeSHAP attributions for {len(top_ids)} series", flush=True)
    contribs = state["trained"].gbm.contributions(P, Ol, top_ids, horizon=7, top=8)

    # ---- ensemble weights per series
    W = state["weights"]
    cand = state["meta"].candidates

    dd = diag.set_index("series_id").reindex(sids)
    hist_slice = slice(max(0, Ol - HISTORY_DAYS + 1), Ol + 1)
    hist_dates = [str(d.date()) for d in P.dates[hist_slice]]
    fut_dates = [str(P.dates[Ol + h].date()) for h in range(1, HORIZON + 1)]

    y_masked = P.y[:, hist_slice]
    y_raw = P.y_raw[:, hist_slice]
    carried = P.carried[:, hist_slice]

    mean_d, var_d = quantile_moments(fan)

    series_out = []
    for i, sid in enumerate(sids):
        row = dd.loc[sid]
        e = exc.loc[sid]
        lg = led_s.reindex([sid]).iloc[0]

        f = fan[i][:, CHART_IDX]
        wk = f.reshape(4, 7, len(CHART_Q)).sum(axis=1)

        w_i = W[i]
        order = np.argsort(-w_i)[:5]

        series_out.append(
            {
                "id": str(sid),
                "item": str(P.series["item_id"].iloc[i]),
                "store": str(P.series["store_id"].iloc[i]),
                "dept": str(P.series["dept_id"].iloc[i]),
                "cat": str(P.series["cat_id"].iloc[i]),
                "state": str(P.series["state_id"].iloc[i]),
                "cls": str(row["planning_class"]),
                "health": round(float(row["health_score"]), 3),
                "adi": round(float(row["adi"]), 2) if np.isfinite(row["adi"]) else None,
                "cv2": round(float(row["cv2"]), 2) if np.isfinite(row["cv2"]) else None,
                "abc": str(row["abc"]),
                "xyz": str(row["xyz"]),
                "censoredDays": int(row["censored_days"]) if np.isfinite(row["censored_days"]) else 0,
                "ooaDays": int(row["ooa_days"]) if np.isfinite(row["ooa_days"]) else 0,
                "priceCompleteness": round(float(row["price_completeness"]), 3),
                "ageWeeks": round(float(row["age_weeks"]), 1),
                "regimeStart": str(pd.Timestamp(row["regime_start"]).date())
                               if pd.notna(row["regime_start"]) else None,
                "revenue": round(float(np.nan_to_num(row["revenue"])), 2),
                "price": round(float(cp.price[i]), 2),
                "cost": round(float(cp.unit_cost[i]), 2),
                "marginRate": round(float(cp.margin_rate[i]), 3),
                "shelfLife": int(cp.shelf_life[i]),
                "targetQ": round(float(frac["target_quantile"][i]), 4),
                "underage": round(float(frac["underage_cost"][i]), 4),
                "overage": round(float(frac["overage_cost"][i]), 4),
                "holdingComp": round(float(frac["holding_component"][i]), 4),
                "markdownComp": round(float(frac["markdown_component"][i]), 4),
                "spoilComp": round(float(frac["spoilage_component"][i]), 4),
                "orderUpTo": float(np.round(frac["order_up_to"][i])),
                "weekly": [_r(wk[w], 1) for w in range(4)],
                "fan": [_r(f[t], 1) for t in range(HORIZON)],
                "prior": _r(np.nan_to_num(prior[i], nan=-1), 1),
                "hist": [None if not np.isfinite(v) else int(v) for v in y_masked[i]],
                "histRaw": [int(v) if np.isfinite(v) else 0 for v in y_raw[i]],
                "carried": [bool(v) for v in carried[i]],
                "mean": _r(mean_d[i], 1),
                "sd": _r(np.sqrt(var_d[i]), 2),
                "weights": {cand[k]: round(float(w_i[k]), 4) for k in order},
                "reasons": list(e["reasons"]),
                "priority": round(float(e["priority"]), 4),
                "swing": round(float(e["swing"]), 4),
                "relWidth": round(float(e["rel_width"]), 3),
                "why": contribs.get(str(sid), []),
                "ledger": {
                    "demand": float(np.nan_to_num(lg["demand"])),
                    "sales": float(np.nan_to_num(lg["sales"])),
                    "lostMargin": round(float(np.nan_to_num(lg["lost_margin"])), 2),
                    "realizedMargin": round(float(np.nan_to_num(lg["realized_margin"])), 2),
                    "holdingCost": round(float(np.nan_to_num(lg["holding_cost"])), 2),
                    "wasteCost": round(float(np.nan_to_num(lg["waste_cost"])), 2),
                    "fillRate": round(float(np.nan_to_num(lg["fill_rate"], nan=1.0)), 4),
                    "orders": float(np.nan_to_num(lg["orders"])),
                },
            }
        )

    # ---- calendar annotations over the visible span
    cal = state["panel"][["date", "event_name", "event_type", "snap"]].drop_duplicates("date")
    span_lo = P.dates[hist_slice][0]
    cal = cal[(cal["date"] >= span_lo) & (cal["date"] <= P.dates[Ol] + pd.Timedelta(days=HORIZON))]
    events = [
        {"date": str(pd.Timestamp(r.date).date()), "name": str(r.event_name),
         "type": str(r.event_type)}
        for r in cal.itertuples() if isinstance(r.event_name, str) and r.event_name != "none"
    ]

    # ---- hierarchy rollup for the executive view
    grid = pd.DataFrame({
        "series_id": sids,
        "cat": P.series["cat_id"].astype(str).to_numpy(),
        "dept": P.series["dept_id"].astype(str).to_numpy(),
        "store": P.series["store_id"].astype(str).to_numpy(),
        "state": P.series["state_id"].astype(str).to_numpy(),
        "cls": dd.loc[sids, "planning_class"].to_numpy(),
        "total": fan[..., QUANTILES.index(0.50)].sum(axis=1),
        "p10": fan[..., QUANTILES.index(0.10)].sum(axis=1),
        "p90": fan[..., QUANTILES.index(0.90)].sum(axis=1),
        "revenue": np.nan_to_num(dd.loc[sids, "revenue"].to_numpy()),
        "health": dd.loc[sids, "health_score"].to_numpy(),
        "orderUpTo": frac["order_up_to"],
    })
    rollups = {}
    for lvl in ("cat", "dept", "store", "state", "cls"):
        g = grid.groupby(lvl).agg(
            series=("series_id", "count"), total=("total", "sum"),
            p10=("p10", "sum"), p90=("p90", "sum"),
            revenue=("revenue", "sum"), health=("health", "mean"),
            orderUpTo=("orderUpTo", "sum"),
        ).reset_index().rename(columns={lvl: "key"})
        rollups[lvl] = g.round(2).to_dict("records")

    temporal = live["temporal"]
    bundle = {
        "run": {
            "generatedAt": metrics["generated_at"],
            "origin": str(P.dates[Ol].date()),
            "evalOrigin": metrics["origins"]["eval"],
            "priorOrigin": prior_meta[0],
            "horizon": HORIZON,
            "nSeries": int(P.n_series),
            "nPanelRows": int(metrics["n_panel_rows"]),
            "quantiles": list(QUANTILES),
            "chartQuantiles": list(CHART_Q),
            "historyDays": HISTORY_DAYS,
            "reviewPeriod": CFG.decision.review_period,
            "leadTime": CFG.decision.lead_time,
            "timings": metrics["timings_seconds"],
            "origins": metrics["origins"],
        },
        "dates": {"history": hist_dates, "future": fut_dates},
        "events": events,
        "series": series_out,
        "rollups": rollups,
        "metrics": {
            "censoring": metrics["censoring_detection"],
            "leaderboard": metrics["leaderboard"],
            "fvaLadder": metrics["fva_ladder"],
            "fvaByClass": metrics["fva_by_class"],
            "coverage": metrics["coverage"],
            "economics": metrics["economics"],
            "accuracyVsEconomics": metrics["accuracy_vs_economics"],
            "weightsByClass": metrics["ensemble_weights_by_class"],
            "simulator": metrics["simulator_totals"],
            "mintShrinkage": metrics["mint_shrinkage"],
            "effectiveCandidates": metrics["mean_effective_candidates"],
            "classCounts": diag["planning_class"].value_counts().to_dict(),
            "healthHistogram": np.histogram(
                diag["health_score"].fillna(0), bins=20, range=(0, 1)
            )[0].tolist(),
            "temporal": {
                "weekly": _r(temporal["weekly"].sum(axis=0), 1),
                "monthly": float(np.round(temporal["monthly"].sum(), 1)),
                "dailyTotal": _r(temporal["daily"].sum(axis=0), 1),
            },
            "feedRuns": json.loads((ARTIFACTS / "feed_runs.json").read_text()),
        },
        "candidates": cand,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(_clean(bundle), separators=(",", ":"))
    target = out_dir / "bundle.js"
    target.write_text("window.DQ_DATA=" + payload + ";", encoding="utf-8")
    (out_dir / "bundle.json").write_text(payload, encoding="utf-8")
    print(f"  wrote {target} ({len(payload) / 1e6:.1f} MB)", flush=True)
    return target
