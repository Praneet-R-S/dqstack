---
title: "DQ-Stack interface --- redesign report v2"
subtitle: "The working sidebar, the decision card, and the full component system"
author: "Design review, version 2.0 --- supersedes v1.0"
date: "August 2026"
documentclass: article
fontsize: 10pt
geometry: "margin=2.1cm"
mainfont: "Carlito"
monofont: "DejaVu Sans Mono"
numbersections: true
toc: true
toc-depth: 2
colorlinks: true
linkcolor: "RoyalBlue"
---

\newpage

# The principle that resolves everything

Version 1 of this report argued for cutting the sidebar from nine items to four. That was right about the *problem* and wrong about the *cure*, and this version corrects it.

The fault was never that the sidebar had too many things in it. It was that the things in it belonged to the application rather than to the planner. `How it works`, `Ordering decisions`, `Glossary` and the SQL console are the product explaining itself. A planner scans past them several hundred times a month and uses none of them.

So the rule is not *fewer items*. It is:

> **The sidebar holds the planner's work, not the application's features.**

Under that rule the sidebar gets substantially denser than it is today --- eight distinct blocks instead of three groups --- while the *navigation* inside it shrinks to three destinations. Every one of those blocks carries a live number, which is what separates an instrument from a menu. A sidebar where nothing changes between visits is a list of links. A sidebar where six numbers moved overnight is the first thing a planner reads each morning.

This report specifies that sidebar in full, then the decision card, then the component system underneath both.

# The working sidebar

Width 288px, matching the existing rail token. Collapses to a 64px icon rail. Eight blocks, top to bottom.

## Block 1 --- lockup and cycle chip

The fan mark, the product name, and a single chip carrying the most urgent fact in the application: how long until orders close.

```
  ◺ DQ-Stack                              [ 2d left ]
```

The chip changes state and colour: `open` neutral, `closing soon` on attention when under 24 hours, `closed` on tertiary text with the next opening time. If the deployment has no real cutoff, it carries the forecast vintage instead --- `06:12 today` --- which is still orientation the current interface does not provide.

## Block 2 --- the cycle bar

```
  Week 33                              closes Thu 17:00
  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░░░░░░
```

A 3px progress bar showing position within the ordering week. This is the frame the entire product was missing. It costs one component and it converts a list of cards into a working week with a deadline.

Hover reveals the cycle detail: when forecasts were generated, when orders close, when the next cycle opens, and which feeds have landed.

## Block 3 --- navigation

Three destinations. Not four, not nine.

| Item | Badge |
|---|---|
| Today | Count of undecided items, on accent |
| Products | None |
| Spreadsheet | Pending uncommitted edits, on override amber |

The pending-edit badge is important and currently absent. A planner with 40 uncommitted spreadsheet edits should carry that number with them into every other view, not rediscover it on return.

## Block 4 --- your queue

The largest addition, and the one that makes the sidebar useful rather than decorative. Today's cohort, broken down by cause, each row a filter.

```
  YOUR QUEUE
  ● Running out                          6 · 4.1k
  ● Overstocked                          4 · 2.2k
  ● Promotion ended                     12 · 9.4k
  ● Data problems                        3
  ● Skipped earlier                      6
```

Every row carries a count and, where money applies, exposure. Clicking filters the queue; the active filter is the only row with a raised background. A dot in the semantic colour of the cause gives the planner a pre-attentive read on the shape of their day before they have read a single word.

**Why exposure and not just count.** A planner deciding where to start needs to know that three data problems may matter more than twelve promotion endings. Counts alone hide that, and counts alone are what every incumbent shows.

**Rows are generated, not fixed.** The list comes from the same headline-reason taxonomy the cards use, ordered by exposure, capped at six with the remainder collapsed into `Everything else`.

## Block 5 --- watching

Pinned products, with live status.

```
  WATCHING
  ★ Greek yoghurt 500g                       +41%
  ★ Sourdough loaf                         steady
  ★ Free-range eggs 12                       −8%
    See all 14
```

Every planner has a handful of products they personally distrust. Pinning costs one control on a product row and buys the strongest retention feature in the product, because a watchlist is the first thing a returning user checks.

Watched items always surface in Today regardless of exposure ranking, marked as *watched* rather than *flagged*, so the planner understands why they are seeing something that is not otherwise notable.

## Block 6 --- saved views

Named filter combinations with live counts.

```
  SAVED VIEWS
  ⚑ Fresh, CA stores                          18
  ⚑ Thin margin                                7
  ⚑ New products, first 8 weeks                4
```

Saved views are how a planner encodes their own mental model of their range. Give them a name field, a count, and a keyboard shortcut, and they become the planner's real navigation --- which is why they belong in the sidebar and `How it works` does not.

## Block 7 --- scope

Which stores and categories this planner covers, with a quick switcher. In a single-planner deployment this collapses to a one-line summary. In a real one it is how a planner moves between the four stores they own without rebuilding a filter each time.

## Block 8 --- session

```
  Decided today
  11  of 24 · 8.2k covered
  ↩ Undo last
```

Progress, exposure covered, and an always-available undo. The session block is the answer to the largest safety gap in the current product: a planner who approves thirty items and then learns the price feed was stale currently has no recourse.

## The footer

Below a divider, in tertiary text: system condition when degraded, the `Evidence` link, theme toggle, account.

`Evidence` sits here deliberately. It is a real destination --- accuracy, calibration, economics, method, SQL --- and analysts and examiners genuinely need it. Putting it in the footer says plainly that it is not part of the daily loop without hiding it or apologising for it.

## Collapsed rail

At 64px: the mark, three navigation icons with their badges, the queue dots as a vertical stack of coloured markers with counts on hover, and the session count. Everything else is unavailable until expanded, which is the correct trade --- a collapsed rail is for a planner who wants the canvas, not for one who wants their work state.

## Sidebar specification

| Property | Value |
|---|---|
| Expanded width | 288px |
| Collapsed width | 64px |
| Section label | 10px, 0.06em tracking, uppercase, tertiary |
| Row height | 30px default, 26px compact |
| Row padding | 5px vertical, 8px horizontal |
| Row text | 12.5px navigation, 12px lists |
| Numbers | JetBrains Mono 11px, tertiary unless semantic |
| Status dot | 5px, semantic colour, 8px from label |
| Active row | `--surface-raised` background, radius 5px |
| Block gap | 13px |
| Divider | 0.5px, `--border`, full bleed inside 14px padding |
| Badge | 11px mono, radius 9px, accent fill for counts, amber for pending |

\newpage

# The decision card

## Anatomy

Four tiers. Two numbers visible where there were eight.

| Tier | Content | Height |
|---|---|---|
| 0 | Exposure, age, position in queue | 40px |
| 1 | Identity line, then `Stock up to` and the cost curve | 96px |
| 2 | One-sentence reason, then the fan chart bled to card edges | 140px |
| 3 | Approve / Change / Skip, and `Why this number? (W)` | 46px |

Everything else --- forecast and range, change since last week, service target, data quality, confidence, drivers, peer comparison --- lives in the drawer behind `W`.

## The cost curve

The one new mark in the system, and the reason to build this redesign rather than merely tidy the current one.

The system's sharpest claim is that two products with an identical forecast deserve different orders, because mistakes cost different amounts. That claim currently lives in a destination a planner reads once. As a 112 by 46 glyph beside the order figure, it is on every card, all the time.

**What it draws.** Horizontal axis is order quantity. The curve is expected cost of being wrong at that quantity --- steep on the expensive side, shallow on the cheap side. Two marks: the median forecast (dashed, tertiary) and the recommended order (solid, forecast blue).

For fresh milk with a 17% margin and five days of shelf life, the curve is steep on the right and the order sits below the median. For a durable at 70% margin, it is steep on the left and the order sits well above. After a week a planner reads the shape without reading a number, and has absorbed the entire ordering thesis without meeting the phrase *critical fractile*.

**Rules.** Static, never animated except when an override value is being typed. Cached to an offscreen canvas keyed by its data, exactly as the micro-fan already is. Absent when the product has no forecast, where the existing "Not yet --- needs a forecast first" wording holds the space. Never on the sidebar, never in an empty state --- the fan owns those.

**Accessible label.** "Running out costs about four times as much as over-ordering, so the recommended order sits above the middle estimate."

## The drawer

Slides over the right third in 180ms. The card does not move.

```
  Why 140?

  Next week          138 units       likely 96 – 190
  Change             −22%            vs last week
  Service target     50%             fresh, thin margin
  Data quality       good            no gaps, no empty-shelf days
  Confidence         high            ranges held up 9 times in 10

  WHAT MOVED IT
  Promotion ended                          −31 units
  Day of week (Tue–Thu heavy)               +6 units
  Seasonal drift                            −4 units

  THE SAME PRODUCT ELSEWHERE
  [ p10–p90 range, median notch, this store marked ]

  ▸ How this was produced
```

`How this was produced` is rung four and the only place in any Work surface where method vocabulary is permitted. Collapsed by default. Inside it: which methods contributed and their weights, which stages ran, the forecast vintage, and a link into `Evidence`.

## Batch grouping

When three or more cards in the cohort share a headline reason, a grouped action appears beneath the card.

> **11 more ended a promotion** · USD 9,400 · `Review as group`

`Review as group` opens a compact list --- one row each with order quantity, exposure, and a sparkline --- with per-row exclusion. `Approve all` requires a confirm naming the count and combined exposure.

**Grouping rules.** Same headline reason, same direction of change, and no member above a configurable exposure ceiling --- high-value items always get their own card. Critically, grouping *partitions the cohort chosen at load*; it must never re-slice it, or the growing-denominator defect returns wearing a new costume.

## Changing a number

Three additions to the existing flow.

**Guardrail.** If the entered value falls outside the predictive distribution, say so --- do not block.

> 240 is above the highest outcome the model considers likely (its 99th percentile is 186). Save anyway?

**Reason code, required.** The pipeline already treats override reason codes as a signal that the model is failing on a product; the interface has to capture one. Six options plus free text: `Promotion I know about`, `Local event`, `Supplier issue`, `Store request`, `Model looks wrong`, `Other`. Free text required only after `Other`. Keep the list short --- a twenty-item taxonomy gets one option picked for everything.

**Impact before commit.** The margin effect against the recommendation, quoting the same estimator the sticky bar and spreadsheet use. Three surfaces now share it; extract it to one function before adding the third, not after.

As the number changes, the mark on the cost curve moves with it. That is the one piece of motion worth adding, because it makes the consequence visible in the product's own geometry.

\newpage

# Everything else on Today

## Since you were last here

First visit of a day only, above the first card, dismissed by acting or by `Esc`.

> **Since Friday** --- 3 products moved into risk · 1 sales feed was late and has caught up · Greek yoghurt 500g changed by 41% · 6 items you skipped are still waiting.

Each clause filters the queue. One small component, and it answers the question every planner has on Monday morning.

## System condition

Ambient, and silent when healthy. A persistent green tick is noise.

> Sales data for 340 products has not updated since Tuesday. Their forecasts use older data --- 12 items in your queue are affected. `Show me which`

> Forecast ranges have been running narrow for eight days. Treat the likely range as optimistic until this clears.

Both state what happened, what it means for the planner's work, and offer the next action. Neither apologises, neither says "error", neither is vague.

## The empty state

The most important emotional moment in the product. Make it a summary, not a confirmation.

> **Today's queue is clear.**
>
> You decided 24 items covering USD 18,400 of exposure. You changed 3 forecasts. The system handled 1,190 other products without needing you.
>
> `Export decisions as CSV` · `See what you changed`

The last sentence is the one that matters. A tool whose success is invisible by construction has to state the scale of what it handled, or the planner has no way to feel it working.

## Density

| Mode | Card height | Chart | Visible at 1440px |
|---|---|---|---|
| Focused | 420px | full fan | 2 |
| Default | 340px | full fan | 3 |
| Compact | 180px | sparkline, cost curve retained | 6 |

Compact is not degraded --- it is how a confident planner works a long queue. The cost curve survives into it precisely because it is the fastest signal on the card.

## Card transitions

`J` and `K` currently move with no cue, and a planner who cannot feel that they moved will lose their place in a fast run. Add a 120ms directional shift: outgoing card translating 8px and fading, incoming entering from the opposite side. Keyboard-triggered only, zeroed under reduced motion, never on first paint.
\newpage

# Products

The list-beside-detail layout is right and stays. The sort-matched trailing figure --- where the trailing number changes to match the active sort, because sorting by money while showing units looks like a broken sort --- is a genuinely good detail and should not be touched. Six additions.

| Addition | Why |
|---|---|
| Watchlist star on every row | Feeds sidebar block 5; the cheapest retention feature available |
| Peer range in the list row | A 60px p10--p90 bar with median notch. "This store is the outlier" is the most useful thing a planner learns while scanning, and it is currently buried in the detail pane |
| Decided-this-cycle marker | Without it, Today and Products are two views of the same items with no shared memory |
| Notes | Free text per product, visible to the team, surfaced on the card. Planners keep this knowledge in their heads today |
| History timeline | Forecast, override and outcome per cycle, as a strip. Answers "has this been wrong before?" |
| Alert rules | "Tell me when this moves more than 20%." Planner-defined, feeding the queue |

Alert rules deserve emphasis: they let a planner encode their own judgement into the system, which is the strongest possible answer to the trust problem. A planner who has taught the tool three rules is a planner who has stopped fighting it.

# The spreadsheet

Very little. It is well made and correctly demoted from default, and the micro-fan defect record shows it has survived real scrutiny.

- **Reason-code column.** If overrides need reason codes on a card they need them in bulk, or every override routes through the spreadsheet to dodge the field. Fill-down applies to it like any column.
- **Persistent pending count**, carried into the sidebar badge.
- **Column presets** matching saved views, so the two surfaces share one mental model.

# Evidence

Everything removed from the planner's daily view lands here, unchanged in substance. Five tabs.

| Tab | Contents |
|---|---|
| Accuracy | Leaderboard, WSPL and WRMSSE, paired inference, the honest statement that the full system loses to its own best single model on point accuracy |
| Calibration | Coverage table before and after conformal, the poor lower tail stated plainly |
| Economics | The ledger, the configuration comparison, the accuracy-versus-economics rank correlation |
| Method | The eight stages with this run's numbers, the reconciliation trade, censoring precision and recall |
| Query | The SQL console |

**Nothing here gets softened in the move.** Reporting that combination helps at only two of eight origins, and that lower-tail calibration is poor, is the reason the rest of the document can be believed. Relocating these views must not touch their honesty.

\newpage

# Component library

Every reusable piece, with measurements, so the build does not re-decide them per screen.

## Primitives

| Component | Spec |
|---|---|
| Card | `--surface`, 0.5px border, 1px lit top edge, radius 10px |
| Section label | 10px, 0.06em, uppercase, tertiary |
| Metric block | 11px tertiary label above, figure below at the appropriate `--fig-*` |
| Status dot | 5px circle, semantic colour, always paired with a word |
| Badge | 11px mono, radius 9px, 1px by 6px padding |
| Chip | 11px, radius 9px, 2px by 6px, tinted background from the same hue family |
| Divider | 0.5px `--border` |
| Drawer | Right third, 180ms slide, `--surface` with 0.5px left border |
| Sticky bar | 52px, `--surface-raised`, top border, always visible during a session |
| Inline definition | Dotted 1px underline, tertiary; hover or tap reveals a 220px popover |

## Data marks

| Mark | Size | Notes |
|---|---|---|
| Fan chart | Full card width, 140px | Three nested bands at 50 / 80 / 98, observed solid, prior ghosted dashed, origin rule |
| Micro-fan | 40 by 14 | Grid cells. Already built. Cache per data key |
| Cost curve | 112 by 46 | New. Same caching discipline |
| Sparkline | 60 by 16 | Compact density and group review rows |
| Peer range | 60 by 12 | p10--p90 bar with median notch and this-store marker |
| Confidence gutter | 3px vertical | Row left edge, data-health colour |

## Buttons

Exactly one filled control per view. `Approve` is filled; `Change` and `Skip` are text. This rule is already in the product and is what makes a card answerable in one click without reading all of it --- protect it.

| Variant | Spec |
|---|---|
| Primary | `--ui-accent` fill, white text, 12px medium, 6px by 15px, radius 5px |
| Text | `--text-primary`, no fill, 6px by 9px |
| Quiet | `--text-secondary`, no fill |
| Destructive | Text style with `--risk` colour; never filled |

\newpage

# Design system changes

## Finish the accent split

The reference diagnoses this precisely: `--series-forecast` carried both "predicted quantity" and "clickable", a hue meaning two unrelated things cannot be learned, so `--ui-accent` was introduced across 38 surfaces --- and then given an identical value so no pixel moved.

That was the correct refactor and the right sequencing. Finish it.

| Token | Now | Proposed |
|---|---|---|
| `--series-forecast` | `#4C6FFF` / `#3050E8` | unchanged |
| `--ui-accent` | `#4C6FFF` / `#3050E8` | `#7A5CF0` / `#5B3FD4` |

Violet sits far enough from the forecast blue to separate at 11px, and does not collide with risk `#E0523C`, attention `#C99A2E`, override `#E09B15` or excess `#14A99A`. Run the existing 34-check contrast harness before adopting --- it is a one-command answer, not a judgement call.

**The stronger option.** Give links and secondary controls no hue at all --- ink colour, underline on hover, weight for emphasis --- and spend the violet solely on the primary action, the focus ring, and active navigation. In an application where nine hues already carry data meaning, spending a tenth on "you may click this" is spending it badly. This is the more disciplined position and the one I would take.

## Hero figures should not be monospace

Monospace for every numeral is the right instinct in the wrong scope. Tabular by construction beats a font feature surviving every code path --- decisive in the grid.

But `--fig-xl` at 40px is *money at risk*, and there is no column for it to align to. At that size a monospace face reads as a system readout rather than a quantity of money, and JetBrains Mono is wide, so the largest figure in the product is the one that fits least comfortably.

| Role | Now | Proposed |
|---|---|---|
| `--fig-xl` 40px | Mono | Instrument Sans, tabular figures |
| `--fig-lg` 28px | Mono | Instrument Sans, tabular figures |
| `--fig-md` 20px | Mono | unchanged --- often in a column |
| Grid, sidebar counts, any column | Mono | unchanged |

Verify Instrument Sans exposes `tabular-nums lining-nums`. If not: keep mono at hero sizes with tracking tightened to `-0.04em`, or adopt a data face for the figure roles only.

The existing split of the figure class into numeral and word variants --- so "Good" at 20px mono does not read as a status code --- is exactly right. This extends it one step.

## New tokens

Additions only. 22 tokens are load-bearing and resolved by name at paint time; a rename is a silent rendering failure in an unrelated file.

```css
--cost-underage        --age-fresh          --cycle-open
--cost-overage         --age-stale          --cycle-closing
--cost-mark                                 --cycle-closed

--state-degraded       --sidebar-active     --density-row-today
--queue-dot-size       --badge-accent       --badge-pending
```

Every new token a canvas reads must join the `util.js` cache list in the same commit, or the first paint after a theme flip resolves it empty --- the same defect class already recorded.

## Icons

The reference mentions no icon set, which means text and colour carry all status signalling. That works until a planner is scanning at speed.

Ten glyphs, 16px, single-weight strokes drawn from the fan's geometry rather than pulled from a library: flagged, watched, overridden, awaiting forecast, stale data, decided, skipped, escalated, grouped, pinned. Drawing them from the existing motif keeps them from reading as an import, and ten is few enough to draw properly.

**Rule: an icon never appears alone.** Every glyph pairs with a word or a number. An icon-only status is a glossary the user must memorise.

## Motion

Current policy --- 120ms feedback, 180ms panel, nothing on load or during reads, zeroed under reduced motion --- is correct and must not be loosened. Three additions, no more:

- Card transition on `J`/`K`: 120ms directional, keyboard only.
- Cost-curve mark tracking a typed override: 120ms.
- Drawer: 180ms, already within policy.

Nothing else. In particular the queue must not animate on item removal --- clearing 24 cards would produce 24 animations.

\newpage

# Keyboard

Existing bindings are well chosen, and the reasoning that review letters should be sayable rather than mirror spreadsheet convention is right. Add eight.

| Key | Action | Key | Action |
|---|---|---|---|
| `Cmd-Z` | Undo last decision | `G` | Group action, when offered |
| `Esc` | Close drawer, dismiss, cancel | `P` | Pin to watchlist |
| `1`–`3` | Density mode | `F` | Focus the queue filter |
| `[` `]` | Previous / next saved view | `\` | Collapse sidebar |

**The screen-reader conflict.** Single letters collide with browse mode, where `J` and `K` belong to the reader. Scope `role="application"` tightly to the card stack --- never the page --- and announce on entry: "Decision queue. Press J and K to move between decisions, A to approve." Outside that region browse mode is untouched.

# Command palette

`Cmd-K` searches today. Make it act.

```
  > approve all promotion-ended          12 items · 9.4k
  > filter to store CA_1
  > jump to Greek yoghurt 500g
  > save this view as…
  > export today's decisions
  > show what I skipped
  > switch to compact density
```

Actions first when the query matches a verb, search results after. This is where a keyboard-fluent planner lives, and it costs far less than building equivalent controls into every view.

# States

Six per surface, specified rather than discovered.

| State | Today | Products |
|---|---|---|
| Loading | Card skeleton with chart area reserved; no spinner, no shimmer | List skeleton, blank detail |
| Empty, success | The summary reward | "No products match. `Clear filters`" |
| Empty, first run | Four-decision walkthrough | "No products yet. `Add one`" |
| Degraded | Status line with affected count and `Show me which` | Stale badge on affected rows |
| Error | "Could not load the queue. `Try again`", plus what was last seen | Same pattern |
| Offline | Decisions continue as drafts; "You are offline. Decisions are saved here until you reconnect." | Read-only |

# Copy deck

The words are design material. Sentence case, active voice, verb-first controls, no apologies in errors, empty screens as invitations.

| Situation | Write | Not |
|---|---|---|
| Primary action | Approve | Submit |
| After approving | Approved | Successfully approved |
| No forecast yet | Not yet --- needs a forecast first | N/A |
| Stale feed | Sales data for 340 products has not updated since Tuesday | Data sync error |
| Override guardrail | 240 is above the highest outcome the model considers likely | Value out of range |
| Queue cleared | Today's queue is clear | All done! |
| Confidence | Ranges held up 9 times in 10 | Calibration: 0.90 |
| Sell pattern | Sells in bursts | Lumpy (ADI 2.1, CV² 0.8) |
| Data health | No gaps, no empty-shelf days | Health score 0.94 |
| Change reason | Promotion ended Tuesday | Promotional uplift decay detected |

The existing sell-pattern wording --- steadily, in bursts, occasionally, too new to tell --- is already the model. Extend that instinct to every remaining term. The full translation table from report v1 section 2.4 still applies unchanged.

\newpage

# Accessibility

The ARIA table beside the canvas grid is the right pattern and is already built. Extend it.

- **The fan chart needs a text equivalent.** It is the primary evidence on every card and it is a canvas. "Forecast for the next 28 days: middle estimate 138 units, likely range 96 to 190, widening after day 14. Last week's forecast was 177."
- **The cost curve needs one**, per section 3.2.
- **Live region on card advance**: "Decision 5 of 24. Whole milk 2 litre, store CA underscore 1."
- **The application-mode boundary** from section 9.
- **Density modes change what is painted, not what is announced.**
- **Colour is never sole encoding.** One meaning per hue is necessary but not sufficient; risk and excess must also differ by glyph or word, since roughly one user in twelve cannot separate `#E0523C` from `#14A99A`.

The existing contrast finding --- axis labels moved from tertiary to secondary because 3.87:1 failed on the surfaces charts actually sit on, with the token left unrevalued because it is read by name in dozens of non-chart places --- is exactly the right way to handle this class of problem. Apply the same reasoning to every new token.

# Build order

## Phase 1 --- the sidebar and the frame

| Change | Touches |
|---|---|
| Sidebar rebuild, eight blocks | Nav component, new sidebar state store |
| Cycle bar and chip | New component, reads run timestamp |
| Queue breakdown rows | Queue builder, reason taxonomy already exists |
| Session block with undo | Draft set already holds the data |
| Navigation to three, Evidence to footer | Routes only |

Phase 1 delivers the visible transformation and touches no renderer and no token.

## Phase 2 --- the card

| Change | Touches |
|---|---|
| Card retiering | `review.js`, card component |
| Drawer, rungs 3 and 4 | New component |
| Cost curve renderer | New canvas renderer, 3 tokens, `util.js` cache list |
| Override reason codes, guardrail, impact | Card, spreadsheet, shared estimator |
| Batch grouping | `review.js`, `config.py` rules |
| Vocabulary sweep | `plain.js` |

## Phase 3 --- design system

Accent split, hero figure face, icon set, density modes, card transition.

## Phase 4 --- reach

Watchlist, saved views, alert rules, notes, history timeline, track record panel, first-run walkthrough, command palette actions, since-you-were-last-here, mobile approvals, notification digest.

## What will break

- **Token renames are fatal.** Every change here is an addition. Keep it that way.
- **New canvas tokens must join `util.js` in the same commit**, or the first theme flip resolves them empty.
- **CSS grid template rows.** The grid collapsed to zero height three times because a child was added without a matching template row. The drawer, cycle bar, queue block, batch banner and session block are all new grid children. Add the template row in the same commit and assert non-zero canvas height in the smoke run.
- **The estimator now has three callers.** Extract before the third, not after.
- **The cohort must stay fixed at load.** Grouping partitions it; it must not re-slice it.
- **The cost curve is the only frame-budget risk.** Apply the micro-fan lesson: cache per data key, degrade to the two marks during scroll, re-measure before merging. You have room at 4.2ms median against a 16ms budget --- room, not licence.

# What not to change

- The fan identity and its exclusions --- including the tick on the completed-work state, where a fan would say the opposite of what happened.
- The canvas grid above ten thousand cells, and the ARIA mirror beside it.
- The two-backend query router, and the decision to follow the measurement rather than the specification's claim.
- Mono in the grid and in every aligned column.
- "Not yet --- needs a forecast first."
- Confidence wording calibrated to this assortment's terciles.
- The nine-step spacing scale, no half-steps.
- Nothing animating on load.
- One filled button per view.
- The honesty of every evidence surface.

# Acceptance tests

| # | Test | Pass |
|---|---|---|
| 1 | Untrained planner given the queue | Correct first decision inside 90 seconds |
| 2 | Card scan | Order quantity and exposure found in under 3 seconds |
| 3 | Method leakage | Zero occurrences of MinT, conformal, quantile, WSPL, Croston, Tweedie, fractile on any Work surface --- CI string check |
| 4 | Disclosure depth | Any method detail reachable in exactly two deliberate actions |
| 5 | Cost curve | After five cards, planner states unprompted why a durable's order sits above its forecast and milk's below |
| 6 | Sidebar utility | Planner uses a queue filter or saved view within their first session, unprompted |
| 7 | Undo | Any decision this session reversed in under 5 seconds |
| 8 | Degraded mode | Stale feed produces a visible line and an accurate affected count before a single card is opened |
| 9 | Frame budget | Card list with cost curves: median paint under 16ms, zero frames over 32ms |
| 10 | Contrast | 34 checks plus new tokens, both themes, zero failures |
| 11 | Screen reader | Full queue workable by keyboard with fan and cost curve described |
| 12 | Estimator agreement | Sticky bar, spreadsheet and override preview identical for identical inputs --- asserted in CI |
| 13 | Cohort stability | Queue denominator never increases during a session |

# Summary

**The sidebar is the redesign.** Eight blocks of the planner's own work --- cycle, three destinations, queue breakdown with exposure, watchlist, saved views, scope, session with undo --- replacing a menu of application features. Every block carries a live number. That single change is what makes the product feel like an instrument rather than a viewer.

**The cost curve is the idea.** The system's argument is that identical forecasts deserve different orders. Put it on every card as a shape and the planner learns it in a week without meeting a single technical term.

**Time is the missing frame.** Cycle bar, item age, since-you-were-last-here. Three small components, and the tool stops being a list and becomes a working week.

**The accent split is the unfinished job.** Diagnosed correctly, refactored correctly, then given an identical value. Move it.
