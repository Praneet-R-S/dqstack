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
  export.py        UI data bundle; stamp_assets() content-hashes every asset URL
  serve.py         the read path: DuckDB store, Arrow IPC, HTTP API, static ui/
  data/generate.py synthetic M5-shaped panel generator
tests/             94 tests: leakage, simulator, contracts, diagnose, backtest,
                   sensitivity, serve (the Arrow query table + routes)
ui/                planner workspace. No build step, no bundler, no framework.
  index.html       four views; the only markup
  css/tokens.css   design tokens. Canvas renderers read these at paint time.
  css/app.css      the shell
  css/simple.css   the plain-language layer: review cards, More menu, glossary
  js/util.js       token cache, formatters, esc()
  js/plain.js      every word a non-specialist reads; nothing else invents a phrase
  js/duck.js       DuckDB-WASM + the two-backend query resolver (Duck, Query)
  js/review.js     the Today review flow — the front door
  js/catalog.js    the Products view: browse, one-product detail, add/remove
  js/grid.js       the canvas grid: selection, editing, undo, grouping, micro-fan
  js/chart.js      the canvas fan chart
  js/views.js      pipeline / evidence / decisions sheets
  js/app.js        wiring, state layers, override economics, SQL console
.claude/launch.json  dev-server entry for the preview harness
artifacts/         results tables, ledger, metrics.json  (gitignored)
data/              generated panel                        (gitignored)
```

**Running the workspace.** `python -m dqstack.serve` then <http://127.0.0.1:8000/>.
Opening `ui/index.html` from disk also works, with the SQL engine disabled — see C2.

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
read path (CON-3). It also serves `ui/` statically, which is not a convenience — see
DuckDB-WASM below. Verify with `python -m dqstack.serve --selftest`; run it with
`python -m dqstack.serve` and open <http://127.0.0.1:8000/>.

Measured, not asserted:

| budget | target | measured |
|---|---|---|
| app code, gzipped | < 250 KB | **67.2 KB** (9 files, 222.9 KB raw) |
| grid paint, median | < 16 ms | **4.2 ms** cold, 3.1 ms warm |
| grid paint, p95 | < 16 ms | **11.4 ms** cold, 6.5 ms warm |
| frames > 32 ms | zero | **zero** (worst single frame 15.8 ms) |
| full dataset replace | < 150 ms | 72 ms |
| WCAG 2.2 AA contrast | — | **26 checks, 26 pass** (13 pairs x 2 themes) |
| horizontal overflow, 4 views | zero | **zero** |

Arrow versus JSON on this project's own 28,000-row matrix: **1.44 MB against 3.26 MB
(2.3x) and 1.1 ms against 65.6 ms to parse (61x)**. The spec's claim that "the parse
cost difference is worse than the transfer cost difference" holds, with a factor of 26
between the two ratios.

### C2. DuckDB-WASM in the browser — DONE, and the spec's claim partly refuted

`ui/js/duck.js`. `/series.arrow` emits the 31 scalar series columns as Arrow IPC
(**195 KB**, against 3.7 MB for the full bundle — the fan, 126 days of history and the
weight vectors are render inputs, never query inputs, so they stay in the JS heap).
The browser registers that buffer as a DuckDB table via `insertArrowFromIPCStream`
and answers filtering, sorting and grouping as SQL, with **zero network round trips**
per interaction.

Two mechanical constraints, both of which cost real debugging:

- **`file://` cannot host it at all.** A null origin can neither dynamic-import a
  cross-origin ES module nor spawn its worker. This is why `serve.py` now serves the
  workspace over HTTP.
- **`new Worker(crossOriginUrl)` is forbidden by the HTML spec**, and the CDN is
  cross-origin by definition. The fix is a same-origin blob shim whose only statement
  is `importScripts(cdnUrl)`, which is not subject to the restriction.

**The measurement, which is the interesting part.** The spec calls in-browser SQL
"the single biggest perceived-performance win available to you". Measured on this
project's own table, that is true for one query shape and false for the other.

Row selection (filter, sort, take the head):

| rows | DuckDB-WASM | JavaScript | ratio |
|---|---|---|---|
| 1,000 | 10.9 ms | 0.5 ms | 0.04x |
| 10,000 | 35.0 ms | 5.2 ms | 0.15x |
| 30,000 | 46.2 ms | 11.6 ms | 0.25x |
| 100,000 | 74.5 ms | 79.9 ms | **1.07x — crossover** |
| 300,000 | 93.0 ms | 215.4 ms | 2.32x |

Group rollup (GROUP BY one hierarchy column, six aggregates) **never crosses over**:
0.06x at 1k, 0.10x at 30k, 0.22x at 300k. The reason is cardinality, not engine
quality — a hierarchy level has three to ten distinct keys, and a ten-bucket hash
aggregation over a flat array is about as cheap as computation gets, while every
DuckDB query pays a fixed cost (postMessage to the worker, parse, plan, materialise
an Arrow result back). Below ~10^5 rows that fixed cost *is* the query.

So `Query` routes by the measurement rather than by the claim: `CROSSOVER_ROWS =
100_000` for row selection, JavaScript for the rollups, and DuckDB unconditionally
for the SQL console — where the alternative is not a slower answer but no answer.
**This project's 1,000 series therefore run on the JavaScript path in normal use**,
and the engine pill says so rather than implying otherwise. Shipping the slower path
under a performance label would have been the easy thing to do and would have been
wrong.

What *is* unambiguously won is the third thing the spec does not dwell on: arbitrary
analytical SQL against the planning table, client-side, with no endpoint to write.
That is the **SQL console** (click the engine pill), read-only guarded, with six
worked examples.

**Both backends are held to agreement, not hoped to agree.** `DQ.selftest()` in the
console runs 15 row-selection cases, 5 rollup levels and the reason histogram through
both engines and diffs them: **all pass**. It has already earned its keep — it caught
DuckDB returning `sum(CASE …)` over int32 as a 128-bit integer, which Arrow surfaces
as a four-word object rather than a BigInt, so the filter chips would have rendered
`[object Object]`. `Duck.narrow()` handles both wide-integer forms. `tests/test_serve.py`
(15 tests) pins the Python half: column set, reason encoding, horizon sums, null-freedom
on every sort column, IPC round trip, path-traversal refusal.

**Graceful degradation is verified, not assumed.** Opened from `file://` the workspace
is fully functional — 848 queue rows, filtering, magnitude sort, grouping — and the
pill reads `JS ENGINE` with an actionable reason.

### C3. UI second pass — DONE

The brief was that the workspace looked clumsy and should be something people want to
use. Density was not the problem and was not given up: row height, grid font sizes and
information-per-screen are unchanged. What was missing was craft.

- `tokens.css`: a five-step surface ladder (borders were doing the job of light), a
  two-part elevation model (hairline ring + shadow, which is what actually reads as
  depth on a dark panel), accent tints, one gradient, and a light theme that is a real
  palette rather than an inversion. Every previous token name survives, because the
  canvas renderers resolve tokens through `getComputedStyle` at paint time.
- `app.css`: segmented tab control, a real toggle switch, custom selects, the engine
  pill with a status LED, a raised-and-amber override card (the one control that costs
  money), cards with an accent hairline, refined tables, the SQL console, and a
  responsive clamp so a 1280px laptop does not clip the toolbar.
- All 14 inline `style=` attributes removed from `index.html` except the four legend
  swatches, which are genuinely data-bound to series semantics.

Three real defects found and fixed while verifying:

1. **A theme flip left elements painted in the old theme.** Every property that failed
   was one the element declared a `transition` for — tabs, reason chips, engine pill,
   toggle switch — while `.override`, which declares none, updated correctly.
   `setTheme()` now suppresses transitions across the swap for two frames (one for the
   attribute to be styled, one for it to be painted; a single frame reinstates them too
   early and the stale paint returns). This also removes an unwanted 120 ms cross-fade
   of every surface in the app.
2. **The dock starved the grid.** `grid-template-rows: auto 1fr var(--dock-h)` gave the
   toolbar and dock 394 px unconditionally, so on a 450 px-tall window the grid region
   resolved to **four pixels** — a fully populated canvas painting into a 912x0 box.
   Now `auto minmax(120px, 1fr) minmax(0, var(--dock-h))`: the grid keeps a floor and
   the dock yields height first.
3. **9 px of horizontal overflow in the Evidence view.** A grid item defaults to
   `min-width: auto` and refuses to shrink below its content, so one wide table pushed
   the sheet past its track. `min-width: 0` on `.sheet`, `.two-col > *`, `.cards > *`,
   `.stage > *`.

### C4. Usability rebuild — DONE

The brief, given twice: the front end looked complex, a normal user could not
understand it, every feature should be reachable in three clicks, and it should
look good. The previous pass had improved the *craft* of an expert instrument
without changing the fact that it was an expert instrument. This pass changed what
the front door is.

**The diagnosis.** Three things made it unusable by a non-specialist, and none of
them was density.

1. It opened on a spreadsheet of 848 flagged series with a canvas grid, a fan
   chart, an attribution panel and an override form all live at once. "848 items
   need attention" is not a to-do list, it is a reason to close the tab.
2. It spoke the model's vocabulary — `relWidth`, `targetQ`, `orderUpTo`, WSPL,
   conformal, intermittent — none of which a buyer can act on.
3. Nothing told you what to do. Every control was equally weighted.

**What replaced it.**

`ui/js/review.js` — a card-based review flow, now the landing view. One question
per card, one obvious answer: **Approve / Change / Skip**, plus *Why this number?*
Each card carries an inline SVG history-and-fan sparkline and four figures
(forecast, versus last week, stock-up-to, data quality). Exactly one filled button
is on screen per card, which is what makes it answerable in one click.

`ui/js/plain.js` — the single source of every word a non-specialist reads. Class
labels ("Sells in bursts"), the seven reason codes as sentences with a *what to do*
line, data-quality and confidence wording, service-level rationale, and a
pattern-based translator turning model feature names into English
(`rmean_180` to "Average sales over the last 180 days"). Every plain phrase keeps
its technical term reachable on hover, so an analyst reading over a planner's
shoulder is not lied to.

`ui/css/simple.css` — light theme by default (a dense dark terminal is the single
strongest signal that a tool is for specialists), generous card layout, one accent,
one filled button, colour meaning exactly one thing.

Navigation went from four architectural names to four plain ones — **Today · All
products · How it's doing · How it works** — with everything an expert wants and a
newcomer does not behind one **More** menu (glossary, ordering decisions, theme,
row height, keyboard sheet, SQL console).

**Navigation is a sidebar**, not top tabs plus a More menu. Six destinations and
three tools do not fit in a horizontal bar, and the menu put everything two clicks
deep. The sidebar names all of them at once: *Your work* (Today, with a live badge;
All products), *Understand it* (How it's doing, Ordering decisions, How it works),
*Tools* (glossary, SQL console with an engine-status dot, keyboard shortcuts), and a
footer carrying theme, row height and the forecast date. It collapses to a 60px icon
rail — remembered in `localStorage`, handing 188px back to the grid — and becomes an
overlay below 1000px, where the workspace also drops its exception worklist (Today
is the same queue, better presented).

Sidebar nav buttons deliberately keep `class="tab"` and `data-view`, and tool
buttons keep `data-menu`, so the existing view switcher and menu dispatcher work
unchanged. The sidebar is a re-presentation of controls that already existed, not a
second set of them.

**Three clicks, verified in the browser rather than asserted:**

| task | clicks |
|---|---|
| See what needs attention | 0 (landing) |
| Approve a recommendation | **1** |
| Understand why a number is what it is | **1** |
| Skip an item | **1** |
| Open the full products table | **1** |
| Return to today's list | **1** |
| Change a quantity | **2** + typing |
| Any of the five destinations, from anywhere | **1** |
| Glossary / shortcuts / theme / row height / collapse | **1** |
| Run an analytical SQL query | **2** |

Jargon audit on the Today view: **zero** of sixteen model terms appear, expanded
or collapsed.

**Four real defects found while verifying, all fixed:**

1. **The headline relabelled the backlog.** It read "489 decisions need you today"
   because it counted the whole urgent tier rather than the list on screen. Now it
   reports the cohort, with the remainder honestly described as behind these.
2. **The progress denominator grew as you worked.** The cohort was re-sliced on
   every render, so clearing an item pulled the next in from a queue of 489 and the
   readout went "1 of 25", "2 of 26" — a finish line that moves away is worse than
   no progress bar. `pickCohort()` now fixes today's list at build time, and
   clearing it reaches "24 of 24 done" and empty.
3. **Every card said "confidence low".** The hardcoded cutoffs (0.55 / 1.1) were
   guesses; the real `relWidth` distribution runs 0.30 to 1214 with terciles at
   1.22 and 2.61. `Plain.calibrate()` now derives the words from the assortment's
   own terciles, which also makes them comparative — "wider than two thirds of your
   range". Result: confidence spreads 3 high / 13 moderate / 8 low, quality 8 / 5 /
   9 / 2 across the 24 visible cards.
4. **Collapsing the sidebar did nothing.** `transition: grid-template-columns` is
   not reliably interpolable, and animating the track the canvas measures itself
   against means the canvas resizes against a moving target. Removed; the width
   simply changes.
5. **The grid collapsed to zero height again.** Adding the orientation hint gave
   `.main` a fourth child against three template rows, so the grid region fell into
   the dock's row and the dock into an implicit fourth. Second occurrence of this
   exact bug class in the project; a row is now declared for it.

**Also corrected: a false alarm I raised and then disproved.** 427 of 1000 series
have `orderUpTo` below the sum of their nine daily median forecasts, which looked
like a Stage 7 bug. It is not. The order-up-to level is a Negative Binomial
quantile fitted to the *moments of the nine-day total*, and the median of a sum is
not the sum of the medians for a skewed count distribution. The empirical check
settles it: achieved fill rate is **above** the target quantile in all four target
bands and rises monotonically with it (94.2% unit-weighted against a 71.1% mean
cycle-service target). What was real was the *display*: showing a 7-day median
beside a 9-day order-up-to level invited exactly the wrong inference, so the card
now states the window and the service level explicitly, and the glossary explains
that the two are not comparable.

Code size after all of this: **88.0 KB gzipped** against the 250 KB budget.
The `file://` path still works with no server and no query engine.

### C5. Products view, search repair, and range editing — DONE

Three complaints, one root cause: there was no screen whose job was *one product*.

**Search was broken twice over.** `jumpTo()` set the spreadsheet's scope and
selected a row in it — while the visible view was Today. Nothing appeared to
happen because nothing did: the selection landed in a hidden grid. And the palette
matched only raw identifiers, so a planner who read "Food 1-006" off a card and
typed it got zero results, because `FOODS_1_001` does not contain "food 1-0". The
displayed name is now the primary search key, terms match independently
("food ca-4" works), a miss says so instead of showing an empty box, and a result
opens the product in the new Products view.

**`ui/js/catalog.js` — the Products view.** List beside detail: search, category
chips, a needs-attention filter, 200 rows at a time. The detail pane gives one
product a full-width SVG chart with axis labels, four figures, its flags in plain
words, an inline forecast edit sharing Grid's draft set, and the model's own
drivers. This is where search results land and where the range is changed.

**Add and remove products** — previously absent entirely. Both are session-local
drafts with a pending bar, like forecast overrides, because there is no write path
to a system of record. Removal shows what the product currently earns before you
confirm and is undoable.

Adding was the interesting one. Two numbers a new product needs behave completely
differently, and conflating them was the first version's bug:

- **The service level *is* computable now.** It falls out of the price, cost and
  shelf life the planner just typed. `Catalog.fractile()` mirrors
  `critical_fractile()` in decide.py, including the spoilage term being excess
  cover beyond shelf life rather than a flat fraction — otherwise the bug fixed in
  Stage 7 would be reintroduced in the browser. The constants come from
  `D.run.holdingRate / markdownRisk / minQuantile / maxQuantile`, newly exported by
  `export.py`, **not** from JS literals that would drift from config.py. The dialog
  previews it live as you type, which turns out to demonstrate the thesis better
  than any chart does:

  | product | margin | shelf | target |
  |---|---|---|---|
  | fresh, thin margin (milk) | 17% | 5 d | **50%** (floored) |
  | fresh, fat margin | 67% | 5 d | **78%** |
  | durable, fat margin | 70% | 730 d | **93%** |
  | durable, thin margin | 8% | 730 d | **50%** |

- **The stock level is *not* computable now**, because an order-up-to level is a
  predictive quantile of demand over the risk period and a new product has no
  predictive distribution. The first version inherited the prototype's
  `orderUpTo`, which had been computed at the *prototype's* target quantile — so
  the card showed a stock level that contradicted the service level beside it.
  It is now `null`, rendered as "Not yet — needs a forecast first, aiming at 50%
  service". A missing number that explains itself beats a confident wrong one.

Everything else a new product shows is an estimate from the median of comparable
products in the same category and store, labelled as one. That is in substance what
the cold-start model does; inventing a forecast in the browser would violate CON-3
and be dishonest besides.

**The spreadsheet is no longer the default.** "All products" was the second nav
item and the only way to browse, which is why it read as too complicated — it is a
bulk-editing instrument, thirty columns and keyboard-driven, and a poor answer to
"show me that product". It moved to **Tools → Spreadsheet**, and its header now
says what it is for and links to Products for lookups.

Navigation is now: *Your work* — Today, Products · *Understand it* — How it's
doing, Ordering decisions, How it works · *Tools* — glossary, query the data,
spreadsheet, shortcuts. Six destinations, all one click.

App code 102.9 KB gzipped against the 250 KB budget; 94 tests pass; zero
horizontal overflow across all six views.

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
- **Search must match what is on the screen**, not the internal identifier. The
  bundle's item codes (`FOODS_1_001`) are never shown to a user.
- **Never copy a derived number from a prototype product.** `orderUpTo` is a
  quantile at the *source* product's target level; reusing it contradicts any newly
  derived service level. Withhold it instead.
- **Fractile constants live in `D.run`**, exported from `CFG.decision`. Do not
  re-type them into JavaScript.
- **A progress denominator must not be recomputed from a live queue.** If the list
  refills as you clear it, "1 of 25" becomes "2 of 26" and the goal recedes. Fix
  the cohort once.
- **Never hardcode thresholds for words like "low" or "poor".** Check the actual
  distribution first; the guessed cutoffs put 24 of 24 cards in one bucket.
- **`orderUpTo` covers review period + lead time (9 days) and is a quantile of the
  *total*, not a sum of daily quantiles.** It is not comparable to a weekly median
  and will look wrong beside one. Verified correct by achieved fill rate.
- **Adding a child to `.main` or `#app` without adding a template row collapses a
  region to zero height.** This has now happened twice.
- **`new Worker(crossOriginUrl)` is illegal**, and `file://` cannot load DuckDB-WASM
  at all. Both are spec behaviour, not bugs to fix. See `duck.js`.
- **A CSS `transition` on a property fed by a custom property can keep painting the
  old value** after the token changes. If a theme flip leaves something stale, look
  for a transition on exactly the property that is stuck, not at the token.
- **`min-width: auto` is a grid item's default** and is the cause of essentially every
  unexplained horizontal overflow in a CSS grid layout.
- **Do not measure computed styles right after changing the theme**, and never read a
  live `CSSStyleDeclaration` after restoring it — both produced convincing false
  positives in this session before the CSSOM dump settled it.
- **DuckDB widens aggregates.** `count()` and `sum()` over BIGINT arrive as BigInt;
  `sum()` over int32 is promoted to HUGEINT and arrives as a four-word little-endian
  array. Neither is a JS number. `Duck.narrow()` exists for this.
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
