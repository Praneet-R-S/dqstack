/* ============================================================================
   Overview — the hub.

   Every module the application has existed, but nothing showed them together. The
   app opened straight into one of them, so a newcomer -- and anyone evaluating the
   system for a business -- could not see the whole of it at once, and had to find
   each part by exploring the sidebar.

   This is the home screen business software is expected to have: one tile per
   module, each carrying its own live headline number and one click into it. The
   numbers are the same ones the modules themselves show, read from the same
   functions, so the hub can never disagree with the screen it links to.

   Order matters. Tiles run in the order the work is done -- decide, order, count --
   then the places you go to understand it.
   ============================================================================ */

window.Overview = (() => {
  const $ = (id) => document.getElementById(id);
  const e = (v) => U.esc(v);
  let D = null;
  let hooks = {};

  const scoped = () => D.series.filter((s) => !Review.scope || s.store === Review.scope);

  /** A tiny inline trend for the hero: next-28-day total demand. */
  function spark(values, w = 280, h = 56) {
    if (!values.length) return "";
    const hi = Math.max(...values), lo = Math.min(...values);
    const x = (i) => (w * i) / (values.length - 1);
    const y = (v) => h - 4 - ((h - 8) * (v - lo)) / Math.max(1e-9, hi - lo);
    const d = values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
    return `<svg class="ov-spark" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" aria-hidden="true">
      <path class="ov-spark-area" d="${d}L${w},${h}L0,${h}Z"/>
      <path class="ov-spark-line" d="${d}"/></svg>`;
  }

  function tiles() {
    const c = Review.counts();
    const prog = Review.progress();
    const queueRisk = Review.byCause().reduce((a, g) => a + (g.exposure || 0), 0);
    const sum = Stock.summary(scoped());
    const plan = Stock.countPlan(scoped());
    const fullShelf = scoped().reduce((a, s) => a + (s.orderUpTo || 0) * (s.cost || 0), 0);
    const board = D.metrics.leaderboard || [];
    const issued = board.find((r) => r.method === "DQ-Stack (as issued for orders)")
                || board.find((r) => r.method === "lgbm_quantile");
    const naive = board.find((r) => r.method === "seasonal_naive");
    const gain = issued && naive ? Math.round((1 - issued.wrmsse / naive.wrmsse) * 100) : null;
    const pol = Plain.policies(D.metrics.economics);
    const feeds = D.metrics.feedRuns || [];
    const feedsOk = feeds.filter((f) => f.outcome !== "rejected").length;

    return [
      { view: "today", step: "1", title: "Today's decisions",
        fig: String(c.urgent), unit: c.urgent === 1 ? "decision" : "decisions",
        sub: `${Plain.money(queueRisk)} of profit at stake` +
             (prog.decided ? ` · ${prog.decided} made so far` : ""),
        cta: "Review forecasts", tone: c.urgent ? "accent" : "ok" },
      { view: "orders", step: "2", title: "Order list",
        fig: sum.counted ? Plain.money(sum.toOrderCost) : Plain.money(fullShelf),
        unit: sum.counted ? "to order" : "full shelf",
        sub: sum.counted ? `${sum.toOrderLines} lines from ${sum.counted} counted products`
                         : "Count what is on the shelf to turn this into an order",
        cta: "Build the order", tone: "accent" },
      { view: "orders", focus: "due", step: "3", title: "Stock take",
        fig: plan.n.toLocaleString(), unit: "due a count",
        sub: `${Plain.money(plan.dueValue)} of stock not counted recently`,
        cta: "Start counting", tone: plan.n ? "attention" : "ok" },
      { view: "products", title: "Products",
        fig: scoped().length.toLocaleString(), unit: "products",
        sub: `${new Set(scoped().map((s) => s.store)).size} stores · ${
          scoped().filter((s) => s.reasons.length).length.toLocaleString()} flagged`,
        cta: "Browse the range" },
      { view: "charts", title: "Charts",
        fig: String(Charts.list().length), unit: "charts",
        sub: "Demand, stock and model quality in one list",
        cta: "Open charts" },
      { view: "charts", chart: "methods", title: "Model quality",
        fig: gain != null ? `${gain}%` : "—", unit: "less error",
        sub: "than repeating the same period last year",
        cta: "See the evidence", tone: "ok" },
      { action: "policy", title: "Policy cost",
        fig: pol?.blanket ? Plain.money(Math.abs(pol.current.earned - pol.blanket.earned)) : "—",
        unit: "more profit",
        sub: "than one service level for everything",
        cta: "Compare policies" },
      { action: "evidence", title: "Data feeds",
        fig: `${feedsOk}/${feeds.length}`, unit: "feeds current",
        sub: feedsOk === feeds.length ? "Every feed passed its quality checks" : "Some feeds need attention",
        cta: "How it works", tone: feedsOk === feeds.length ? "ok" : "attention" },
      { view: "workspace", title: "Detailed view",
        fig: c.urgentTotal + c.checkTotal + c.laterTotal > 999
          ? Plain.units(c.urgentTotal + c.checkTotal + c.laterTotal)
          : String(c.urgentTotal + c.checkTotal + c.laterTotal),
        unit: "flagged",
        sub: "Every number, for editing many products at once",
        cta: "Open spreadsheet" },
    ];
  }

  function render() {
    if (!D) return;
    const host = $("ovRoot");
    const run = D.run;
    const made = run.generatedAt ? new Date(run.generatedAt) : null;
    const daily = D.metrics.temporal?.dailyTotal || [];
    const total = daily.reduce((a, b) => a + b, 0);

    host.innerHTML = `
      <header class="ov-hero">
        <div class="ov-hero-main">
          <p class="ov-kicker">Demand planning${Review.scope ? " · " + e(Review.prettyStore(Review.scope)) : ""}</p>
          <h1>Here is where things stand</h1>
          <p class="ov-lede">Forecasts for <b>${D.series.length.toLocaleString()} products</b>
            across the next <b>${run.horizon} days</b>${made
              ? `, made ${e(made.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" }))}`
              : ""}. Work through the numbered steps in order; everything else is there when you need it.</p>
        </div>
        <div class="ov-hero-fig">
          <span class="ov-hero-n">${Plain.units(total)}</span>
          <span class="ov-hero-cap">units expected, next ${daily.length} days</span>
          ${spark(daily)}
        </div>
      </header>
      <div class="ov-grid">
        ${tiles().map((t, i) => `
          <button class="ov-tile${t.step ? " is-step" : ""}" data-i="${i}"
                  ${t.tone ? `data-tone="${t.tone}"` : ""}>
            <span class="ov-tile-top">
              ${t.step ? `<span class="ov-step">${t.step}</span>` : ""}
              <span class="ov-tile-t">${e(t.title)}</span>
            </span>
            <span class="ov-fig">${e(t.fig)}<small>${e(t.unit)}</small></span>
            <span class="ov-sub">${e(t.sub)}</span>
            <span class="ov-cta">${e(t.cta)} <span aria-hidden="true">&rarr;</span></span>
          </button>`).join("")}
      </div>`;
  }

  function init(bundle, h = {}) {
    D = bundle; hooks = h;
    $("ovRoot").addEventListener("click", (ev) => {
      const b = ev.target.closest(".ov-tile");
      if (!b) return;
      const t = tiles()[+b.dataset.i];
      if (!t) return;
      if (t.action) return hooks.onAction?.(t.action);
      hooks.onGo?.(t.view, t);
    });
  }

  return { init, render };
})();
