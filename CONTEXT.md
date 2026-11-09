# DQ-Stack — project context and progress

**Purpose of this file.** A durable handover so work can resume without the
conversation history. It records what the project is, what has been built, every
defect found and why it mattered, what is left, and the decisions that are settled
so they are not relitigated. Update it when something material changes.

Last updated: after commit `644af67` (six verified findings fixed).

---

## 0. Start here

The pipeline and its evidence are finished and committed. **All current work is
interface.** Two external design reviews have been received; v2 supersedes v1 and
corrects it. **C11** records the review's own eight blocks (all built). **C12** records
the eight features built after them (`truth.js`, `curve.js`). **C13** is the inventory
layer — stock counts and the order list (`stock.js`, `orders.js`). **C14** adds cycle
counting and brings the reference PDF current. **C15** is the usability pass over the
whole application — orientation, one vocabulary, and the framing of the dense surfaces.
**C16** is the purple brand, Charts, the Overview hub and the honest model report.
Read the trap lists in C12 through C16 before touching charts, contrast,
measurement harnesses, wording or the sidebar.

    python -m dqstack.serve          # then http://127.0.0.1:8000/
    python -m pytest -q              # 161 tests
    python docs/build_reference_pdf.py   # regenerate the 20-page reference

Verification habits that have repeatedly paid for themselves:

- `pytest tests/test_markup.py` after **any** edit to `index.html`. Tag balance,
  layout landmarks, and every `$("id")` in app.js resolving. Two commits in a row ate
  structural `</div>`s and browsers auto-closed them, so nothing complained.
- `pytest tests/test_vocabulary.py` after any copy change on a Work surface.
- The contrast harness after any colour change; the overflow sweep across
  6 views x 2 themes x 2 rail states after any layout change.
- Measure in the browser rather than assuming. Every visual claim in this file that
  says "measured" was, and several assumptions turned out wrong when checked.

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

### C6. Self-audit of the front end, and what it turned up — DONE

Asked to find the design flaws myself. Six real ones, found by probing the running
application rather than by reading the code.

1. **The primary surface had no keyboard flow.** The spreadsheet had nine
   keybindings; the Today list, which is what a planner works every morning, had
   zero — twenty-four decisions meant twenty-four trips to the mouse. Added
   **J/K** to move, **A** or **Enter** to approve, **C** to change, **S** to skip,
   **W** for why, **O** to open the product. Letters chosen to be sayable rather
   than to mirror spreadsheet convention, because this is not a spreadsheet. The
   focus ring only paints after a key is actually pressed, since a ring sitting on
   the first card before anyone touched the keyboard reads as a stuck state. Keys
   are inert while a modal is open or a field has focus.

2. **The review flow had no terminal action.** You could clear the whole list and
   the only trace was a tab labelled "Reviewed" — the spreadsheet had a Commit
   button and the front door did not. Added a sticky summary bar: what you decided,
   what it is worth (quoting `totalPendingImpact`, the *same* estimator the
   spreadsheet uses, so the two can never disagree), and the two things you might
   do with it.

3. **Decisions could not leave the browser.** Added CSV export of the whole
   session — product, store, why it was flagged, action, model forecast, your
   value, the delta, money at risk, both dates — with range additions and removals
   in the same file. UTF-8 BOM so Excel on Windows does not mangle it, and a Blob
   download so it works from `file://` too.

4. **No route from a decision card to the product's own page.** Added a *Full
   details* button and the **O** key.

5. **Products had no store filter and no sort.** Ten stores, a thousand products,
   alphabetical only — "the worst thing in my store" meant reading a thousand rows.
   Added a store filter and five orderings, and the trailing figure on each row now
   changes to match the sort, because sorting by "most at risk" while displaying
   unit forecasts looks like a broken sort.

6. **A brand-new product reported "Data quality: Poor".** Wrong: there is no
   history to judge, which is a different statement from judging it badly. Now
   "Not yet known", with the sub-line reporting how many comparable products the
   estimate came from instead of a confidence level inherited from a prototype.

Also fixed: a dead `.rc-why::first-line` rule (the line break depends on the
viewport, so it coloured an arbitrary number of words) replaced by an explicit
element; and the product list was `display: none` below 1000px, leaving no way to
browse at all on a narrow window — it is now a stacked panel above the detail.

**Added because it is genuinely useful, not to fill space:** *the same product in
other stores* on the product page — a peer bar chart of next week's forecast across
every store carrying the item, each one clickable. It is the fastest sanity check
on an odd forecast ("is it just this store?") and, for a cold-start product, it is
the evidence the estimate was built from.

**Two bugs found in my own new code while verifying it:**

- **Committing undid the review.** `onCommit` cleared the reviewed map, so all
  twenty-four cards reappeared the moment you finished them. Committed items are
  now tracked separately: they stay reviewed, the summary counts only what is
  still pending, and the Reviewed tab marks them committed and withdraws Undo.
- **The commit toast claimed a ledger write that does not happen.** It said
  "recorded in the override ledger"; there is no write path to a system of record.
  It now says to export the CSV, which is the truth and also the useful next step.

App code 108.6 KB gzipped against the 250 KB budget; 94 tests pass; zero
horizontal overflow across all six views.

### C7. Visual identity: the fan — DONE

Brief: make the aesthetics better, with an identity related to demand and supply.
Run as a six-lens parallel audit of the CSS, markup and render code (identity,
colour, typography, spacing, surface, data-viz). Four lenses reported; the colour
and surface agents stalled and were stopped, so their ground was covered by hand —
the contrast table below is measured, not delegated.

**The motif is the uncertainty fan, and it was chosen because it is already the
product's own geometry** rather than a metaphor applied to it: an origin rule with a
cone opening to the right of it is what `chart.js`, `grid.js`, `review.js` and
`catalog.js` all already draw. Rejected alternatives: shelf strata, the
underage/overage balance, the weekly demand rhythm.

- **The mark** is that geometry at 16px — a vertical rule plus two nested wedges
  (the 50% and 98% bands). It replaces a glowing gradient dot that sat eight pixels
  from the engine-status LED and was visually indistinguishable from it. It survives
  the sidebar collapsing to a rail, which is what keeps the icon rail feeling like
  the product.
- **A favicon** at last, same geometry, one hue at two values — a favicon cannot
  read custom properties or know the tab strip's theme, so ink-plus-accent would
  lose its rule on a dark strip.
- **The film grain is deleted, not replaced.** It was declared "the only decorative
  element in the interface", said nothing about the domain, and at 0.020 opacity was
  invisible on the panels this is actually used on. The signature slot now holds
  `--cone-wash`: a conic-gradient wedge sharing the mark's origin-and-widening
  geometry at a sixth of its alpha, on the scrolling surfaces only.
- **The fan itself is now one object with one rendering.** The two inline-SVG
  surfaces drew a single flat p10–p90 slab while `chart.js` drew three nested bands;
  they now all draw the same three levels at the same alphas. Each band is filled
  from a **shared gradient that fades towards the horizon** — not decoration: the
  further into the future, the less the model knows. Verified in-browser that CSS
  custom properties inside `<stop>` re-resolve on theme change and that cross-SVG
  `url(#id)` references paint, so one hidden `<defs>` serves every chart in both
  themes.

**Blue meant two things.** `--series-forecast` was simultaneously "this is a
predicted quantity" (read by both canvases) and the colour of every link, focus
ring, selected row and primary button. A hue that means two unrelated things cannot
be learned, so `--ui-accent` was added — a new name with a byte-identical value, so
no pixel moved — and 38 interaction surfaces were repointed to it by hand, selector
by selector, leaving every data surface on `--series-forecast`. This is the
precondition for colour meaning anything domain-wise, not a substitute for it.

**Three real rendering bugs the audit found:**

1. **Sparkline strokes were distorted ~2×.** `preserveAspectRatio="none"` stretches
   a 300-unit viewBox across a ~600px card, so every stroke painted twice as thick
   horizontally as vertically — which is why the line read as sketchy.
   `vector-effect: non-scaling-stroke`.
2. **Chart axis labels failed WCAG AA in both themes.** `--text-tertiary` measures
   3.87:1 dark / 3.86:1 light on the surfaces the charts sit on — the numbers that
   give every chart its scale were the lowest-contrast text in the product. Moved to
   `--text-secondary` (7.80:1 / 5.66:1). The token itself was *not* revalued: it is
   read by name in dozens of non-chart places and by both canvases.
3. **The grid micro-fan double-encoded uncertainty, backwards.** Band opacity was
   multiplied by confidence on top of band *height* already encoding spread, so the
   widest intervals rendered both taller and paler — the riskiest rows were the
   hardest to see in a grid scanned fifty rows at a time.

**Typography** gained a figure scale (`--fig-xl/lg/md` with paired tracking): seven
hero numerals had used six sizes and five tracking values, so no two panels agreed
on how big "the answer" is. `--fs-24` closes the 20→28 hole that four literals were
filling. Words no longer render in the numeral face — `.rc-fig-word` splits "Good" /
"Fair" out of JetBrains Mono, which exists here for digit alignment a word cannot
use. The serif went from one orphaned selector to carrying every section heading on
the three reference views, making the document-versus-instrument split real.

**Spacing**: one `--gutter` (the left content edge used to move 24→32→48px on every
navigation, and `.sheet` had no `margin-inline`, so on a wide screen the reference
views sat hard left while Today was centred); one `--rail-w` for both list-beside-
detail views; stacked-list gaps raised above their own card padding, because when
the space between elevated surfaces is smaller than the space inside them they fuse
into a ribbon; and `.rc` became a grid with a single gap, replacing four
margin-bottoms and a negative-margin hack that existed to compensate for them.

**The fan was the most cramped element on the card** — 63 data points in 68px, boxed
in a bordered well, reading as a thumbnail beside the text rather than as the
subject. It now bleeds to the card edges at 104px.

Also: prior-version ghost and a 1/2/5 tick ladder on the product chart (a pure
power-of-ten ladder sent a peak of 210 to a top of 300, wasting a third of the
plot); the last-actual dot, which the whole chart is a claim about and which was
invisible; table headers out of the numeral face; and measures for two prose blocks
that had none and ran ~200 characters per line.

**Measured: 34 contrast checks across both themes, 0 failures. 94 tests pass. Zero
horizontal overflow across all six views in both themes. 114.5 KB gzipped against
the 250 KB budget.**

### C8. Interaction states, the peer chart, and three layout regressions — DONE

Continuation of the visual pass, covering the ground the two stalled audit lenses
(colour and surface/states) would have taken. Probed the running application rather
than reading the CSS.

- **Sixteen of seventeen interactive controls had a hover rule and no `:active`
  rule** — only `.btn` acknowledged a press. On a list a planner presses twenty-four
  times in a row, that is the whole difference between responsive and laggy. All
  seventeen now travel 1px on press: physical, cannot collide with a semantic hue,
  invisible unless you are pressing. Controls carrying a persistent selected state
  are exempt so the press does not fight the selection, and the whole set is
  disabled under `prefers-reduced-motion`.
- **The peer chart was the only surface in the application that dropped uncertainty
  entirely**, while asking the reader to judge whether a store is an outlier — which
  a bare median bar cannot support. Each row is now a p10–p90 range with a median
  notch and a faint cohort-median upright behind it, so "sits clear of its peers"
  and "merely overlaps them" are finally distinguishable. Everything needed was
  already on each series.
- **The `◎` dingbat empty state** became the cone watermark, which carries the right
  semantic for "nothing selected yet": a future not yet resolved.
- **Drift finished off**: five `border-radius: 4px` to `--r-sm`, nine hardcoded
  uppercase tracking values to `--tracking-caps`, three dialog bodies to one frame.
- **The grid hint is dismissible** and remembered, handing 71px back to the grid —
  87px of permanent onboarding was sitting on the densest, most keyboard-driven
  surface in the product, read once and then furniture forever.

**Three layout regressions found and fixed, two of them mine from the previous
commit:**

1. **`margin-inline: auto` broke the sheets.** Adding it to `.sheet` turned a
   stretched grid item into a shrink-to-fit one, so it sized to its widest table
   (1150px) inside a 1032px track and pushed **118px of horizontal overflow** onto
   the Evidence view. `width: 100%` alongside the auto margins restores stretch and
   lets the centring bite only once `max-width` binds. `.rv` was unaffected because
   it already declared a width.
2. **`display: none` on the grid hint collapsed the grid to zero height.** It
   removes the item from grid placement, so the remaining children shifted up a row
   into the hint's `auto` track — **the third occurrence of this exact bug class in
   this project**. The template is now restated for the dismissed case.
3. **The workspace overflowed at laptop widths.** `.main` and its children lacked
   `min-width: 0`, so the canvas's stale pixel width held the track open; and
   underneath that, `.crumbs-right` is ~690px of controls that `nowrap` turned into
   view overflow rather than a second line. Both fixed — the toolbar now wraps,
   which degrades instead of breaking.

**Verified: zero horizontal overflow across six views × two themes × two sidebar
states, at both 1100px and 1512px. 94 tests pass. 117.0 KB gzipped.**

### C9. External design review, first batch — DONE

An outside review (`DQ-Stack_Interface_Redesign_Report.md`) diagnosed one structural
fault with five consequences. Two of its recommendations rested on factual claims, so
those were verified in-browser before acting.

**Verified first, not assumed:**

- *Instrument Sans exposes tabular figures.* True. With
  `font-variant-numeric: tabular-nums lining-nums` the width spread across digit
  combinations is **exactly 0px**, against 83px unset — and it renders at *precisely*
  JetBrains Mono's width, so the swap changes no layout and loses no alignment.
- *The proposed violet is safe.* Partly. `#7A5CF0` measures 4.30 / 4.06 / **3.72**:1
  on dark base / surface / raised. That clears 3:1 for UI components but **fails
  4.5:1 for body-size text**, so the review's "stronger option" — de-hue links, keep
  violet for the focus ring and primary action — is not merely the more disciplined
  choice, it is the only compliant one. Adopted on that basis.

**Implemented:**

1. **The structural fault.** Nine sidebar entries serving four audiences became
   **four**: Work (Today, Products, Spreadsheet) and one **Evidence** destination
   holding the three reference sheets plus the SQL console and glossary as tabs.
   `state.lastEvidence` remembers the tab, and the chrome title reads
   "Evidence · Accuracy". Nothing was deleted — the evidence views are the proof the
   argument holds — but they left the planner's daily field of view.
2. **The cycle ribbon**, which the review correctly called the largest omission: not
   a feature but a frame. No cutoff exists in this deployment, so it reports the
   vintage and horizon — "Week 32 · forecasts generated 6 Aug 19:53 · covering 28
   days · 489 flagged · 1 decided" — and lights up `closing` / `closed` automatically
   if `D.run.cutoff` is ever set.
3. **The accent split finished.** `--ui-accent` moved to violet (`#7A5CF0` /
   `#5B3FD4`); the primary-action gradient is darker still because white label text
   needs 4.5:1 and the lighter violet gives only 4.55 with no margin. Links and text
   buttons now carry **no hue** — ink plus underline — which is both the disciplined
   position and the fix for the 3.72:1 problem.
4. **Hero figures left the numeral face.** `--fig-xl` and `--fig-lg` are Instrument
   Sans with tabular figures; `--fig-md` stays monospace because it does sit in a
   column. Two new tokens name the distinction: `--face-fig-hero`, `--face-fig-col`.
5. **`tests/test_vocabulary.py`** — the review's acceptance test 3, made executable.
   Scans the three planner-facing files for 25 method terms, with `term:` fields,
   `title` attributes and comments exempt because those are the permitted disclosure
   routes. It found **one genuine leak** ("a forecast to take a quantile of" in the
   new-product banner, now rephrased) and one bug in itself — the stripper missed
   trailing comments. It also carries a guard that plants a known-bad string, so
   over-stripping the checker cannot masquerade as passing.

**One defect of my own, caught immediately:** rewriting the sidebar footer dropped
`#originLabel`, which `boot()` dereferenced — the throw aborted all wiring, so the
ribbon was blank and Evidence did not switch. The element was genuinely redundant
once the ribbon reported the vintage, so the reference was removed rather than the
element restored, and a check now asserts every `$("id")` in app.js resolves.

**Measured: 38 contrast checks across both themes including the new violet and the
button gradient, 0 failures. 99 tests pass. Zero horizontal overflow across 6 views
x 2 themes. 119.9 KB gzipped.**

**Deliberately deferred from the review**, in its own priority order: card retiering
into a three-tier layout with a rung-3 drawer; the cost-curve glyph (its best idea —
worth doing as inline SVG rather than the proposed new canvas renderer, which
side-steps the frame-budget risk entirely); override reason codes and the
plausibility guardrail; `Cmd-Z` undo and the decision log; batch grouping; watchlist;
track-record panel; first-run walkthrough; command-palette actions; density modes;
icon set; mobile approvals.

### C10. The sidebar as a status board — DONE

Brief: add more features to the sidebar and make it look full. There is a real tension
here, because the external review had just diagnosed a flat nine-item rail serving four
audiences as *the* structural fault, and that fix had shipped one commit earlier.

Resolved by filling the rail with **live state and actions rather than destinations**.
A persistent rail is good at exactly one thing — telling you continuously where things
stand — and it was doing none of it. Navigation stays at four entries. Three of the
additions are items the review itself deferred.

| Block | What it carries |
|---|---|
| Scope | A store selector honoured by **both** the queue and the product list |
| Today | Live badge, plus the four queue tiers with their own counts, each one click |
| Products | Watchlist entry with a count |
| Spreadsheet | Amber badge counting uncommitted drafts |
| This session | Decisions made, expected margin effect, Export and Commit |
| Evidence | One entry, with the query-engine dot |
| Footer | Feed condition, theme, row height, shortcuts |

- **Scope** filters inside `Review.build()`, not at render, because the cohort has to
  be drawn from the scoped set — filtering later would leave the progress denominator
  counting items the planner cannot see. Setting `CA_3` takes the queue from 848 to 83.
- **Watchlist** is persisted to `localStorage`, and a watched product enters the urgent
  tier **whether or not it was flagged** — that is the entire point of pinning, and the
  card marks it *Watched* so the reason it is there is never mysterious.
- **Feed condition** reads the Stage-0 `feedRuns` records the pipeline already writes:
  rejected outcomes, then staleness, then check warnings, then "All 6 feeds current".
  Silence is the healthy state; it only raises its voice when something is late.
- Everything repaints from one `paintSidebar()` wired into the review hooks and
  `Grid.onEdits`, so a decision moves the tier counts, the session line, the ribbon and
  the commit affordance together.

The rail now fills its height exactly — 428px of content in 428px — with seven live
numbers on it, and collapses cleanly to the 60px icon rail.

**A latent defect this uncovered, which had already shipped.** The previous commit's
footer replacement had consumed the `</div>` that closed `.sb-foot`. Browsers
auto-closed it and the layout still rendered, so nothing complained. This commit's edit
then searched for "the next `</div>`" and, finding none nearby, swallowed the sidebar
close, the entire chrome opening **and the cycle ribbon**. The symptom was a 9,931px
footer containing 372 buttons.

`tests/test_markup.py` now closes that class off for good: tag balance over
`index.html`, presence of the six layout landmarks, every `$("id")` in `app.js`
resolving to a real element, and a control-character scan over all nine UI scripts
(heredoc round-trips have twice written a literal backspace or a raw BOM into a source
file, where it is invisible in a diff).

**Measured: 111 tests pass. Zero horizontal overflow across 3 views x 2 themes x 2 rail
states, plus Evidence. 124.1 KB gzipped.**

### C11. External design review v2 — PHASE 1 DONE

`DQ-Stack_Interface_Redesign_v2.md` (in the user's Downloads; **copied to
`docs/reviews/` so it survives**) supersedes v1 and **corrects its own central
recommendation**. Read that correction before doing anything else in this area:

> v1 said cut the sidebar from nine items to four. That was right about the problem
> and wrong about the cure. The fault was never that the sidebar had too many things
> in it — it was that the things in it belonged to the **application** rather than to
> the **planner**. So the rule is not *fewer items*. It is: **the sidebar holds the
> planner's work, not the application's features.**

Under that rule the rail gets **denser** (eight blocks) while *navigation* shrinks to
three destinations. Every block carries a live number, which is what separates an
instrument from a menu.

**v2's eight blocks — Phase 1 is complete:**

| # | Block | State |
|---|---|---|
| 1 | Lockup + cycle chip | **DONE** — chip reads `07:53 PM`, states open/closing/closed |
| 2 | Cycle bar — week + 3px progress | **DONE** — "Week 32 / 28-day horizon", bar at 23.9% |
| 3 | Navigation: 3 destinations + badges | **DONE** |
| 4 | **Queue by cause**, count **and exposure** | **DONE** — 6 rows, ordered by exposure |
| 5 | Watching — pinned items with movement | **DONE** |
| 6 | Saved views | not started (v2 puts it in Phase 4) |
| 7 | Scope — store switcher | **DONE** |
| 8 | Session — *n* of *N*, covered, **Undo last** | **DONE** — also `Cmd-Z` anywhere in Today |
| — | Footer: condition, **Evidence**, theme, density, shortcuts | **DONE** |

The chrome ribbon was **removed**: blocks 1 and 2 replace it, so "where are we in the
week" has one home rather than two.

Geometry is v2's own: **288px** expanded, **64px** collapsed, 30px rows, 10px/0.06em
section labels, 5px dots, 11px mono badges. The rail fits exactly at 1512x900 — rows
of 60 / 43 / 500 / 89 / 200 — with **15 live numbers** on it and no scrolling.

**The axis change, which was the point.** C10 grouped by severity; v2 groups by
**cause with exposure**, and the result speaks for itself: *Running out 23 ($16k)*,
*Moved a lot 19 ($1.3k)*, *Hard to predict 22 ($416)*. A severity tier could not say
that 23 stock-outs are worth forty times the 22 wide intervals.

**Undo is in**, which closes the largest safety gap: `undoLast()` walks Map insertion
order and **refuses to undo a committed decision**, because the planner was told it
was final.

**Four defects while doing this, all mine, all caught by the checks:**

1. `test_markup.py` caught the sidebar rewrite leaving seven stale `$("id")` refs.
2. Removing the ribbon CSS block took `.ev-tabs` and `.sb-rule` with it as collateral;
   restored from `git show HEAD:`.
3. Replacing the board module deleted `paintHealth`, whose throw aborted every later
   paint — the cycle, scope and watch blocks all silently stayed empty.
4. The `Cmd-Z` handler borrowed `mod` from a *different* keydown listener's scope.

Each was invisible in the diff and obvious in the browser. Measure, do not assume.

**v2 items beyond Phase 1, in its own priority order:** card retiering into four tiers
with the rung-3 drawer; **the cost curve** (its headline idea — 112×46 glyph, and note
it is better as inline SVG than v2's proposed new canvas renderer, which removes the
frame-budget risk entirely since the card already renders its sparkline that way);
override reason codes + plausibility guardrail + impact; batch grouping; density modes;
card transitions; saved views; alert rules; notes; history timeline; command-palette
actions; since-you-were-last-here; mobile approvals.

**Already satisfied from v2, do not redo:** the accent split (violet, C9), hero figures
in Instrument Sans with tabular figures (C9), the vocabulary CI check (C9), the
watchlist and persistent draft count and ambient feed condition (C10).

### C12. Eight features beyond both reviews — DONE

Asked "what else can we add", answered with a prioritised list, and told "add them
all". All eight shipped, each with its own commit. Two modules are new: `ui/js/truth.js`
and `ui/js/curve.js`.

**1. The excluded-day layer (`truth.js`). This was a bug fix, not a feature.**
`hist[i]` is `null` where the pipeline set a day aside, and both planner-facing charts
called `y(v)` on it — and `y(null)` is arithmetically `y(0)`. Every excluded day was
drawn as a day of zero demand, on 16,120 days across 579 of 1,000 products. Food 1-001
at CA_4 has 96 days it was not stocked and its chart showed a flat line along the floor.
`chart.js`, the expert surface, had always handled this correctly; the two surfaces built
for people who cannot read the pipeline were the dishonest ones.

Two exclusions are distinguished because they ask different things of the reader:
`gap` (not carried, `carried[i] === false`, 14,069 days) takes a neutral wash because
absence of evidence invites no investigation; `suspect` (carried, till recorded zero,
2,051 days) takes the amber of the flag that reports it. `chart.js` drew the latter in
`--series-override` — the colour that means "a planner edited this" — and now uses
`--state-attention` with the others.

Also fixed here: 57 isolated known days between two excluded ones drew as nothing (a
one-point subpath paints nothing); the fan hinged on `hist[last]` even when that day was
excluded; and **every circle on the sparkline was an ellipse** because
`preserveAspectRatio="none"` stretches it 3.06x. Round-cap zero-length subpaths with
`non-scaling-stroke` are immune — verified by hit-testing the live stroke, symmetric at
r=2 and absent at r=5 in every direction. `dot()` in review.js is the helper.

**2. Track record and blend.** `s.ledger` (per-series realised outcome over the
`evalOrigin`→`origin` window) and `s.weights` were exported from the first run and never
shown. Both now appear in the card drawer and on Products, from one implementation.
Labelled "tested" throughout — it is a backtest, and `EVAL_DAYS` is read from the run,
not written as a literal.

**3. The cost curve (`curve.js`) — the review's headline idea.** The load-bearing
property is that the drawn minimum IS the recommended level; a low point sitting beside
the number above it would discredit both, silently. So it is constructed, not fitted:
`dC/dQ` vanishes at `F(Q) = ` the critical fractile, so the demand distribution is
rebuilt from two things the bundle states exactly — the mean of the cover window and
`F(orderUpTo) = targetQ`. Two constraints determine a lognormal (feasible for 809/1000);
the other 188 drop the mean constraint and keep the quantile one, which is the one that
places the minimum.

**Cost weights come from `targetQ`, never from raw `underage`/`overage`.** `targetQ` is
the fractile the recommendation was actually computed at, post-clamp, and the exported
components are single precision — so the raw ratio differs for nearly every series.
`test_cost_curve.py` documents this so nobody "simplifies" it back.

Verified: worst minimum offset **0.000%** across all 997 series with a forecast.
`tests/curve_invariant.js` runs the real module; `tests/test_cost_curve.py` wraps it and
skips if node is absent.

It differentiates, which was the point: the under/over cost ratio runs 1.25 to 5.36, and
the extremes are the thesis — 720-day-shelf 44-53%-margin hobby lines are visibly
lopsided, 21-day-shelf 17%-margin food is visibly flat.

**4. The card in four rungs.** rung 0 position + exposure + age; rung 1 identity + the
number + the cost curve; rung 2 one sentence + the fan; rung 3 act. Six figures moved
into the drawer behind `W`, four of which explain the cost curve. Exposure stopped being
a hero figure — the position readout "1 of 24" says why the card is there. `.rc-top` and
`.rc-risk` are gone.

**5. Override reason codes, guardrail, impact.** A reason is **required** and enforced at
the action layer as well as by the disabled button. The guardrail states odds, live as
typed ("About a 1 in 11 chance demand reaches this level"). `hooks.impactOf` exposes the
existing `overrideImpact()` to the card, so the spreadsheet cell, the card form and the
commit summary all quote one estimator. Reason, note and expected effect leave in the
CSV — the reason is the only part of a decision that cannot be reconstructed later.

**6. Bulk approve and batching.** Fixed thresholds ($10/$50/$250) were measured and
found useless: the urgent cohort runs $377–$1,861, so everything below $500 cleared
nothing and $1,000 cleared 88% of items and 72% of the money — approve-all in disguise.
Thresholds are now percentiles of the live list, and an offer survives only if it clears
≤45% of items **and** ≤15% of the money. Low priority correctly offers nothing.

**Undo now reverses an action, not a row**: every decision carries a `seq`, so a bulk
approve of five is one press of undo. Batching is a **window** over the fixed cohort —
`shownList()` — never a re-slice; verified that cards go 24→5 while the headline, money,
progress and "1 of 24" all hold.

**7. Saved views, sidebar block 6 — the last of the review's eight.** Scope + cause + tab
under a chosen name, in `localStorage` key `dq.views`, each row carrying the count it
would show now. Counts come from the cohort directly, because applying a scope rebuilds
the queue and would lose the session's place just to fill a badge.

**The rail had exactly zero slack — 507px of content in 507px at 1440x900.** Block 6 paid
for itself: 31px from capping the queue at five causes (it already collapses its tail, so
nothing is dropped — verified the five rows still sum to all 72 live items), 30px from
moving "+ Save" onto the group label, ~5px from tighter spacing. All eight blocks now fit
on first load, and the rail only scrolls once a planner has actively saved views.

**8. Policy cost panel.** The nine measured arms in `metrics.economics`, named after what
they mean rather than after the mechanism each removes, ranked by earnings, opened from
the footer beside Evidence. The blanket-95% row is the whole argument:

    A service level for each product   $833k   94.2% met   112 units held   $4.2k written off
    One service level for everything   $771k   99.1% met   205 units held    $40k written off

It also reports, unsoftened, that "let the totals disagree" earns $1.5k more than the
current configuration — reconciliation costs a little margin and buys totals that add up,
and the row's own note says so.

**New tests (129 total, up from 113).** `test_cost_curve.py` (3, incl. the node-backed
invariant), `test_markup.py::test_every_module_parses` (11 — `node --check` per module),
`test_vocabulary.py::test_every_blended_method_has_a_plain_label`, plus `truth.js` and
`curve.js` joining the vocabulary and control-character checks, and an assertion that
`index.html` still loads every module the renderers dereference.

**The vocabulary stripper was loosened twice, both times correctly, both times verified
not to blind the check.** Object keys (`nhits:`) and quoted object keys
(`"Croston/SBA only":`) are code — they are keyed by the pipeline's own method names
precisely so they can map to a plain label. Both exemptions are anchored to line start,
`{` or `,`, so a leak in a `label:` value, in a `note:` value, or in a quoted string that
is not a key is still caught. Confirmed by test.

**Payload:** 160.7 KB gzipped for all HTML/CSS/JS, up from 127.3 KB.

### Traps added to the list by this work

- **`node --check` belongs in the suite.** A `const shown` collided with an existing
  binding in the same function; because these are plain scripts, the first symptom was
  an unrelated `Review is not defined` thrown by *app.js*. A blank shell and an error
  naming the wrong file. Now tested.
- **`--text-tertiary` is not a text colour.** It measures 3.86:1 light / 3.87:1 dark.
  This work hit it six more times (chart ticks, curve end labels, the "tested" chip, the
  outcome figure labels, the position readout, the required marker). Use
  `--text-secondary` for anything a reader has to read; tertiary is for furniture.
- **`--ui-accent` is a component colour held to 3:1.** As text on the card it is 4.06:1.
- **A contrast harness must composite alpha.** Mine reported six impossible 1.0–1.08
  ratios because it treated the override form's `rgba(.../0.07)` tint as opaque orange.
  Walk ancestors and composite until an opaque layer is found.
- **One keystroke, two handlers.** The card form's Enter sits on `#rvList` and the
  shortcut handler on `document`; `inField()` only stopped the second while focus
  happened to be in a text input. With focus on Save, Enter saved *and* approved the
  cursor's card. Guard on the container (`.rc-edit`), not on the focused tag.
- **A hidden browser pane suspends `requestAnimationFrame`** — awaiting it hangs the
  call. It also makes heavy canvas renders slow enough that a six-view sweep in one call
  exceeds the 30s budget. Use `setTimeout` only, and split sweeps across calls.
- **Setting `documentElement.dataset.theme` directly bypasses the app's theme path.**
  Click the app's own control and re-query elements after; never reuse a retained
  `CSSStyleDeclaration` across a flip.
- **Verify a feature's premise against the data before designing around it.** Three
  claims in the original feature list were wrong on inspection: `hist` vs `histRaw` is
  masking, not a repair; both calendar events *are* in the window; and per-product
  reliability *is* derivable. Two features I had called blocked were buildable, and one I
  had called easy was a bug.

### C13. Inventory management — IN PROGRESS, paused cleanly

Asked to make the interface fuller, stand apart from other demand/supply tools, and
above all to fill the sidebar with things that help run a store's inventory. Two
commits landed; the work is committed, tested and safe to leave.

**The gap this closed.** The application forecast demand and computed a target —
"stock up to 156 units" — and stopped. It never asked how many are on the shelf, so
it could not produce an order. It was a forecaster wearing the clothes of an
inventory tool.

**`ui/js/stock.js`** owns the missing input. On-hand is **planner-entered and
persisted** (`localStorage` key `dq.onhand`). It is deliberately never derived,
estimated or back-filled, and **there is no "fill these in for me" button**: a
fabricated count flows straight into an order quantity and out of the building as a
purchase. This is the one place in the system where a plausible invention costs real
money. Products are `counted` or `uncounted` and the interface always says which; a
product with no count shows `—`, never `order 0`.

    order quantity = stock up to  -  what is on the shelf
    days of cover  = on hand      /  expected demand per day

Cover thresholds are the ordering policy's own windows (`leadTime`, `reviewPeriod +
leadTime`), so they cannot drift from the logic that set the target.

**`ui/js/orders.js`** is the new fourth destination and the screen that produces an
order rather than a recommendation. Editable count per row saving as typed, Enter
moving to the next, rows ordered by how soon the shelf empties — the only ordering
that makes a list safe to abandon halfway. Exports a purchase-order CSV with a TOTAL
line, separate from `exportDecisions` because it is a different document for a
different reader.

**Uncounted products are ranked by what a full shelf is worth** (`target × cost`),
the only value figure available for something nobody has counted. A thousand blank
fields is not a task anyone starts; "$713k of shelf value, most valuable first" is.

**The rail is now eight collapsible `<details>` sections** with the open set
remembered in `dq.rail`. Native disclosure, so it is keyboard-operable and announces
its own state. **A collapsed section still reports its figure in its heading** —
the whole argument for this rail is that a closed block is not a silent one. New
blocks: **Replenishment** (order cost over the counted share it describes),
**Stock cover** (four buckets, each a door into the order list filtered to it),
**Range mix** (ABC × XYZ as a 3×3 grid, shade = share of stock value; the pipeline
has assigned both to all 1,000 products since the first run and no planner screen
ever showed them), **Stores** (all ten ranked from `D.rollups.store`, the one in
scope pinned top; clicking sets scope in Review, Orders and the select at once).

Before any counting the replenishment block leads with **what a full shelf of the
range is worth ($719k)**, because "$0 to spend" reads as a broken screen rather than
an honest zero — and it is the same arithmetic the order total will use, against a
stock level of nothing.

**Test count is now 147.** The jump is `test_ids_referenced_by_js_all_exist`, now
parametrised over all thirteen modules.

### Traps added by C13

- **The id check must cover every module, not app.js.** `orders.js` shipped with a
  `$("orUncounted")` left over from an earlier draft of its header; the throw
  happened inside the render, so the order list came up completely empty with
  nothing on screen to say why, and the console named a line in a file I was not
  looking at. Now checked across all modules. Ids a module writes into its own
  template literal count as real — `catalog.js` legitimately writes and then reads
  `catQty`.
- **Chrome serialises `color-mix()` as `color(srgb 0.677 0.730 0.943)` with 0–1
  components.** A contrast probe that reads those as 0–255 sees near-black and
  reports a passing 8:1 cell as 1.15:1. Every contrast harness in this project must
  parse that form — it produced two false failures before it was spotted.
- **`--text-tertiary` claimed a seventh victim**: the `.sb-group` headings, which
  stopped being decorative the moment they became clickable `<summary>` elements.
- **`--ui-accent` claimed another**: the order quantity at 4.3:1. Weight replaced hue.
- **Measuring in a zero-width pane is worthless.** `documentElement.clientWidth` came
  back `0` and every overflow figure with it; a reported 15px overflow vanished at a
  real viewport. Always `resize_window` before believing a layout measurement.

### C14. Cycle counting and the reference — DONE

**Cycle counting.** Counts now carry a timestamp and a recount interval driven by ABC
class (A 7 days, B 28, C 91). Without an age a figure entered three months ago and one
entered this morning are indistinguishable, and the order list depends on both equally.
New rail block **Stock take**, reported by class because "412 due" is a wall and "all
306 A lines are due" is a morning's work with a reason. Order rows show the count's age
under the input that sets it. `Due a count` filter on the order list.

**The `dq.onhand` format changed** from `{id: number}` to `{id: {n, at}}`. Legacy plain
numbers are read as counts of *unknown age* and treated as **stale**, which is the safe
reading; `0` survives as a real count of nothing (full-target order, critical cover) and
must never be confused with uncounted. Verified against a planted legacy store.

**The reference PDF is current again** — 24 pages, up from 20. Part V rewritten for the
four-rung card and the nine-block rail; **new Part VI on replenishment**; Evidence,
Engineering record and Limits renumbered VII–IX. `build()` now returns `(path, pages)`
and the script prints the page count.

Trap: verifying a PDF by regex over its raw streams inflates nothing and reports every
probe missing — the harness failing, not the document. `pip install pypdf` and read it
properly.

### C15. Usability pass — the whole application

Asked to make the entire project usable without confusion. Audited it as a first-time
visitor with every stored key cleared, then fixed what the audit found rather than what
seemed likely.

**Orientation.** The application opened on "24 decisions to make today" with nothing
saying what it is or how its two jobs relate. `#helpBox` — *How this works* — opens once
(`dq.seen`) and lives in the footer afterwards. Two numbered steps in the order they
happen, the order arithmetic, and four things worth knowing: nothing is sent anywhere,
a wide range is a warning not a fault, you need not finish, `?` shows the shortcuts.

**Three confusions found by measurement:**

- Every tier's cohort is capped at the same limit, so three tabs read `24`, `24`, `24`
  over lists of 489, 276 and 83 — a filter that looked broken. Now `24 of 489`.
- The queue block reported `72` (all tiers) beside the Today badge's `24` (urgent
  only): two right answers to one question. The block reports **exposure** now, a
  different unit that cannot be misread as a competing count.
- The order list led with "Needs a decision", which is also a Today tab meaning
  something else. Now "To order or count"; "Not counted" → "Never counted", since it
  sat beside "Due a count" and never-counted is a subset of due.

**The detailed view had never had the plain layer.** A rendered-DOM scan (not a source
scan) found the Spreadsheet printing the pipeline's own identifiers: filter chips read
`risk`, `stale`, `censored`, `wide`, `swing`, `cold-start`, `terminal`; the breadcrumb
said "item-locations · panel rows"; rows read `2_016 · CA_4` and `terminal · health
0.80`; captions read "lost mgn"; the weights panel read "lgbm quantile", "croston sba",
"adida". All had plain equivalents in `plain.js` already.

**Root cause: a second vocabulary.** `util.js` carried `REASON_LABEL` with its own seven
values ("service risk", "terminal decline", "censored history"…) and the Spreadsheet
read that one while everything else read `plain.js`. Deleted; all four call sites route
through `plain.js`. `CLASS_SHORT` mapped to abbreviated *jargon* ("intermit",
"terminal") and now maps to short *plain* words ("occasional", "ending").

**The most important fix is invisible.** The grid's accessibility mirror — what a screen
reader reads — printed the class key, a bare health score, "P10 to P90" and "target
quantile". That is the one surface where raw vocabulary has no visual context to lean
on. It now reads "Food 2-016, Store CA-4, Being discontinued, Patchy data quality,
492 units, likely between 278 and 796".

**Dense surfaces are framed rather than dumbed down.** The Spreadsheet and the Evidence
page each say who they are for; their technical column names are correct for those
readers. Framing, not an apology. Both dismissible/inline.

**Result:** rendered-DOM scan across Today, Order list, Products, Spreadsheet and the
sidebar returns **zero** jargon leaks, with real terms preserved on hover.

**Two guards added** (149 tests): no module may define a rival label table, and
`renderFilters` must render through `Plain.short()`. Both verified to fail when they
should.

### Traps added by C15

- **`--text-tertiary` has now cost ten elements.** It is 4.6:1 on `--bg-surface`
  *only*; on `--bg-inset`, a card, or any tinted panel it is 3.86–3.87:1. The token
  definition in `tokens.css` now warns about itself. Rules, dividers, disabled states
  and decorative glyphs only — never text a reader must read.
- **A rendered-DOM scan finds what a source scan cannot.** The vocabulary test reads
  files; it had passed for months while the Spreadsheet printed raw keys, because that
  surface was never in `SURFACES`. Walk the live DOM per surface when the question is
  "what does a user actually see".
- **The heredoc backslash trap bit again** — `\n` inside a bash heredoc became a real
  newline and the test file would not parse. Use the Write/Edit tools for anything
  containing escapes. This is the third time.

### C16. Purple brand, Charts, Overview, and an honest model report — DONE

Asked to make it business-ready: shades of purple, every graph in one feature list, all
the modules presented together, and to make sure the model is well trained.

**Palette.** `tokens.css` is three layers: primitives (purple / violet / plum / lilac
ramps plus red, teal, amber), semantic roles referencing them, components reading only
roles. No raw hex in the semantic layer; every token *name* survives because the canvas
renderers read them by name. **Two purples on purpose**: data is a cool violet (~245°),
action a warm orchid (~272°), 27° apart — the app already learned once that one hue for
"forecast" and "click me" cannot be learned. States stay red / amber / teal, outside the
purple family. `tests/test_tokens.py` pins names, checks each `--*-rgb` triple matches
its colour in both themes, forbids raw hex in roles, keeps the purples ≥20° apart.
Canvas verified by sampling painted pixels (Chrome substitutes `var()` inside custom
properties, so `getComputedStyle` returns plain hex). `U.tk()` returns grey `#888` for
any unregistered name — `--ui-accent` is now registered.

**`--text-tertiary` was raised at the source.** A full sweep of 4,470 text elements found
31 failures on it. It is now 4.68:1 or better on every surface including `--bg-active`.
Final sweeps: 0 failures across 4,337 + 1,762 elements (the latter including SVG `fill`).

**Charts** (`ui/js/charts.js`, nav "Charts"): 15 charts as a feature list — demand,
stock & ordering, model quality — each with the question it answers, a sentence computed
from the data, and how to read it. Inline SVG, uniform scaling, coloured by token class.

**Overview** (`ui/js/overview.js`) is now the **landing view**: one tile per module with
its live headline and one click in, three numbered tiles for the work in order.

**Model report — the real findings:**
- Training **reproduces** within 0.06% on every metric (full retrain, compared against
  the previous `metrics.json`).
- Bias was scored on the **median**; on zero-heavy skewed demand that reads 12–17% low by
  construction. New `evaluate.mean_from_quantiles()` / `bias_mean`: orders forecast −3.1%.
- Coverage counted ties at zero as hits. New `strict_after`, `tie_aware_gap`,
  `tie_aware_ok`. Upper range (≥75%, sets stock) honest within 1 point. **Lower tail is a
  real miss**: 10% of outcomes below the stated 5% floor. Open.
- The leaderboard's "DQ-Stack (full)" is the *reconciled* forecast (for totals; ~18% worse
  at product level by design — totals get 30–40% better). Orders read the unreconciled one
  (`for_decisions=False`). New row **"DQ-Stack (as issued for orders)" = 0.691**, the best
  on the board, 30% less error than seasonal naive.
- Rank correlation was unstable (0.53 vs 0.38) because three arms share one forecast and
  float noise ranked them. Accuracy now compared at 6 dp: ρ = 0.458 stable, p = 0.215.
- Also open: the reconciled totals view runs ~15% high on its mean.

### Traps added by C16

- **`artifacts/` and `ui/data/` are gitignored.** A pipeline run is NOT restorable from
  git. Copy `artifacts/metrics.json` somewhere before any retrain if you need to compare.
- A retrain is ~25 minutes on this machine (backtest alone 670s), longer than the 15 the
  stored timings suggest. `--export` overwrites the bundle and restamps `index.html`.
- `bundle.js` must equal `"window.DQ_DATA=" + bundle.json + ";"` byte for byte — no
  trailing newline — if you ever patch a derived statistic without a full run.
- A contrast harness must read SVG text via `fill`, not `color`, and must skip gradient
  pills (their `backgroundColor` is transparent).
- `U` in the browser is a top-level `const`, not `window.U`.

### C14. What is left, for the next session

Everything committed, suite green at **147 tests**, working tree clean.

1. **Supplier grouping — BLOCKED ON A DECISION, needs the user.** Orders are placed to
   suppliers, not products, and this is the largest remaining gap in the replenishment
   story. It needs a supplier dimension with its own lead time, which means:
   `config.py` + wherever series attributes are assigned, then `decide.py` taking a
   **per-series** lead time instead of the global `cfg.decision.lead_time`, then the UI
   grouping the order list by supplier with per-supplier totals and minimum order values.
   **The blocker:** `pipeline.py` has no stage-level resume (only `--quick`), so this
   costs a **full ~15.5 minute re-run** and changes `orderUpTo` for every product —
   invalidating every verified figure in this file, the committed artifacts, and the PDF.
   Do not start it without the user agreeing to that. The cheap shortcut — synthesising
   suppliers into `bundle.json` with a side script — is a coherence trap: a later
   pipeline run would silently drop them.
2. **Reorder alerts.** A cover threshold a planner sets, surfacing as a rail block. Data
   is all present; the open question is whether an alert with no notification channel is
   worth more than the filter that already exists.
3. **Count variance / shrinkage.** When a recount lands, compare it against what the
   system expected (previous count minus sales since). Genuinely valuable, and the
   timestamp added in C14 is the precondition — but it needs sales-since-count, which
   the bundle does not currently carry.
4. Still unbuilt from v2's own list: density modes, card transitions on J/K, notes,
   history timeline, first-run walkthrough, command-palette actions, mobile approvals.

### C17. The store-operations application — the product specification, steps 1 to 11 DONE

The user supplied `DQ-Stack-Product-Specification.pdf` (27 pages, Nov 2026): a
multi-company Indian store-operations SaaS with this engine at its centre, built in 12
steps each with a "done when" test. Built here, step by step, each step's test passing
on SQLite and PostgreSQL 16 before the next began. The demo pipeline and planner above
are unchanged when called as before (233 engine and site tests still pass).

**Where things are.** `dqstack/app/` is the server (FastAPI + SQLAlchemy 2, Alembic,
argon2). `webapp/` is the store app at `/app` (vanilla JS: `core.js` helpers, `start.js`
frame and router, `screens-a/b/c.js`). `site/` is the public site at `/`. The planner
stays at `/planner`. `deploy/`, `Dockerfile`, `docker-compose.yml` deploy it.
`docs/SECURITY.md` is the section 15 status. Tests: `tests/app/test_step{1..11}_*.py`,
`test_platform.py`, `test_site_serving.py`; `tests/test_site.py` for the site.

| Step | Done when | Proof |
|---|---|---|
| 1 Foundations | a user of one company cannot read another by changing ids | test_step1 (+ triggers on SQLite and PostgreSQL) |
| 2 Company, stores, people | sign up, 3 stores, invite a manager who signs in | test_step2 |
| 3 Imports | 5,000 rows with 20 bad import 4,980; re-upload changes nothing; undo removes | test_step3 |
| 4 Ledger | on hand = sum of 10,000 random movements | test_step4 (+ 6 concurrent tills on PostgreSQL) |
| 5 Floor work | offline count syncs once; large difference waits; partial delivery leaves order open | test_step5 (scenarios 10, 11) |
| 6 Sales in | a day's file moves stock and the daily table; a late file replaces the day | test_step6 |
| 7 Engine on real data | first forecast and backtest unattended; nightly in minutes; failure keeps yesterday's | test_step7 (real engine: 140 s train, 5.5 s nightly) |
| 8 Orders | Rs 5,000 minimum and 12-unit cases correct by hand (scenario 12) | test_step8 |
| 9 Checkout | scenarios 3 to 8 | test_step9 |
| 10 POS API | 1,000 sales sent twice reduce stock once | test_step10 |
| 11 Alerts and reports | each alert once in a scripted day; reports match hand figures | test_step11 |
| 12 Hardening and pilot | load test, accessibility, security review, restore drill, 4-week pilot | partly: see below |

**How the engine serves a company (step 7).** `forecast/source.py` builds the engine's
five feeds from the database; `pipeline.run(tables=..., splits=...)` trains with origin
blocks sized to the history; `dqstack/live.py` saves the model (gzip pickle in
`model_artifacts`) and forecasts from it nightly without retraining (reproduces
training's live forecast exactly). Tiers by history (`jobs.plan`): >= 667 days demo
settings; >= 358 scaled blocks; >= 302 minimal blocks; >= 56 an early forecast from the
statistical candidates; less, none. The thresholds come from make_splits' arithmetic
plus NHITS needing input_size + horizon before the last training origin; a test checks
every tier against make_splits itself. Promotion: a new model replaces the active one
only if `live.recent_scores` (same recent weeks, observed sales) gives it at least as
much simulated margin. Stock targets use each product's own supplier lead time and
order frequency (`live.targets`, grouped by risk period) — which resolves C14 item 1
without touching `decide.py`.

**Step 12, honestly.** Done: `scripts/loadtest.py` and `scripts/scale_seed.py`, the
section 15 status in `docs/SECURITY.md`, platform portal, export and deletion;
point-in-time recovery with WAL-G (`deploy/postgres/`, `deploy/restore-to-time.sh`);
the CI jobs `pitr-drill` and `scale` (200 tills, 100 stores x 10,000 products) — written
and syntax-checked, first proven when CI runs, since Docker was not available while
building; OTP (two-step sign-in, reset by phone) and Google/Microsoft sign-in
(`signin.py`, `oidc.py`); WhatsApp Business Cloud API and SMS (`whatsapp.py`, `sms.py`)
for orders, alerts and receipts, falling back to links when not configured. Not done: a
WCAG audit with assistive technology; the 2-store pilot; legal review of the privacy and
terms drafts; a chartered accountant's check before Checkout is used as a GST invoice
system; real-service trials of Google, Microsoft, WhatsApp and an SMS gateway (needs the
client's accounts and approved templates). Training a very large company in parallel by
category is not built.

**After step 12.** The 23 screens (`webapp/js/screens-a/b/c.js`). A cashier's app is
the till only (menu, routes and, in `deps.py`, the server). "My shift" gives a cashier
their own takings. Reports download as Excel, PDF or CSV, or all in one workbook.
Two people counting one product are compared count against count (carried forward by
sales between them), never against the system figure; the manager picks one, or sends
it for a recount.

**Running it.** `python -m dqstack.app` (development, migrates SQLite first);
`python -m dqstack.app.manage seed-demo --email ...` makes a demo company with a trained
forecast (login written to the git-ignored `data/demo-login.txt` on this machine);
`python -m dqstack.app.worker` runs the schedule. Production: `deploy/README.md`.

### Traps added by C17

- **Migrations are now added as revisions** (`0002_signin_extras.py` follows the
  baseline); do not regenerate `0001` any more. The drift test fails if the models and
  migrations disagree.
- **Browsers keep the app's files**: without `Cache-Control: no-cache` on `/app` a new
  release ran old JavaScript until a hard reload (fixed in `main.py`).
- **The app's own log lines were invisible**: uvicorn configures only its loggers, so
  `main._app_logging` attaches one for `dqstack.*` (development mail and codes show there).
- **A bill locks its stock rows in sorted order** (`ledger.lock_rows`) before posting
  lines; locking in scan order can deadlock two tills on PostgreSQL.
- **Writes during a request hold row locks to the end**: an API key's `last_used_at`
  once serialised every request on that key; it is now written in its own transaction.
- **`diagnose.run` writes `data/processed/` on every call.** Any engine call for a
  company must run inside `forecast.jobs.scratch()` (in-process) or with
  `DQ_ARTIFACTS_DIR`, `DQ_INTERIM_DIR`, `DQ_PROCESSED_DIR` set (subprocess), or it
  overwrites the demo's files. It happened once; they were regenerated from the panel.
- **SQLite ignores VARCHAR lengths; PostgreSQL does not.** A 65-character id in a
  64-character column passed every SQLite test. Run `DQ_APP_TESTS_ON_POSTGRES=1`.
- **The session does not autoflush.** A query right after a change does not see it:
  flush first (`floor.maybe_close`, ledger idempotency keys both bit).
- **Opening stock is a baseline, not a count.** `baseline_at` anchors which sales move
  stock; `last_counted_at` drives the cycle-count schedule. Conflating them made
  products never due for counting.
- **Tests that set stock "now" depend on the time of day**: after a store's 22:00 close,
  today's sales are rightly absorbed by a figure set later. Date opening stock back.
- **Heredocs through the tool transport eat backslashes**: `\n` inside an f-string and
  a trailing `\` line continuation both broke files. Use the Edit tool for those.
- **FastAPI 0.118 cannot run on Starlette 1.x; 0.142+ needs opentelemetry-api >= 1.44**,
  which conflicts with crewai in the base environment. 0.140.13 is the pin.
- **Starlette matches routes in order**: `/orders/suggestions` must be declared before
  `/orders/{order_id}`.
- **Alert dedupe keys must be one format** (`kind:...`) wherever an alert is raised, or
  the scheduler raises the same condition twice.
- **The app's database role must not own the tables**, or it could disable the
  append-only triggers. `deploy/postgres-init.sh` sets that up.

### D. Correctly out of scope (for the planner)
Multi-tenancy, approval workflows and connectors now exist in the store application
(C17). For the planner itself: the agentic layer, and features public data cannot
exercise.

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
- **Never slice markup by searching for the next closing tag.** Two commits in a row
  ate structural `</div>`s that way, and the first was invisible because browsers
  auto-close. `tests/test_markup.py` asserts tag balance now; trust it over reading.
- **Every `$("id")` in app.js must resolve.** A single stale lookup in `boot()`
  throws and silently aborts all wiring downstream of it — the symptom looks like
  several unrelated features being broken.
- **Links carry no hue by design.** `--ui-accent` is 3.72:1 on the dark raised
  surface, which is fine for a focus ring and not for text.
- **`min-width: 0` on every grid item that contains a canvas or a table.** Both
  hold their track open otherwise, and the symptom appears on an ancestor.
- **`margin-inline: auto` on a grid item needs `width: 100%`** or the item stops
  stretching and sizes to its content.
- **`--ui-accent` is for interaction; `--series-forecast` is for data.** They share
  a value today. Do not collapse them back.
- **Chart tick labels must use `--text-secondary`.** `--text-tertiary` fails AA on
  the surfaces charts sit on, and revaluing the token is not the fix.
- **A commit must not clear the state that records what was done.** Track
  committed separately from reviewed, or finishing the work undoes it.
- **Do not claim a write that does not happen.** There is no system of record; the
  CSV is the only way decisions leave the browser.
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
