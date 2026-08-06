"""Stage 1 — diagnose each series.

Three outputs per item-location series, plus a per-row mask:

    planning_class   one of six buckets (SBC quadrants + cold_start + terminal)
    health_score     drives the confidence gutter in the UI
    regime_start     sets the Stage 2 training window
    censor mask      which zeros are not demand signal

Why this stage carries the most weight. Feeding stockout zeros into training
teaches the model to under-forecast precisely the items that sell out, which
causes more stockouts. The loop is self-reinforcing and it is invisible in
accuracy metrics — a model that under-forecasts a censored series scores *well*
against the censored actuals it helped produce. Only Stage 7's economics, or
ground truth, exposes it.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import signal, stats

from .config import CFG, INTERIM, PROCESSED

CLASSES = ("smooth", "erratic", "intermittent", "lumpy", "cold_start", "terminal")

# Days on which stores do not trade. Zeros here are structural.
CLOSURE_EVENTS = ("Christmas",)


# ------------------------------------------------------------------ changepoints

def pelt_l2(x: np.ndarray, penalty: float, min_size: int) -> list[int]:
    """PELT changepoint detection with a least-squares (mean-shift) cost.

    Exact segmentation via dynamic programming with pruning; O(n) amortised.
    Returns interior changepoint indices.
    """
    n = len(x)
    if n < 2 * min_size:
        return []
    cs = np.concatenate([[0.0], np.cumsum(x)])
    cs2 = np.concatenate([[0.0], np.cumsum(x * x)])

    def cost(a: int, b: int) -> float:
        m = b - a
        s = cs[b] - cs[a]
        return float(cs2[b] - cs2[a] - s * s / m)

    F = np.full(n + 1, np.inf)
    F[0] = -penalty
    prev = np.zeros(n + 1, dtype="int64")

    # Candidate set in a preallocated buffer with a live count, so each iteration
    # is numpy slicing rather than list rebuilding. The inner loop must be
    # vectorised: with the *correct* pruning rule the candidate set stays large
    # (that is the point — the buggy rule was fast precisely because it discarded
    # candidates it had no right to), and a Python-level loop over it costs ~31
    # minutes for 1,000 series against ~20 seconds vectorised.
    cand = np.empty(n + 2, dtype="int64")
    cand[0] = 0
    ncand = 1

    for t in range(min_size, n + 1):
        c = cand[:ncand]
        m = t - c
        live = m >= 1
        c, m = c[live], m[live]
        if c.size == 0:
            continue

        seg = cs[t] - cs[c]
        costs = (cs2[t] - cs2[c]) - seg * seg / m
        total = F[c] + costs + penalty

        elig = m >= min_size
        if not elig.any() or not np.isfinite(total[elig]).any():
            # Nothing admissible yet; keep accumulating candidates.
            cand[ncand] = t
            ncand += 1
            continue
        j = np.flatnonzero(elig)[np.argmin(total[elig])]
        F[t], prev[t] = total[j], c[j]

        # Killick's pruning rule: discard s only when it can never again be the
        # optimal predecessor, i.e. keep while F[s] + C(s,t) + K <= F[t], with
        # K = 0 for the L2 mean-shift cost. Substituting the constant `penalty`
        # for `cost(s, t)` over-prunes whenever cost(s, t) < penalty — exactly the
        # low-within-variance, level-shift regime this detector exists to find. It
        # returns a higher-cost partition than the optimum, often collapsing to
        # {0, t} and finding nothing, while still claiming to be exact.
        keep = c[F[c] + costs <= F[t]]
        ncand = keep.size
        cand[:ncand] = keep
        cand[ncand] = t
        ncand += 1

    cps: list[int] = []
    t = n
    while t > 0:
        s = prev[t]
        if s > 0:
            cps.append(int(s))
        if s == t:
            break
        t = s
    return sorted(cps)


# ------------------------------------------------------------------ structure

def _decompose_strengths(y: np.ndarray, period: int = 7) -> tuple[float, float]:
    """Trend and seasonal strength, Wang-Smith-Hyndman formulation.

    1 - var(remainder) / var(component + remainder), floored at zero.
    """
    n = len(y)
    if n < 3 * period:
        return 0.0, 0.0
    k = period if period % 2 else period + 1
    kernel = np.ones(k) / k
    trend = np.convolve(y, kernel, mode="same")
    edge = k // 2
    trend[:edge] = trend[edge]
    trend[-edge:] = trend[-edge - 1]

    detr = y - trend
    idx = np.arange(n) % period
    seas = np.zeros(n)
    for p in range(period):
        m = idx == p
        if m.any():
            seas[m] = detr[m].mean()
    rem = detr - seas

    vr = float(np.var(rem))
    vt = float(np.var(trend + rem))
    vs = float(np.var(seas + rem))
    t_str = 0.0 if vt <= 1e-12 else max(0.0, 1.0 - vr / vt)
    s_str = 0.0 if vs <= 1e-12 else max(0.0, 1.0 - vr / vs)
    return float(np.clip(t_str, 0, 1)), float(np.clip(s_str, 0, 1))


def _spectral_entropy(y: np.ndarray) -> float:
    if len(y) < 32 or np.allclose(y, y[0]):
        return 1.0
    _, pxx = signal.welch(y - y.mean(), nperseg=min(256, len(y)))
    p = pxx / max(pxx.sum(), 1e-12)
    p = p[p > 0]
    return float(-(p * np.log(p)).sum() / np.log(len(p)))


def _acf(y: np.ndarray, lag: int) -> float:
    if len(y) <= lag + 2:
        return 0.0
    a, b = y[:-lag], y[lag:]
    sa, sb = a.std(), b.std()
    if sa < 1e-9 or sb < 1e-9:
        return 0.0
    return float(np.clip(np.corrcoef(a, b)[0, 1], -1, 1))


def _longest_run(mask: np.ndarray) -> int:
    if not mask.any():
        return 0
    d = np.diff(np.concatenate([[0], mask.view(np.int8), [0]]))
    return int((np.flatnonzero(d == -1) - np.flatnonzero(d == 1)).max())


# ------------------------------------------------------------------ censoring

def detect_censoring(
    sales: np.ndarray, carried: np.ndarray, wday: np.ndarray, cfg=CFG,
    closed: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Two-signal censoring detection.

    Structural (high confidence): the item was not carried, so the zero is not
    demand signal at all. Confidence 1.0 by construction — absence of a price
    record for a store-week is a fact about assortment, not an inference.

    Probabilistic (lower confidence): item carried, price present, zero sold,
    and this series rarely sells zero on a comparable day. The comparable day is
    the same weekday, because a Sunday zero on a Sunday-heavy series means
    something different from a Tuesday zero.

    Returns (structural_mask, probabilistic_confidence).
    """
    n = len(sales)
    structural = ~carried
    conf = np.zeros(n, dtype="float64")

    # Store-closure days (Christmas) are zero for every item in the store at once.
    # That is a structural zero, not a per-item stockout, and treating it as one
    # manufactures a false positive on every series simultaneously — the single
    # largest source of false positives in the detector.
    if closed is not None:
        structural = structural | closed

    obs = carried & ~structural
    if obs.sum() < cfg.diagnosis.censor_min_history:
        return structural, conf

    # P(zero | weekday), estimated from carried days STRICTLY BEFORE each row.
    #
    # A whole-series estimate makes the mask non-causal, and the mask is what
    # defines the training target: `y[suspect] = NaN` decides which cells exist at
    # all. A supply problem late in the panel would raise the weekday zero-rate and
    # retrospectively un-flag an earlier day, so P.y[:, :O+1] was not a function of
    # data up to O for any origin. Expanding per weekday with Laplace smoothing
    # keeps it causal at the same cost; early rows sit near 0.5 and are therefore
    # conservative, which is the right failure direction for a detector that masks
    # data.
    p_row = np.full(n, 0.5)
    prior_n = np.zeros(n)
    for d in range(7):
        idx = np.flatnonzero(obs & (wday == d + 1))
        if len(idx) == 0:
            continue
        z = (sales[idx] == 0).astype("float64")
        cum_z = np.concatenate([[0.0], np.cumsum(z)[:-1]])      # strictly before
        cum_n = np.arange(len(idx), dtype="float64")
        p_row[idx] = (cum_z + 1.0) / (cum_n + 2.0)
        prior_n[idx] = cum_n

    # Trailing level from carried days only, so assortment gaps do not deflate it.
    s = pd.Series(np.where(carried, sales, np.nan))
    level = s.rolling(28, min_periods=6).median().shift(1).to_numpy()
    level = np.nan_to_num(level, nan=0.0)

    # Require a minimum of prior same-weekday observations before flagging, so the
    # first few weeks of a series cannot be masked on almost no evidence.
    zero_now = obs & (sales == 0) & (prior_n >= 4)
    pz = p_row
    # Two independent multipliers: how surprising a zero is for this weekday,
    # and how much volume the series was recently doing.
    surprise = np.clip(1.0 - pz / 0.85, 0.0, 1.0)
    volume = np.clip(level / 2.0, 0.0, 1.0)
    conf = np.where(zero_now, surprise * volume, 0.0)

    # A run of consecutive suspicious zeros is more likely a stockout than a
    # coincidence, so escalate confidence within runs.
    run = np.zeros(n)
    c = 0.0
    for i in range(n):
        c = c + 1.0 if conf[i] > 0.15 else 0.0
        run[i] = c
    # An isolated zero is a run of length one and earns no boost: the escalation
    # starts on the *second* consecutive suspicious day. Boosting a single zero by
    # 22% treats "it happens" as evidence of a stockout, and single zeros are the
    # overwhelming majority of zeros on an intermittent series.
    conf = np.clip(conf * (1.0 + 0.22 * np.clip(run - 1.0, 0.0, 4.0)), 0.0, 0.99)
    return structural, conf


# ------------------------------------------------------------------ main

def run(panel: pd.DataFrame | None = None, cfg=CFG, asof=None
        ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Diagnose every series using data up to `asof` only.

    The cutoff exists because these outputs are *features*. Computing them once
    over the full history and attaching them to a training row at an earlier
    origin leaks: a 2025 example would carry that series' 2026 intermittency,
    health score and age. It cannot leak the target value, only the series'
    character, which is precisely the kind of mild leakage that survives review
    and inflates a backtest.
    """
    if panel is None:
        panel = pd.read_parquet(INTERIM / "panel.parquet")
    if asof is not None:
        panel = panel[panel["date"] <= pd.Timestamp(asof)].reset_index(drop=True)

    panel = panel.sort_values(["series_id", "date"], kind="stable").reset_index(drop=True)
    asof = pd.Timestamp(panel["date"].max())

    n_rows = len(panel)
    mask_structural = np.zeros(n_rows, dtype=bool)
    mask_conf = np.zeros(n_rows, dtype="float64")
    mask_suspected = np.zeros(n_rows, dtype=bool)

    records: list[dict] = []
    codes = panel["series_id"].to_numpy()
    starts = np.flatnonzero(np.concatenate([[True], codes[1:] != codes[:-1]]))
    bounds = np.append(starts, n_rows)

    # Closure days, resolved once for the whole panel.
    ev = panel["event_name"].astype(str).to_numpy()
    closed_all = np.isin(ev, CLOSURE_EVENTS)

    sales_all = panel["sales"].to_numpy(dtype="float64")
    carried_all = panel["is_carried"].to_numpy()
    wday_all = panel["wday"].to_numpy()
    price_all = panel["sell_price"].to_numpy(dtype="float64")
    dates_all = panel["date"].to_numpy()

    for b in range(len(starts)):
        lo, hi = bounds[b], bounds[b + 1]
        sl = slice(lo, hi)
        sid = str(codes[lo])
        sales = sales_all[sl]
        carried = carried_all[sl]
        wday = wday_all[sl]
        price = price_all[sl]
        dts = dates_all[sl]

        structural, conf = detect_censoring(sales, carried, wday, cfg,
                                            closed=closed_all[sl])
        suspected = conf >= cfg.diagnosis.censor_prob_threshold
        mask_structural[sl] = structural
        mask_conf[sl] = conf
        mask_suspected[sl] = suspected

        # Analysis series: NaN where the observation is not demand signal.
        y = np.where(carried, sales, np.nan)
        if cfg.enable_censor_masking:
            y = np.where(suspected, np.nan, y)

        live = np.flatnonzero(carried)
        if len(live) == 0:
            # Never carried in this store. Not plannable, but it must still
            # appear in the diagnostics table or the Data Health console would
            # silently under-count the assortment.
            records.append(
                {
                    "series_id": sid,
                    "planning_class": "terminal",
                    "n_obs": 0,
                    "health_score": 0.0,
                    "regime_start": pd.Timestamp(dts[0]),
                    "age_weeks": 0.0,
                    "ooa_rate": 1.0,
                    "price_completeness": 0.0,
                    "revenue": 0.0,
                    "span_days": len(sales),
                }
            )
            continue
        first_i, last_i = int(live[0]), int(live[-1])
        obs = y[first_i : last_i + 1]
        valid = obs[~np.isnan(obs)]
        n_valid = len(valid)
        if n_valid < 14:
            records.append(
                {
                    "series_id": sid,
                    "planning_class": "cold_start",
                    "n_obs": n_valid,
                    "health_score": 0.25,
                    "regime_start": pd.Timestamp(dts[first_i]),
                    "age_weeks": max(1.0, n_valid / 7.0),
                }
            )
            continue

        nz = valid[valid > 0]
        n_nz = len(nz)
        adi = float(n_valid / n_nz) if n_nz else float(n_valid)
        cv2 = float((nz.std() / nz.mean()) ** 2) if n_nz > 1 and nz.mean() > 0 else 0.0
        zero_share = float((valid == 0).mean())
        # `valid` drops masked days, so a run measured on it is a run in
        # compressed index space: two zeros separated by a fortnight out of
        # assortment would count as consecutive. Measure on the calendar series,
        # where a gap is a gap.
        longest_zero = _longest_run(np.nan_to_num(obs, nan=-1.0) == 0)

        mean_d = float(valid.mean())
        cv = float(valid.std() / mean_d) if mean_d > 0 else 0.0
        med = float(np.median(valid))
        iqr_med = float((np.percentile(valid, 75) - np.percentile(valid, 25)) / med) if med > 0 else 0.0

        filled = np.nan_to_num(obs, nan=float(np.nanmedian(obs)))
        t_str, s_str = _decompose_strengths(filled, period=7)
        spec_ent = _spectral_entropy(filled)
        acf1 = _acf(filled, 1)
        acf7 = _acf(filled, 7)
        acf364 = _acf(filled, 364) if len(filled) > 380 else 0.0

        age_days = int((asof - pd.Timestamp(dts[first_i])).days) + 1
        age_weeks = age_days / 7.0

        # --- regime segmentation -> training window
        regime_start = pd.Timestamp(dts[first_i])
        if cfg.enable_regime_windows and len(filled) >= 3 * cfg.diagnosis.pelt_min_segment:
            scale = max(filled.std(), 1e-6)
            cps = pelt_l2(
                filled / scale,
                penalty=cfg.diagnosis.pelt_penalty,
                min_size=cfg.diagnosis.pelt_min_segment,
            )
            # Walk the changepoints from most recent backwards and take the first
            # that leaves enough training history. Testing only the last one and
            # otherwise falling back to the whole series discards a perfectly usable
            # earlier break, reverting a multi-regime series to a training window
            # that spans all of its regimes.
            for cp in reversed(cps):
                cand = first_i + cp
                keep_days = int((asof - pd.Timestamp(dates_all[lo + cand])).days)
                if keep_days >= cfg.diagnosis.min_train_days:
                    regime_start = pd.Timestamp(dates_all[lo + cand])
                    break
        weeks_since_break = float((asof - regime_start).days) / 7.0

        # --- terminal test: recent level collapsed, or a trailing zero run
        look = cfg.diagnosis.terminal_lookback_days
        # The comparison window must not contain the window it is compared against.
        # Falling back to `prior = valid` when history is short puts `recent`
        # inside its own baseline, pulling the ratio toward 1 and silently
        # disabling the test on exactly the young series most likely to be dying.
        if n_valid > 2 * look:
            recent, prior = valid[-look:], valid[-2 * look : -look]
        elif n_valid >= 2 * 28:
            h = n_valid // 2
            recent, prior = valid[-h:], valid[:-h]
        else:
            recent = prior = None
        ratio = (
            float(recent.mean() / prior.mean())
            if recent is not None and prior.mean() > 0
            else 1.0
        )

        # A *trailing* run, not the longest run anywhere in the window. An item
        # that was out for three weeks in month one and has sold every day since
        # is not terminal, and `_longest_run` over the window cannot tell the
        # difference.
        zero_tail = 0
        for v in valid[::-1]:
            if v != 0:
                break
            zero_tail += 1

        delisted = bool(last_i < len(carried) - 14)
        is_terminal = bool(
            ratio < cfg.diagnosis.terminal_decline_ratio
            or zero_tail >= cfg.diagnosis.terminal_min_zero_run
            or delisted
        )

        # --- classification
        if age_weeks < cfg.diagnosis.cold_start_weeks:
            klass = "cold_start"
        elif is_terminal:
            klass = "terminal"
        elif adi < cfg.diagnosis.adi_threshold:
            klass = "smooth" if cv2 < cfg.diagnosis.cv2_threshold else "erratic"
        else:
            klass = "intermittent" if cv2 < cfg.diagnosis.cv2_threshold else "lumpy"

        # --- data health
        window = slice(first_i, last_i + 1)
        span = max(last_i - first_i + 1, 1)
        censored_rate = float(suspected[window].mean())
        ooa_rate = float((~carried[window]).mean())
        price_completeness = float(np.isfinite(price[window]).mean())
        # last_i is the last day the item was *carried*. Freshness is about
        # whether the series is still transacting, so measure from the last non-zero
        # sale; an item sitting on the shelf selling nothing for a month is not
        # fresh data, however current its price record is.
        nz_idx = np.flatnonzero(np.nan_to_num(obs, nan=0.0) > 0)
        last_sale_i = (first_i + int(nz_idx[-1])) if len(nz_idx) else last_i
        last_sale_gap = int((asof - pd.Timestamp(dates_all[lo + last_sale_i])).days)
        freshness = float(np.clip(1.0 - last_sale_gap / 14.0, 0.0, 1.0))

        # `price_completeness` is 1 - ooa_rate by construction here: FR-07 defines
        # availability *as* price-record presence, so the two terms are the same
        # measurement and summing both charges assortment gaps twice (0.45 of the
        # score). The redundant term is dropped and its weight folded into the
        # assortment term, which keeps the components summing to one. The column is
        # still reported, because on a real feed a carried day can lack a price and
        # the two would then differ.
        w = cfg.health
        w_assort = w.out_of_assortment + w.price_completeness
        health = 1.0 - (
            w.censored * np.clip(censored_rate / 0.08, 0, 1)
            + w_assort * np.clip(ooa_rate / 0.25, 0, 1)
            + w.freshness * (1.0 - freshness)
        )
        health = float(np.clip(health, 0.0, 1.0))

        avg_price = float(np.nanmean(price[window])) if np.isfinite(price[window]).any() else np.nan
        price_vol = float(np.nanstd(price[window]) / avg_price) if avg_price and avg_price > 0 else 0.0
        revenue = float(np.nansum(np.where(carried[window], sales[window], 0) * np.nan_to_num(price[window])))

        records.append(
            {
                "series_id": sid,
                "planning_class": klass,
                "adi": adi,
                "cv2": cv2,
                "zero_share": zero_share,
                "longest_zero_run": longest_zero,
                "cv": cv,
                "iqr_over_median": iqr_med,
                "mean_demand": mean_d,
                "trend_strength": t_str,
                "seasonal_strength": s_str,
                "spectral_entropy": spec_ent,
                "acf1": acf1,
                "acf7": acf7,
                "acf364": acf364,
                "age_weeks": age_weeks,
                "weeks_since_break": weeks_since_break,
                "regime_start": regime_start,
                "is_terminal": is_terminal,
                "n_obs": n_valid,
                "censored_days": int(suspected[window].sum()),
                "censored_rate": censored_rate,
                "ooa_days": int((~carried[window]).sum()),
                "ooa_rate": ooa_rate,
                "price_completeness": price_completeness,
                "freshness": freshness,
                "days_since_last_sale": last_sale_gap,
                "health_score": health,
                "avg_price": avg_price,
                "price_volatility": price_vol,
                "revenue": revenue,
                "span_days": span,
            }
        )

    diag = pd.DataFrame(records)

    # ABC by revenue, XYZ by variability — commercial features
    diag["revenue"] = diag["revenue"].fillna(0.0)
    share = diag["revenue"].sort_values(ascending=False).cumsum() / max(diag["revenue"].sum(), 1)
    abc = pd.Series("C", index=share.index)
    abc[share <= 0.80] = "A"
    abc[(share > 0.80) & (share <= 0.95)] = "B"
    diag["abc"] = abc.reindex(diag.index)
    diag["xyz"] = pd.cut(
        diag["cv"].fillna(0), bins=[-0.01, 0.5, 1.0, np.inf], labels=["X", "Y", "Z"]
    ).astype(str)

    masks = pd.DataFrame(
        {
            "series_id": panel["series_id"].to_numpy(),
            "date": panel["date"].to_numpy(),
            "structural_masked": mask_structural,
            "censor_confidence": mask_conf,
            "suspected_censored": mask_suspected,
        }
    )

    diag.to_parquet(PROCESSED / "dim_series_diagnostics.parquet", index=False)
    masks.to_parquet(PROCESSED / "censoring_masks.parquet", index=False)
    return diag, masks


def score_censoring(masks: pd.DataFrame, truth: pd.DataFrame) -> dict[str, float]:
    """Score the probabilistic detector against generator ground truth.

    Reported honestly in two ways: over all censored rows, and over the
    full-zero subset. Partial-censoring days (sales recorded but below latent
    demand) are close to undetectable from sales alone, and pretending
    otherwise would overstate the detector.
    """
    m = masks
    pred = m["suspected_censored"].to_numpy()
    actual = m["was_censored"].to_numpy() & m["was_carried"].to_numpy()
    full_zero = actual & (m["sales"].to_numpy() == 0)

    def prf(p, a):
        tp = int((p & a).sum())
        fp = int((p & ~a).sum())
        fn = int((~p & a).sum())
        prec = tp / max(tp + fp, 1)
        rec = tp / max(tp + fn, 1)
        f1 = 2 * prec * rec / max(prec + rec, 1e-9)
        return prec, rec, f1, tp, fp, fn

    p1, r1, f1, tp, fp, fn = prf(pred, actual)
    # Restrict to the full-zero subset on BOTH sides. Scoring predictions against
    # `full_zero` while still counting hits on partial-censoring days as false
    # positives penalises the detector for being right about a day this metric
    # excludes by construction.
    p2, r2, f2, *_ = prf(pred & (full_zero | ~actual), full_zero)
    return {
        "precision": p1, "recall": r1, "f1": f1,
        "tp": tp, "fp": fp, "fn": fn,
        "precision_full_zero": p2, "recall_full_zero": r2, "f1_full_zero": f2,
        "actual_censored_rows": int(actual.sum()),
        "flagged_rows": int(pred.sum()),
    }


if __name__ == "__main__":
    diag, masks = run()
    print(f"series diagnosed  {len(diag):,}\n")
    print(diag["planning_class"].value_counts().to_string())
    print()
    print(
        diag.groupby("planning_class")[["adi", "cv2", "health_score", "n_obs"]]
        .mean()
        .round(3)
        .to_string()
    )
    print(f"\nmean health       {diag['health_score'].mean():.3f}")
    print(f"regime-truncated  {(diag['weeks_since_break'] * 7 < diag['span_days'] - 1).sum():,} series")

    truth = pd.read_parquet(INTERIM / "truth.parquet")
    truth["series_id"] = truth["item_id"].astype(str) + "|" + truth["store_id"].astype(str)
    panel = pd.read_parquet(INTERIM / "panel.parquet")[["series_id", "date", "sales"]]
    m = masks.merge(truth[["series_id", "date", "was_censored", "was_carried"]],
                    on=["series_id", "date"], how="inner").merge(panel, on=["series_id", "date"])
    sc = score_censoring(m, truth)
    print("\ncensoring detection vs ground truth")
    for k, v in sc.items():
        print(f"  {k:24s} {v if isinstance(v, int) else round(v, 4)}")
