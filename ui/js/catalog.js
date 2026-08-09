/* ============================================================================
   Products — browse the range, see one product, change the range.

   This view exists because three separate things were wrong.

   Search went nowhere. Picking a result called jumpTo(), which selected a row in
   the spreadsheet — but the spreadsheet was not the visible view, so from the
   Today screen a search did nothing at all. A search result now opens the product
   here, which is a place designed to show one product.

   There was no way to look through the range. The only listing was the canvas
   grid, which is a bulk-editing instrument: thirty columns, keyboard-driven, and
   the wrong tool entirely for "show me that product". A list beside a detail pane
   is the oldest layout in software because it is the right one for browsing.

   There was no way to add or remove a product. The system could forecast an
   assortment but not change one, which is a strange gap in something a planner
   uses daily.

   On adding: a new product genuinely has no forecast. The pipeline produces those
   in batch overnight, and CON-3 forbids running inference on the read path, so
   inventing a number in the browser would be dishonest twice over. Instead a new
   product is marked as awaiting its first forecast and shown an indicative figure
   taken from the median of comparable products in the same category and store —
   which is, in substance, what the cold-start model does, stated as the estimate
   it is rather than dressed up as a forecast.

   Adding and removing are session-local and clearly labelled as pending, exactly
   like forecast overrides, because there is no write path to a system of record.
   ============================================================================ */

window.Catalog = (() => {
  const $ = (id) => document.getElementById(id);

  let D = null;
  let Q50 = 3;
  let hooks = {};
  let all = [];                    // every product, pipeline plus session-added

  const state = {
    query: "",
    cat: "",
    flagged: false,
    store: "",
    sort: "name",
    selected: null,
    added: new Map(),              // id -> product (session-local)
    removed: new Set(),            // ids marked for removal
    limit: 200,
  };

  const COVER = () => (D.run.reviewPeriod || 7) + (D.run.leadTime || 2);

  /* Added products are named by the planner, so they must not be run through the
     M5 identifier prettifier — it does not match `HOBBIES_NEW_001` and fell back
     to printing the raw key. */
  const name = (s) => s._name || Review.prettyItem(s.item);
  const isNewProduct = (s) => !!s._new;

  /* ------------------------------------------------------------- the list */

  /* A planner is usually responsible for a store or a region, and the list was
     alphabetical across all ten of them with no way to narrow or reorder — so
     finding "the worst thing in my store" meant reading a thousand rows. */
  const atRisk = (s) => (s.ledger?.lostMargin || 0) + (s.ledger?.wasteCost || 0);

  const SORTS = {
    name: (a, b) => (name(a) < name(b) ? -1 : name(a) > name(b) ? 1 : 0),
    risk: (a, b) => atRisk(b) - atRisk(a),
    revenue: (a, b) => (b.revenue || 0) - (a.revenue || 0),
    demand: (a, b) => week1(b) - week1(a),
    health: (a, b) => (a.health || 0) - (b.health || 0),
  };

  const visible = () => {
    const q = state.query.trim().toLowerCase();
    const out = all.filter((s) => {
      if (state.cat && s.cat !== state.cat) return false;
      if (state.store && s.store !== state.store) return false;
      if (state.flagged && !s.reasons.length) return false;
      if (!q) return true;
      return s.item.toLowerCase().includes(q) || s.store.toLowerCase().includes(q) ||
             s.id.toLowerCase().includes(q) || s.dept.toLowerCase().includes(q) ||
             name(s).toLowerCase().includes(q);
    });
    // Tie-broken on name so the order is stable and does not shuffle on re-render.
    const cmp = SORTS[state.sort] || SORTS.name;
    return out.sort((a, b) => cmp(a, b) || SORTS.name(a, b));
  };

  function renderList() {
    const list = visible();
    const shown = list.slice(0, state.limit);
    $("catCount").textContent = list.length === all.length
      ? `${all.length.toLocaleString()} products`
      : `${list.length.toLocaleString()} of ${all.length.toLocaleString()}`;

    $("catList").innerHTML = shown.length
      ? shown.map((s) => {
          const removed = state.removed.has(s.id);
          const isNew = state.added.has(s.id);
          const head = s.reasons.length ? Plain.headline(s.reasons) : null;
          return `<button class="cat-row" data-id="${U.esc(s.id)}"
                    aria-current="${state.selected === s.id}"
                    ${removed ? 'data-removed="true"' : ""}>
            <span class="cat-row-dot" style="background:${
              head ? `var(--state-${head.tone === "urgent" ? "risk"
                                   : head.tone === "warn" ? "attention" : "ok"})`
                   : "var(--border-strong)"}"></span>
            <span class="cat-row-main">
              <span class="cat-row-name">${U.esc(name(s))}</span>
              <span class="cat-row-sub">${U.esc(Review.prettyStore(s.store))}</span>
            </span>
            <span class="cat-row-end">
              ${isNew ? '<span class="cat-tag new">new</span>'
                      : removed ? '<span class="cat-tag gone">removing</span>'
                      : `<span class="cat-row-num">${rowNum(s)}</span>`}
            </span>
          </button>`;
        }).join("")
      : `<p class="cat-empty">No product matches &ldquo;${U.esc(state.query)}&rdquo;.</p>`;

    $("catMore").hidden = list.length <= state.limit;
    $("catMore").textContent = `Show ${Math.min(200, list.length - state.limit)} more ` +
      `(${(list.length - state.limit).toLocaleString()} not shown)`;
  }

  /** Whatever the list is sorted by is what the trailing figure shows. Sorting by
      "most at risk" while displaying unit forecasts looks like a broken sort. */
  function rowNum(s) {
    switch (state.sort) {
      case "risk": return Plain.money(atRisk(s));
      case "revenue": return Plain.money(s.revenue || 0);
      case "health": return (s.health ?? 0).toFixed(2);
      default: return Plain.units(week1(s));
    }
  }

  /* ----------------------------------------------------------- the detail */

  const week1 = (s) => (s.weekly ? s.weekly[0][Q50] : 0);

  /** A larger chart than the review card's sparkline: this is the one place a
      planner comes specifically to look at a single product's shape, so it gets
      axis labels and a legend rather than a 68px glyph. */
  function chart(s, w = 720, h = 220) {
    const PAD = { l: 46, r: 12, t: 12, b: 26 };
    const HIST = 56;
    const hist = (s.hist || []).slice(-HIST);
    const fan = s.fan || [];
    const n = hist.length + fan.length;
    if (!n) return '<p class="cat-nochart">No history to draw yet.</p>';

    let hi = 0;
    for (const v of hist) hi = Math.max(hi, v);
    for (const f of fan) hi = Math.max(hi, f[5]);
    hi = Math.max(hi, 1);
    // A 1/2/5 ladder rather than pure powers of ten: the latter rounds a peak of
    // 210 up to 300 and throws away a third of the plot height.
    const mag = Math.pow(10, Math.floor(Math.log10(hi)));
    const top = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((v) => v >= hi) || 10 * mag;

    const iw = w - PAD.l - PAD.r, ih = h - PAD.t - PAD.b;
    const x = (i) => PAD.l + (iw * i) / (n - 1);
    const y = (v) => PAD.t + ih - (ih * v) / top;

    const ticks = [0, top / 2, top].map((v) =>
      `<line x1="${PAD.l}" y1="${y(v).toFixed(1)}" x2="${w - PAD.r}" y2="${y(v).toFixed(1)}" class="cc-grid"/>
       <text x="${PAD.l - 8}" y="${(y(v) + 4).toFixed(1)}" class="cc-tick">${Plain.units(v)}</text>`).join("");

    const histLine = hist.map((v, i) =>
      `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");

    const j0 = hist.length - 1;
    const anchorY = y(hist[j0] ?? 0);

    // Same three nested levels as review.js and chart.js. One statistical object,
    // one rendering, on every surface that draws it.
    const LEVELS = [[0, 6, "cc-98"], [1, 5, "cc-80"], [2, 4, "cc-50"]];
    const bands = !fan.length ? "" : LEVELS.map(([lo, hiQ, cls]) => {
      const top = [`M${x(j0).toFixed(1)},${anchorY.toFixed(1)}`];
      const bot = [];
      fan.forEach((f, k) => {
        const xi = x(j0 + 1 + k);
        top.push(`L${xi.toFixed(1)},${y(f[hiQ]).toFixed(1)}`);
        bot.push(`${xi.toFixed(1)},${y(f[lo]).toFixed(1)}`);
      });
      return `<path d="${top.join("")}L${bot.reverse().join("L")}Z" class="${cls}"/>`;
    }).join("");

    const mid = fan.map((f, k) =>
      `${k ? "L" : "M"}${x(j0 + 1 + k).toFixed(1)},${y(f[Q50]).toFixed(1)}`);

    /* The prior version, as a ghost. The product page is where a planner asks "did
       this move since last cycle?" and the answer was available only as a word on
       the review card. chart.js already renders this; the data, the colour token
       and a reference implementation all existed, so this is porting. Negative
       sentinels mark days the earlier vintage did not cover, and break the path. */
    let prior = "", started = false;
    (s.prior || []).forEach((v, k) => {
      if (v < 0) { started = false; return; }
      const xi = x(j0 + 1 + k);
      prior += `${started ? "L" : "M"}${xi.toFixed(1)},${y(v).toFixed(1)}`;
      started = true;
    });

    return `<svg class="cat-chart" viewBox="0 0 ${w} ${h}" role="img"
      aria-label="Daily sales for the last ${hist.length} days and the forecast for the next ${fan.length} days, with its likely range">
      <rect class="fan-future" x="${x(j0).toFixed(1)}" y="${PAD.t}"
            width="${(w - PAD.r - x(j0)).toFixed(1)}" height="${ih}"/>
      ${ticks}
      ${bands}
      ${prior ? `<path d="${prior}" class="cc-prior"/>` : ""}
      <path d="${histLine}" class="cc-hist"/>
      ${mid.length ? `<path d="${mid.join("")}" class="cc-fc"/>` : ""}
      <line x1="${x(j0).toFixed(1)}" y1="${PAD.t}" x2="${x(j0).toFixed(1)}" y2="${PAD.t + ih}" class="cc-now"/>
      <circle cx="${x(j0).toFixed(1)}" cy="${anchorY.toFixed(1)}" r="3" class="cc-last"/>
      <text x="${(x(j0) + 6).toFixed(1)}" y="${PAD.t + 12}" class="cc-nowlab">today</text>
      <text x="${PAD.l}" y="${h - 8}" class="cc-tick cc-start">${hist.length} days of sales</text>
      <text x="${w - PAD.r}" y="${h - 8}" class="cc-tick cc-end">${fan.length}-day forecast</text>
    </svg>`;
  }

  function renderDetail() {
    const host = $("catDetail");
    const s = all.find((x) => x.id === state.selected);
    if (!s) {
      host.innerHTML = `<div class="cat-blank">
        <div class="cat-blank-mark" aria-hidden="true"></div>
        <h3>Pick a product</h3>
        <p>Choose one from the list to see how it has been selling, what it is
           forecast to sell next, and how much to stock.</p>
      </div>`;
      return;
    }

    const e = U.esc;
    const isNew = state.added.has(s.id);
    const removed = state.removed.has(s.id);
    // A brand-new product has no history, so "Poor data quality" is the wrong
    // statement — there is nothing to judge yet, which is a different thing from
    // judging it badly.
    const q = isNewProduct(s) ? Plain.qualityUnknown() : Plain.quality(s.health);
    const conf = Plain.confidence(s.relWidth);
    const svc = Plain.service(s);
    const b = s.weekly ? { lo: s.weekly[0][1], hi: s.weekly[0][5] } : { lo: 0, hi: 0 };
    const flags = s.reasons.map((k) => Plain.REASON_BY_KEY[k]).filter(Boolean);
    const drivers = (s.why || []).slice(0, 4);
    const maxAbs = Math.max(1e-9, ...drivers.map((d) => Math.abs(d.contribution)));

    host.innerHTML = `
      <header class="cat-head">
        <div>
          <h2>${e(name(s))}</h2>
          <p>${e(Review.prettyStore(s.store))} &middot; ${e(Plain.catWord(s.cat))}
             &middot; ${e(Plain.CLASS[s.cls]?.label || s.cls)}</p>
        </div>
        <div class="cat-head-act">
          ${removed
            ? `<span class="cat-tag gone big">Marked for removal</span>
               <button class="btn" data-act="undo-remove">Keep it</button>`
            : `<button class="btn btn-danger" data-act="remove">Remove from range</button>`}
        </div>
      </header>

      ${isNew ? `<div class="cat-note info">
        <b>Awaiting its first forecast.</b> New products are forecast by the
        overnight run, so the figures below are an <b>estimate</b> taken from the
        median of comparable products in the same category and store &mdash; not a
        forecast from this product's own history, because it does not have one yet.
        The <b>service level is real</b>: it comes from the price, cost and shelf
        life you entered. The <b>stock level cannot be</b> until there is a forecast
        to take a quantile of.
      </div>` : ""}

      ${removed ? `<div class="cat-note warn">
        <b>Pending removal.</b> Nothing has been sent anywhere &mdash; this is a
        draft change, the same as an edited forecast. It currently earns
        <b>${Plain.money(s.ledger?.realizedMargin || 0)}</b> of margin over the
        simulated period.
      </div>` : ""}

      ${flags.length ? `<div class="cat-flags">
        ${flags.map((f) => `<span class="cat-flag t-${f.tone}" title="${e(f.detail)}">
          ${e(f.label)}</span>`).join("")}
      </div>` : ""}

      <div class="cat-chartbox">
        ${chart(s)}
        <div class="rc-chart-key">
          <span><i class="k-hist"></i>sold so far</span>
          <span><i class="k-fc"></i>forecast</span>
          <span><i class="k-band"></i>likely range</span>
          <span><i class="k-prior"></i>last week's version</span>
        </div>
      </div>

      <div class="rc-figs">
        <div class="rc-fig rc-fig-main">
          <span class="rc-fig-cap">${isNew ? "Estimated" : "Forecast"} for next week</span>
          <span class="rc-fig-val">${Plain.units(week1(s))}<small>units</small></span>
          <span class="rc-fig-sub">likely between ${Plain.units(b.lo)} and ${Plain.units(b.hi)}</span>
        </div>
        <div class="rc-fig" title="Order-up-to level: the predictive quantile of total demand over the review period plus lead time, at this product's own critical fractile.">
          <span class="rc-fig-cap">Stock up to</span>
          ${s.orderUpTo == null
            ? `<span class="rc-fig-word rc-pending">Not yet</span>
               <span class="rc-fig-sub">needs a forecast first &middot; aiming at
                 ${svc.pct}% service</span>`
            : `<span class="rc-fig-val">${Plain.units(s.orderUpTo)}<small>units</small></span>
               <span class="rc-fig-sub">covers ${COVER()} days, to ${svc.pct}% service</span>`}
        </div>
        <div class="rc-fig" title="${e(q.note)}">
          <span class="rc-fig-cap">Data quality</span>
          <span class="rc-fig-word rc-q-${q.tone}">${e(q.word)}</span>
          <span class="rc-fig-sub">${isNewProduct(s)
            ? `estimated from ${s._estimateFrom || 0} similar products`
            : `confidence ${e(conf.word.toLowerCase())}`}</span>
        </div>
        <div class="rc-fig">
          <span class="rc-fig-cap">Sells for</span>
          <span class="rc-fig-val">$${(s.price || 0).toFixed(2)}</span>
          <span class="rc-fig-sub">costs $${(s.cost || 0).toFixed(2)} &middot;
            ${Math.round((s.marginRate || 0) * 100)}% margin</span>
        </div>
      </div>

      <div class="cat-edit">
        <label for="catQty">Change next week&rsquo;s forecast</label>
        <input id="catQty" class="rc-input" type="number" min="0" step="1"
               value="${Math.round(week1(s))}" inputmode="numeric">
        <span class="rc-edit-unit">units</span>
        <button class="btn btn-primary" data-act="save-qty">Save</button>
        <span class="cat-edit-note" id="catSaved"></span>
      </div>

      ${drivers.length ? `<section class="cat-why">
        <h4>What drives this forecast</h4>
        <ul class="rc-drivers">
          ${drivers.map((d) => {
            const pos = d.contribution >= 0;
            return `<li>
              <span class="rd-name">${e(Plain.feature(d.feature))}</span>
              <span class="rd-bar"><i class="${pos ? "up" : "down"}"
                style="width:${((Math.abs(d.contribution) / maxAbs) * 100).toFixed(0)}%"></i></span>
              <span class="rd-val ${pos ? "rc-up" : "rc-down"}">${pos ? "raised" : "lowered"} it</span>
            </li>`;
          }).join("")}
        </ul>
      </section>` : ""}

      ${similarBlock(s)}

      <p class="rc-fine">Internal id <code>${e(s.id)}</code> &middot;
        ${e(svc.why)}</p>`;
  }

  /** The same product in other stores. Useful in two directions: it is the fastest
      sanity check on an odd-looking forecast ("is it just this store?"), and for a
      new or cold-start product it is the evidence the estimate was built from. */
  function similarBlock(s) {
    const peers = all
      .filter((x) => x.item === s.item && x.id !== s.id && !state.removed.has(x.id))
      .sort((a, b) => week1(b) - week1(a))
      .slice(0, 6);
    if (!peers.length) return "";
    const e = U.esc;

    /* Range bars, not median bars.

       This was the only chart in the application that dropped uncertainty
       entirely, while asking the reader to make a judgement — "is this store an
       outlier?" — that a bare median cannot support. A store whose p10-p90
       straddles all of its peers is a completely different situation from one
       whose interval sits clear of them, and the single bar could not tell them
       apart. Everything needed was already on each series and already displayed
       on the figure cards above. */
    const rows = [s, ...peers];
    const lo = (x) => (x.weekly ? x.weekly[0][1] : 0);
    const hiQ = (x) => (x.weekly ? x.weekly[0][5] : 0);
    const scale = Math.max(...rows.map(hiQ), 1);
    const pc = (v) => ((v / scale) * 100).toFixed(1);

    // The cohort's own middle, so "is this one unusual?" has something to be
    // unusual against. Median of the peers' medians, self excluded.
    const meds = peers.map(week1).sort((a, b) => a - b);
    const cohortMed = meds.length ? meds[Math.floor(meds.length / 2)] : 0;

    const bar = (x, self) => `<span class="cp-bar">
        <span class="cp-range" style="left:${pc(lo(x))}%;width:${pc(hiQ(x) - lo(x))}%"></span>
        <span class="cp-med" style="left:${pc(week1(x))}%"></span>
        ${cohortMed > 0 ? `<span class="cp-ref" style="left:${pc(cohortMed)}%"></span>` : ""}
      </span>`;

    return `<section class="cat-similar">
      <h4>The same product in other stores</h4>
      <ul class="cat-peers">
        <li class="is-self">
          <span class="cp-name">${e(Review.prettyStore(s.store))} <b>(this one)</b></span>
          ${bar(s, true)}
          <span class="cp-num">${Plain.units(week1(s))}</span>
        </li>
        ${peers.map((x) => `<li>
          <button class="cp-name cp-link" data-goto="${e(x.id)}">${e(Review.prettyStore(x.store))}</button>
          ${bar(x, false)}
          <span class="cp-num">${Plain.units(week1(x))}</span>
        </li>`).join("")}
      </ul>
      <p class="rc-fine">
        Next week's forecast for the same product, one row per store. The bar is the
        likely range and the notch is the middle of it; the faint upright is the
        typical store. A range that sits clear of the others is worth a look before
        you act on it &mdash; one that merely overlaps them is not.
      </p>
    </section>`;
  }

  /* ------------------------------------------------------- add and remove */

  /** The critical fractile, for a product whose economics the planner just typed.

      This is the one calculation the browser has to reproduce rather than read,
      because the whole argument of the ordering logic is that the service level
      falls out of *this product's* price, cost and shelf life — and for a new
      product those numbers exist only in the form that was just submitted. Copying
      the prototype's target quantile instead, which is what the first version did,
      showed a 65% service level for a 12-day-shelf 38%-margin item and quietly
      contradicted the thesis on the very screen where the planner supplies the
      inputs.

      Mirrors `critical_fractile()` in dqstack/decide.py, including the spoilage
      term being excess cover beyond shelf life rather than a flat fraction — the
      bug fixed there would otherwise be reintroduced here. Constants come from the
      bundle, not from literals, so they cannot drift from config.py. */
  function fractile(price, cost, shelfLife) {
    const days = COVER();
    const marginRate = price > 0 ? (price - cost) / price : 0;
    const underage = price * marginRate;
    const holding = cost * (D.run.holdingRate ?? 0.0022) * days;
    const markdown = cost * (D.run.markdownRisk ?? 0.16);
    const excessCover = Math.min(1, Math.max(0, (days - shelfLife) / Math.max(shelfLife, 1)));
    const spoil = shelfLife < 60 ? cost * excessCover * 0.5 : 0;
    const overage = holding + markdown + spoil;
    const raw = underage / Math.max(underage + overage, 1e-9);
    return {
      q: Math.min(D.run.maxQuantile ?? 0.995,
                  Math.max(D.run.minQuantile ?? 0.5, raw)),
      underage, overage, holding, markdown, spoil, marginRate,
    };
  }

  /** A new product has no history, so its indicative figures come from the median
      of its cohort. Stated as an estimate everywhere it is shown. */
  function estimateFrom(cat, store) {
    const peers = D.series.filter((s) => s.cat === cat && s.store === store);
    const pool = peers.length >= 5 ? peers : D.series.filter((s) => s.cat === cat);
    if (!pool.length) return null;
    const mid = (arr) => {
      const a = arr.slice().sort((x, y) => x - y);
      return a[Math.floor(a.length / 2)];
    };
    const wk = mid(pool.map(week1));
    const scale = wk / Math.max(1e-9, mid(pool.map((s) => s.weekly[0][Q50])));
    const proto = pool[Math.floor(pool.length / 2)];
    return { weeklyBase: wk, proto, scale, n: pool.length };
  }

  function addProduct(form) {
    const est = estimateFrom(form.cat, form.store);
    if (!est) return null;
    const p = est.proto;
    const fr = fractile(form.price, form.cost, form.shelfLife);
    const n = state.added.size + 1;
    const code = `${form.cat}_NEW_${String(n).padStart(3, "0")}`;
    const id = `${code}|${form.store}`;
    const label = (form.name || "").trim() || `New ${Plain.catWord(form.cat)} ${n}`;

    // The shape has to match a pipeline series exactly, because every renderer
    // downstream — the chart, the figures, the review card — reads these fields.
    const s = {
      id, item: code, _name: label,
      store: form.store, cat: form.cat, dept: p.dept, state: p.state,
      cls: "cold_start", health: 0.5, adi: p.adi, cv2: p.cv2,
      abc: "C", xyz: "Z", censoredDays: 0, ooaDays: 0,
      priceCompleteness: 1, ageWeeks: 0,
      revenue: 0, price: form.price, cost: form.cost,
      marginRate: fr.marginRate,
      shelfLife: form.shelfLife,
      // Derived from what was just entered, not inherited from the prototype.
      targetQ: fr.q, underage: fr.underage, overage: fr.overage,
      holdingComp: fr.holding, markdownComp: fr.markdown, spoilComp: fr.spoil,
      // Deliberately null, not inherited. An order-up-to level is the predictive
      // quantile of demand over the risk period, and this product has no predictive
      // distribution yet — the prototype's level was computed at the prototype's own
      // target quantile and so contradicted the service level derived above. A
      // missing number that explains itself beats a confident wrong one.
      orderUpTo: null,
      // No own history: an empty series, not a fabricated one.
      hist: [], histRaw: [], carried: [],
      weekly: p.weekly.map((w) => w.map((v) => Math.round(v * est.scale))),
      fan: p.fan.map((f) => f.map((v) => +(v * est.scale).toFixed(1))),
      prior: p.prior.map(() => -1),
      mean: p.mean.map((v) => v * est.scale),
      sd: p.sd.slice(),
      weights: p.weights, reasons: ["cold-start"], priority: 0.4,
      swing: 0, relWidth: p.relWidth, why: [],
      ledger: { demand: 0, sales: 0, lostMargin: 0, realizedMargin: 0,
                holdingCost: 0, wasteCost: 0, fillRate: 1, orders: 0 },
      _new: true, _estimateFrom: est.n,
    };
    state.added.set(id, s);
    all.push(s);
    all.sort((a, b) => (a.item < b.item ? -1 : a.item > b.item ? 1 : 0));
    state.selected = id;
    hooks.onToast?.(`${name(s)} added — awaiting its first forecast`);
    renderList(); renderDetail(); renderPending();
    return s;
  }

  function removeProduct(id) {
    state.removed.add(id);
    hooks.onToast?.("Marked for removal — nothing sent yet");
    renderList(); renderDetail(); renderPending();
  }

  function undoRemove(id) {
    state.removed.delete(id);
    renderList(); renderDetail(); renderPending();
  }

  function renderPending() {
    const n = state.added.size + state.removed.size;
    const bar = $("catPending");
    bar.hidden = n === 0;
    if (!n) return;
    const bits = [];
    if (state.added.size) bits.push(`${state.added.size} added`);
    if (state.removed.size) bits.push(`${state.removed.size} to remove`);
    $("catPendingText").textContent =
      `${bits.join(" · ")} — draft changes to the range, not yet sent anywhere.`;
  }

  /* -------------------------------------------------------------- the API */

  function select(id) {
    state.selected = id;
    renderList(); renderDetail();
    const row = $("catList").querySelector(`[data-id="${CSS.escape(id)}"]`);
    row?.scrollIntoView({ block: "nearest" });
    $("catDetail").scrollTop = 0;
  }

  /** Called by the command palette. If the product is filtered out of the current
      list, the filters are cleared rather than leaving the user looking at a
      detail pane whose row they cannot see — the previous behaviour was to select
      a row in a view that was not even on screen. */
  function reveal(id) {
    const s = all.find((x) => x.id === id);
    if (!s) return false;
    if (!visible().some((x) => x.id === id)) {
      state.query = ""; state.cat = ""; state.flagged = false; state.store = "";
      $("catSearch").value = "";
      $("catStore").value = "";
      document.querySelectorAll("#catFilters [data-cat]").forEach((b) =>
        b.setAttribute("aria-pressed", String(b.dataset.cat === "")));
      $("catFlagged").setAttribute("aria-pressed", "false");
    }
    // Make sure it is inside the render window.
    const idx = visible().findIndex((x) => x.id === id);
    if (idx >= state.limit) state.limit = Math.ceil((idx + 1) / 200) * 200;
    select(id);
    return true;
  }

  function init(bundle, h = {}) {
    D = bundle; hooks = h;
    Q50 = D.run.chartQuantiles.indexOf(0.5);
    all = D.series.slice().sort((a, b) => (a.item < b.item ? -1 : a.item > b.item ? 1 : 0));

    $("catSearch").addEventListener("input", (e) => {
      state.query = e.target.value; state.limit = 200; renderList();
    });
    $("catFilters").addEventListener("click", (e) => {
      const b = e.target.closest("[data-cat]");
      if (!b) return;
      state.cat = b.dataset.cat; state.limit = 200;
      $("catFilters").querySelectorAll("[data-cat]").forEach((x) =>
        x.setAttribute("aria-pressed", String(x === b)));
      renderList();
    });
    $("catFlagged").addEventListener("click", (e) => {
      state.flagged = !state.flagged;
      e.currentTarget.setAttribute("aria-pressed", String(state.flagged));
      state.limit = 200; renderList();
    });
    $("catList").addEventListener("click", (e) => {
      const b = e.target.closest(".cat-row");
      if (b) select(b.dataset.id);
    });
    $("catStore").addEventListener("change", (e) => {
      state.store = e.target.value; state.limit = 200; renderList();
    });
    $("catSort").addEventListener("change", (e) => {
      state.sort = e.target.value; state.limit = 200; renderList();
    });
    $("catMore").addEventListener("click", () => { state.limit += 200; renderList(); });

    $("catDetail").addEventListener("click", (e) => {
      const goto = e.target.closest("[data-goto]");
      if (goto) return select(goto.dataset.goto);
      const b = e.target.closest("[data-act]");
      if (!b) return;
      if (b.dataset.act === "remove") removeProduct(state.selected);
      if (b.dataset.act === "undo-remove") undoRemove(state.selected);
      if (b.dataset.act === "save-qty") {
        const v = Math.max(0, Math.round(+$("catQty").value || 0));
        hooks.onChange?.(all.find((x) => x.id === state.selected), v);
        $("catSaved").textContent = `Saved — ${Plain.units(v)} units`;
        setTimeout(() => { const el = $("catSaved"); if (el) el.textContent = ""; }, 2600);
      }
    });

    // Add-product dialog.
    const stores = [...new Set(D.series.map((s) => s.store))].sort();
    $("catStore").innerHTML = '<option value="">All stores</option>' + stores.map((x) =>
      `<option value="${U.esc(x)}">${U.esc(Review.prettyStore(x))}</option>`).join("");
    $("apStore").innerHTML = stores.map((s) =>
      `<option value="${U.esc(s)}">${U.esc(Review.prettyStore(s))}</option>`).join("");
    $("catAdd").addEventListener("click", () => {
      $("addBox").dataset.open = "true"; $("addBackdrop").dataset.open = "true";
      $("apError").textContent = "";
      $("apName").focus();
    });
    const closeAdd = () => {
      $("addBox").dataset.open = "false"; $("addBackdrop").dataset.open = "false";
    };
    $("apCancel").addEventListener("click", closeAdd);
    $("addBackdrop").addEventListener("click", closeAdd);
    const previewFractile = () => {
      const price = +$("apPrice").value || 0;
      const cost = +$("apCost").value || 0;
      const shelf = Math.max(1, +$("apShelf").value || 30);
      const el = $("apPreview");
      if (!(price > 0) || cost >= price) { el.textContent = ""; return; }
      const fr = fractile(price, cost, shelf);
      el.innerHTML = `Service level for these numbers: <b>${Math.round(fr.q * 100)}%</b> ` +
        `&mdash; ${Math.round(fr.marginRate * 100)}% margin against ` +
        `$${fr.overage.toFixed(2)} of holding, markdown and spoilage risk per unit.`;
    };
    ["apPrice", "apCost", "apShelf"].forEach((id) =>
      $(id).addEventListener("input", previewFractile));
    $("catAdd").addEventListener("click", previewFractile);

    $("apSave").addEventListener("click", () => {
      const price = Math.max(0.01, +$("apPrice").value || 0);
      const cost = Math.max(0, +$("apCost").value || 0);
      if (cost >= price) {
        $("apError").textContent = "Cost must be less than the selling price.";
        return;
      }
      $("apError").textContent = "";
      addProduct({ name: $("apName").value, cat: $("apCat").value,
                   store: $("apStore").value, price, cost,
                   shelfLife: Math.max(1, +$("apShelf").value || 30) });
      $("apName").value = "";
      closeAdd();
    });

    renderList(); renderDetail(); renderPending();
  }

  return { init, select, reveal, renderList, renderDetail,
           get state() { return state; }, get all() { return all; } };
})();
