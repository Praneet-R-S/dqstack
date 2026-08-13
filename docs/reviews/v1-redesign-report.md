---
title: "DQ-Stack interface: redesign report"
subtitle: "What to hide, what to add, what to change --- and how"
author: "Design review, version 1.0"
date: "August 2026"
documentclass: article
fontsize: 10pt
geometry: "margin=2.2cm"
mainfont: "Carlito"
monofont: "DejaVu Sans Mono"
numbersections: true
toc: true
toc-depth: 2
colorlinks: true
linkcolor: "RoyalBlue"
---

\newpage

# Diagnosis

The system is well built. The pipeline is honest, the budgets are measured rather than asserted, the defect record is the strongest section in the reference document, and the fan identity is correctly derived from the product's own geometry rather than applied to it. None of that needs rescuing.

The interface has one structural fault, and five consequences fall out of it.

## The fault: four audiences, one navigation

Six destinations sit in a flat sidebar. They serve four different people:

| Destination | Actually serves |
|---|---|
| Today | Demand planner |
| Products | Demand planner |
| Spreadsheet | Demand planner (bulk mode) |
| How it's doing | Supply-chain analyst |
| Ordering decisions | A first-time reader, once |
| How it works | Reviewer or examiner |
| SQL console | Supply-chain analyst |
| Glossary | Anyone, momentarily |

The planner --- the primary user, six hours a day --- needs three of these. The other five are permanently in their peripheral vision, and two of them (`How it works`, `Ordering decisions`) exist to explain the system rather than to operate it. A planner who has used the tool for a month will never open either again, but will scan past both several hundred times.

This is the thing to fix first, and everything else in this report is downstream of it.

## Consequence 1: method vocabulary reaches the planner

`How it works` presents eight pipeline stages with this run's own numbers. That is exactly right for an examiner and exactly wrong for a planner. A demand planner does not need to know that Stage 5 uses MinT with Schaefer-Strimmer shrinkage, or that Stage 4 was moved from an FFORMA-style proxy to convex stacking because the proxy's optimum is a simplex vertex. That paragraph is a genuine intellectual result and it belongs in the written work, not in the sidebar of an ordering tool.

The cost is not merely clutter. Exposed machinery tells the user the system is something to be understood before it can be trusted, when the design intent is the opposite --- that trust should be legible at the point of decision without any understanding of the mechanism.

## Consequence 2: the card asks one question and shows eight numbers

The reference states that each item is "one card asking one question, with one obvious answer." The card then carries: money at risk, product, store, category, sell-pattern class, headline reason, a 63-day chart, next week's forecast, its range, change since last week, stock-up-to level, service target, data quality, confidence, and five controls.

That is fifteen elements. A card that answers in one click cannot require reading fifteen elements to reach the click. The information is all good --- the problem is that it is all at the same level.

## Consequence 3: the application has no sense of time

Demand planning is cyclical. Orders close. There is a cutoff, a week number, a "this is the last day to change anything" moment. The interface as described has no notion of any of it: no cycle, no deadline, no elapsed time on a flagged item, no record of what changed since the planner last logged in.

This is the largest missing feature in the product, and it is invisible in a feature list because it is not a feature --- it is a frame that everything else sits inside.

## Consequence 4: trust is a destination, not an ambient property

Calibration coverage, censoring precision, forecast value added and the accuracy-versus-economics comparison all live in `How it's doing`. They are the evidence that the product's central claim holds. But at the moment a planner decides, the only trust signal on screen is a per-card "data quality and confidence" pair.

If a feed failed last night, the planner finds out by noticing a confidence figure looks low on several cards --- if they notice at all. System condition needs to be ambient and continuous, not discovered.

## Consequence 5: one hue carries two meanings

The reference identifies this precisely: `--series-forecast` meant both "predicted quantity" and "clickable," and a hue that means two unrelated things cannot be learned. A `--ui-accent` token was then introduced --- with an identical value, so that no pixel moved.

That is a correct refactor and an incomplete fix. The diagnosis was right and the values are still colliding. Section 7.1 finishes it.

\newpage

# The information architecture

## Three surfaces, one evidence area

Reduce the sidebar to what a planner operates, and collect everything else into a single destination that is honest about being for a different reader.

```
  WORK
    Today          the decision queue          (landing)
    Products       browse, inspect, change the range
    Spreadsheet    bulk editing

  ────────────────────────────────────

    Evidence       accuracy, calibration, economics,
                   method, SQL console
```

Four items, not nine. `Glossary`, `Ordering decisions`, `How it works`, `How it's doing` and the SQL console all move inside `Evidence` as tabs. `Shortcuts` stops being a navigation item entirely --- it is already on `?`, and a keyboard sheet that needs a sidebar entry is a keyboard sheet nobody found.

**Why one merged Evidence rather than removing them.** These views are the proof that the system's argument holds, and an examiner or analyst genuinely needs them. Hiding them would be dishonest. But grouping them under a heading that says plainly "this is the evidence, not the work" removes them from the planner's daily field of view at no cost to the reader who wants them.

**Role gating, if roles exist.** If the deployment knows who is signed in, default `Evidence` to collapsed for planners and expanded for analysts. If it does not, the grouping alone does most of the work.

## The disclosure ladder

Every screen in the product should obey the same four-rung ladder. This is the mechanism that hides background detail without deleting it.

| Rung | Content | Where it lives |
|---|---|---|
| 1. The answer | One number, one action | Always visible on the card |
| 2. The reason | One sentence, plain language | Always visible, under the answer |
| 3. The support | Figures, drivers, data quality, peers | One keystroke (`W`) or one click --- a drawer |
| 4. The method | Which models, which stages, which parameters | Inside the drawer, behind "How this was produced" |

Rung 4 is where every method term is permitted to appear, and it is the only place. A planner reaches it in two deliberate actions, which is exactly the right cost: available to the curious, absent for everyone else.

## What to hide, precisely

| Currently visible | Who it is for | New home | Mechanism |
|---|---|---|---|
| `How it works` --- eight stages | Examiner | Evidence to Method | Tab |
| `Ordering decisions` explainer | First-time reader | Drawer rung 4, plus first-run | Contextual |
| WSPL, WRMSSE, MASE, bias | Analyst | Evidence to Accuracy | Tab |
| Wilcoxon, sign tests, paired inference | Examiner | Evidence to Accuracy | Tab |
| Coverage table, conformal before/after | Analyst | Evidence to Calibration | Tab |
| MinT, reconciliation trade | Analyst | Evidence to Method | Tab |
| Model names on a card | Nobody, as names | Rung 4, translated | See 2.4 |
| SQL console | Analyst | Evidence to Query | Tab |
| Glossary as a destination | Anyone, briefly | Inline definitions on first use | Dotted underline, hover or tap |
| Ledger cost components | Analyst | Evidence to Economics; summary only on card | Tab |

## The vocabulary translation

No planner-facing surface uses a term from the left column. Rung 4 may use both, with the plain term leading.

| System term | Planner-facing wording |
|---|---|
| Critical fractile | How much it costs to be wrong |
| Service target / order-up-to | Stock up to; how often you will have it in stock |
| Predictive quantile, P10--P90 | Likely range |
| Conformal calibration | Ranges checked against what actually happened |
| WSPL / WRMSSE / MASE | (never shown) --- "how well the ranges hold up" |
| MinT reconciliation | Totals add up across stores and categories |
| Convex stacking / candidate pool | Several methods, blended |
| Croston, SBA, TSB, ADIDA, NHITS, Tweedie | (never shown as names) --- "forecast from its bursty pattern" |
| ADI, CV-squared, SBC quadrant | Sells steadily / in bursts / occasionally / too new to tell |
| Censoring detection | We think the shelf was empty, not that nobody wanted it |
| Regime change / PELT | Its pattern changed on 14 June |
| Forecast value added | Whether this step is earning its place |
| Vintage-safe featurisation | (never shown) |

Note that the existing sell-pattern wording --- steadily, in bursts, occasionally, too new to tell --- is already correct and is the model for the rest. Extend that instinct to every remaining term.

\newpage

# The signature: make the argument visible

The system's sharpest claim is one sentence: *two products with an identical forecast get different orders, because the right stock level depends on what mistakes cost.* Currently that claim is taught in a destination called `Ordering decisions` --- a page a planner reads once.

It should be a glyph on every card.

## The cost curve

A small asymmetric shape, roughly 132 by 44 pixels, drawn beside the order figure. The horizontal axis is order quantity. The curve is the expected cost of being wrong at that quantity --- steep on the side where mistakes are expensive, shallow on the side where they are cheap. Two marks sit on it: the median forecast, and the recommended order.

```
   fresh milk                     durable, high margin
   overage punishing              underage punishing

   \                    /              \                      /
    \                  /                \                    /
     \                /                  \                  /
      \__       _____/                    \_____       ____/
         \_____/                                 \____/
        |  |                                        |    |
       med ord                                     med  ord
   order sits below median                   order sits well above median
```

The planner does not read a number from it. They read a *shape*, and the shape is different for milk and for a durable. After a week they have internalised the entire ordering thesis without encountering the phrase "critical fractile," and when they see an order sitting far above the median they already know why.

**Why this and not something else.** The existing fan is the identity and it appears on the sidebar lockup, the empty states, and every chart --- correctly. But the fan expresses *uncertainty*, which is only half the product. The cost curve expresses *asymmetry*, which is the half that makes this system different from every other forecasting tool. It is drawn from the product's own mathematics rather than invented for decoration, it is the only new mark proposed in this report, and it is small enough to sit on a card without competing with the fan chart above it.

**Rules for it.** Static, never animated. Rendered on the existing canvas, cached like the micro-fan. Absent when the product has no forecast yet, in which case the space carries the existing "Not yet --- needs a forecast first" wording. Never used decoratively, never on the sidebar, never in an empty state --- the fan owns those.

**Accessible equivalent.** `aria-label` of the form: "Running out costs about four times as much as over-ordering, so the recommended order sits above the middle estimate."

\newpage

# Redesigning Today

## Card anatomy, before and after

**Before** --- fifteen elements, all at one level.

**After** --- three tiers.

```
┌──────────────────────────────────────────────────────────┐
│  $2,140 at risk          flagged 3 days ago    ▸ 4 of 24 │  tier 0: rank + age
├──────────────────────────────────────────────────────────┤
│  Whole milk 2L · Store CA_1 · sells steadily             │
│                                                          │
│         Stock up to  140                                 │  tier 1: THE ANSWER
│                                                          │
│  Promotion ended Tuesday. Demand is falling back         │  tier 2: THE REASON
│  toward its usual level.                                 │
│                                                          │
│  ┌────────────────────────────────┐  ┌───────────────┐   │
│  │ [ fan chart, bled to edges ]   │  │ [cost curve]  │   │  tier 2: evidence
│  └────────────────────────────────┘  └───────────────┘   │
│                                                          │
│  [ Approve ]   Change   Skip        Why this number? (W) │  tier 3: on demand
└──────────────────────────────────────────────────────────┘
```

Two numbers are visible instead of eight: the order quantity, and the money at risk. The remaining six figures --- forecast and range, change since last week, service target, data quality, confidence --- move behind `W`, which opens a drawer over the right third of the screen without leaving the card.

**What each tier is for.** Tier 0 is why this card is in front of you and where you are. Tier 1 is the decision. Tier 2 is enough to accept the decision without further reading, which is the common case. Tier 3 is for the minority of cards where the planner wants to argue with the system.

**The single filled button rule is correct and stays.** Exactly one filled control per card. `Approve` is filled, `Change` and `Skip` are text.

## The drawer (rung 3)

Opened by `W` or by clicking the chart. Slides in over the right third in 180ms; the card does not move.

```
  Why 140?

  Next week          138 units      likely 96 – 190
  Change             −22% vs last week
  Service target     50%            fresh, thin margin
  Data quality       good           no gaps, no empty-shelf days
  Confidence         high           ranges held up 9 times in 10

  What moved it
    Promotion ended               −31 units
    Day of week (Tue–Thu heavy)    +6 units
    Seasonal drift                 −4 units

  Compared with the same product elsewhere
    [ p10–p90 range with median notch, this store marked ]

  ▸ How this was produced                        (rung 4)
```

Rung 4 collapses by default and is the only place method vocabulary appears.

## Batch decisions

The queue is a fixed cohort chosen once at load --- correct, and the fix to the growing-denominator defect was the right one. But a planner facing 24 cards where 12 share a cause should not click 24 times.

When three or more cards in the cohort share a headline reason, offer a grouped action at the top of the queue:

> **12 items: promotion ended**  Their forecasts all fall back toward the usual level. Combined exposure USD 9,400.  `Review as a group` · `Approve all 12`

`Review as a group` opens a compact list --- one row each, order quantity, money at risk, sparkline --- with per-row exclusion. `Approve all 12` requires a confirm step naming the count and the combined exposure, because it is the one irreversible-feeling action in the product.

Grouping rules: same headline reason, same direction of change, no item above a configurable exposure ceiling (high-value items always get their own card).

## The cycle ribbon

A quiet persistent strip below the page header on every Work surface. This is the missing frame.

```
  Week 33 · orders close Thursday 17:00 · 2 days left        24 decisions waiting
```

States: **open** (neutral), **closing soon** (under 24 hours, attention colour), **closed** (the queue becomes read-only and the ribbon says what the next cycle opens). If the deployment has no real cutoff, the ribbon shows the forecast vintage instead --- "forecasts generated today 06:12" --- which is still more orientation than the current interface provides.

## Since you were last here

Above the first card, on the first visit of a day only. Dismissed permanently by acting on it or by pressing `Esc`.

> **Since Friday** --- 3 products moved into risk · 1 sales feed was late and has caught up · the forecast for Greek yoghurt 500g changed by 41% · 6 items you skipped are still waiting.

Each clause is a link that filters the queue. This costs one small component and answers the question every planner has on Monday morning.

## System condition, ambient

A single-line status in the ribbon area, visible only when something is wrong. Silence is the healthy state; a persistent green tick is noise.

**Degraded:**

> Sales data for 340 products has not updated since Tuesday. Their forecasts use older data --- 12 items in your queue are affected. `Show me which`

**Calibration drift:**

> Forecast ranges have been running narrow for eight days. Treat the likely range as optimistic until this clears.

Both are written in the interface's voice, state what happened and what it means for the planner's work, and offer the next action. Neither apologises and neither says "error."

## The empty state as a reward

The most important emotional moment in the product is the one where the work is finished. The reference notes the completed-work state correctly keeps a tick rather than a fan. Take it further --- make it a summary rather than a confirmation.

> **Today's queue is clear.**
>
> You decided 24 items covering USD 18,400 of exposure. You changed 3 forecasts. The system handled 1,190 other products without needing you.
>
> `Export decisions as CSV` · `See what you changed`

That last sentence is the one that matters. A planner who cannot see the scale of what was handled automatically has no way to feel the tool is working, because the tool's success is invisible by construction.

## Changing a number

The current flow is `Change the number`. It needs three additions.

**1. A guardrail against the distribution.** If the entered value falls outside what the model considers plausible, say so before saving --- do not block it.

> 240 is above the highest outcome the model considers likely (its 99th percentile is 186). Save anyway?

**2. A reason code, required.** The pipeline already treats overrides with reason codes as an input to later runs. The interface has to capture one. Six options, sourced from the deployment, plus free text:

`Promotion I know about` · `Local event` · `Supplier issue` · `Store request` · `Model looks wrong` · `Other`

Free text is optional except after `Other`, where it is required. Keep the list short --- a twenty-item reason taxonomy gets a single option picked for everything.

**3. Impact, before commit.** Show the margin effect of the change against the recommendation, quoting the same estimator the spreadsheet and the sticky bar already use. The reference is explicit that the two surfaces must not disagree; extend that constraint to a third.

As the number changes, the mark on the cost curve moves with it. That is the one piece of motion worth adding to this screen, because it makes the consequence of the edit visible in the product's own geometry.

## Card transitions

Nothing animates on load, which is right. But moving between cards with `J` and `K` currently has no cue, and a planner who cannot feel that they moved will lose their place in a fast run.

Add a 120ms directional shift --- the outgoing card translating 8px and fading, the incoming entering from the opposite side. Only on keyboard navigation, zeroed under `prefers-reduced-motion`, and never on the first paint.
\newpage

# Redesigning Products

The list-beside-detail layout is right and stays. Four changes.

**Sort and trailing figure already agree** --- the reference notes the trailing figure changes to match the sort, which fixes a real class of confusion. Keep exactly as is.

**Add a watchlist.** A planner has fifteen products they personally distrust. Pinning them costs one star control per row and one filter chip, and it is the cheapest loyalty feature in the product. Pinned items always surface in Today regardless of exposure ranking, marked as watched rather than flagged.

**Add the peer comparison to the list, not just the detail.** The detail pane shows the same product across other stores as a p10--p90 range with a median notch --- a genuinely good component. A 60px version of it belongs in the list row, because "this store is the outlier" is the single most useful thing a planner can learn while scanning.

**Add a "what changed" column.** Products the planner has already decided on this cycle should be visibly marked as such, with the decision and its timestamp. Without it, Products and Today are two views of the same items with no shared memory.

# Redesigning the Spreadsheet

Very little. It is well made, correctly demoted from default, and the micro-fan defect record shows it has already survived real scrutiny.

**One addition: a reason-code column.** If overrides require reason codes on a card, they require them in bulk too, or planners will route every override through the spreadsheet to avoid the field. Fill-down applies to the reason column like any other.

**One change: pending edits need a persistent count.** A planner who has 40 uncommitted edits and navigates away should see that count follow them into Today and Products, not discover it on return.

# New surfaces to add

Ordered by value to the planner.

## Decision log with undo

The single largest safety gap. A planner who approves thirty items and then learns the price feed was stale has no recourse.

A reverse-chronological log of this session's decisions --- item, decision, order quantity, timestamp, and an undo control per row, plus `Undo last 10`. Reachable from the sticky bar and from `Cmd-Z` at any point in Today. The log is already implicitly held in the draft set; this exposes it.

The `Commit` control currently "marks them done without claiming a write to a system of record that does not exist," which is honest. Add one sentence to it: after committing, the log stays readable until the session ends, and states plainly that committing did not send anything anywhere.

## Your track record

The pipeline treats override reason codes as a signal that the model is failing on a product. The planner never sees the other half of that: whether *their* corrections helped.

A small panel on Today, updated weekly:

> **Your overrides, last 30 days**
> 12 helped, worth about USD 3,400 · 8 made things worse, about USD 1,200 · net USD 2,200 better than leaving them alone
>
> You do best on promotions. You tend to over-correct on slow sellers. `See the detail`

Two design constraints on this. It must never be framed as a score or a ranking against other planners --- that turns a learning instrument into a performance review and planners will stop overriding, which loses the signal the pipeline wants. And it must be honest when the answer is unflattering, because a panel that only ever reports good news is not read twice.

## First run

Not a tour. Four real decisions with the reasoning already shown, worked in sequence, with the drawer open by default on the first two and closed on the second two. The planner learns the loop by doing it, not by reading about it, and arrives at the fifth card with the drawer habit already formed.

Skippable, and never shown again once any card is approved.

## Command palette with actions

`Cmd-K` currently searches. Make it act.

```
  >  approve all promotion-ended            12 items · USD 9,400
  >  filter queue to store CA_1
  >  jump to Greek yoghurt 500g
  >  export today's decisions
  >  show items I skipped
  >  switch to light theme
```

Search results and actions in one list, actions first when the query matches a verb. This is where a keyboard-fluent planner spends their time, and it costs far less than building equivalent controls into every view.

## Mobile approvals

Read and approve only --- never bulk editing, never the spreadsheet. Card stack, swipe or tap to approve and skip, the drawer available as a sheet, no override entry beyond the guardrail-free simple case. A planner clearing eight cards in a queue at 7am is a real behaviour; a planner editing a canvas grid on a phone is not.

## Notifications digest

An end-of-cycle summary, emailed or exported: decided, changed, exposure covered, and what the next cycle opens with. This is also the artefact a category manager wants, and it costs one template over data the ledger already holds.

# Design system changes

## Finish the accent split

The reference diagnoses the problem exactly --- `--series-forecast` carried both "predicted quantity" and "clickable," a hue meaning two unrelated things cannot be learned, so `--ui-accent` was added across 38 surfaces. Then it was given an identical value so that no pixel moved.

That was the correct refactor and the right sequencing. Now finish it: move the interactive role to a distinct hue.

| Token | Now | Proposed | Role |
|---|---|---|---|
| `--series-forecast` | `#4C6FFF` / `#3050E8` | unchanged | predicted quantities, fan bands |
| `--ui-accent` | `#4C6FFF` / `#3050E8` | `#7A5CF0` / `#5B3FD4` | links, focus, selection, primary action |

A violet sits far enough from the forecast blue to be distinguishable at a glance and at small sizes, and does not collide with risk `#E0523C`, attention `#C99A2E`, override `#E09B15` or excess `#14A99A`. Verify both values against the existing contrast harness before adopting --- the build already computes 34 checks across both themes, so this is a one-command answer rather than a judgement.

**A stronger option, if you want the more disciplined position.** Give links and secondary controls no hue at all --- ink colour, underline on hover, weight for emphasis --- and reserve the violet solely for the primary action and the focus ring. In an application where nine hues already carry data meaning, an interface that spends a tenth on "you may click this" is spending it badly. This is the more opinionated choice and the one I would take.

## Hero figures should not be monospace

JetBrains Mono for every numeral is the right instinct in the wrong scope. A monospace face is tabular by construction, which guarantees column alignment without depending on a font feature surviving every code path --- that reasoning is sound, and in the grid it is decisive.

But `--fig-xl` at 40px is `money at risk`. There is no column for it to align to. At that size a monospace face reads as a system readout rather than a quantity of money, and JetBrains Mono is wide, so the largest and most important figure in the product is also the one that fits least comfortably.

| Token | Now | Proposed |
|---|---|---|
| `--fig-xl` 40px | JetBrains Mono | Instrument Sans, tabular figures |
| `--fig-lg` 28px | JetBrains Mono | Instrument Sans, tabular figures |
| `--fig-md` 20px | JetBrains Mono | JetBrains Mono --- often in a column |
| Grid cells | JetBrains Mono | unchanged |
| Any aligned column | JetBrains Mono | unchanged |

Verify that Instrument Sans exposes lining tabular figures via `font-variant-numeric: tabular-nums lining-nums`. If it does not, the fallback order is: keep mono at hero sizes but reduce tracking to `-0.04em`, or adopt a data-oriented sans for the figure roles only.

The existing split of the figure class into a numeral variant and a word variant --- so that "Good" at 20px monospace does not read as a status code --- is exactly the right instinct. This extends it one step further.

## New tokens

Additions only, never renames --- the reference is explicit that 22 tokens are load-bearing and resolved by name at paint time in `util.js`, and that renaming one is a silent rendering failure in an unrelated file.

```css
--cost-underage      /* the expensive side of the cost curve */
--cost-overage       /* the cheap side */
--cost-mark          /* median and recommended-order marks */

--age-fresh          /* flagged today */
--age-stale          /* flagged 3+ days ago */

--cycle-open
--cycle-closing
--cycle-closed

--state-degraded     /* feeds late, forecasts stale */

--density-row-today  /* card list density */
```

Every canvas renderer that reads a new token must be added to the cached-token list in `util.js` in the same commit, or the first paint after a theme flip will resolve it as empty.

## Density

Today has one density. Planners disagree about this and both camps are right for their own workflow. Three modes, persisted, matching the pattern the spreadsheet already needs:

| Mode | Card height | Chart | Cards visible at 1440px |
|---|---|---|---|
| Focused | ~420px | full fan | 2 |
| Default | ~340px | full fan | 3 |
| Compact | ~180px | sparkline only, cost curve retained | 6 |

Compact is not a degraded view --- it is the mode a confident planner works a long queue in, and the cost curve survives into it precisely because it is the fastest signal on the card.

## Icons

The reference mentions no icon set, which suggests text and colour are carrying all status signalling. That works until a planner is scanning at speed.

Add a small hand-drawn set --- eight to ten glyphs, 16px, single-weight strokes derived from the fan's geometry rather than pulled from a generic icon font: flagged, watched, overridden, awaiting forecast, stale data, decided, skipped, escalated. Drawing them from the existing motif keeps them from reading as a library import, and eight is few enough to draw properly.

Rule: an icon never appears alone. Every glyph is paired with a word or a number, because an icon-only status is a glossary the user has to memorise.

## Motion

Current policy --- 120ms feedback, 180ms panel, nothing on load or while data is read, zeroed under reduced motion --- is correct and should not be loosened. Two additions only:

- Card transition on `J`/`K`: 120ms directional, keyboard-triggered only.
- The cost-curve mark moving as an override value is typed: 120ms, because it makes a consequence visible rather than decorating a state change.

Nothing else. In particular, the queue must not animate when an item is removed --- a planner clearing 24 cards would see 24 animations.

\newpage

# Interaction and keyboard

Existing bindings --- `J`/`K`, `A`/`Enter`, `C`, `S`, `W`, `O`, `?`, `Cmd-K` --- are well chosen, and the reasoning that review letters should be sayable rather than mirroring spreadsheet convention is right. Add five:

| Key | Action |
|---|---|
| `Cmd-Z` | Undo last decision (works anywhere in Today) |
| `Esc` | Close drawer, dismiss digest, cancel an edit |
| `G` | Group action, when a batch is offered |
| `P` | Pin to watchlist |
| `1`–`3` | Density mode |

**One conflict to resolve.** Single-letter bindings collide with screen-reader browse mode, where `J` and `K` are navigation keys the reader owns. Implement an explicit application-mode region around the card stack --- `role="application"` scoped tightly to the stack, never to the page --- and announce on entry: "Decision queue. Press J and K to move between decisions, A to approve." Outside that region, browse mode is untouched.

# States

Six states per surface, specified rather than discovered.

| State | Today | Products |
|---|---|---|
| **Loading** | Card skeleton with the chart area reserved; no spinner, no shimmer | List skeleton, detail blank |
| **Empty (success)** | The summary reward described in 4.8 | "No products match. `Clear filters`" |
| **Empty (first run)** | The four-decision walkthrough | "No products yet. `Add one`" |
| **Degraded** | Status line naming affected count and `Show me which` | Stale badge on affected rows |
| **Error** | "Could not load the queue. `Try again`" plus what was last seen | Same pattern |
| **Offline** | Decisions continue as drafts; banner: "You are offline. Decisions are saved here until you reconnect." | Read-only |

Two rules for the copy, following the existing voice. Errors do not apologise and are never vague about what happened. An empty screen is an invitation to act, not a mood.

# Accessibility additions

The parallel ARIA table beside the canvas grid is the right pattern and is already built. Extend it.

- **The fan chart needs a text equivalent.** A canvas is invisible to assistive technology, and the chart is the primary evidence on every card. Provide a described summary: *"Forecast for the next 28 days: middle estimate 138 units, likely range 96 to 190, widening after day 14. Last week's forecast was 177."*
- **The cost curve needs one too**, per section 3.1.
- **Live region on card advance**, announcing position and identity: "Decision 5 of 24. Whole milk 2 litre, store CA underscore 1."
- **The application-mode boundary** from section 8.
- **Density modes must not break the ARIA mirror** --- compact mode changes what is painted, not what is announced.
- **Colour is never the sole encoding.** The palette assigns one meaning per hue, which is necessary but not sufficient: risk and excess must also differ by glyph or by word, since roughly one user in twelve cannot reliably separate `#E0523C` from `#14A99A`.

The existing contrast finding --- axis labels at 3.87:1 moved from tertiary to secondary text, with the token left unrevalued because it is read by name in dozens of non-chart places --- is exactly the right way to handle this class of problem. Apply the same reasoning to any new token.

\newpage

# Implementation plan

Ordered by value delivered per unit of risk. Each item names what it touches, because the token contract makes blast radius the real cost.

## Phase 1 --- structural, low risk

| Change | Touches | Note |
|---|---|---|
| Sidebar to 3 + Evidence | Nav component, routes | Pure IA; no view rewritten, only regrouped |
| Vocabulary sweep | `plain.js` | The file already exists for exactly this purpose |
| Cycle ribbon | New component, all Work views | Reads run timestamp; cutoff configurable |
| Card retiering | `review.js`, card component | Move six figures into the drawer |
| Drawer (rung 3 and 4) | New component | Absorbs most of what the card sheds |

Phase 1 alone resolves the stated brief --- background detail hidden, planner surface clarified --- without touching a renderer or a token.

## Phase 2 --- the signature and the safety net

| Change | Touches | Note |
|---|---|---|
| Cost curve glyph | New canvas renderer, 3 new tokens, `util.js` cache list | Cache per data key exactly as the micro-fan does |
| Decision log with undo | `review.js`, sticky bar, draft set | Draft set already holds the data |
| Override reason codes | Card, spreadsheet, export schema | Pipeline already expects them |
| Override guardrail and impact | Card, shared estimator | Must quote the existing estimator, not a third one |
| Batch grouping | `review.js`, queue builder | Grouping rules in `config.py` |

The cost-curve renderer is the only item here that can miss the frame budget. Apply the lesson already learned from the micro-fan: cache each rendered glyph to an offscreen canvas keyed by its data, and degrade to the two marks alone during scroll. Re-run the paint measurement before merging --- the budget is 16ms and the current median is 4.2ms cold, so there is room, but not unlimited room.

## Phase 3 --- design system

| Change | Touches | Note |
|---|---|---|
| Accent split | `tokens.css`, 38 surfaces | Values only; no structural change. Re-run the 34 contrast checks |
| Hero figure face | `tokens.css`, figure classes | Verify `tabular-nums` support first |
| Icon set | New assets, status components | Eight to ten glyphs, drawn not imported |
| Density modes | `review.js`, tokens | Compact keeps the cost curve |
| Card transition | `review.js` | Keyboard only, reduced-motion zeroed |

## Phase 4 --- reach

Track record panel, first-run walkthrough, command palette actions, watchlist, since-you-were-last-here digest, mobile approvals, notification digest.

## What will break, and how to avoid it

- **Token renames are fatal.** 22 tokens are resolved by name at paint time. Every change in this report is an addition. Keep it that way.
- **New tokens read by canvases must join the `util.js` cache list in the same commit**, or the first paint after a theme flip resolves them empty --- the same class as the defect already recorded where a theme flip left elements behind.
- **The CSS grid template row rule.** The grid collapsed to zero height three times because a child was added without a matching template row. The drawer, the cycle ribbon and the batch banner are all new children of existing grids. Add the template row in the same commit, and add a smoke assertion on non-zero canvas height.
- **Three surfaces now quote the margin estimator** --- sticky bar, spreadsheet, and now the override preview. Extract it to one function before adding the third, not after.
- **The queue must stay a fixed cohort.** Batch grouping partitions the cohort chosen at load; it must not re-slice it, or the growing-denominator defect returns in a new costume.

# What not to change

Explicitly, because a redesign report that does not say what is already right invites damage.

- **The fan identity, and its exclusions.** Sidebar lockup, tab, the two "nothing here yet" empty states, one cone wash. Not on the completed-work state, where a tick is correct because a fan there would say the opposite of what happened.
- **The canvas grid above ten thousand cells**, and the ARIA mirror beside it.
- **The two-backend query router**, and the decision to follow the measurement rather than the specification's claim. Choosing JavaScript for rollups because a ten-bucket hash beats a query's fixed cost is the most intellectually honest engineering decision in the document.
- **Mono in the grid.** Tabular by construction beats a font feature surviving every code path.
- **"Not yet --- needs a forecast first."** Refusing to invent an order-up-to level for a product with no predictive distribution, after an earlier version inherited a peer's level, is the product's thesis defended at the smallest scale.
- **Confidence wording calibrated to this assortment's terciles**, not to guessed cutoffs.
- **Nine-step spacing scale, no half-steps.**
- **Nothing animating on load.**
- **The honesty of the evidence surfaces.** Reporting that the full system loses to its own best single model on point accuracy, that combination helps at only two of eight origins, and that lower-tail calibration is poor, is the reason the rest of the document can be believed. Moving those views into `Evidence` must not soften any of it.

# Acceptance tests

Testable, so the redesign can be verified rather than admired.

| # | Test | Pass condition |
|---|---|---|
| 1 | New planner, no training, given the queue | Approves a correct first decision within 90 seconds |
| 2 | Card scan | Order quantity and money at risk located in under 3 seconds |
| 3 | Method leakage | Zero occurrences of MinT, conformal, quantile, WSPL, Croston, Tweedie or fractile on any Work surface, verified by a string check in CI |
| 4 | Disclosure depth | Any method detail reachable in exactly two deliberate actions from a card |
| 5 | Cost curve comprehension | After five cards, planner can state without prompting why a durable's order sits above its forecast and milk's sits below |
| 6 | Undo | Any decision in the current session reversible in under 5 seconds |
| 7 | Degraded mode | Stale feed produces a visible status line and an accurate affected count before the planner opens a single card |
| 8 | Frame budget | Card list with cost curves holds median paint under 16ms and zero frames over 32ms |
| 9 | Contrast | 34 checks plus new tokens, both themes, zero failures |
| 10 | Screen reader | Full queue workable by keyboard with the fan chart and cost curve described |
| 11 | Estimator agreement | Sticky bar, spreadsheet and override preview return identical figures for identical inputs, asserted in CI |
| 12 | Empty state | Completed-work summary states decisions made, exposure covered, and count handled automatically |

# Summary

The interface does not need rebuilding. It needs one structural change and one new idea.

**The structural change:** nine sidebar items serving four audiences become three plus an evidence area, and every card obeys a four-rung disclosure ladder in which method vocabulary is permitted only on the fourth rung. That alone resolves the brief.

**The new idea:** the cost curve. The system's whole argument is that two identical forecasts deserve different orders, and that argument currently lives in a page a planner reads once. Put it on every card as a shape, and the planner absorbs it without ever meeting the word "fractile."

**The largest omission to fix:** the product has no sense of time. A cycle ribbon, an item age, and a since-you-were-last-here digest cost three small components and change the tool from a list into a working week.

**The one thing to finish:** the accent split was diagnosed correctly, refactored correctly, and then given an identical value. Move it.
