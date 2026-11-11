# BRIEF: DQ-Stack landing page

**Self-authored under explicit creative delegation.** The user asked: "add a nice landing
page scroll effects with different images / it has to explain the product / make it the
best u have ever seen", after "complete everything". No interview was run; every answer
below is an authored decision, labelled as such. Evidence comes from the product
specification (DQ-Stack-Product-Specification.pdf), the built app, and its demo store.

## The eight topics

1. **Vibe (authored):** shop time, honest numbers, festival light, quiet confidence.
   References: a railway departure board (live numbers that change on their own); a
   cloth-bound bahi-khata ledger (every movement written down, nothing rubbed out); a
   panchang wall calendar in a kirana shop (the year arranged around festivals).
2. **Journey (user's words: "it has to explain the product"; sequence authored):** the
   store closes; the nightly guess and what it costs; every movement written down; the
   overnight run; the festival the forecast sees coming; the morning decisions; the
   team's day; why nothing is lost; how to start; questions; the order is ready.
3. **Energy (authored):** calm open, uneasy second beat, relief, quiet night, the
   loudest moment at the festival, then steady and practical, resolving calm.
4. **Feeling and the one moment (authored):** see the feeling curve and the peak below.
5. **The thing no site does (authored):** the page keeps shop time. The clock in the
   bar is the store's clock, and the page is one trading day, from closing at 22:00 to
   the order being ready at 06:00. The bespoke interaction: drag a stock target and
   watch, in rupees, what being wrong costs, for a product that keeps a year and one
   that keeps three days.
6. **Distance from premium minimal (authored):** editorial premium with real retail
   ephemera (receipts, shelf labels, a ledger, a wall calendar) beside the product's
   own screens. Not brutalist, not playful. Dense where the content is data.
7. **One world or scenes (authored):** distinct scenes, cut by the clock. Not a
   continuous camera flight.
8. **Assets:** no photography or footage exists and no generation key is set. The real
   assets are the product itself: screens captured at 2x from the demo store. The
   retail artefacts are rendered in HTML, CSS and canvas from real product data.

## Visual rules in force
The user's design skill (Geist only, the named type styles and spacing scale, flat
backgrounds from its dark list, no hyphens or em dashes in copy, one primary action,
tagline reveal section) and the scroll-craft hard rules. Where they conflict (gradient
text on the hero heading), the design skill's specific hero rule is followed.

## The journey
1. Recognition: the store at closing time and the question every owner asks.
2. Tension: the nightly guess; too little empties the shelf, too much expires.
3. Turn: stock is never typed over; every movement is written down and adds up.
4. Substance: the overnight run, step by step, ready by 06:00.
5. Peak: the festival nine days out, and the store getting ready for it.
6. Conviction: the tagline (exact stock and a better forecast feed each other).
7. Agency: the morning decisions; drag a stock target and see what being wrong costs.
8. Range: the team's day, role by role, with the real screens.
9. Reassurance: nothing is lost silently.
10. Ease: start with one store and a spreadsheet, three steps.
11. Questions.
12. Commitment: 06:00, the order is ready. One action.

## The feeling curve (written before the score)
| Act | Clock | Feeling | What causes it |
|---|---|---|---|
| 1 Closing | 22:00 | calm recognition | a shelf of real price labels, the day's screen, a receipt; "that is my shop" |
| 2 The guess | 22:04 | unease | lines assemble one by one naming the two ways an order goes wrong |
| 3 The ledger | 22:10 | relief | a scribbled stock figure wipes away into movements that add up |
| 4 Overnight | 01:00 | quiet anticipation | a dim departure board ticks through the nightly steps |
| 5 Festival (peak) | 01:03 | wonder | the forecast rises into Diwali, lamps light, deliveries land, the shelf holds |
| 6 Tagline | | conviction | the core sentence lights word by word |
| 7 Morning | 07:30 | agency | the visitor drags a stock target and sees the rupees move |
| 8 The day | 09:15 to 21:50 | belonging | the team's screens slide past, hour by hour |
| 9 Nothing lost | | reassurance | small proofs settle into place: offline queue, ledger, separation |
| 10 Start | | ease | three plain steps and the real setup checklist |
| 11 Questions | | clarity | short answers |
| 12 Ready | 06:00 | resolution | the clock reaches morning, the order is ready, one action |

No two adjacent acts share a feeling. Authored silence: the half screen of dim board
after the last nightly step (act 4) is deliberate quiet before the peak, not dead scroll.

## The peak
Act 5. What a visitor would say: "You scroll through the nine days before Diwali and
watch the store get itself ready." It has the largest scroll span on the page.

## The tell-someone sentence
"It's the site where you scroll through one night in a shop and watch it get ready for
Diwali on its own."

## Signature move
"Try the decision" (act 7): a draggable stock target over a real demand distribution.
Two products, one that keeps a year and one that keeps three days. The page computes
expected rupees lost to an empty shelf and to leftovers at every target, draws the
cost of being wrong, and marks where DQ-Stack would set the target. Keyboard and touch
operable. Coded in the page.

## Grammar
**Chronicle (one trading day).** Navigation is the shop clock; sequence is time of day;
the ending is the next morning. Chosen because the product is literally a 24 hour loop
(spec, "The daily cycle"). Filmic one-shot lost: there is no footage and a single flight
would hide the product. Editorial chapters lost: chapters have no reason to follow each
other here, time does. Gallery and catalogue lost: the product is a process, not a set
of objects. Working surface lost: a live app is one click away; the page must explain
first. Worldflight lost: the brief is distinct scenes, cut by the clock.

## Score
| Beat | Device | Why this one |
|---|---|---|
| Recognition | parallax, layered planes with pointer depth | the shop at closing has depth: shelf, screen, receipt |
| Tension | pin + kinetic lines | the frame holds while the problem is named line by line |
| Turn | reveal (clip-path wipe) + ledger | a wipe is a change of state; the scribble gives way to arithmetic |
| Substance | board (split flap) | a departure board is how a process reads at night |
| Peak | scrub (canvas, pinned, longest span) | the visitor's scroll is the calendar moving toward Diwali |
| Conviction | word by word reveal | the mandatory tagline moment |
| Agency | pointer (signature) | the page stops telling and lets them decide |
| Range | pan (horizontal on desktop) | lateral travel reads as a day passing and as options |
| Reassurance | deck (cards settle) | separate proofs, each its own object |
| Ease | static rise | nothing should move much when someone is deciding to start |
| Questions | accordion | answers on demand |
| Commitment | resolve (clock to 06:00, held) | the loop closes; the page stops and holds the action |

Device families: 11, none repeated adjacently, one scrub.

## Verification (2026-11-11)
Headless Chrome through the DevTools protocol (the skill's playwright harness was not
installed): every scene photographed at several scroll positions on desktop (1440x900),
phone (390x844, touch emulation) and reduced motion, plus 768, 1024, 1180, 1280 and 1366
wide heroes. No script errors, no horizontal overflow at any width. The decision card's
numbers were exercised in a browser (product switch, slider, "Use DQ-Stack's target").

Fixed after looking: an orphaned word in the hero headline (new headline); the receipt
cut off at rest; the shelf too dim to read as a shop; lamp glow drawn as hard discs; the
ledger's sticky note hidden by the wipe; the clock not updating on the last frame; in
reduced motion, the festival copy painted under the warm layer (flex order changes paint
order); the ledger table and night board cramped on phones; festival names colliding on
phones; the hero overlapping its own screen between 1000 and 1180 wide; invalid markup in
the decision readouts.

Feel check (scrolled cold, one word per act, then compared with the curve): calm, uneasy,
relieved, quiet, wonder, sure, in control, busy, safe, easy, answered, done. Matches the
intended curve; the peak is the largest change and holds the most scroll.

Not verified: a real phone (iOS Safari video and touch scrolling behaviour, Low Power
Mode), a low-end Android device's frame rate, a screen reader pass, and Lighthouse scores.
