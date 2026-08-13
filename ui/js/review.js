/* ============================================================================
   The review flow — the front door.

   The old front door was a spreadsheet of 848 flagged series with a canvas grid,
   a fan chart, an attribution panel and an override form all on screen at once.
   For an analyst who knows the vocabulary that is a good instrument. For anyone
   else it is unusable, and "848 items need attention" is not a to-do list, it is
   a reason to close the tab.

   So this view makes three changes.

   It shows a day's work, not the whole backlog. The queue is ranked by money at
   risk and cut to a number a person can finish, with the rest reachable but not
   in the way. A planner who clears this list has done the work that matters; the
   long tail was never going to be reviewed by hand anyway.

   It asks one question per card, with one obvious answer. Every card ends in
   Approve / Change / Skip, so the common case is one click. The expert workspace
   is still there for bulk edits, and this view links to it — it is not a
   replacement for the grid, it is the thing you see before you need the grid.

   It says everything in words first and numbers second. All wording comes from
   plain.js; nothing here invents a phrase.
   ============================================================================ */

window.Review = (() => {
  const $ = (id) => document.getElementById(id);

  let D = null;
  let Q50 = 3;
  let rows = [];                 // the full ranked queue
  let hooks = {};

  const state = {
    tab: "urgent",
    reviewed: new Map(),         // id -> {action, value}
    committed: new Set(),        // ids already committed; still reviewed, not pending
    expanded: new Set(),
    editing: null,
    // Held across the re-render that opening the form causes, so a reason chosen
    // before a keystroke is not lost by the keystroke.
    editReason: "",
    editNote: "",
    editValue: null,
    limit: 24,
    cause: "",                   // sidebar filter: a headline-reason key
    cursor: 0,                   // keyboard position within the visible list
    keyboard: false,             // only paint the ring once a key has been used
  };

  /* ------------------------------------------------------------- the queue */

  /** Money at risk is the honest ranking key: lost margin is what the ledger
      says this item costs if nothing changes. Priority breaks ties, because a
      brand-new product can be urgent while having no history of loss yet. */
  const atRisk = (s) => s.ledger.lostMargin + s.ledger.wasteCost;

  /* Scope and watchlist both change which items are in the queue at all, so both
     belong in build() rather than in a render filter — the cohort has to be chosen
     from the scoped set, or the progress denominator would count items the planner
     cannot see. */
  let scope = "";                 // store id, or "" for everything
  const watched = new Set();

  function setScope(store) { scope = store || ""; build(); render(); }
  function setWatched(ids) {
    watched.clear();
    for (const id of ids) watched.add(id);
    build(); render();
  }

  function build() {
    Plain.calibrate(D.series);
    rows = D.series
      .filter((s) => !scope || s.store === scope)
      /* A watched product is in the queue whether or not it was flagged. That is the
         entire point of pinning: a planner who does not trust a product wants to see
         it every cycle, not only when the model happens to raise a flag. */
      .filter((s) => s.reasons.length > 0 || watched.has(s.id))
      .map((s) => {
        const head = Plain.headline(s.reasons);
        const pinned = watched.has(s.id);
        return {
          s, id: s.id, head, pinned,
          risk: atRisk(s),
          // Pinned items sort into the top tier regardless of exposure, because the
          // planner has said explicitly that they want to see them.
          tier: pinned || head.tone === "urgent" || atRisk(s) >= 200 ? "urgent"
              : head.tone === "warn" ? "check" : "later",
        };
      })
      .sort((a, b) => b.risk - a.risk || b.s.priority - a.s.priority);
    pickCohort();
  }

  /* Today's list is chosen once and then held.

     The first version re-sliced the top N on every render, so clearing an item
     pulled the next one in from a queue of 489 and the progress readout went
     "1 of 25 done", "2 of 26 done" — a denominator that grows as you work is
     worse than no progress bar at all, because the finish line moves away from
     you. The whole promise of this view is a list you can finish, and a list you
     can finish has to be a fixed set of items.

     So the cohort is fixed at build time. Reviewing an item moves it to the
     Reviewed tab and shrinks the list towards empty. The remaining hundreds are
     honestly labelled as behind these, and they become tomorrow's cohort on the
     next run rather than sliding in under the planner's feet today. */
  const cohort = { urgent: [], check: [], later: [] };

  function pickCohort() {
    for (const tier of ["urgent", "check", "later"]) {
      cohort[tier] = rows.filter((r) => r.tier === tier)
                         .slice(0, state.limit)
                         .map((r) => r.id);
    }
  }

  const byId = (id) => rows.find((r) => r.id === id);

  function matchesCause(r) {
    const c = state.cause;
    if (!c) return true;
    if (c === "__skipped") return state.reviewed.get(r.id)?.action === "skipped";
    if (c === "__rest") {
      const g = byCause().find((x) => x.key === "__rest");
      return !!g && g.members.includes(r.head.key);
    }
    return r.head.key === c;
  }

  const forTab = (tab) => {
    const base = tab === "done"
      ? rows.filter((r) => state.reviewed.has(r.id))
      : cohort[tab].filter((id) => !state.reviewed.has(id)).map(byId).filter(Boolean);
    return base.filter(matchesCause);
  };

  /* The queue broken down by cause, each row carrying a count and the exposure
     behind it.

     Grouping by cause rather than by severity is the change v2 of the review asked
     for, and it is right: a planner choosing where to start needs to know that three
     data problems may be worth more than twelve promotion endings, and a severity
     tier cannot tell them that. Counts alone are what every incumbent shows.

     Rows are generated from the same headline-reason taxonomy the cards use, ordered
     by exposure, so a reason added to plain.js appears here without further work. */
  function byCause() {
    const live = cohort.urgent.concat(cohort.check, cohort.later)
      .filter((id) => !state.reviewed.has(id)).map(byId).filter(Boolean);

    const groups = new Map();
    for (const r of live) {
      const k = r.head.key;
      let g = groups.get(k);
      if (!g) groups.set(k, (g = { key: k, label: Plain.short(k), tone: r.head.tone,
                                   n: 0, exposure: 0 }));
      g.n += 1;
      g.exposure += r.risk;
    }
    const rowsOut = [...groups.values()].sort((a, b) => b.exposure - a.exposure || b.n - a.n);

    // Capped at six with the remainder collapsed, per the report.
    let head = rowsOut, rest = [];
    if (rowsOut.length > 6) { head = rowsOut.slice(0, 5); rest = rowsOut.slice(5); }
    if (rest.length) {
      head = head.concat([{
        key: "__rest", label: "Everything else", tone: "info",
        n: rest.reduce((a, g) => a + g.n, 0),
        exposure: rest.reduce((a, g) => a + g.exposure, 0),
        members: rest.map((g) => g.key),
      }]);
    }

    // Skipped earlier is a session fact rather than a reason, but it is the row a
    // planner asks for most often, so it sits with them.
    const skipped = [...state.reviewed.entries()]
      .filter(([, d]) => d.action === "skipped").map(([id]) => id);
    if (skipped.length) {
      head = head.concat([{ key: "__skipped", label: "Skipped earlier", tone: "info",
                            n: skipped.length, exposure: 0 }]);
    }
    return head;
  }

  function setCause(key) {
    state.cause = state.cause === key ? "" : key;
    state.cursor = 0;
    // The skipped row is the one filter that shows already-reviewed items.
    if (state.cause === "__skipped") state.tab = "done";
    else if (state.tab === "done") state.tab = "urgent";
    render();
  }

  /** `left` is what is still on today's list; `behind` is everything in the tier
      that did not make the cut. They are reported separately and never added. */
  const counts = () => {
    const c = { done: state.reviewed.size };
    for (const tier of ["urgent", "check", "later"]) {
      const total = rows.filter((r) => r.tier === tier).length;
      c[tier] = cohort[tier].filter((id) => !state.reviewed.has(id)).length;
      c[tier + "Total"] = total;
      c[tier + "Behind"] = Math.max(0, total - cohort[tier].length);
      c[tier + "Size"] = cohort[tier].length;
    }
    return c;
  };

  /* ---------------------------------------------------- forecast arithmetic */

  /** Week-one totals. The card asks about next week because that is the decision
      with a delivery attached to it; the 28-day view lives in the chart. */
  const week1 = (s) => s.weekly[0][Q50];

  function priorWeek1(s) {
    let sum = 0;
    for (let d = 0; d < 7; d++) {
      const v = s.prior[d];
      if (v < 0) return null;          // prior version did not cover this day
      sum += v;
    }
    return sum;
  }

  const band = (s) => ({ lo: s.weekly[0][1], hi: s.weekly[0][5] });   // p10 / p90

  /* The order-up-to level covers the review period plus the lead time, which is a
     different and longer window than the weekly forecast beside it. Saying so is
     the difference between two numbers that inform each other and two numbers
     that look like a bug. */
  let COVER_DAYS = 9;

  /* How long the held-out test in the ledger actually ran, read from the run rather
     than assumed. Quoting "28 days" from a literal would silently become a lie the
     first time the evaluation window changed. */
  let EVAL_DAYS = 28;

  function changeNote(ch) {
    if (!ch) return "no earlier version to compare";
    if (ch.dir === "flat") return "no meaningful change";
    if (ch.note) return ch.note;
    return `${ch.pct}% ${ch.dir}`;
  }

  /* ------------------------------------------------------------- sparkline
     Inline SVG rather than canvas: one of these per card, they must survive
     being re-rendered on every action, and at this size the fan reads better as
     a filled path than as the grid's 40x14 glyph. */
  /* A round dot on a chart that is deliberately stretched.

     The sparkline sets `preserveAspectRatio="none"` so it fills whatever width the
     card gives it, which at 1014px for a 300-unit viewBox is a 3.4x horizontal
     stretch. Every `<circle>` on it was therefore painted as an ellipse — including
     the hinge dot marking the last actual sale, which is the single point the whole
     chart is a claim about.

     A zero-length subpath with a round cap is immune, because the stroke is applied
     after the viewBox transform when `non-scaling-stroke` is set. Verified by
     rasterising one: 8x8 painted pixels under a 3x stretch. Radius comes from the
     stroke width in CSS, so the shape stays a single source of truth. */
  const dot = (cx, cy) => `M${cx.toFixed(1)},${cy.toFixed(1)}L${cx.toFixed(1)},${cy.toFixed(1)}`;

  function spark(s, w = 300, h = 104) {
    const HIST = 35;
    const from = Math.max(0, (s.hist || []).length - HIST);
    const hist = (s.hist || []).slice(from);
    const fan = s.fan;                     // 28 days x 7 quantiles
    const n = hist.length + fan.length;

    let hi = 0;
    for (const v of hist) if (v != null) hi = Math.max(hi, v);
    for (const f of fan) hi = Math.max(hi, f[5]);
    hi = hi || 1;

    const x = (i) => (w * i) / (n - 1);
    const y = (v) => h - 5 - ((h - 14) * v) / hi;

    /* Excluded days break the line rather than dropping it to the floor.

       `y(null)` is arithmetically `y(0)`, so every day the pipeline set aside used
       to be drawn as a day on which nobody wanted the product — the precise
       confusion the pipeline exists to resolve, reintroduced by the chart. */
    const hl = Truth.line(hist, x, y);
    const histLine = hl.d;

    const spans = Truth.runs(s, from, HIST).map((r) => {
      const x0 = x(r.i0) - (w / (n - 1)) / 2;
      const x1 = x(r.i1) + (w / (n - 1)) / 2;
      const meta = Plain.EXCLUDED[r.kind];
      const days = r.i1 - r.i0 + 1;
      return `<rect class="spark-${r.kind}" x="${Math.max(0, x0).toFixed(1)}" y="0"
                    width="${Math.max(1.5, x1 - x0).toFixed(1)}" height="${h}"
              ><title>${U.esc(meta.label)} — ${days} day${days === 1 ? "" : "s"}</title></rect>`;
    }).join("");

    /* Calendar events the pipeline already models and the interface never showed.
       A planner looking at a spike could not tell a holiday from a data problem. */
    const evs = Truth.events(D.dates, D.events, from, HIST).map((ev) =>
      `<g class="spark-ev"><line x1="${x(ev.i).toFixed(1)}" y1="0"
             x2="${x(ev.i).toFixed(1)}" y2="${h}"/>
        <path d="${dot(x(ev.i), 4)}"/>
        <title>${U.esc(Truth.eventLabel(ev.name))} — ${U.esc(ev.date)}</title></g>`).join("");

    const j0 = hist.length - 1;
    const known = Truth.lastKnown(hist);
    const anchorY = y(known ? known.v : 0);

    /* Three nested bands, not one slab.

       This surface used to draw a single p10-p90 rectangle-ish shape, while
       chart.js drew three nested levels for the same statistical object — so the
       chart a planner sees twenty-four times a screen was the least honest of the
       four. Nesting is what gives a fan its shape: the dense core reads as
       "probably this" and the pale edge as "but it could be that", pre-attentively
       and without reading an axis.

       The quantile pairs and alphas are chart.js's, exactly, so the two agree. */
    const LEVELS = [[0, 6, "fan-98"], [1, 5, "fan-80"], [2, 4, "fan-50"]];
    const bands = LEVELS.map(([lo, up, cls]) => {
      const top = [`M${x(j0).toFixed(1)},${anchorY.toFixed(1)}`];
      const bot = [];
      fan.forEach((f, k) => {
        const xi = x(j0 + 1 + k);
        top.push(`L${xi.toFixed(1)},${y(f[up]).toFixed(1)}`);
        bot.push(`${xi.toFixed(1)},${y(f[lo]).toFixed(1)}`);
      });
      return `<path d="${top.join("")}L${bot.reverse().join("L")}Z" class="${cls}"/>`;
    }).join("");

    const mid = fan.map((f, k) =>
      `${k ? "L" : "M"}${x(j0 + 1 + k).toFixed(1)},${y(f[Q50]).toFixed(1)}`).join("");

    const a = Truth.audit(s, from, HIST);
    const ex = Plain.excluded(a);

    return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"
                 role="img" aria-label="Sales for the last ${hist.length} days and the forecast for the next ${fan.length}, with its likely range${
                   ex ? `. ${ex.short} are set aside` : ""}">
      <rect class="fan-future" x="${x(j0).toFixed(1)}" y="0"
            width="${(w - x(j0)).toFixed(1)}" height="${h}"/>
      ${spans}
      ${bands}
      <path d="${histLine}" class="spark-hist"/>
      ${hl.dots.length
        ? `<path class="spark-dot" d="${hl.dots.map(([cx, cy]) => dot(cx, cy)).join("")}"/>`
        : ""}
      <path d="${mid}" class="spark-fc"/>
      ${evs}
      <line x1="${x(j0).toFixed(1)}" y1="2" x2="${x(j0).toFixed(1)}" y2="${h - 2}" class="spark-now"/>
      ${known && known.i === j0
        ? `<path class="spark-last-halo" d="${dot(x(j0), anchorY)}"/>
           <path class="spark-last" d="${dot(x(j0), anchorY)}"/>`
        : ""}
    </svg>`;
  }

  /* The legend, built from what the chart actually drew.

     A fixed legend listing every possible layer teaches a planner to ignore it,
     because most entries describe nothing on screen. These appear only when the
     product has the thing they name. */
  function chartKey(s, { window: win = 35, prior = false } = {}) {
    const from = Math.max(0, (s.hist || []).length - win);
    const a = Truth.audit(s, from, win);
    const nEv = Truth.events(D.dates, D.events, from, win).length;
    const e = U.esc;
    const item = (cls, label, hint) =>
      `<span title="${e(hint)}"><i class="${cls}"></i>${e(label)}</span>`;

    return `<div class="rc-chart-key">
      ${item("k-hist", "sold so far", "Units recorded through the till each day.")}
      ${item("k-fc", "forecast", "The middle of the forecast: as likely to be above as below.")}
      ${item("k-band", "likely range", "Where the outcome is expected to fall. Wider means less certain.")}
      ${prior ? item("k-prior", "last week's version", "The forecast this product had in the previous cycle.") : ""}
      ${a.suspects ? item("k-suspect", Plain.EXCLUDED.suspect.short, Plain.EXCLUDED.suspect.note) : ""}
      ${a.gaps ? item("k-gap", Plain.EXCLUDED.gap.short, Plain.EXCLUDED.gap.note) : ""}
      ${nEv ? item("k-ev", nEv === 1 ? "holiday" : "holidays",
                   "A date the forecast already accounts for. Shown so an unusual day has an explanation.") : ""}
    </div>`;
  }

  /* The cost of being wrong, as a shape.

     This is the argument the ordering layer makes, and it was nowhere on screen. Two
     products with the same forecast get different recommended levels because the two
     ways of being wrong do not cost the same, and how unequal they are depends on the
     product's own margin and shelf life. A paragraph saying that is a paragraph
     nobody reads. A curve with the recommendation at the bottom of it is learnable in
     a week without meeting a technical term.

     Fixed 112x46 rather than a stretched viewBox, so the marker is a circle and not
     an ellipse. Each curve is normalised to its own peak, which is what makes the
     comparison across products work: a lopsided curve and a symmetric bowl both fill
     the box, so the reader is comparing shape rather than magnitude. Measured across
     the assortment the shapes genuinely differ -- a 720-day-shelf hobby line is
     visibly lopsided, a 21-day-shelf food line is visibly flat. */
  function costCurve(s) {
    const c = Curve.points(s, COVER_DAYS);
    if (!c || !(c.peak > 0)) return "";
    const t = Plain.tradeoff(c);
    const W = 112, H = 46, PADX = 2, TOP = 4, BOT = H - 5;

    const x = (q) => PADX + ((W - 2 * PADX) * (q - c.lo)) / (c.hi - c.lo);
    const y = (v) => BOT - ((BOT - TOP) * v) / c.peak;

    const line = c.pts.map((p, i) =>
      `${i ? "L" : "M"}${x(p.q).toFixed(1)},${y(p.excess).toFixed(1)}`).join("");
    const area = `${line}L${x(c.hi).toFixed(1)},${BOT}L${x(c.lo).toFixed(1)},${BOT}Z`;
    const ox = x(c.opt);

    const label = `Cost of ordering more or less than ${Plain.units(c.opt)} units. ` +
      (t ? t.word + ". " + t.detail : "");

    return `<div class="rc-curve" title="${U.esc(t ? t.detail : "")}">
      <svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img"
           aria-label="${U.esc(label)}">
        <line class="ccv-base" x1="${PADX}" y1="${BOT}" x2="${W - PADX}" y2="${BOT}"/>
        <path class="ccv-area" d="${area}"/>
        <path class="ccv-line" d="${line}"/>
        <line class="ccv-opt" x1="${ox.toFixed(1)}" y1="${TOP - 1}"
              x2="${ox.toFixed(1)}" y2="${BOT}"/>
        <circle class="ccv-dot" cx="${ox.toFixed(1)}" cy="${BOT}" r="2.6"/>
      </svg>
      <div class="rc-curve-ends" aria-hidden="true">
        <span>${Plain.money(c.lessCost)} worse</span>
        <span>best</span>
        <span>${Plain.money(c.moreCost)} worse</span>
      </div>
      ${t ? `<p class="rc-curve-say t-${t.tone}">${U.esc(t.word)}</p>` : ""}
    </div>`;
  }

  /* ----------------------------------------------------------------- cards */

  function card(r) {
    const s = r.s;
    const e = U.esc;
    const done = state.reviewed.get(r.id);
    const open = state.expanded.has(r.id);
    const editing = state.editing === r.id;

    const now = week1(s);
    const prior = priorWeek1(s);
    const ch = Plain.change(now, prior);
    const b = band(s);
    const q = Plain.quality(s.health);
    const conf = Plain.confidence(s.relWidth);
    const svc = Plain.service(s);
    const secondary = Plain.others(s.reasons);
    const shown = done && done.action === "changed" ? done.value : now;

    const list = forTab(state.tab);
    const isFocus = state.keyboard && list[Math.min(state.cursor, list.length - 1)]?.id === r.id;

    const pos = list.findIndex((x) => x.id === r.id);

    /* Four rungs, in the order a decision is actually made.

       The card previously presented one flat surface: a money figure, an identity, a
       reason, a chart and four equal figures, all competing. A planner clearing
       twenty-four of these reads the same layout twenty-four times, so what sits
       where matters more here than anywhere else in the application.

       rung 0  is this worth my attention -- position in the day's list, exposure
       rung 1  what am I deciding -- the product, the number, and what it costs to
               get it wrong
       rung 2  why is it in front of me -- one sentence, and the shape behind it
       rung 3  do it

       Everything else moved into the drawer behind W. Six figures came off the face
       of the card, and four of them explain the cost curve rather than the
       decision, which is exactly what a second rung is for. */
    return `<article class="rc" data-id="${e(r.id)}" data-tone="${r.head.tone}"
             ${isFocus ? 'data-focus="true"' : ""}
             ${done ? `data-done="${e(done.action)}"` : ""}>

      <div class="rc-rung0">
        ${pos >= 0 ? `<span class="rc-pos" title="Position in today's list">
          ${pos + 1} of ${list.length}</span>` : ""}
        <span class="rc-exp" title="Profit expected to be lost if nothing changes">
          <b>${Plain.money(r.risk)}</b> at risk
        </span>
        ${s.ageWeeks != null && s.ageWeeks < 26
          ? `<span class="rc-chip" title="How long this product has been selling here. A short history is why a forecast can be unsure.">
               selling ${Math.max(1, Math.round(s.ageWeeks))} week${Math.round(s.ageWeeks) === 1 ? "" : "s"}</span>`
          : ""}
        <span class="rc-rung0-end">
          ${r.pinned ? '<span class="rc-stamp watch">Watched</span>' : ""}
          ${done ? `<span class="rc-stamp">${e(doneWord(done))}</span>` : ""}
        </span>
      </div>

      <div class="rc-rung1">
        <div class="rc-id">
          <h3>${e(prettyItem(s.item))}</h3>
          <p>${e(prettyStore(s.store))} · ${e(catWord(s.cat))} · ${e(Plain.CLASS[s.cls]?.label || s.cls)}</p>
        </div>
        <div class="rc-fig rc-fig-main">
          <span class="rc-fig-cap">Forecast for next week</span>
          <span class="rc-fig-val">${Plain.units(shown)}<small>units</small></span>
          <span class="rc-fig-sub">likely between ${Plain.units(b.lo)} and ${Plain.units(b.hi)}</span>
        </div>
        <div class="rc-fig rc-fig-stock" title="Order-up-to level: the predictive quantile of total demand over the review period plus lead time, at this product's own critical fractile.">
          <span class="rc-fig-cap">Stock up to</span>
          <span class="rc-fig-val">${Plain.units(s.orderUpTo)}<small>units</small></span>
          <span class="rc-fig-sub">covers ${COVER_DAYS} days, to ${svc.pct}% service</span>
          ${costCurve(s)}
        </div>
      </div>

      <p class="rc-why" title="${e(r.head.term)}">
        <span class="rc-dot"></span><b>${e(r.head.label)}</b> — ${e(r.head.detail)}
      </p>

      <div class="rc-chart">
        ${spark(s)}
        ${chartKey(s)}
      </div>

      ${editing ? editForm(r, s, now) : `
      <div class="rc-actions">
        ${done ? `
          ${state.committed.has(r.id) ? "" :
            '<button class="btn btn-quiet" data-act="undo">Undo</button>'}
          <span class="rc-done-note">${e(doneNote(done, now) + committedNote(r.id))}</span>
        ` : `
          <button class="btn btn-primary" data-act="approve">
            Approve ${Plain.units(now)} units <span class="kbd">A</span>
          </button>
          <button class="btn" data-act="edit">Change <span class="kbd">C</span></button>
          <button class="btn btn-quiet" data-act="skip">Skip <span class="kbd">S</span></button>
        `}
        <button class="btn btn-quiet" data-act="open"
                title="Open this product's own page">Full details</button>
        <button class="rc-more" data-act="toggle" aria-expanded="${open}">
          ${open ? "Hide detail" : "Why this number?"} <span class="kbd">W</span>
        </button>
      </div>`}

      ${open ? detail(s, secondary, q, conf, svc, r) : ""}
    </article>`;
  }

  /* The override form: a number, a reason, and a guardrail.

     Changing a forecast used to be a bare number field. Three things were missing and
     each of them matters more than the field did.

     A reason. Every other fact about a decision is reconstructable later -- what the
     model said, what was chosen, what it was worth -- but "because the display goes up
     on Tuesday" exists only in the head of whoever typed the number, and it is the
     part that could teach next quarter's model something. It is required, because an
     optional audit field is an empty audit field.

     A guardrail. The planner is allowed to be right when the model is wrong; that is
     what an override is for. But leaving the range the forecast considers likely
     should be a deliberate act, so the form says where the number sits, in odds, and
     updates as it is typed.

     The consequence. What the change is worth, from the same estimator the
     spreadsheet's impact figure and the commit summary use, so the three cannot
     disagree.

     The reason chips and the guardrail update the DOM in place rather than through
     render(), because a re-render mid-edit would discard whatever is half-typed. */
  function editForm(r, s, now) {
    const e = U.esc;
    const row = s.weekly[0];
    const val = state.editValue != null ? state.editValue : Math.round(now);

    return `<div class="rc-edit">
      <div class="rc-edit-row">
        <label for="ed-${e(r.id)}">Use this number instead</label>
        <input id="ed-${e(r.id)}" class="rc-input" type="number" min="0" step="1"
               value="${val}" inputmode="numeric" data-model="${Math.round(now)}">
        <span class="rc-edit-unit">units next week</span>
        <span class="rc-edit-impact" data-role="impact"></span>
      </div>

      <p class="rc-guard" data-role="guard" data-tone=""></p>

      <fieldset class="rc-reasons">
        <legend>Why are you changing it? <span class="rc-req">needed</span></legend>
        <div class="rc-reason-chips" data-role="reasons">
          ${Plain.OVERRIDE_REASONS.map((x) => `
            <button type="button" class="rc-reason" data-reason="${e(x.key)}"
                    aria-pressed="${state.editReason === x.key}"
                    title="${e(x.hint)}">${e(x.label)}</button>`).join("")}
        </div>
        <input class="rc-note" type="text" maxlength="140" data-role="note"
               value="${e(state.editNote || "")}"
               placeholder="Anything worth adding (optional)">
      </fieldset>

      <div class="rc-edit-act">
        <button class="btn btn-primary" data-act="save">Save</button>
        <button class="btn btn-quiet" data-act="cancel">Cancel</button>
      </div>
    </div>`;
  }

  /** Paint the guardrail and the impact figure for whatever is currently typed.

      Called on every keystroke and once when the form opens. Writes into the two
      placeholder elements rather than re-rendering, so the caret stays put. */
  function paintEditFeedback(cardEl) {
    const inp = cardEl.querySelector(".rc-input");
    if (!inp) return;
    const s = rows.find((x) => x.id === cardEl.dataset.id)?.s;
    if (!s) return;

    const v = Math.max(0, Math.round(+inp.value || 0));
    const model = +inp.dataset.model;
    const guard = cardEl.querySelector('[data-role="guard"]');
    const imp = cardEl.querySelector('[data-role="impact"]');

    const p = Plain.plausibility(s.weekly[0], v);
    if (guard && p) {
      guard.dataset.tone = p.tone;
      guard.innerHTML = `<b>${U.esc(p.word)}.</b> ${U.esc(p.note)}`;
    }

    if (imp) {
      const changed = Math.abs(v - model) > 0.5;
      const d = changed && hooks.impactOf ? hooks.impactOf(s, v) : 0;
      imp.textContent = changed
        ? `${Plain.money(d, { sign: true })} expected effect`
        : "same as the forecast";
      imp.dataset.sign = !changed ? "flat" : d >= 0 ? "pos" : "neg";
    }

    const save = cardEl.querySelector('[data-act="save"]');
    if (save) save.disabled = !state.editReason;
  }

  /** The expansion. Everything here was previously the only way to see any of
      it; now it is opt-in, one click, and written in sentences. */
  function detail(s, secondary, q, conf, svc, r) {
    const e = U.esc;
    const drivers = (s.why || []).slice(0, 5);
    const maxAbs = Math.max(1e-9, ...drivers.map((d) => Math.abs(d.contribution)));
    const ch = Plain.change(week1(s), priorWeek1(s));

    /* The six figures the face of the card shed.

       Ordered deliberately: the two that qualify the decision come first, then the
       four that explain the cost curve's shape. A planner who has just been told
       "running out costs 2.2x more than overstocking" and wants to know why finds
       the margin and the shelf life here, which is where the answer actually is. */
    const shed = `<section class="rc-shed">
      <h4>The rest of the numbers</h4>
      <div class="rc-figs">
        <div class="rc-fig">
          <span class="rc-fig-cap">Versus last week</span>
          <span class="rc-fig-word rc-${ch ? ch.dir : "flat"}">${ch ? e(ch.word) : "—"}</span>
          <span class="rc-fig-sub">${e(changeNote(ch))}</span>
        </div>
        <div class="rc-fig" title="${e(q.note)}">
          <span class="rc-fig-cap">Data quality</span>
          <span class="rc-fig-word rc-q-${q.tone}">${e(q.word)}</span>
          <span class="rc-fig-sub">confidence ${e(conf.word.toLowerCase())}</span>
        </div>
        <div class="rc-fig">
          <span class="rc-fig-cap">Sells for</span>
          <span class="rc-fig-val">$${(s.price || 0).toFixed(2)}</span>
          <span class="rc-fig-sub">costs $${(s.cost || 0).toFixed(2)} ·
            ${Math.round((s.marginRate || 0) * 100)}% margin</span>
        </div>
        <div class="rc-fig" title="Why the two mistakes cost what they do: a product that keeps for years can be overstocked cheaply, one that spoils in a week cannot.">
          <span class="rc-fig-cap">Keeps for</span>
          <span class="rc-fig-val">${Plain.units(s.shelfLife || 0)}<small>days</small></span>
          <span class="rc-fig-sub">${(s.shelfLife || 0) <= 14
            ? "leftovers are usually wasted"
            : (s.shelfLife || 0) <= 90 ? "some room to hold stock"
            : "holding stock costs little"}</span>
        </div>
        <div class="rc-fig">
          <span class="rc-fig-cap">Sales a year</span>
          <span class="rc-fig-val">${Plain.money(s.revenue || 0)}</span>
          <span class="rc-fig-sub">how much this line is worth to you</span>
        </div>
        <div class="rc-fig" title="${e(Plain.CLASS[s.cls]?.hint || "")}">
          <span class="rc-fig-cap">How it sells</span>
          <span class="rc-fig-word">${e(Plain.CLASS[s.cls]?.label || s.cls)}</span>
          <span class="rc-fig-sub">${(s.adi || 0) <= 1.1
            ? "sells most days"
            : `a sale about every ${Math.round(s.adi || 1)} days`}</span>
        </div>
      </div>
    </section>`;

    return `<div class="rc-detail">
      ${shed}

      ${outcomeBlock(s, EVAL_DAYS)}

      ${drivers.length ? `
      <section>
        <h4>What pushed this number up or down</h4>
        <ul class="rc-drivers">
          ${drivers.map((d) => {
            const pos = d.contribution >= 0;
            const wpc = (Math.abs(d.contribution) / maxAbs) * 100;
            return `<li>
              <span class="rd-name">${e(Plain.feature(d.feature))}</span>
              <span class="rd-bar"><i class="${pos ? "up" : "down"}"
                    style="width:${wpc.toFixed(0)}%"></i></span>
              <span class="rd-val ${pos ? "rc-up" : "rc-down"}">
                ${pos ? "raised" : "lowered"} it</span>
            </li>`;
          }).join("")}
        </ul>
        <p class="rc-fine">These come from the model itself, not from a rule someone wrote.</p>
      </section>` : ""}

      ${blendBlock(s)}

      <section>
        <h4>How much to trust it</h4>
        <dl class="rc-dl">
          <dt>Data quality</dt><dd>${e(q.word)} — ${e(q.note)}</dd>
          <dt>Confidence</dt><dd>${e(conf.word)} — ${e(conf.note)}</dd>
          ${(() => {
            /* Counted from the same history the chart draws, rather than read from
               the pipeline's own tally over its longer training window. Two
               different numbers for one idea is how a planner learns to distrust
               both. */
            const a = Truth.audit(s);
            const out = [];
            if (a.suspects) out.push(`<dt>${e(Plain.EXCLUDED.suspect.label)}</dt>
              <dd>${a.suspects} day${a.suspects === 1 ? "" : "s"}. ${e(Plain.EXCLUDED.suspect.note)}</dd>`);
            if (a.gaps) out.push(`<dt>${e(Plain.EXCLUDED.gap.label)}</dt>
              <dd>${a.gaps} day${a.gaps === 1 ? "" : "s"}. ${e(Plain.EXCLUDED.gap.note)}</dd>`);
            if (out.length) out.push(`<dt>History used</dt>
              <dd>${a.known} of the last ${a.total} days are treated as evidence.
                  The rest are shaded on the chart above.</dd>`);
            return out.join("");
          })()}
        </dl>
      </section>

      <section>
        <h4>Why the service level is ${svc.pct}%, not 95%</h4>
        <p>${e(svc.why)} A blanket 95% target across every product would overstock
           this one or leave money on the table elsewhere.</p>
      </section>

      ${secondary.length ? `
      <section>
        <h4>Also worth knowing</h4>
        <ul class="rc-also">
          ${secondary.map((x) => `<li><b>${e(x.label)}.</b> ${e(x.detail)}</li>`).join("")}
        </ul>
      </section>` : ""}

      <section>
        <h4>What to do</h4>
        <p>${e(r.head.action)}</p>
      </section>

      <p class="rc-fine">
        Technical name for this flag: <code>${e(r.head.term)}</code>.
        Open <b>All products</b> to edit this alongside its neighbours.
      </p>
    </div>`;
  }

  /* How the last cycle actually went.

     The system asked to be trusted and never said how it had done. Every product
     already carried the result of running these settings across the 28 days before
     the forecast date -- what shoppers wanted, what could be sold, what the gap
     cost -- and none of it was on any screen.

     The proportion bar is the point of the block: a planner sees at a glance whether
     the shelf held up, and the money underneath says what the shortfall was worth.
     Labelled as a test throughout, because it is one. */
  function outcomeBlock(s, days) {
    const o = Plain.outcome(s, days);
    if (!o) return "";
    const e = U.esc;
    const pct = Math.max(0, Math.min(100, o.met * 100));
    return `<section class="rc-out">
      <h4>How the last ${o.days} days went
        <span class="rc-out-tag" title="Measured by running these settings over ${o.days} days that were held back from the forecast. It is a test, not a live trading record.">tested</span>
      </h4>
      <p class="rc-out-head rc-q-${o.tone}">${e(o.headline)}</p>
      <div class="rc-out-bar" role="img"
           aria-label="${Math.round(pct)}% of demand met">
        <i style="width:${pct.toFixed(1)}%"></i>
      </div>
      <p class="rc-out-note">${e(o.note)}</p>
      <dl class="rc-out-figs">
        <div><dt>Shoppers wanted</dt><dd>${Plain.units(o.wanted)} units</dd></div>
        <div><dt>You could sell</dt><dd>${Plain.units(o.sold)} units</dd></div>
        <div><dt>Profit earned</dt><dd>${Plain.money(o.earned)}</dd></div>
        ${o.lost > 0
          ? `<div><dt>Profit missed</dt><dd class="rc-down">${Plain.money(o.lost)}</dd></div>`
          : ""}
        ${o.cost > 0
          ? `<div><dt title="Cost of holding stock, plus anything that had to be written off.">Stock cost</dt>
               <dd>${Plain.money(o.cost)}</dd></div>`
          : ""}
      </dl>
    </section>`;
  }

  /* Where the number came from, as a share of the answer rather than a method list.

     The blend is this system's central claim and its weights were exported from the
     first run onwards without ever being shown. "Mostly this product's own sales
     pattern, combined with the curve new products follow" is a truthful answer to
     "why should I believe this", and it needs no technical vocabulary to land. Each
     row keeps its real name on hover. */
  function blendBlock(s) {
    const b = Plain.blend(s.weights);
    if (!b) return "";
    const e = U.esc;
    return `<section class="rc-blend">
      <h4>Where this number came from</h4>
      <div class="rc-blend-bar" role="img" aria-label="${e(b.sentence)}">
        ${b.rows.map((r, i) => `<i class="bl-${i}" style="width:${(r.share * 100).toFixed(1)}%"
             title="${e(r.label)} — ${Math.round(r.share * 100)}%"></i>`).join("")}
      </div>
      <ul class="rc-blend-list">
        ${b.rows.map((r, i) => `<li>
          <i class="bl-${i}"></i>
          <span class="bl-name" title="${e(r.hint)} (${e(r.term)})">${e(r.label)}</span>
          <span class="bl-pct">${Math.round(r.share * 100)}%</span>
        </li>`).join("")}
      </ul>
      <p class="rc-fine">Several methods run on every product and are weighted by how
         well each has done on products like this one. No single method wins
         everywhere, which is why the number is a blend rather than a pick.</p>
    </section>`;
  }

  /* --------------------------------------------------------------- naming
     The synthetic panel uses M5's opaque identifiers. They cannot be turned into
     real product names, but they can at least stop looking like database keys. */
  const CAT_WORD = { FOODS: "Food", HOUSEHOLD: "Household", HOBBIES: "Hobbies" };
  const catWord = (c) => CAT_WORD[c] || c;

  function prettyItem(item) {
    const m = /^(FOODS|HOUSEHOLD|HOBBIES)_(\d+)_(\d+)$/.exec(item);
    return m ? `${catWord(m[1])} ${m[2]}-${m[3]}` : item;
  }
  const prettyStore = (st) => `Store ${String(st).replace("_", "-")}`;

  const doneWord = (d) =>
    d.action === "approved" ? "Approved" : d.action === "changed" ? "Changed" : "Skipped";

  const committedNote = (id) => (state.committed.has(id) ? " · committed" : "");

  function doneNote(d, orig) {
    if (d.action === "approved") return `Approved at ${Plain.units(orig)} units.`;
    if (d.action === "skipped") return "Skipped — left as it was.";
    const ch = Plain.change(d.value, orig);
    // The reason is read back, so a planner scrolling their own decisions sees why
    // rather than only what.
    const why = d.reason ? ` — ${Plain.OVERRIDE_BY_KEY[d.reason]?.label || d.reason}` : "";
    return `Changed to ${Plain.units(d.value)} units` +
           (ch && ch.dir !== "flat" ? ` (${ch.word})` : "") + why +
           (d.note ? `: “${d.note}”` : ".");
  }

  /* --------------------------------------------------------------- render */

  function render() {
    const c = counts();
    const total = rows.length;
    const doneN = c.done;

    // Today's list is what is on screen — at most `limit` cards — not the whole
    // urgent tier. The number in the headline has to be the number of things a
    // planner can finish, or the view has simply relabelled the backlog.
    const shown = forTab("urgent");
    const onList = shown.length;
    const behind = c.urgentBehind;
    const listRisk = shown.reduce((a, r) => a + r.risk, 0);
    // Denominator is the cohort chosen at boot, so it never moves.
    const size = c.urgentSize || 1;
    const cleared = size - onList;
    const pct = Math.round((cleared / size) * 100);

    $("rvHeadline").textContent = onList > 0
      ? `${onList} ${onList === 1 ? "decision" : "decisions"} to make today`
      : doneN > 0 ? "Today's list is clear" : "Nothing urgent today";

    $("rvSub").textContent = onList > 0
      ? `The highest-value decisions first, chosen by how much profit is at stake. ` +
        (behind
          ? `${behind.toLocaleString()} lower-value items are flagged behind these — they are on tomorrow's list, not today's.`
          : `That is everything flagged as urgent.`)
      : `${c.check.toLocaleString()} worth checking when you have a moment · ` +
        `${total.toLocaleString()} products flagged in total.`;

    $("rvProgressBar").style.width = pct + "%";
    $("rvProgressText").textContent = cleared
      ? `${cleared} of ${size} done`
      : `${size} to go`;
    $("rvMoney").textContent = Plain.money(listRisk);

    // The sidebar badge is the only place the outstanding count appears outside
    // this view, so it is pushed rather than polled.
    hooks.onCount?.(onList);
    hooks.onRibbon?.();
    hooks.onTiers?.(c);
    hooks.onQueue?.(byCause());

    // Tabs. A tier holding more than a screenful shows "24 of 489" rather than
    // 489, so the number never implies more work than the list actually presents.
    for (const k of ["urgent", "check", "later", "done"]) {
      const b = document.querySelector(`.rv-tab[data-tab="${k}"]`);
      if (!b) continue;
      b.querySelector(".n").textContent = String(c[k]);
      b.title = k === "done"
        ? "Decisions you have made in this session"
        : c[k + "Behind"]
          ? `${c[k]} on today's list · ${c[k + "Behind"].toLocaleString()} more behind them`
          : `${c[k]} on today's list — that is all of them`;
      b.setAttribute("aria-selected", String(state.tab === k));
      b.hidden = k === "done" && c.done === 0;
    }

    renderSummary();

    const list = forTab(state.tab);
    if (state.cursor >= list.length) state.cursor = Math.max(0, list.length - 1);
    $("rvList").innerHTML = list.length
      ? list.map(card).join("")
      : emptyState(c);

    if (state.editing) {
      const inp = $("rvList").querySelector(".rc-input");
      if (inp) {
        inp.focus(); inp.select();
        // The guardrail and the impact figure are correct from the moment the form
        // opens, rather than only after the first keystroke.
        paintEditFeedback(inp.closest(".rc"));
      }
    }
  }

  /* Decisions used to accumulate with nowhere to go: you could clear the whole
     list and the only trace was a tab labelled "Reviewed". The spreadsheet has a
     Commit button and the front door did not, which meant the primary surface had
     no terminal action at all. This bar is that ending — what you did, what it is
     worth, and the two things you might want to do with it. */
  function renderSummary() {
    const bar = $("rvSummary");
    if (!bar) return;
    // Only *uncommitted* decisions are pending. Committing used to clear the whole
    // reviewed map, which meant all twenty-four cards reappeared on the list the
    // moment you finished them — the commit undid the review.
    const pending = [...state.reviewed.keys()].filter((id) => !state.committed.has(id));
    const n = pending.length;
    bar.hidden = n === 0;
    if (!n) return;

    let changed = 0;
    for (const id of pending) if (state.reviewed.get(id).action === "changed") changed++;
    const impact = hooks.impact ? hooks.impact() : 0;

    $("rvSummaryText").innerHTML =
      `<b>${n}</b> decision${n === 1 ? "" : "s"} made` +
      (changed ? ` &middot; <b>${changed}</b> number${changed === 1 ? "" : "s"} changed` : "") +
      (changed
        ? ` &middot; expected margin effect <b class="${impact >= 0 ? "rc-up" : "rc-down"}">` +
          `${Plain.money(impact, { sign: true })}</b>`
        : " &middot; nothing changed, so no margin effect");
    $("rvCommit").disabled = n === 0;
  }

  /** What was decided, as data. A planner who has spent twenty minutes on this
      list should be able to take it away — to a buyer, to an email, to a
      spreadsheet — without retyping it. */
  function decisions({ pendingOnly = false } = {}) {
    const out = [];
    for (const [id, d] of state.reviewed) {
      if (pendingOnly && state.committed.has(id)) continue;
      const r = byId(id);
      if (!r) continue;
      const model = week1(r.s);
      out.push({
        product: prettyItem(r.s.item), store: prettyStore(r.s.store),
        category: Plain.catWord(r.s.cat), id,
        flag: r.head.label,
        action: d.action,
        committed: state.committed.has(id) ? "yes" : "no",
        modelForecast: Math.round(model),
        yourValue: d.action === "changed" ? d.value : Math.round(model),
        change: d.action === "changed" ? Math.round(d.value - model) : 0,
        moneyAtRisk: Math.round(r.risk),
        // The audit trail. This is the only part of a decision that cannot be
        // reconstructed from the data afterwards, so it is the part most worth
        // getting out of the browser.
        reason: d.reason ? (Plain.OVERRIDE_BY_KEY[d.reason]?.label || d.reason) : "",
        note: d.note || "",
        expectedEffect: d.action === "changed" && hooks.impactOf
          ? Math.round(hooks.impactOf(r.s, d.value)) : 0,
      });
    }
    return out;
  }

  function emptyState(c) {
    const msg = state.tab === "urgent"
      ? c.done
        ? { h: "That's today's list cleared.",
            p: `You made ${c.done} ${c.done === 1 ? "decision" : "decisions"}. ` +
               (c.check ? `${c.check} more are worth a look when you have time.`
                        : "Nothing else needs checking."),
            cta: c.check ? "check" : null }
        : { h: "Nothing urgent right now.",
            p: "No product is at meaningful risk of running out today.", cta: "check" }
      : state.tab === "check"
        ? { h: "Nothing to check.", p: "No data-quality flags outstanding.", cta: null }
        : state.tab === "later"
          ? { h: "Nothing waiting.", p: "The low-priority queue is empty.", cta: null }
          : { h: "Nothing reviewed yet.", p: "Decisions you make will be listed here.", cta: null };
    return `<div class="rv-empty">
      <div class="rv-empty-mark" aria-hidden="true">✓</div>
      <h3>${U.esc(msg.h)}</h3>
      <p>${U.esc(msg.p)}</p>
      ${msg.cta ? `<button class="btn" data-goto="${msg.cta}">See what to check</button>` : ""}
    </div>`;
  }

  /* ------------------------------------------------------------- keyboard

     The spreadsheet has nine keybindings and the front door had none, which is
     backwards: the review list is the surface a planner works every morning, and
     twenty-four decisions is twenty-four round trips to the mouse. The letters are
     chosen to be sayable — Approve, Change, Skip, Open, Why — rather than to
     mirror the grid's spreadsheet conventions, because this is not a spreadsheet.

     The ring only appears once a key has actually been pressed. A focus ring
     sitting on the first card before anyone has touched the keyboard reads as a
     stuck state. */

  const inField = () =>
    /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement?.tagName || "");

  function moveCursor(delta) {
    const list = forTab(state.tab);
    if (!list.length) return;
    state.keyboard = true;
    state.cursor = Math.max(0, Math.min(list.length - 1, state.cursor + delta));
    render();
    const el = $("rvList").querySelector('.rc[data-focus="true"]');
    el?.scrollIntoView({ block: "nearest" });
  }

  function focused() {
    const list = forTab(state.tab);
    return list[Math.min(state.cursor, list.length - 1)] || null;
  }

  function onKey(ev) {
    if (state.tab === "done") return;
    if (ev.metaKey || ev.ctrlKey || ev.altKey) return;

    /* Anything originating inside an open override form belongs to that form.

       Guarding on inField() alone was not enough. The form's own Enter handler sits
       on #rvList and this one sits on document, so a single Enter bubbles through
       both -- and inField() only stops the second when focus happens to be in a text
       field. With focus on the Save button, Enter saved the change here and then
       approved whatever card the cursor was on, two decisions from one keystroke.
       The container is the right test, because it covers the buttons and the reason
       chips as well as the inputs. */
    if (ev.target.closest && ev.target.closest(".rc-edit")) return;

    // Inside the inline editor only Enter and Escape mean anything; the rest is
    // someone typing a number.
    if (inField()) return;

    const r = focused();
    const k = ev.key;

    if (k === "j" || k === "ArrowDown") { ev.preventDefault(); return moveCursor(1); }
    if (k === "k" || k === "ArrowUp") { ev.preventDefault(); return moveCursor(-1); }
    if (!r) return;

    if (k === "a" || k === "Enter") { ev.preventDefault(); state.keyboard = true; return act(r.id, "approve"); }
    if (k === "s") { ev.preventDefault(); state.keyboard = true; return act(r.id, "skip"); }
    if (k === "c") { ev.preventDefault(); state.keyboard = true; return act(r.id, "edit"); }
    if (k === "o") { ev.preventDefault(); return hooks.onOpenProduct?.(r.id); }
    if (k === "w") { ev.preventDefault(); state.keyboard = true; return act(r.id, "toggle"); }
  }

  /* Undo the most recent decision.

     The largest safety gap in the product: a planner who approves thirty items and
     then learns the price feed was stale had no recourse. A Map preserves insertion
     order, so the most recent decision is simply its last key -- but a *committed*
     decision is not undone here, because the planner has already been told it was
     final. */
  function undoLast() {
    const undoable = [...state.reviewed.keys()].filter((id) => !state.committed.has(id));
    if (!undoable.length) return null;
    const id = undoable[undoable.length - 1];
    const r = byId(id);
    state.reviewed.delete(id);
    hooks.onUndo?.(r ? r.s : null);
    render();
    return r ? prettyItem(r.s.item) : id;
  }

  /* -------------------------------------------------------------- actions */

  const clearEdit = () => {
    state.editing = null; state.editReason = ""; state.editNote = ""; state.editValue = null;
  };

  function act(id, action, value, meta) {
    const r = rows.find((x) => x.id === id);
    if (!r) return;

    if (action === "toggle") {
      state.expanded.has(id) ? state.expanded.delete(id) : state.expanded.add(id);
      return render();
    }
    if (action === "edit") {
      clearEdit(); state.editing = id; return render();
    }
    if (action === "cancel") { clearEdit(); return render(); }
    if (action === "undo") {
      state.reviewed.delete(id);
      hooks.onUndo?.(r.s);
      return render();
    }

    if (action === "approve") state.reviewed.set(id, { action: "approved" });
    if (action === "skip") state.reviewed.set(id, { action: "skipped" });
    if (action === "changed") {
      // A change with no reason is not saved. The chips enforce this in the UI; this
      // is the same rule at the one place every path has to pass through.
      if (!meta || !meta.reason) {
        hooks.onToast?.("Pick a reason for the change first");
        return;
      }
      state.reviewed.set(id, {
        action: "changed", value, reason: meta.reason, note: (meta.note || "").trim(),
      });
      hooks.onChange?.(r.s, value);
    }
    clearEdit();
    state.expanded.delete(id);

    // Announce for screen readers and confirm for everyone else, then advance.
    const d = state.reviewed.get(id);
    hooks.onToast?.(
      d.action === "changed"
        ? `${prettyItem(r.s.item)} set to ${Plain.units(value)} units`
        : d.action === "approved"
          ? `${prettyItem(r.s.item)} approved`
          : `${prettyItem(r.s.item)} skipped`);
    render();
  }

  /* ----------------------------------------------------------------- init */

  function init(bundle, h = {}) {
    D = bundle;
    hooks = h;
    Q50 = D.run.chartQuantiles.indexOf(0.5);
    COVER_DAYS = (D.run.reviewPeriod || 7) + (D.run.leadTime || 2);
    if (D.run.evalOrigin && D.run.origin) {
      const d = Math.round(
        (new Date(D.run.origin) - new Date(D.run.evalOrigin)) / 864e5);
      if (d > 0) EVAL_DAYS = d;
    }
    build();

    // One delegated listener for the whole list, so re-rendering a card cannot
    // leave a stale handler behind.
    $("rvList").addEventListener("click", (ev) => {
      const goto = ev.target.closest("[data-goto]");
      if (goto) { state.tab = goto.dataset.goto; return render(); }

      /* Reason chips are handled without a render, so a half-typed number survives
         being given a reason. */
      const chip = ev.target.closest("[data-reason]");
      if (chip) {
        const cardEl = chip.closest(".rc");
        state.editReason = state.editReason === chip.dataset.reason ? "" : chip.dataset.reason;
        cardEl.querySelectorAll("[data-reason]").forEach((b) =>
          b.setAttribute("aria-pressed", String(b.dataset.reason === state.editReason)));
        return paintEditFeedback(cardEl);
      }

      const btn = ev.target.closest("[data-act]");
      if (!btn) return;
      const cardEl = btn.closest(".rc");
      const id = cardEl.dataset.id;
      const a = btn.dataset.act;
      if (a === "save") {
        const v = Math.max(0, Math.round(+cardEl.querySelector(".rc-input").value || 0));
        const note = cardEl.querySelector('[data-role="note"]')?.value || "";
        return act(id, "changed", v, { reason: state.editReason, note });
      }
      if (a === "open") return hooks.onOpenProduct?.(id);
      // A mouse click also moves the keyboard cursor, so the two never disagree.
      const idx = forTab(state.tab).findIndex((r) => r.id === id);
      if (idx >= 0) state.cursor = idx;
      act(id, a);
    });

    $("rvList").addEventListener("keydown", (ev) => {
      const cardEl = ev.target.closest(".rc");
      if (!cardEl) return;
      const inNote = ev.target.classList.contains("rc-note");
      if (!ev.target.classList.contains("rc-input") && !inNote) return;
      if (ev.key === "Enter") {
        ev.preventDefault();
        // Belt as well as braces: this key has been handled, so it does not travel on
        // to the document listener that reads Enter as "approve".
        ev.stopPropagation();
        const v = Math.max(0, Math.round(+cardEl.querySelector(".rc-input").value || 0));
        const note = cardEl.querySelector('[data-role="note"]')?.value || "";
        act(cardEl.dataset.id, "changed", v, { reason: state.editReason, note });
      }
      if (ev.key === "Escape") {
        ev.preventDefault(); ev.stopPropagation(); act(cardEl.dataset.id, "cancel");
      }
    });

    /* Live guardrail and impact. Both are written into the open form rather than
       re-rendered, so the caret and any partial input survive. The typed value is
       mirrored into state so that a render triggered from elsewhere -- the sidebar,
       a scope change -- does not reset the field to the model's number. */
    $("rvList").addEventListener("input", (ev) => {
      const cardEl = ev.target.closest(".rc");
      if (!cardEl) return;
      if (ev.target.classList.contains("rc-input")) {
        state.editValue = Math.max(0, Math.round(+ev.target.value || 0));
      }
      if (ev.target.classList.contains("rc-note")) state.editNote = ev.target.value;
      if (ev.target.classList.contains("rc-input") ||
          ev.target.classList.contains("rc-note")) paintEditFeedback(cardEl);
    });

    document.querySelectorAll(".rv-tab").forEach((b) =>
      b.addEventListener("click", () => {
        state.tab = b.dataset.tab; state.cursor = 0; render();
      }));

    $("rvCommit").addEventListener("click", () => {
      const rows = decisions({ pendingOnly: true });
      if (!rows.length) return;
      hooks.onCommit?.(rows);
      // Committed items stay reviewed — they are done, not undone.
      for (const r of rows) state.committed.add(r.id);
      render();
    });
    // Export the whole session, committed or not: the file is a record of what was
    // decided, and a planner emailing it does not care which half was committed.
    $("rvExport").addEventListener("click", () => hooks.onExport?.(decisions()));

    // Scoped to the document but inert unless Today is the visible view, so the
    // spreadsheet's own bindings are never shadowed.
    document.addEventListener("keydown", (ev) => {
      if (document.getElementById("view-today")?.dataset.active !== "true") return;
      if (document.querySelector('.palette[data-open="true"], .sheet-modal[data-open="true"]')) return;
      onKey(ev);
    });

    render();
  }

  /** Exposure the session has covered, and the size of the day's list. Both are
      figures the sidebar reports and neither existed anywhere before. */
  function progress() {
    const size = cohort.urgent.length;
    const decidedIds = [...state.reviewed.keys()];
    return {
      decided: decidedIds.length,
      size,
      covered: decidedIds.map(byId).filter(Boolean).reduce((a, r) => a + r.risk, 0),
      changed: [...state.reviewed.values()].filter((d) => d.action === "changed").length,
      canUndo: decidedIds.some((id) => !state.committed.has(id)),
    };
  }

  /** Watched products with the movement a planner pinned them to watch. */
  function watchStatus(limit = 3) {
    const out = [];
    for (const id of watched) {
      const s2 = D.series.find((x) => x.id === id);
      if (!s2) continue;
      const now = week1(s2);
      const ch = Plain.change(now, priorWeek1(s2));
      out.push({
        id, name: prettyItem(s2.item), store: prettyStore(s2.store),
        word: !ch || ch.dir === "flat" ? "steady"
              : `${ch.pct != null ? (ch.dir === "up" ? "+" : "-") + ch.pct + "%" : ch.word}`,
        dir: ch ? ch.dir : "flat",
      });
    }
    out.sort((a, b) => (a.dir === "flat") - (b.dir === "flat"));
    return { shown: out.slice(0, limit), total: out.length };
  }

  return { init, render, decisions, counts, setScope, setWatched, chartKey,
           outcomeBlock, blendBlock, costCurve, get evalDays() { return EVAL_DAYS; },
           byCause, setCause, undoLast, progress, watchStatus,
           get cause() { return state.cause; },
           get state() { return state; }, get scope() { return scope; },
           prettyItem, prettyStore, get rows() { return rows; },
           setTab(t) { state.tab = t; state.cursor = 0; render(); } };
})();
