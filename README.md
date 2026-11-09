# DQ-Stack

Probabilistic demand forecasting and inventory decision platform. A seven-stage
batch ML pipeline, an economics-gated decision layer, and one production-grade
planner portal.

The thesis it implements, in one sentence: **a forecasting system should be
promoted on the money its decisions make, not on the accuracy of its point
forecasts** — and the pipeline is built so that claim can be tested rather than
asserted.

```bash
python -m dqstack.pipeline --export
```

One command runs all seven stages, writes the decision ledger and every results
table to `artifacts/`, and emits the UI data bundle. Then open
[ui/index.html](ui/index.html) directly in a browser — no server needed.

## The store application

The same engine also runs inside a multi-company store-operations app for Indian
retailers: stores, people and roles, imports, a stock ledger, counting, receiving,
transfers, wastage, sales files, Checkout, a POS connector, the nightly forecast from
each company's own data, Today's review, suggested orders, purchase orders, alerts and
reports. Built to the product specification's twelve steps; see CONTEXT.md C17 for
what each step proves and what is not done yet.

```bash
pip install -c constraints.txt -r requirements.txt -r requirements-dev.txt
python -m dqstack.app.manage seed-demo --email you@example.com   # a demo company
python -m dqstack.app                                            # http://127.0.0.1:8130
```

`/app` is the store app, `/` the public site, `/planner` this planner, `/platform` the
vendor's portal, `/api/docs` the API. Deploying: [deploy/README.md](deploy/README.md).
Security status: [docs/SECURITY.md](docs/SECURITY.md). Tests: `python -m pytest`.

---

## The flow

```
  RAW SALES + PRICES + CALENDAR + EVENTS
              |
   [0] INGEST      declared schema, row counts, fingerprints, totals
              |    a failed blocking check STOPS THE RUN
              v
   [1] DIAGNOSE    ADI/CV² planning class, data-health score,
              |    censoring detection, PELT regime segmentation
              v
   [2] FEATURIZE   lags, rolling stats, calendar, price ratios,
              |    hierarchy encodings, Fourier terms — 149 features
              v
   [3] FORECAST    statistical | LGBM-Tweedie | LGBM-quantile |
              |    NHITS | zero-shot prior | cohort lifecycle
              v
   [4] COMBINE     FFORMA meta-learner → per-series weight vector
              v
   [5] RECONCILE   MinT across 12 hierarchy levels + temporal
              v
   [6] CALIBRATE   split conformal, stratified by class × horizon
              v
   [7] DECIDE      critical fractile → order-up-to → simulator
                   → decision ledger → realised economics
                          |
                          └──→ FVA per stage, promotion gate on margin
```

**Stages 3 through 7 all speak in predictive distributions.** Nothing downstream
of Stage 3 ever sees a point forecast. That single interface contract is what
allows the decision layer, the calibration layer and the economic evaluation to
exist at all.

---

## Why each stage is there

**Stage 0 — a gate, not a warning.** Training on quietly corrupted data produces
a forecast that looks fine and is wrong, and nobody finds out for weeks. A failed
blocking check raises `QualityGateFailure` and the run stops.

**Stage 1 — the stage that matters most.** Feeding stockout zeros into training
teaches the model to under-forecast precisely the items that sell out, which
causes more stockouts. The loop is self-reinforcing and **invisible in accuracy
metrics** — a model that under-forecasts a censored series scores *well* against
the censored actuals it helped produce. Only the economics, or ground truth,
exposes it.

Two censoring signals. *Structural*: no price record for a store-week means the
item was not carried, so those zeros are not demand signal at all and are masked
rather than fed in as zeros. *Probabilistic*: item carried, price present, zero
sold, and this series rarely sells zero on a comparable weekday — flagged with a
confidence score.

**Stage 2 — leakage made structurally impossible.** The panel is held as dense
`(n_series, n_days)` matrices and every feature for origin `O` is computed from
the slice `[:, :O+1]`. There is no code path by which a post-origin value can
enter a feature, because post-origin columns are never in scope. `tests/test_leakage.py`
additionally destroys all data after the origin and asserts no feature value
changes.

**Stage 3 — six families, each earning its place.** Croston-family methods
genuinely beat gradient boosting on sparse intermittent series. Tweedie deviance
fits zero-inflated non-negative counts, which is exactly what retail unit sales
are — and is what won M5. Direct multi-horizon estimation, never recursive,
because error compounds over 28 days.

**Stage 4 — combine, do not select.** Selection is a hard argmax over noisy
validation estimates: if two candidates are within noise it discards one entirely,
and it picks differently on a resample. The meta-learner is trained on the real
FFORMA objective — minimise `Σ pₖ·Lₖ` — not on classifying which model won,
because being wrong between two near-equal candidates costs almost nothing while
being wrong between a good and a terrible one costs a lot.

**Stage 5 — coherence by construction.** MinT dominates bottom-up and top-down at
aggregate levels because it uses the full error covariance rather than assuming a
direction of aggregation. Temporal reconciliation makes "the weekly plan disagrees
with the quarterly plan" structurally impossible rather than a recurring meeting.

**Stage 6 — load-bearing, not cosmetic.** Stage 7 reads a quantile off this
distribution to set inventory. **If the P90 is not really the P90, your service
level is not what you think it is.** Stratified by planning class and horizon,
because one global adjustment would over-widen smooth series and under-widen
lumpy ones.

**Stage 7 — the link to money.** The target service quantile is the critical
fractile:

```
target quantile = underage / (underage + overage)
underage        = lost gross margin per unit
overage         = holding cost over the risk period + markdown + spoilage risk
```

This replaces the industry habit of a blanket 95% service level on everything. A
low-margin, high-holding-cost item *should* be planned much lower than a
high-margin one, and the formula says so precisely. It is also the mechanism by
which better calibration converts into better economics — the causal chain the
whole argument rests on.

---

## Results

Every number below is written by the pipeline into `artifacts/metrics.json` and
rendered in the UI's Pipeline and Evidence views. See `artifacts/` for the full
tables.

| Artifact | What it answers |
|---|---|
| `leaderboard.csv` | Which candidate wins on WSPL and WRMSSE |
| `fva_ladder.csv` | Does each stage add value, or destroy it |
| `fva_by_class.csv` | Where the value lands, per planning class |
| `coverage_table.csv` | Nominal vs empirical coverage, before and after conformal |
| `economics.csv` | Realised margin per configuration |
| `decision_ledger.parquet` | One row per item-location-period: quantile forecast, order, outcome, every cost component |
| `ensemble_weights_by_class.csv` | What the meta-learner chose, and where |

Two findings are the point of the exercise:

1. **The ensemble weights land where the forecasting literature says they should,
   without anyone writing a rule.** The Croston family and temporal aggregation
   earn their weight on intermittent and lumpy series; gradient boosting takes
   smooth ones; the seasonal-naive anchor gets ~0.00, which is the correct
   treatment of a benchmark.

2. **Accuracy and economics do not rank configurations identically.** The
   `accuracy_vs_economics` block reports the Spearman correlation and every
   concrete rank inversion — a configuration that is more accurate on WRMSSE yet
   earns less realised margin. If the two rankings agreed, this system could be
   promoted on WRMSSE and the thesis would have nothing to argue.

---

## Data

No real M5 data is used, and that is a deliberate choice rather than a
compromise.

The M5 competition files record **sales**, never **demand**. Every zero is
ambiguous: nobody wanted it, the store did not stock it, or it sold out at 11am.
That ambiguity is the entire subject of Stage 1, and it *cannot be scored* on M5
because the ground truth does not exist in the file.

`dqstack/data/generate.py` emits an M5-shaped panel — 1,000 item-location series
× 1,100 days, three states, ten stores, seven departments — with both:

- `latent_demand` — what customers wanted (never visible to the pipeline)
- `sales` — what the till recorded (the only input models get)

plus `was_carried` and `was_censored`. Stage 1's censoring detector is therefore
evaluable with **precision and recall against truth**, which is a stronger result
than anything obtainable from the public competition data.

The dynamics reproduce what matters in M5: day-of-week and annual seasonality,
SNAP benefit-day lifts on foods, holiday closures and event lifts, weekly price
records with promotional depth and elasticity, level shifts, assortment gaps
aligned to store-weeks, launch ramps, terminal decline, and stockout episodes
correlated with demand pressure. The calendar and price plan extend 28 days past
the last sales date, because holidays and promotions are genuinely known ahead of
time — and without them there is nothing to forecast onto.

Swapping in real M5 is a change to Stage 0 only: point `dqstack/ingest.py` at the
real files and delete the ground-truth columns. Everything downstream is
unchanged, except that the Stage 1 precision/recall table becomes unmeasurable.

---

## The interface

One portal, four screens, following the specification's 12-week cut. Built to be
an instrument someone stares at for six hours a day, which overturns most
conventions of modern web design.

- **Density is a feature.** Three persisted density modes; ~48 rows at 1440px in
  dense mode. Whitespace here is a tax the user pays in scroll distance.
- **Canvas, not DOM.** Above roughly ten thousand visible cells the browser cannot
  lay out that many elements at frame rate. The grid and both charts are canvas.
- **Every hue means exactly one thing**, and nothing is encoded by colour alone —
  direction gets a glyph, magnitude gets a number.
- **Keyboard beats mouse.** Spreadsheet conventions wholesale: arrows, `J`/`K`,
  `Enter`, `Esc`, `⌘K`, `⌘↵`. These users came from Excel and are fast in it.
- **Explanation is adjacent to the number**, never in a separate "insights" tab,
  because a separate tab does not get read.

**The signature element** is the in-cell uncertainty micro-fan: a small canvas
glyph behind every weekly numeral rendering that week's predictive interval as a
tapered band, vertical extent mapping to the P10–P90 spread and opacity mapping
to confidence. Paired with it, a 3px **confidence gutter** at the left edge of
every row, coloured by data-health score. Together they answer "where is the risk,
and can I trust this number" across fifty rows with no interaction at all.

The **override control shows economic impact before commit** — the direct
interface expression of the thesis. The number shown is the expected newsvendor
cost of the override evaluated against the model's own predictive distribution:
what the change costs *if the forecast is right*. It cannot be what the change
costs if the planner is right, because nobody knows that yet, and presenting it
otherwise would be the dishonest version of that number.

Typography note: the specification asks for Inter on the strength of its tabular
figures. Instrument Sans carries the UI here and **every numeral is set in
JetBrains Mono** — a monospace face is tabular by construction, so column
alignment is guaranteed rather than dependent on a font-feature setting surviving
every code path. Instrument Serif appears only on the executive-register surfaces,
where it encodes a mode change from *scanning* to *reading and deciding*.

---

## Layout

```
dqstack/
  config.py        every threshold and ablation switch in one place
  schemas.py       Stage 0 declared schemas, fingerprints, quality gate
  ingest.py        Stage 0 runner; builds the analysis panel
  diagnose.py      Stage 1 classes, health, censoring, PELT
  features.py      Stage 2 dense-matrix featurisation, split plan
  models/
    dist.py        the predictive-distribution contract (NB lift, sanitise)
    statistical.py seasonal naive, ETS, Croston/SBA, TSB, ADIDA
    gbm.py         LightGBM Tweedie + quantile heads, TreeSHAP attribution
    neural.py      NHITS
    foundation.py  the zero-shot slot
    lifecycle.py   cohort launch curves
  combine.py       Stage 4 FFORMA
  reconcile.py     Stage 5 MinT + temporal
  calibrate.py     Stage 6 split conformal / CQR
  decide.py        Stage 7 fractile, simulator, ledger
  evaluate.py      WRMSSE, WSPL, coverage, FVA, accuracy-vs-economics
  pipeline.py      the orchestrator
  export.py        UI data bundle
tests/
  test_leakage.py    origin invariance, purged splits, mask integrity
  test_simulator.py  conservation, no negative stock, closed form, monotonicity
  test_contracts.py  stage-boundary promises
ui/                  the planner workspace
artifacts/           results tables, ledger, metrics.json
```

Run the tests with:

```bash
python -m pytest tests/ -q
```

---

## Honest limitations

Stated plainly, because a specification this size invites overclaiming.

**The foundation-model slot is a stand-in.** Chronos-2 / TimesFM / Moirai
checkpoints are multi-gigabyte downloads unavailable in this environment. Rather
than stub the slot or fabricate a result, `models/foundation.py` fills it with a
model satisfying the same contract — *forecasts a series it has never seen, using
no lagged history of that series* — implemented as a covariate-only global
LightGBM with every lag, rolling statistic and series-specific encoding
explicitly forbidden. It is API-compatible with a batched Chronos call. What must
**not** be claimed is that this measures foundation-model performance. It measures
the value of the slot, not the value of Chronos.

**MinT shrinkage is heavy at this scale.** The hierarchy has ~1,554 nodes and the
residual sample is smaller than that, so the Schäfer–Strimmer intensity is
substantial and MinT(shrink) sits closer to MinT(WLS) than to full MinT. The
intensity is reported in `metrics.json` rather than hidden.

**Spoilage is an excess-cover approximation.** True FIFO unit-age tracking needs a
per-series queue and cannot be vectorised; the simulator instead expires only
stock beyond what can be sold within its shelf life. A flat `1/shelf_life` daily
decay — the obvious shortcut — destroys most of a perishable's inventory
regardless of turnover and makes waste dominate the ledger.

**Conformal coverage assumes exchangeability**, which is violated under regime
change — precisely when correct intervals matter most. Mitigated with a rolling
calibration window and continuous coverage monitoring, not by assuming the
guarantee holds forever.

**Lower-tail coverage is not achievable on zero-inflated demand, and Stage 6 does
not pretend otherwise.** When 43% of actuals are exactly zero and the predicted
low quantile is also zero, `P(y <= q)` is bounded below by `P(y = 0)`, so a nominal
0.05 target is arithmetically out of reach. The calibrator validates each offset on
a held-out half and declines to adjust levels where adjustment does not help,
which is why the reported adjustment table shows the lower levels as
"declined: no out-of-sample gain" rather than silently shifting mass upward to
chase a number it cannot hit. The median is never shifted at all — CQR
conformalises interval bounds, not the point forecast.

**The censoring mask uses whole-sample weekday zero-rates.** `P(zero | weekday)`
is estimated over each series' full history rather than as of each origin, so the
mask that defines the training target carries a small amount of foresight. Fixing
it properly means recomputing masks per origin, which is 42 rebuilds of a
1,000 × 1,128 panel. What leaks is a stationary property of the series, not a
target value, and the static diagnostic *features* — the larger and more direct
leak — are now computed per origin block. This is the one known residual.

**The backtest rolls the evaluation origin, not retraining.** Models are fitted
once on the training block and evaluated at eight purged origins. That measures
how a fixed system behaves as the origin moves, which is the question the
combination-versus-selection claim needs. It does not measure the value of weekly
retraining, and the results must not be read as if it did.

**Eight paired origins is a small sample.** The paired comparisons report Wilcoxon
and exact sign tests, and with eight observations neither reaches conventional
significance unless the effect is large. Where the direction is consistent but the
test is underpowered, that is what the summary says — "consistent direction,
underpowered" is the honest reading, not "no effect".

**Overrides are not persisted.** There is no write path to a system of record
here, so commits are announced and cleared. The impact estimate is real; the
durable override ledger is not built.

**Five portals are not built**, and this is framed as scope rather than omission:
the executive, data-health, model-operations, admin and mobile portals require
production conditions that public or synthetic data cannot exercise — there are no
real planners generating overrides, no live feeds to monitor, no negotiated cost
parameters to configure. The Pipeline, Evidence and Decisions views cover the
observability those portals would provide for the one thing that *can* be
demonstrated here: the pipeline's own behaviour.
