# BRIEF: DQ-Stack landing page, "The Dive"

**Self-authored under explicit creative delegation.** The user asked (2026-11-12): "for
the landing page can u give me the most amazing idea with scroll effects and it must
tell a story / ill give u the images and just tell me what images u need / the image
transition must be smooth". Every answer below is an authored decision unless quoted.
It replaces the current landing page (`dqstack-landing`, Chronicle grammar).

## The eight topics

1. **Vibe (authored):** dusk over a city, the hum of a market street, warm shop light,
   one clear number. References: the film *Powers of Ten* (Eames, 1977), a Mumbai
   rooftop at blue hour, the paper price strip on a kirana shelf.
2. **Journey:** user: "it must tell a story". Sequence authored: above the city at dusk;
   dive to one street, one shop; the shutter lifts; the shelf and its two failures; the
   label becomes the forecast; how the number is made; pull back out as the whole city
   lights up for Diwali; the people in the shop; dawn, one action.
3. **Energy (authored):** quiet open, rising momentum on the dive, a hard stop at the
   shutter, uneasy at the shelf, crisp at the label, steady through the method, loudest
   on the pull back, warm, then calm.
4. **Feeling and the one moment:** see the curve and the peak below.
5. **The thing no site does (authored):** the scroll is altitude. One unbroken dive from
   a whole city to the single number on one shelf label, and the same path back out,
   now with every shop lit. Signature move: the paper price label on the shelf turns
   into the product's live forecast card, in place, pixel for pixel.
6. **Distance from premium minimal (authored):** cinematic photographic, premium, with
   one hard editorial moment (the label). Not playful, not brutalist.
7. **One world or scenes:** user: "the image transition must be smooth". Authored: one
   world for the dive and the pull back (nested, matched photographs, no cuts), with
   distinct stops where the dive pauses and something else happens.
8. **Assets:** the user will supply the photographs (shot list in the reply of
   2026-11-12, mirrored at the end of this file). Product screens are captured from the
   demo store at 2x. No generation unless the user asks.

## Grammar
**Powers of ten (scale).** Navigation is an altitude ruler on the right edge (2 km to
2 cm) that the visitor can also drag; sequence is scale; the ending returns to the
street at dawn. Chronicle lost: it is the current page. Filmic one-shot lost: footage is
not on hand, and stills nested by scale are smoother and cheaper than a camera flight.
Editorial chapters, gallery, catalogue, working surface: none of them is a story that
moves; this product is one decision made the same way everywhere, which is a scale story.

## Journey and feeling curve (curve written first)
| Act | Altitude | Feeling | What causes it |
|---|---|---|---|
| 1 The city | 2 km | awe, quiet | blue-hour aerial in layered planes (sky, city, haze); a held beat of silence |
| 2 The dive | 2 km to 20 m | momentum | nested photographs scale into each other, no cuts |
| 3 The shutter | 5 m | recognition | the dive stops; scroll lifts the shutter on a matched pair |
| 4 The shelf | 2 m | unease | the same shelf blends from full to gaps to expiring overstock |
| 5 The label (signature) | 2 cm | clarity | the paper label becomes the live forecast card: range, then "Order 24" |
| 6 How | screen | trust | the nightly method, real screens panning sideways |
| 7 The pull back (peak) | 2 cm to 2 km | wonder, pride | reverse dive; at altitude the city lights up shop by shop for Diwali |
| 8 The people | 3 m | warmth | owner, cashier, delivery, as a settling deck of portraits |
| 9 Dawn | 5 m | calm resolve | the shutter rises again on a full shelf; one action, held |

No two adjacent acts share a feeling. **Authored silence:** about half a screen of
still aerial at the top of act 1 before the dive begins.

## The peak
Act 7. A visitor would say: "You fly back out of one shop and the whole city lights up
for Diwali." Largest scroll span; act 6 before it is the quietest.

## Tell-someone sentence
"It's the site where you dive from the sky into one shop's shelf, find the one number
that matters, and fly back out as the whole city lights up."

## Score
| Beat | Device | Why |
|---|---|---|
| City | layered parallax + pointer depth | the hero needs real depth: sky, city, haze |
| Dive | zoom-scrub of nested stills | scale is the story; matched framing makes it seamless |
| Shutter | reveal (clip-path, bottom to top) on a matched pair | a lifting shutter is a change of state |
| Shelf | pin + state blend of three matched photos | the frame holds while the shelf goes wrong two ways |
| Label | morph, photo to DOM (signature) | physical becomes digital in the same rectangle |
| How | pan (horizontal) | a process reads as travel |
| Pull back | reverse zoom-scrub + light-up mask over a dusk sequence | the biggest change on the page |
| People | deck | separate people, each their own object |
| Dawn | resolve and hold | the loop closes on the action |

Device families: 8. Two zoom-scrub acts (the cap), never adjacent.

## Fingerprint gate
Against `dqstack-landing`: grammar (scale vs time), nav (altitude ruler vs shop clock),
hero (aerial dive vs shelf parallax), sequence shape (in and back out, symmetric, 9
acts), close (dawn shutter vs 06:00 order), signature (label morph vs drag a target):
6 of 6 differ.

## Shot list (what the user supplies)
See the reply of 2026-11-12. Files go in `source/` named by shot number.

## Build notes (2026-11-12)

**Assets as supplied** (source/1 to 10) and how each is used: 2 the city (split by
`prepare.py` into a clean sky plate, the city cut out lit, and the same cut out with its
lights taken down); 1 the street; 3 the shopfront; 4 the shutter (its slats cropped and
laid into the doorway of 3, so the lift is matched by construction); 5 the aisle; 7 and 8
the shelf; 9 the label; 6 the Diwali shopfront; 10 the team.

**Departures from the plan, forced by the assets:** the city is New York and the street
European, so the copy names neither; there was no full shelf, so the shelf act is a wipe
between too little (7) and too much (8) rather than a three state blend; there was no
dusk sequence, so the lights come on through a computed mask (a few blocks lit from the
start, then a wave out from the shop we left) over the lit and unlit cut outs.

**Verified:** headless Chrome over the DevTools protocol (`shoot.mjs`, no packages):
39 positions at 1440x900, 390x844 with touch, and reduced motion; no script errors,
no horizontal overflow, no CSP violations. Scroll through the dive and the peak: median
and p95 frame 16.7 ms on desktop and phone (software rendering), about 2% of frames
slower, at level changes and during the light-up. Not verified: a real phone.
