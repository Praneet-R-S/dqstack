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
    limit: 24,
    cursor: 0,                   // keyboard position within the visible list
    keyboard: false,             // only paint the ring once a key has been used
  };

  /* ------------------------------------------------------------- the queue */

  /** Money at risk is the honest ranking key: lost margin is what the ledger
      says this item costs if nothing changes. Priority breaks ties, because a
      brand-new product can be urgent while having no history of loss yet. */
  const atRisk = (s) => s.ledger.lostMargin + s.ledger.wasteCost;

  function build() {
    Plain.calibrate(D.series);
    rows = D.series
      .filter((s) => s.reasons.length > 0)
      .map((s) => {
        const head = Plain.headline(s.reasons);
        return {
          s, id: s.id, head,
          risk: atRisk(s),
          tier: head.tone === "urgent" || atRisk(s) >= 200 ? "urgent"
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

  const forTab = (tab) => {
    if (tab === "done") {
      return rows.filter((r) => state.reviewed.has(r.id));
    }
    return cohort[tab].filter((id) => !state.reviewed.has(id)).map(byId).filter(Boolean);
  };

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
  function spark(s, w = 300, h = 104) {
    const HIST = 35;
    const hist = s.hist.slice(-HIST);
    const fan = s.fan;                     // 28 days x 7 quantiles
    const n = hist.length + fan.length;

    let hi = 0;
    for (const v of hist) hi = Math.max(hi, v);
    for (const f of fan) hi = Math.max(hi, f[5]);
    hi = hi || 1;

    const x = (i) => (w * i) / (n - 1);
    const y = (v) => h - 5 - ((h - 14) * v) / hi;

    const histLine = hist.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");

    const j0 = hist.length - 1;
    const anchorY = y(hist[j0] ?? 0);

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

    return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"
                 role="img" aria-label="Sales for the last ${hist.length} days and the forecast for the next ${fan.length}, with its likely range">
      <rect class="fan-future" x="${x(j0).toFixed(1)}" y="0"
            width="${(w - x(j0)).toFixed(1)}" height="${h}"/>
      ${bands}
      <path d="${histLine}" class="spark-hist"/>
      <path d="${mid}" class="spark-fc"/>
      <line x1="${x(j0).toFixed(1)}" y1="2" x2="${x(j0).toFixed(1)}" y2="${h - 2}" class="spark-now"/>
      <circle cx="${x(j0).toFixed(1)}" cy="${anchorY.toFixed(1)}" r="2.6" class="spark-last"/>
    </svg>`;
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

    return `<article class="rc" data-id="${e(r.id)}" data-tone="${r.head.tone}"
             ${isFocus ? 'data-focus="true"' : ""}
             ${done ? `data-done="${e(done.action)}"` : ""}>

      <header class="rc-top">
        <div class="rc-risk" title="Profit expected to be lost if nothing changes">
          <span class="rc-risk-amt">${Plain.money(r.risk)}</span>
          <span class="rc-risk-cap">at risk</span>
        </div>
        <div class="rc-id">
          <h3>${e(prettyItem(s.item))}</h3>
          <p>${e(prettyStore(s.store))} · ${e(catWord(s.cat))} · ${e(Plain.CLASS[s.cls]?.label || s.cls)}</p>
        </div>
        ${done ? `<div class="rc-stamp">${e(doneWord(done))}</div>` : ""}
      </header>

      <p class="rc-why" title="${e(r.head.term)}">
        <span class="rc-dot"></span><b>${e(r.head.label)}</b> — ${e(r.head.detail)}
      </p>

      <div class="rc-chart">
        ${spark(s)}
        <div class="rc-chart-key">
          <span><i class="k-hist"></i>sold so far</span>
          <span><i class="k-fc"></i>forecast</span>
          <span><i class="k-band"></i>likely range</span>
        </div>
      </div>

      <div class="rc-figs">
        <div class="rc-fig rc-fig-main">
          <span class="rc-fig-cap">Forecast for next week</span>
          <span class="rc-fig-val">${Plain.units(shown)}<small>units</small></span>
          <span class="rc-fig-sub">likely between ${Plain.units(b.lo)} and ${Plain.units(b.hi)}</span>
        </div>
        <div class="rc-fig">
          <span class="rc-fig-cap">Versus last week</span>
          <span class="rc-fig-word rc-${ch ? ch.dir : "flat"}">${ch ? e(ch.word) : "—"}</span>
          <span class="rc-fig-sub">${e(changeNote(ch))}</span>
        </div>
        <div class="rc-fig" title="Order-up-to level: the predictive quantile of total demand over the review period plus lead time, at this product's own critical fractile.">
          <span class="rc-fig-cap">Stock up to</span>
          <span class="rc-fig-val">${Plain.units(s.orderUpTo)}<small>units</small></span>
          <span class="rc-fig-sub">covers ${COVER_DAYS} days, to ${svc.pct}% service</span>
        </div>
        <div class="rc-fig">
          <span class="rc-fig-cap">Data quality</span>
          <span class="rc-fig-word rc-q-${q.tone}">${e(q.word)}</span>
          <span class="rc-fig-sub">confidence ${e(conf.word.toLowerCase())}</span>
        </div>
      </div>

      ${editing ? `
      <div class="rc-edit">
        <label for="ed-${e(r.id)}">Use this number instead</label>
        <input id="ed-${e(r.id)}" class="rc-input" type="number" min="0" step="1"
               value="${Math.round(now)}" inputmode="numeric">
        <span class="rc-edit-unit">units next week</span>
        <button class="btn btn-primary" data-act="save">Save</button>
        <button class="btn btn-quiet" data-act="cancel">Cancel</button>
      </div>` : `
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
          ${open ? "Hide detail" : "Why this number?"}
        </button>
      </div>`}

      ${open ? detail(s, secondary, q, conf, svc, r) : ""}
    </article>`;
  }

  /** The expansion. Everything here was previously the only way to see any of
      it; now it is opt-in, one click, and written in sentences. */
  function detail(s, secondary, q, conf, svc, r) {
    const e = U.esc;
    const drivers = (s.why || []).slice(0, 5);
    const maxAbs = Math.max(1e-9, ...drivers.map((d) => Math.abs(d.contribution)));

    return `<div class="rc-detail">
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

      <section>
        <h4>How much to trust it</h4>
        <dl class="rc-dl">
          <dt>Data quality</dt><dd>${e(q.word)} — ${e(q.note)}</dd>
          <dt>Confidence</dt><dd>${e(conf.word)} — ${e(conf.note)}</dd>
          ${s.censoredDays ? `<dt>Possible stock-outs</dt>
            <dd>${s.censoredDays} day${s.censoredDays === 1 ? "" : "s"} in the history
                recorded no sales while the product was probably unavailable.
                Real demand was higher than the sales figures show.</dd>` : ""}
          ${s.ooaDays ? `<dt>Not stocked</dt>
            <dd>${s.ooaDays} day${s.ooaDays === 1 ? "" : "s"} when this product was not
                carried at this store at all. Those days are excluded rather than
                counted as zero demand.</dd>` : ""}
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
    return `Changed to ${Plain.units(d.value)} units` +
           (ch && ch.dir !== "flat" ? ` (${ch.word}).` : ".");
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
      if (inp) { inp.focus(); inp.select(); }
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

  /* -------------------------------------------------------------- actions */

  function act(id, action, value) {
    const r = rows.find((x) => x.id === id);
    if (!r) return;

    if (action === "toggle") {
      state.expanded.has(id) ? state.expanded.delete(id) : state.expanded.add(id);
      return render();
    }
    if (action === "edit") { state.editing = id; return render(); }
    if (action === "cancel") { state.editing = null; return render(); }
    if (action === "undo") {
      state.reviewed.delete(id);
      hooks.onUndo?.(r.s);
      return render();
    }

    if (action === "approve") state.reviewed.set(id, { action: "approved" });
    if (action === "skip") state.reviewed.set(id, { action: "skipped" });
    if (action === "changed") {
      state.reviewed.set(id, { action: "changed", value });
      hooks.onChange?.(r.s, value);
    }
    state.editing = null;
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
    build();

    // One delegated listener for the whole list, so re-rendering a card cannot
    // leave a stale handler behind.
    $("rvList").addEventListener("click", (ev) => {
      const goto = ev.target.closest("[data-goto]");
      if (goto) { state.tab = goto.dataset.goto; return render(); }

      const btn = ev.target.closest("[data-act]");
      if (!btn) return;
      const cardEl = btn.closest(".rc");
      const id = cardEl.dataset.id;
      const a = btn.dataset.act;
      if (a === "save") {
        const v = Math.max(0, Math.round(+cardEl.querySelector(".rc-input").value || 0));
        return act(id, "changed", v);
      }
      if (a === "open") return hooks.onOpenProduct?.(id);
      // A mouse click also moves the keyboard cursor, so the two never disagree.
      const idx = forTab(state.tab).findIndex((r) => r.id === id);
      if (idx >= 0) state.cursor = idx;
      act(id, a);
    });

    $("rvList").addEventListener("keydown", (ev) => {
      if (!ev.target.classList.contains("rc-input")) return;
      const cardEl = ev.target.closest(".rc");
      if (ev.key === "Enter") {
        ev.preventDefault();
        act(cardEl.dataset.id, "changed",
            Math.max(0, Math.round(+ev.target.value || 0)));
      }
      if (ev.key === "Escape") { ev.preventDefault(); act(cardEl.dataset.id, "cancel"); }
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

  return { init, render, decisions, get state() { return state; },
           prettyItem, prettyStore, get rows() { return rows; } };
})();
