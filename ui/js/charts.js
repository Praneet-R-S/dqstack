/* ============================================================================
   Charts — every graph in the application, in one place.

   Graphs were scattered: the forecast fan on a product page, the cost curve on a
   card, the range mix in the sidebar, the policy comparison in a modal, and the
   model's own report card three clicks deep in Evidence. A manager asking "show me
   how we are doing" had to know where each one lived. Several datasets the bundle
   has always carried -- the stage-by-stage value ladder, calibration, where the
   system helps most by product type -- had never been drawn at all.

   This view is a feature list: every chart named down the left with the question
   it answers, and the selected one drawn large on the right with what it shows and
   how to read it. Three groups, in the order a business asks about them: what
   demand looks like, what to do about stock, and whether the model can be trusted.

   Every chart is inline SVG with a uniform-scaling viewBox, so nothing stretches,
   and colours come from the design tokens by class, so both themes are correct
   without a repaint. Every number is computed from the bundle at render time;
   nothing is typed in.
   ============================================================================ */

window.Charts = (() => {
  const $ = (id) => document.getElementById(id);
  const e = (v) => U.esc(v);
  let D = null;
  let hooks = {};
  const state = { selected: "forecast", product: null };

  const W = 760;                       // viewBox width shared by every chart
  const name = (s) => s._name || Review.prettyItem(s.item);

  /* ---- a small SVG toolkit ---------------------------------------------- */

  /** Round a maximum up to a 1/2/2.5/5 ladder so axes read in sayable steps. */
  function niceMax(v) {
    if (!(v > 0)) return 1;
    const mag = Math.pow(10, Math.floor(Math.log10(v)));
    return [1, 2, 2.5, 5, 10].map((m) => m * mag).find((x) => x >= v) || 10 * mag;
  }

  /** Horizontal bars, optionally with a p10-p90 whisker and highlighted rows. */
  function hbars(rows, { fmt = Plain.units, labelW = 230, rowH = 30, max = null,
                         range = false, note = null } = {}) {
    const top = 8, h = top + rows.length * rowH + 30;
    const hi = max ?? niceMax(Math.max(...rows.map((r) => range ? (r.hi ?? r.v) : r.v), 0));
    const x0 = labelW, x1 = W - 70;
    const x = (v) => x0 + ((x1 - x0) * Math.max(0, v)) / hi;
    const ticks = [0, hi / 2, hi].map((t) => `
      <line class="ch-grid" x1="${x(t)}" y1="${top}" x2="${x(t)}" y2="${h - 26}"/>
      <text class="ch-tick" x="${x(t)}" y="${h - 10}" text-anchor="middle">${e(fmt(t))}</text>`).join("");
    const bars = rows.map((r, i) => {
      const y = top + i * rowH, bh = rowH - 12, cy = y + rowH / 2 - 2;
      return `<g class="ch-row${r.hl ? " is-hl" : ""}${r.dim ? " is-dim" : ""}">
        <text class="ch-label" x="${labelW - 12}" y="${cy + 4}" text-anchor="end">${e(r.label)}</text>
        <rect class="ch-bar${r.tone ? " t-" + r.tone : ""}" x="${x0}" y="${cy - bh / 2}"
              width="${Math.max(1, x(r.v) - x0).toFixed(1)}" height="${bh}" rx="3">
          <title>${e(r.label)}: ${e(fmt(r.v))}${r.tip ? " — " + e(r.tip) : ""}</title></rect>
        ${range && r.lo != null ? `
          <line class="ch-whisker" x1="${x(r.lo)}" y1="${cy}" x2="${x(r.hi)}" y2="${cy}"/>
          <line class="ch-whisker" x1="${x(r.lo)}" y1="${cy - 5}" x2="${x(r.lo)}" y2="${cy + 5}"/>
          <line class="ch-whisker" x1="${x(r.hi)}" y1="${cy - 5}" x2="${x(r.hi)}" y2="${cy + 5}"/>` : ""}
        <text class="ch-value" x="${x(range && r.hi != null ? r.hi : r.v) + 8}" y="${cy + 4}">${e(fmt(r.v))}</text>
      </g>`;
    }).join("");
    return svg(h, ticks + bars, note);
  }

  /** Vertical bars for a distribution. */
  function vbars(values, labels, { fmt = Plain.units, h = 300, tone = "", xcap = "" } = {}) {
    const L = 52, R = 16, T = 14, B = 48;
    const hi = niceMax(Math.max(...values, 0));
    const bw = (W - L - R) / values.length;
    const y = (v) => T + (h - T - B) * (1 - v / hi);
    const grid = [0, hi / 2, hi].map((t) => `
      <line class="ch-grid" x1="${L}" y1="${y(t)}" x2="${W - R}" y2="${y(t)}"/>
      <text class="ch-tick" x="${L - 8}" y="${y(t) + 4}" text-anchor="end">${e(fmt(t))}</text>`).join("");
    const bars = values.map((v, i) => `
      <rect class="ch-bar${tone ? " t-" + tone : ""}" x="${(L + i * bw + 3).toFixed(1)}" y="${y(v).toFixed(1)}"
            width="${Math.max(1, bw - 6).toFixed(1)}" height="${(h - B - y(v)).toFixed(1)}" rx="2">
        <title>${e(labels[i])}: ${e(fmt(v))}</title></rect>`).join("");
    const every = Math.ceil(values.length / 10);
    const xl = labels.map((l, i) => i % every ? "" : `
      <text class="ch-tick" x="${(L + i * bw + bw / 2).toFixed(1)}" y="${h - B + 18}" text-anchor="middle">${e(l)}</text>`).join("");
    return svg(h, grid + bars + xl +
      (xcap ? `<text class="ch-cap" x="${(L + W - R) / 2}" y="${h - 8}" text-anchor="middle">${e(xcap)}</text>` : ""));
  }

  function svg(h, body, note) {
    return `<svg class="ch-svg" viewBox="0 0 ${W} ${h}" role="img"
                 preserveAspectRatio="xMidYMid meet">${body}</svg>` +
      (note ? `<p class="cx-note">${note}</p>` : "");
  }

  /* ---- data helpers ------------------------------------------------------ */

  const MODEL_LABEL = (k) => {
    if (k === "DQ-Stack (full)") return "Whole system — totals view";
    if (k === "DQ-Stack (as issued for orders)") return "Whole system — used for orders";
    return Plain.MODELS[k]?.label || Plain.POLICIES[k]?.label || k.replace(/_/g, " ");
  };

  const STAGE_LABEL = (st) => {
    const s = String(st).toLowerCase();
    if (s.includes("naive")) return "Same period last year";
    if (s.includes("best single")) return "Best single method";
    if (s.includes("combination")) return "+ blending methods";
    if (s.includes("reconcil")) return "+ making totals add up";
    if (s.includes("calibrat")) return "+ checking the ranges";
    return st;
  };

  function productList() {
    const atRisk = (s) => (s.ledger?.lostMargin || 0) + (s.ledger?.wasteCost || 0);
    return D.series.slice().sort((a, b) => atRisk(b) - atRisk(a));
  }

  function currentProduct() {
    if (!state.product) state.product = productList()[0]?.id || null;
    return D.series.find((s) => s.id === state.product) || D.series[0];
  }

  function productPicker() {
    const cur = currentProduct();
    return `<label class="cx-pick">
      <span>Product</span>
      <select id="cxProduct" class="sel">
        ${productList().map((s) => `<option value="${e(s.id)}"${s.id === cur.id ? " selected" : ""}>
          ${e(name(s))} · ${e(Review.prettyStore(s.store))}</option>`).join("")}
      </select>
    </label>`;
  }

  /* ---- the catalogue ----------------------------------------------------
     Each entry: which group, a title, the one question it answers, and a render
     function returning { figure, reads } -- the drawing, and a sentence computed
     from the same data saying what it shows right now. */

  const CHARTS = [
    /* ================================================== demand */
    {
      id: "forecast", group: "Demand", title: "Product forecast",
      q: "What is one product expected to sell, and how sure is that?",
      picker: true,
      render() {
        const s = currentProduct();
        const wk = s.weekly ? s.weekly[0] : null;
        return {
          figure: Catalog.chart(s, W, 320) + Review.chartKey(s, { window: 56, prior: true }),
          reads: wk ? `${name(s)} at ${Review.prettyStore(s.store)} is expected to sell
            <b>${Plain.units(wk[3])} units</b> next week, most likely between
            <b>${Plain.units(wk[1])}</b> and <b>${Plain.units(wk[5])}</b>.` : "",
          how: "The line on the left is what sold. Past the upright line is the forecast: the dark centre is most likely, the pale edges are possible. Shaded days in the history are days that were set aside — not stocked, or the shelf was probably empty.",
        };
      },
    },
    {
      id: "next28", group: "Demand", title: "Total demand, next 28 days",
      q: "How much will the whole range sell each day?",
      render() {
        const d = D.metrics.temporal?.dailyTotal || [];
        // "22 Aug", not "08-22": a date a person reads, from the same ISO string.
        const days = (D.dates.future || []).slice(0, d.length).map((x) => new Date(x + "T00:00:00"));
        const labels = days.map((x) => x.toLocaleDateString(undefined, { day: "numeric", month: "short" }));
        const total = d.reduce((a, b) => a + b, 0);
        const peak = d.indexOf(Math.max(...d));
        return {
          figure: vbars(d, labels, { h: 300, xcap: "day" }),
          reads: `About <b>${Plain.units(total)} units</b> across the next ${d.length} days,
            busiest on <b>${e(days[peak]?.toLocaleDateString(undefined,
              { weekday: "long", day: "numeric", month: "long" }) || "")}</b> at
            ${Plain.units(d[peak] || 0)} units.`,
          how: "Each bar is one day's forecast for all products and stores together. The weekly rhythm is the shopping pattern the model learned.",
        };
      },
    },
    {
      id: "bycat", group: "Demand", title: "Demand by category",
      q: "Which part of the range carries the volume?",
      render() {
        const rows = (D.rollups.cat || []).map((r) => ({
          label: Plain.catWord(r.key), v: r.total, lo: r.p10, hi: r.p90,
          tip: `${r.series} products`,
        })).sort((a, b) => b.v - a.v);
        const sum = rows.reduce((a, r) => a + r.v, 0);
        return {
          figure: hbars(rows, { range: true }),
          reads: `<b>${e(rows[0]?.label)}</b> carries ${Math.round((rows[0]?.v / sum) * 100)}% of
            forecast volume over the next ${D.run.horizon} days.`,
          how: "The bar is the expected total; the whisker is the likely range around it. A long whisker means the category's total is uncertain, not just its products.",
        };
      },
    },
    {
      id: "stores", group: "Demand", title: "Stores compared",
      q: "Which stores need the most stock?",
      render() {
        const mine = Review.scope;
        const rows = (D.rollups.store || []).map((r) => ({
          label: Review.prettyStore(r.key), v: r.orderUpTo, hl: r.key === mine,
          tip: `${r.series} products, ${Plain.money(r.revenue)} yearly sales`,
        })).sort((a, b) => b.v - a.v);
        return {
          figure: hbars(rows),
          reads: `<b>${e(rows[0].label)}</b> needs the most stock and
            <b>${e(rows[rows.length - 1].label)}</b> the least —
            ${(rows[0].v / rows[rows.length - 1].v).toFixed(1)}× apart.`,
          how: "Units each store should hold to cover the next delivery window. Your store, if one is in scope, is highlighted.",
        };
      },
    },

    /* ================================================== stock */
    {
      id: "costcurve", group: "Stock & ordering", title: "Cost of being wrong",
      q: "What does ordering too much or too little cost this product?",
      picker: true,
      render() {
        const s = currentProduct();
        const cover = (D.run.reviewPeriod || 7) + (D.run.leadTime || 2);
        const c = Curve.points(s, cover, { span: 0.5, n: 61 });
        if (!c) return { figure: '<p class="cx-empty">This product has no forecast yet, so there is no curve to draw.</p>', reads: "", how: "" };
        const h = 320, L = 64, R = 20, T = 18, B = 50;
        const hi = niceMax(c.peak);
        const x = (q) => L + ((W - L - R) * (q - c.lo)) / (c.hi - c.lo);
        const y = (v) => T + (h - T - B) * (1 - v / hi);
        const path = c.pts.map((p, i) => `${i ? "L" : "M"}${x(p.q).toFixed(1)},${y(p.excess).toFixed(1)}`).join("");
        const area = `${path}L${x(c.hi)},${y(0)}L${x(c.lo)},${y(0)}Z`;
        const grid = [0, hi / 2, hi].map((t) => `
          <line class="ch-grid" x1="${L}" y1="${y(t)}" x2="${W - R}" y2="${y(t)}"/>
          <text class="ch-tick" x="${L - 8}" y="${y(t) + 4}" text-anchor="end">${e(Plain.money(t))}</text>`).join("");
        const xt = [c.lo, c.opt, c.hi].map((q) => `
          <text class="ch-tick" x="${x(q)}" y="${h - B + 18}" text-anchor="middle">${e(Plain.units(q))}</text>`).join("");
        const t = Plain.tradeoff(c);
        return {
          figure: svg(h, `${grid}
            <path class="ch-area" d="${area}"/><path class="ch-line" d="${path}"/>
            <line class="ch-mark" x1="${x(c.opt)}" y1="${T}" x2="${x(c.opt)}" y2="${y(0)}"/>
            <circle class="ch-mark-dot" cx="${x(c.opt)}" cy="${y(0)}" r="5"/>
            <text class="ch-mark-label" x="${x(c.opt)}" y="${T - 4}" text-anchor="middle">suggested ${e(Plain.units(c.opt))}</text>
            ${xt}
            <text class="ch-cap" x="${(L + W - R) / 2}" y="${h - 8}" text-anchor="middle">units stocked</text>`),
          reads: `${name(s)}: <b>${e(t?.word || "")}</b>. ${e(t?.detail || "")}`,
          how: "Height is extra cost compared with getting it exactly right. The bottom of the curve is the suggested stock level. A curve that rises faster on the left means running short is the expensive mistake for this product.",
        };
      },
    },
    {
      id: "mix", group: "Stock & ordering", title: "Range mix",
      q: "Where does the value sit, and how predictable is it?",
      render() {
        const mix = Stock.abcMix(D.series.filter((s) => !Review.scope || s.store === Review.scope));
        const max = Math.max(...Object.values(mix.cell).map((c) => c.value), 1);
        const cw = 170, ch = 74, L = 150, T = 34;
        let cells = "";
        "ABC".split("").forEach((a, r) => "XYZ".split("").forEach((x, c) => {
          const cell = mix.cell[a + x] || { n: 0, value: 0 };
          const share = mix.value ? cell.value / mix.value : 0;
          cells += `<g class="ch-cell" style="--fill:${(cell.value / max).toFixed(3)}">
            <rect x="${L + c * cw}" y="${T + r * ch}" width="${cw - 6}" height="${ch - 6}" rx="6">
              <title>${a}${x}: ${e(Stock.classAdvice(a, x))}</title></rect>
            <text class="ch-cell-n" x="${L + c * cw + (cw - 6) / 2}" y="${T + r * ch + 32}" text-anchor="middle">${cell.n}</text>
            <text class="ch-cell-s" x="${L + c * cw + (cw - 6) / 2}" y="${T + r * ch + 52}" text-anchor="middle">${Math.round(share * 100)}% of value</text>
          </g>`;
        }));
        const colH = ["steady (X)", "some variation (Y)", "hard to predict (Z)"].map((t, c) =>
          `<text class="ch-label" x="${L + c * cw + (cw - 6) / 2}" y="${T - 12}" text-anchor="middle">${t}</text>`).join("");
        const rowH = ["biggest earners (A)", "middle (B)", "long tail (C)"].map((t, r) =>
          `<text class="ch-label" x="${L - 12}" y="${T + r * ch + 38}" text-anchor="end">${t}</text>`).join("");
        const az = mix.cell.AZ || { n: 0 };
        return {
          figure: svg(T + 3 * ch + 10, colH + rowH + cells),
          reads: `<b>${az.n}</b> products are valuable but hard to predict (AZ) — the ones
            that most deserve a person's attention rather than a rule.`,
          how: "Rows are what a product earns you, columns how predictable it is. Darker cells hold more stock value. Hover a cell for how to handle that group.",
        };
      },
    },
    {
      id: "cover", group: "Stock & ordering", title: "Stock cover",
      q: "How long will the counted stock last?",
      render() {
        const sum = Stock.summary(D.series.filter((s) => !Review.scope || s.store === Review.scope));
        if (!sum.counted) {
          return { figure: `<p class="cx-empty">No stock has been counted yet. Enter counts on the
            <b>Order list</b> and this chart fills in as you go.</p>`, reads: "", how: "" };
        }
        const rows = [["critical", "risk"], ["low", "attention"], ["ok", "excess"], ["over", "forecast"]]
          .map(([k, tone]) => ({ label: Stock.COVER_WORD[k].word, v: sum[k] || 0, tone,
                                 tip: Stock.COVER_WORD[k].note }));
        return {
          figure: hbars(rows, { fmt: (v) => String(Math.round(v)) }),
          reads: `Of <b>${sum.counted}</b> counted products, <b>${sum.critical}</b> run out
            before a delivery can arrive.`,
          how: "Counted products only, grouped by how many days the stock on the shelf will last against expected demand.",
        };
      },
    },
    {
      id: "policy", group: "Stock & ordering", title: "What each policy earns",
      q: "Is a service level per product worth it?",
      render() {
        const p = Plain.policies(D.metrics.economics);
        const rows = p.rows.map((r) => ({
          label: r.label, v: r.earned,
          hl: r.key === "DQ-Stack (full)", tone: r.key === "full, blanket 95% service" ? "risk" : "",
          tip: `${(r.met * 100).toFixed(1)}% of demand met, ${Plain.units(r.stock)} units held`,
        }));
        const minV = Math.min(...rows.map((r) => r.v));
        return {
          figure: hbars(rows, { fmt: Plain.money, labelW: 290, max: null }),
          reads: e(p.headline || ""),
          how: "Profit each way of running the system earned over the same held-out days. The highlighted bar is how it runs now; the red bar is the common habit of one service level for everything.",
        };
      },
    },

    /* ================================================== model quality */
    {
      id: "calibration", group: "Model quality", title: "Are the ranges honest?",
      q: "When it says 90% likely, is it right 90% of the time?",
      render() {
        const cov = D.metrics.coverage || [];
        const h = 340, L = 60, R = 24, T = 16, B = 52;
        const x = (v) => L + (W - L - R) * v, y = (v) => T + (h - T - B) * (1 - v);
        const grid = [0, 0.25, 0.5, 0.75, 1].map((t) => `
          <line class="ch-grid" x1="${L}" y1="${y(t)}" x2="${W - R}" y2="${y(t)}"/>
          <text class="ch-tick" x="${L - 8}" y="${y(t) + 4}" text-anchor="end">${Math.round(t * 100)}%</text>
          <text class="ch-tick" x="${x(t)}" y="${h - B + 18}" text-anchor="middle">${Math.round(t * 100)}%</text>`).join("");
        const hasStrict = cov.some((c) => c.strict_after != null);
        const band = hasStrict ? cov.map((c) => `
          <line class="ch-band" x1="${x(c.nominal)}" y1="${y(c.strict_after)}" x2="${x(c.nominal)}" y2="${y(c.empirical_after)}">
          </line>`).join("") : "";
        const dots = cov.map((c) => {
          const ok = hasStrict ? c.tie_aware_ok : Math.abs(c.empirical_after - c.nominal) <= 0.03;
          return `<circle class="ch-dot ${ok ? "is-ok" : "is-off"}" cx="${x(c.nominal)}" cy="${y(c.empirical_after)}" r="5">
            <title>Said ${Math.round(c.nominal * 100)}%: actual at or below it ${(c.empirical_after * 100).toFixed(1)}% of the time${
              hasStrict ? `, strictly below ${(c.strict_after * 100).toFixed(1)}%` : ""}</title></circle>`;
        }).join("");
        const upper = cov.filter((c) => c.nominal >= 0.75);
        const worstUpper = Math.max(...upper.map((c) => Math.abs(c.empirical_after - c.nominal)));
        const okCount = hasStrict ? cov.filter((c) => c.tie_aware_ok).length : null;
        /* Said plainly when it is true. Accounting for zero-sale days shrinks the
           lower-tail gap a long way -- from about 30 points to about 5 at the 5% level
           on the current data -- but does not close it. Where the floor is still too
           high, the chart says so in words rather than leaving the reader to infer
           from the dots that it is only an artifact. */
        const floor = hasStrict ? cov.find((c) => c.nominal <= 0.05 + 1e-9) : null;
        const floorHigh = floor && floor.strict_after > floor.nominal + 0.02;
        return {
          figure: svg(h, `${grid}
            <line class="ch-diag" x1="${x(0)}" y1="${y(0)}" x2="${x(1)}" y2="${y(1)}"/>
            ${band}${dots}
            <text class="ch-cap" x="${(L + W - R) / 2}" y="${h - 8}" text-anchor="middle">what the forecast said</text>
            <text class="ch-cap" x="14" y="${(T + h - B) / 2}" text-anchor="middle" transform="rotate(-90 14 ${(T + h - B) / 2})">what actually happened</text>`),
          reads: `In the range orders are set from (75% and above), the forecast is within
            <b>${(worstUpper * 100).toFixed(1)} points</b> of what it promised.` +
            (okCount != null ? ` Counting days of zero sales correctly, <b>${okCount} of ${cov.length}</b> levels are honest.` : "") +
            (floorHigh ? ` The low end of the range is set too high: <b>${Math.round(floor.strict_after * 100)}%</b>
              of outcomes fall below the stated ${Math.round(floor.nominal * 100)}% floor. That affects the
              "likely between" figures, not the stock levels, which come from the upper range.` : ""),
          how: "Dots on the diagonal mean the forecast keeps its promises. Low levels sit above the line partly because so many days sell nothing: a zero counts as at or below any forecast, which flatters nothing and inflates the low end." +
            (hasStrict ? " The vertical bars show the range a level is allowed to fall in once that is accounted for. A dot is honest when the diagonal crosses its bar; a bar entirely above the diagonal is a real miss." : ""),
        };
      },
    },
    {
      id: "methods", group: "Model quality", title: "Accuracy by method",
      q: "Which forecasting methods are most accurate?",
      render() {
        const rows = (D.metrics.leaderboard || []).map((r) => ({
          label: MODEL_LABEL(r.method), v: r.wrmsse,
          hl: r.method === "DQ-Stack (as issued for orders)",
          dim: r.method === "seasonal_naive" || r.method === "zeroshot_prior",
          tip: `typical error ${Math.round(r.mase * 100)}% of a naive forecast's`,
        })).sort((a, b) => a.v - b.v);
        const issued = rows.find((r) => r.hl);
        const naive = (D.metrics.leaderboard || []).find((r) => r.method === "seasonal_naive");
        return {
          figure: hbars(rows, { fmt: (v) => v.toFixed(2), labelW: 290 }),
          reads: issued && naive
            ? `The forecasts used for orders have <b>${Math.round((1 - issued.v / naive.wrmsse) * 100)}% less error</b> than repeating the same period last year.`
            : `Lower is better; repeating last year scores about ${naive ? naive.wrmsse.toFixed(2) : "1.0"}.`,
          how: "Shorter bars are more accurate. A score of 1 is roughly as good as copying the same period last year. The highlighted row is what orders are actually built from.",
        };
      },
    },
    {
      id: "ladder", group: "Model quality", title: "Value added at each stage",
      q: "Does each step of the system earn its place?",
      render() {
        const rows = (D.metrics.fvaLadder || []).map((r) => ({
          label: STAGE_LABEL(r.stage), v: r.wrmsse,
          tone: /reconcil/i.test(r.stage) ? "attention" : "",
        }));
        return {
          figure: hbars(rows, { fmt: (v) => v.toFixed(2), labelW: 250 }),
          reads: `Error falls from <b>${rows[0]?.v.toFixed(2)}</b> to <b>${rows[2]?.v.toFixed(2)}</b>
            by the blending step. Making totals add up raises product-level error by design —
            it makes store and category totals far more accurate, and orders use the version before it.`,
          how: "Each bar is product-level error after one more stage. The amber bar is the stage that trades product accuracy for accurate totals; it is shown honestly rather than hidden.",
        };
      },
    },
    {
      id: "accprofit", group: "Model quality", title: "Does accuracy mean profit?",
      q: "Is the most accurate setup the most profitable one?",
      render() {
        const ec = D.metrics.economics || [];
        const h = 340, L = 76, R = 24, T = 20, B = 52;
        const xs = ec.map((r) => r.wrmsse), ys = ec.map((r) => r.realized_margin);
        const x0 = Math.min(...xs) * 0.97, x1 = Math.max(...xs) * 1.03;
        const y0 = Math.min(...ys) * 0.98, y1 = Math.max(...ys) * 1.01;
        const x = (v) => L + (W - L - R) * (v - x0) / (x1 - x0);
        const y = (v) => T + (h - T - B) * (1 - (v - y0) / (y1 - y0));
        const grid = [y0, (y0 + y1) / 2, y1].map((t) => `
          <line class="ch-grid" x1="${L}" y1="${y(t)}" x2="${W - R}" y2="${y(t)}"/>
          <text class="ch-tick" x="${L - 8}" y="${y(t) + 4}" text-anchor="end">${e(Plain.money(t))}</text>`).join("");
        const dots = ec.map((r) => {
          const lab = Plain.POLICIES[r.method]?.label || r.method;
          const cur = r.method === "DQ-Stack (full)";
          return `<g><circle class="ch-dot ${cur ? "is-hl" : ""}" cx="${x(r.wrmsse)}" cy="${y(r.realized_margin)}" r="${cur ? 7 : 5}">
            <title>${e(lab)}: error ${r.wrmsse.toFixed(3)}, profit ${e(Plain.money(r.realized_margin))}</title></circle>
            <text class="ch-point-label" x="${x(r.wrmsse) + 9}" y="${y(r.realized_margin) + 4}">${e(lab)}</text></g>`;
        }).join("");
        const rho = D.metrics.accuracyVsEconomics?.spearman_rho;
        return {
          figure: svg(h, `${grid}${dots}
            <text class="ch-cap" x="${(L + W - R) / 2}" y="${h - 8}" text-anchor="middle">forecast error (left is more accurate)</text>`),
          reads: rho != null ? `Accuracy and profit only partly agree (rank correlation
            <b>${rho.toFixed(2)}</b>). The most accurate setup is not the most profitable, which is
            why stock levels are set from each product's economics rather than from accuracy alone.` : "",
          how: "Each dot is one way of running the system. Up is more profit, left is more accurate. If accuracy were all that mattered the dots would fall on a straight line.",
        };
      },
    },
    {
      id: "byclass", group: "Model quality", title: "Where the system helps most",
      q: "Which kinds of product does it forecast best?",
      render() {
        const rows = (D.metrics.fvaByClass || []).map((r) => ({
          label: `${Plain.CLASS[r.planning_class]?.label || r.planning_class} (${r.n_series})`,
          v: Math.max(0, r.fva) * 100,
          tip: `error ${r.system_wrmsse.toFixed(2)} against ${r.anchor_wrmsse.toFixed(2)} for last year's pattern`,
        })).sort((a, b) => b.v - a.v);
        return {
          figure: hbars(rows, { fmt: (v) => `${Math.round(v)}%`, labelW: 260 }),
          reads: `The biggest improvement over last year's pattern is for
            <b>${e(rows[0]?.label || "")}</b>: ${Math.round(rows[0]?.v || 0)}% less error.`,
          how: "How much less error the system makes than copying the same period last year, for each kind of product. Longer is better.",
        };
      },
    },
    {
      id: "quality", group: "Model quality", title: "Data quality",
      q: "How clean is the sales history behind the forecasts?",
      render() {
        const hist = D.metrics.healthHistogram || [];
        const labels = hist.map((_, i) => (i / hist.length).toFixed(2));
        const n = hist.reduce((a, b) => a + b, 0);
        const good = hist.slice(Math.floor(hist.length * 0.85)).reduce((a, b) => a + b, 0);
        return {
          figure: vbars(hist, labels, { h: 290, fmt: (v) => String(Math.round(v)), xcap: "data quality score (1 = perfect)", tone: "excess" }),
          reads: `<b>${Math.round((good / n) * 100)}%</b> of products have a data quality score of 0.85 or better.`,
          how: "How many products fall at each data quality score. Scores drop for missing days, suspect values and days a product was not on sale.",
        };
      },
    },
    {
      id: "classes", group: "Model quality", title: "How products sell",
      q: "What kinds of demand is the range made of?",
      render() {
        const cc = D.metrics.classCounts || {};
        const rows = Object.entries(cc).map(([k, v]) => ({
          label: Plain.CLASS[k]?.label || k, v, tip: Plain.CLASS[k]?.hint || "",
        })).sort((a, b) => b.v - a.v);
        const n = rows.reduce((a, r) => a + r.v, 0);
        const hard = (cc.intermittent || 0) + (cc.lumpy || 0);
        return {
          figure: hbars(rows, { fmt: (v) => String(Math.round(v)), labelW: 220 }),
          reads: `<b>${Math.round((hard / n) * 100)}%</b> of products sell only occasionally or in bursts — the
            hardest patterns to forecast, and the reason the system blends several methods.`,
          how: "Each product is classified by how often it sells and how much the amounts vary. Hover a bar for what the pattern means.",
        };
      },
    },
  ];

  const BY_ID = Object.fromEntries(CHARTS.map((c) => [c.id, c]));

  /* ---- render ------------------------------------------------------------ */

  function renderList() {
    const groups = [...new Set(CHARTS.map((c) => c.group))];
    $("cxList").innerHTML = groups.map((g) => `
      <p class="cx-group">${e(g)}</p>
      ${CHARTS.filter((c) => c.group === g).map((c) => `
        <button class="cx-item" data-chart="${c.id}" aria-current="${c.id === state.selected}">
          <span class="cx-item-t">${e(c.title)}</span>
          <span class="cx-item-q">${e(c.q)}</span>
        </button>`).join("")}`).join("");
  }

  function renderStage() {
    const c = BY_ID[state.selected] || CHARTS[0];
    let out;
    try { out = c.render(); }
    catch (err) {
      out = { figure: `<p class="cx-empty">This chart could not be drawn from the current data.</p>`,
              reads: "", how: "" };
      console.error("chart", c.id, err);
    }
    $("cxStage").innerHTML = `
      <header class="cx-head">
        <div>
          <p class="cx-kicker">${e(c.group)}</p>
          <h2>${e(c.title)}</h2>
          <p class="cx-q">${e(c.q)}</p>
        </div>
        ${c.picker ? productPicker() : ""}
      </header>
      ${out.reads ? `<p class="cx-reads">${out.reads}</p>` : ""}
      <div class="cx-figure">${out.figure}</div>
      ${out.how ? `<p class="cx-how"><b>How to read it.</b> ${e(out.how)}</p>` : ""}`;
  }

  function render() { if (D) { renderList(); renderStage(); } }

  function show(id) {
    if (BY_ID[id]) state.selected = id;
    render();
  }

  function init(bundle, h = {}) {
    D = bundle; hooks = h;
    $("cxList").addEventListener("click", (ev) => {
      const b = ev.target.closest("[data-chart]");
      if (b) show(b.dataset.chart);
    });
    $("cxStage").addEventListener("change", (ev) => {
      if (ev.target.id === "cxProduct") { state.product = ev.target.value; renderStage(); }
    });
  }

  return { init, render, show, list: () => CHARTS.map((c) => ({ id: c.id, group: c.group, title: c.title })),
           get state() { return state; } };
})();
