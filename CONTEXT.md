# DQ-Stack — project context and progress

**Purpose of this file.** A durable handover so work can resume without the
conversation history. It records what the project is, what has been built, every
defect found and why it mattered, what is left, and the decisions that are settled
so they are not relitigated. Update it when something material changes.

Last updated: after commit `644af67` (six verified findings fixed).

---

## 1. What this is

An operational demand-planning platform: a seven-stage batch ML pipeline producing
probabilistic demand forecasts, an inventory decision layer that converts them into
orders, and one planner portal. Built from four requirement documents (URD, BRD, PRD,
SRS) plus a large system specification, all originally at
`C:\Users\prane\Downloads\filesss\` and `C:\Users\prane\Downloads\files\`.

**The thesis claim:** a forecasting system should be promoted on the money its
decisions make, not on the accuracy of its point forecasts. Everything is built so
that claim can be *tested* rather than asserted.

**Scope follows the specification's own §"The 12-week capstone cut":** implement all
of Stages 1–7 (the research contribution) plus one portal, and explicitly do not
build the other five portals — they are framed as features that public data cannot
exercise, which is a thesis chapter, not an omission.

Run everything with:

```bash
make all          # generate data, run all stages, export the UI bundle
make test         # 79 tests
python -m dqstack.pipeline --export      # the pipeline alone (~25-40 min)
```

Then open `ui/index.html` directly in a browser. No server needed.

---

## 2. Repository map

```
dqstack/
  config.py        every threshold + ablation switches. Q50_INDEX lives here.
  schemas.py       Stage 0 declared schemas, fingerprints, quality gate
  ingest.py        Stage 0 runner; builds the analysis panel
  diagnose.py      Stage 1: ADI/CV2 class, health, censoring, PELT regimes
  features.py      Stage 2: dense-matrix featurisation, SplitPlan, Panel
  models/
    dist.py        the predictive-distribution contract (NB lift, sanitize)
    statistical.py seasonal naive, ETS, Croston/SBA, TSB, ADIDA
    gbm.py         LightGBM Tweedie + 9 quantile heads, TreeSHAP attribution
    neural.py      NHITS
    foundation.py  the zero-shot slot (covariate-only global model)
    lifecycle.py   cohort launch curves
  combine.py       Stage 4: convex stacking + FFORMA proxy
  reconcile.py     Stage 5: MinT with shrinkage, temporal MinT
  calibrate.py     Stage 6: split conformal / CQR
  decide.py        Stage 7: critical fractile, simulator, ledger
  evaluate.py      WRMSSE, WSPL, coverage, FVA, hierarchical accuracy
  backtest.py      rolling-origin backtest with paired inference
  sensitivity.py   cost-parameter sweep
  ablate.py        ablation runner (NOT YET EXECUTED)
  pipeline.py      the orchestrator
  export.py        UI data bundle
  data/generate.py synthetic M5-shaped panel generator
tests/             79 tests: leakage, simulator, contracts, diagnose, backtest, sensitivity
ui/                planner workspace (canvas grid + canvas chart, no build step)
artifacts/         results tables, ledger, metrics.json  (gitignored)
data/              generated panel                        (gitignored)
```

---

## 3. Settled decisions — do not relitigate

**Synthetic data, deliberately.** Real M5 records *sales*, never *demand*, so every
zero is ambiguous and Stage 1's censoring detector cannot be scored on it. The
generator emits `latent_demand` alongside `sales` plus `was_carried` / `was_censored`,
which makes censoring detection measurable with precision and recall. Swapping in real
M5 is a Stage 0 change only; the cost is that the Stage 1 precision/recall table
becomes unmeasurable.

**Calendar and prices extend 28 days past the last sales date.** Holidays and the
promotional plan are genuinely known ahead; without them there is nothing to forecast
onto. `Panel.n_history` marks where observations stop.

**Typography.** The spec asks for Inter for tabular figures. Instrument Sans carries
the UI and *every numeral is JetBrains Mono* — a monospace face is tabular by
construction, so alignment is guaranteed rather than dependent on a font-feature
setting surviving every code path. Instrument Serif appears only on executive
surfaces, encoding a mode change from scanning to deciding.

**The foundation-model slot is a documented stand-in.** Chronos-2 / TimesFM weights
are multi-GB downloads unavailable here. `models/foundation.py` fills the slot with a
covariate-only global LightGBM that satisfies the same contract — forecasts a series
it has never seen, using no lagged history of that series. It must **not** be claimed
to measure foundation-model performance; it measures the value of the slot.

**Currency stays in dollars.** The data is M5-calibrated and the benchmark literature
is USD. The critical fractile is a ratio, so currency cannot change any forecast or
decision — only labels. If rupees are needed, do a display-level conversion in
`ui/js/util.js`, not a regeneration.

---

## 4. Defects found and fixed — the methodological story

**Thirty-plus defects, and roughly eleven were silent**: no exception, no warning,
just a plausible-looking number. This is the most valuable material in the project
and belongs in the thesis.

**Three presented identically — as a pipeline stage that appeared to trade accuracy
for something else.** None was a statistical trade-off:

| symptom | actual cause |
|---|---|
| reconciliation "hurt accuracy" | matrix conditioning; `G·S ≠ I` |
| calibration "cost 19% WRMSSE" | `sanitize()` sorting promoted an adjusted p25 into the median slot |
| every candidate "over-forecast by 16%" | `len(QUANTILES)//2` is index 4 = **P75**, not the median |

Any of the three, taken at face value, would have become a defended finding.
**A negative FVA deserves a mechanical investigation before it gets a narrative.**

**Four "checks" could not fail** — worse than no check, because the run record credits
them: `coherence_error` compared an expression with itself; `temporal_reconcile` was an
algebraic identity; FR-06 compared the sales total against itself;
`cfg.enable_combination` was printed and never consulted.

**Two Stage-7 halves disagreed about what overage means.** The critical fractile
priced markdown and spoilage; the ledger levied neither. Markdown was fixed first, and
spoilage — the twin — was missed until independent verification found it. It was 54% of
total overage on FOODS while the simulator returned `waste_cost` of exactly 0.00.
There is now a test for the *general property*: every overage component the fractile
prices must be one the ledger can realise.

**Leakage was claimed to be structurally impossible, and was not.** Stage-1
diagnostics were computed over the full 1,100 days and attached as features at every
training origin; the leakage tests missed it because they only exercised
`origin_features()`. Fixed with per-origin-block vintages. The censoring mask was a
second, subtler instance — and `Panel.with_series` carried a docstring positively
asserting that `y` does not depend on when Stage 1 ran, which was false and hid it.

**Also worth remembering:** `lifecycle_cohort` extrapolated a launch curve onto mature
series producing ~5,000× values, which poisoned the whole ensemble at 0.2% weight
because combination averages levels, not ranks. Hence `plausibility_cap()` as defence
in depth — measured at 0.197% of values, never touching a median or a P95.

---

## 5. Results as they stand

Numbers move between runs; treat `artifacts/metrics.json` as authoritative and the
*stage ladder within a single run* as the valid comparison, never a diff across runs.

**Validated:**

- **Stage 6 calibration** improves WSPL at **8/8 origins** (Wilcoxon p=0.008) with
  WRMSSE, MASE and bias *identical* to the uncalibrated arm — exactly what the
  median-anchor design intends.
- **The critical fractile beats a blanket 95% service level in 75/75 cost scenarios**
  (holding 0.33×–3×, markdown 0.25×–4×, margin 0.6×–1.5×), mean advantage ~$120k,
  never below ~$31.6k. This is the examiner-proof answer to "what if your holding cost
  is wrong".
- **The ensemble owns bias**: |bias| ~0.06 against ~0.14 for the best single model,
  better at 8/8. Every individual candidate under-forecasts by 13–36%; only the
  combination does not.
- **Censoring detection**: precision ~0.73, recall ~0.62 on full-zero days. The gap
  down to ~0.43 overall recall is partial-censoring days, undetectable from sales
  alone — an honest asymmetry worth reporting.

**Reconciliation: report with it, order without it.**

Judged across all twelve aggregation levels, MinT improves the top and degrades the
bottom, monotonically: total **-0.45**, state -0.36, category -0.31, store -0.19, ...
item_store **+0.44**. Leaf-level WRMSSE goes 0.77 to 1.20, worse than the
seasonal-naive anchor at 0.98. The ablation prices it: the arm without reconciliation
earns **+150,692** realised margin over the full system, the largest positive delta in
the table.

Stage 7 sets an order-up-to level per item-location, so the decision is made at the
leaves and there the trade is against us. `ReconcileConfig.for_decisions = False`
routes the *decision* path through the unreconciled distribution while the aggregate
planning views keep the reconciled one, which is coherent by construction and
materially better at every level a planning organisation reviews. Reporting one number
for both would have to be wrong for one of them. The spec anticipated this and
declined to choose: "which matters depends on the decision being made."

**Open / negative:**

- **Combination does not clearly pay here.** The FFORMA proxy `Σ pₖLₖ` is *linear in
  p*, so its optimum over the simplex is a vertex — pure selection — and it cannot
  express any benefit from averaging. Replaced with convex stacking on
  `pinball(y, Σ wₖqₖ)`, which moved combination from 0/8 to 4/8 and dropped the paired
  test from p=0.008 to p=0.055. With one candidate dominating a pool whose next-best is
  8% worse, this is not FFORMA's regime. **Report as a finding, not a failure.** The
  arm is chosen on the *meta* origins, never on eval.
- **Reconciliation** was 0/8 at the bottom level. `evaluate.hierarchical_accuracy()`
  now scores all twelve levels, because judging MinT at the leaves tests the one level
  where it is expected to lose. Read that table before concluding anything.

---

## 6. What is left

### A. Evidence gaps — DONE except the writing
1. **`make ablate` runs**, writing `artifacts/ablation.csv` (light arms: four backtest
   origins, no cost sweep).
2. **CI exists** (`.github/workflows/ci.yml`): tests, a UTF-8 source check, a JS parse
   check, and an end-to-end smoke run asserting every artifact plus the run invariants
   (conservation < 1e-6, no negative stock, projection error < 1e-6).
3. **The M5 benchmark gate is unmeetable on synthetic data** and still needs reframing
   in the thesis rather than quiet omission. The substitute claim is the censoring
   precision/recall that real M5 cannot provide. STILL OPEN — a writing task.

### B. Planner features — DONE
All nine of the spec's keybindings: arrows, Shift-arrow range extension, J/K,
Cmd-D fill-down, Cmd-C/Cmd-V Excel-compatible TSV copy and paste, Cmd-Z/Shift-Cmd-Z
undo-redo across the whole pending set, Enter, Cmd-Enter, Esc (itself undoable), E,
Cmd-K, and `?` for a shortcut sheet. Plus hierarchical grouping with expand/collapse
and live aggregation that includes pending edits, and persisted column widths/order.

### C. Architecture and budgets — DONE
`dqstack/serve.py`: DuckDB analytical store over the Parquet lake with aggregation as
SQL (CON-5), Arrow IPC transfer (CON-1), read-only HTTP API with no inference on the
read path (CON-3). Verify with `python -m dqstack.serve --selftest`.

Measured, not asserted:

| budget | target | measured |
|---|---|---|
| app code, gzipped | < 250 KB | **47.8 KB** |
| grid paint, p95 | < 16 ms | **5.9 ms** (was 40 ms) |
| frames > 32 ms | zero | **zero** |
| sort / regroup | < 150 ms | 33 ms / 118 ms |
| WCAG 2.2 AA contrast | 9 checks | **9 pass** |

Arrow versus JSON on this project's own 28,000-row matrix: **1.44 MB against 3.26 MB
(2.3x) and 1.1 ms against 65.6 ms to parse (61x)**. The spec's claim that "the parse
cost difference is worse than the transfer cost difference" holds, with a factor of 26
between the two ratios.

Still open in C: **DuckDB-WASM in the browser** (the client-side query trick the spec
calls the biggest perceived-performance win), and the UI still reads the static bundle
rather than the API — the API exists and is tested, but nothing consumes it yet.

### D. Correctly out of scope
Five portals, multi-tenancy, approval workflows, connectors, agentic layer. Frame as
features public data cannot exercise.

### E. Writing
Untouched. The spec's own closing warning: the common failure is building impressively
for eleven weeks and writing badly for one.

---

## 7. Known residuals, deliberately accepted

- **Spoilage is an excess-cover approximation.** True FIFO unit-age tracking needs a
  per-series queue and cannot be vectorised.
- **The backtest rolls the evaluation origin, not retraining.** It measures how a
  fixed system behaves as the origin moves. It does **not** measure the value of
  weekly retraining and must not be read as if it did.
- **Eight paired origins is underpowered.** Where direction is consistent but the test
  is not significant, the honest verdict is "consistent direction, underpowered".
- **Lower-tail coverage is arithmetically unreachable** on 43%-zero demand; Stage 6
  declines to chase it and reports which levels it declined and why.

---

## 8. Traps for a future session

- **Never write `len(QUANTILES) // 2`** for the median. Use `config.Q50_INDEX`. There
  is a test that greps the package for the halving idiom.
- **Redirected stdout on Windows is cp1252.** One non-ASCII character in a log line
  raises UnicodeEncodeError and kills a 25-minute run at the moment it reports
  progress. Entry points call `_utf8_stdout()`; keep arm labels ASCII regardless.
- **A `file://` page caches assets indefinitely.** `export.stamp_assets()` stamps every
  local URL in index.html with a content hash. If a fix "isn't taking", check the hash
  changed before debugging the logic.
- **The ARIA mirror is on the render path**, not documentation. A null field there — a
  group header's deliberately-absent target quantile — threw and took down the paint.
- **The micro-fan is the frame budget.** Not the mirror, not getComputedStyle. It is
  cached to offscreen canvases and degraded during scroll; clearing that cache on every
  edit is intentional.
- **`pathlib.write_text` on Windows uses cp1252** and will corrupt em-dashes in source
  files. Pass `encoding="utf-8"` explicitly. One file had to be repaired from git.
- **`ResizeObserver` must be retained** in a variable or it is garbage-collected and
  silently never fires — this left both canvases stuck at their first measured size.
- **Correct PELT pruning is slow if the inner loop is Python.** It must stay
  vectorised: 31 minutes versus 30 seconds for 1,000 series.
- **A run in flight uses the modules loaded at its start.** After changing code, its
  numbers are stale — kill and relaunch rather than reporting them.
- **`ui/data/bundle.js` and `artifacts/` are gitignored** but reproducible from
  `SEED = 20260804`.
