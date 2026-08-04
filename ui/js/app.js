/* ============================================================================
   Application wiring.

   State is kept in four distinct layers and never conflated, which is what makes
   undo, cancel and impact preview tractable:

     1. bundle    the committed forecast as produced by the pipeline. Read-only.
     2. pending   an overlay of {seriesId, week} -> value, rendered on top.
     3. ui        selection, filters, scope, density, theme.
     4. derived   totals, impact estimates — computed, never stored.
   ============================================================================ */

(() => {
  const D = window.DQ_DATA;
  const $ = (id) => document.getElementById(id);
  if (!D) {
    document.body.innerHTML =
      '<div style="padding:48px;font-family:sans-serif">' +
      "<h1>No data bundle</h1><p>Run <code>python -m dqstack.pipeline --export</code> " +
      "to generate <code>ui/data/bundle.js</code>, then reload.</p></div>";
    return;
  }

  const CHART_Q = D.run.chartQuantiles;         // [.05 .10 .25 .50 .75 .90 .95]
  const Q50 = CHART_Q.indexOf(0.5);

  const state = {
    view: "workspace",
    scope: "queue",
    filters: new Set(),
    pending: new Map(),                          // "id:week" -> value
    selected: null,
  };

  /* ---- probability weights for the stored quantile levels. Trapezoidal, with
          the outer levels extended to 0 and 1 so the tails carry mass. ------ */
  const QW = (() => {
    const edges = [0];
    for (let i = 1; i < CHART_Q.length; i++) edges.push((CHART_Q[i] + CHART_Q[i - 1]) / 2);
    edges.push(1);
    const w = [];
    for (let i = 0; i < CHART_Q.length; i++) w.push(edges[i + 1] - edges[i]);
    const s = w.reduce((a, b) => a + b, 0);
    return w.map((v) => v / s);
  })();

  const interpQ = (qs, p) => {
    if (p <= CHART_Q[0]) return qs[0];
    if (p >= CHART_Q[CHART_Q.length - 1]) return qs[qs.length - 1];
    for (let i = 1; i < CHART_Q.length; i++) {
      if (p <= CHART_Q[i]) {
        const t = (p - CHART_Q[i - 1]) / (CHART_Q[i] - CHART_Q[i - 1]);
        return qs[i - 1] + t * (qs[i] - qs[i - 1]);
      }
    }
    return qs[qs.length - 1];
  };

  /* ============================================================ grid rows */

  function toRow(s) {
    const med = s.fan.reduce((a, f) => a + f[Q50], 0);
    return {
      id: s.id, item: s.item, store: s.store, dept: s.dept, cat: s.cat, state: s.state,
      cls: s.cls, health: s.health, abc: s.abc,
      weekly: s.weekly,
      priorWeekly: weeklyPrior(s),
      fan: s.fan,
      total: med,
      p10: s.fan.reduce((a, f) => a + f[1], 0),
      p90: s.fan.reduce((a, f) => a + f[5], 0),
      targetQ: s.targetQ, orderUpTo: s.orderUpTo,
      swing: s.swing, relWidth: s.relWidth,
      reasons: s.reasons, priority: s.priority,
      ledger: s.ledger, lostMargin: s.ledger.lostMargin,
      _s: s,
    };
  }

  function weeklyPrior(s) {
    // The prior version only covers the overlapping horizon, so a week is only
    // comparable when every one of its days has a prior value.
    const out = [];
    for (let w = 0; w < 4; w++) {
      let sum = 0, ok = true;
      for (let d = 0; d < 7; d++) {
        const v = s.prior[w * 7 + d];
        if (v < 0) { ok = false; break; }
        sum += v;
      }
      out.push(ok ? sum : null);
    }
    return out;
  }

  const ALL_ROWS = D.series.map(toRow);
  const BY_ID = new Map(ALL_ROWS.map((r) => [r.id, r]));

  function visibleRows() {
    let rows = ALL_ROWS;
    if (state.scope === "queue") rows = rows.filter((r) => r.reasons.length > 0);
    if (state.filters.size)
      rows = rows.filter((r) => r.reasons.some((x) => state.filters.has(x)));
    return rows.slice().sort((a, b) => b.priority - a.priority);
  }

  /* ============================================================ worklist */

  function renderFilters() {
    const counts = {};
    ALL_ROWS.forEach((r) => r.reasons.forEach((x) => (counts[x] = (counts[x] || 0) + 1)));
    const order = ["risk", "stale", "censored", "wide", "swing", "cold-start", "terminal"];
    $("wlFilters").innerHTML = order
      .filter((k) => counts[k])
      .map(
        (k) =>
          `<button class="chip r-${k.replace("-start", "")}" data-reason="${k}"
             aria-pressed="${state.filters.has(k)}" title="${U.REASON_LABEL[k]}">
             <span class="dot"></span>${k}<span class="n">${counts[k]}</span></button>`
      )
      .join("");
    $("wlFilters").querySelectorAll(".chip").forEach((b) =>
      b.addEventListener("click", () => {
        const k = b.dataset.reason;
        state.filters.has(k) ? state.filters.delete(k) : state.filters.add(k);
        renderFilters();
        refresh();
      })
    );
  }

  function renderWorklist(rows) {
    const queue = ALL_ROWS.filter((r) => r.reasons.length > 0);
    $("wlCount").textContent = U.nf(queue.length);
    $("wlFoot").textContent = `${U.nf(rows.length)} shown · ${U.nf(ALL_ROWS.length)} series`;

    const top = rows.slice(0, 240);
    $("wlList").innerHTML = top
      .map((r, i) => {
        const tags = r.reasons
          .slice(0, 4)
          .map(
            (x) =>
              `<span class="wl-tag" style="color:var(${U.REASON_COLOR[x] || "--text-tertiary"})"
                 title="${U.REASON_LABEL[x]}"></span>`
          )
          .join("");
        const lost = r.ledger.lostMargin;
        return `<div class="wl-item" role="option" data-id="${r.id}"
                  aria-current="${state.selected === r.id}" aria-selected="${state.selected === r.id}"
                  style="--h-color:${U.healthColor(r.health)}">
          <div class="wl-gutter"></div>
          <div>
            <div class="wl-id">${r.item.replace(/^(FOODS|HOUSEHOLD|HOBBIES)_/, "")} · ${r.store}</div>
            <div class="wl-meta">${U.CLASS_SHORT[r.cls] || r.cls} · health ${r.health.toFixed(2)}</div>
          </div>
          <div class="wl-val">
            <div class="n">${lost > 0.005 ? U.money(lost) : U.nf(Math.round(r.total))}</div>
            <div class="u">${lost > 0.005 ? "lost mgn" : "units 28d"}</div>
            <div class="wl-tags">${tags}</div>
          </div>
        </div>`;
      })
      .join("");

    $("wlList").querySelectorAll(".wl-item").forEach((n) =>
      n.addEventListener("click", () => {
        const idx = Grid.rows.findIndex((r) => r.id === n.dataset.id);
        if (idx >= 0) Grid.select(idx);
      })
    );
  }

  /* ============================================================ detail */

  function renderDetail(r) {
    const s = r._s;
    const why = (s.why || []).filter((w) => w.feature !== "__base__");
    const maxAbs = Math.max(...why.map((w) => Math.abs(w.contribution)), 1e-6);

    const whyHtml = why.length
      ? why
          .map((w) => {
            const frac = Math.abs(w.contribution) / maxAbs;
            const pos = w.contribution > 0;
            const width = frac * 50;
            const left = pos ? 50 : 50 - width;
            const col = pos ? "--state-excess" : "--state-risk";
            return `<div class="why-row">
              <span class="why-name" title="${w.feature}${w.value !== null ? " = " + w.value : ""}">${prettyFeature(w.feature)}</span>
              <span class="why-bar"><span class="mid"></span>
                <i style="left:${left}%;width:${width}%;background:var(${col})"></i></span>
              <span class="why-val" style="color:var(${col})">${U.signed(w.contribution, 2)}</span>
            </div>`;
          })
          .join("")
      : '<div style="font-size:11px;color:var(--text-tertiary)">Attribution computed for the head of the worklist only.</div>';

    const weights = Object.entries(s.weights).sort((a, b) => b[1] - a[1]);
    const wMax = Math.max(...weights.map((w) => w[1]), 1e-6);

    $("detailPanel").innerHTML = `
      <div class="panel-h">why this number</div>
      ${whyHtml}
      <p style="font-size:10px;color:var(--text-tertiary);margin:8px 0 0;line-height:1.5">
        Exact TreeSHAP contributions to the day-7 point forecast, in units. Not feature importance —
        importance says what matters on average, this says what moved <em>this</em> number.
      </p>

      <div class="panel-h" style="margin-top:18px">data health</div>
      <div class="health-meter" style="--h-color:${U.healthColor(s.health)}">
        <i style="width:${(s.health * 100).toFixed(1)}%"></i>
      </div>
      <dl class="kv">
        <dt>health score</dt><dd style="color:${U.healthColor(s.health)}">${s.health.toFixed(3)}</dd>
        <dt>suspected censored days</dt><dd>${s.censoredDays}</dd>
        <dt>out-of-assortment days</dt><dd>${s.ooaDays}</dd>
        <dt>price completeness</dt><dd>${U.pct(s.priceCompleteness, 1)}</dd>
        <dt>series age</dt><dd>${s.ageWeeks.toFixed(1)}w</dd>
        <dt>training window from</dt><dd>${s.regimeStart || "start"}</dd>
      </dl>

      <div class="panel-h" style="margin-top:18px">method mix</div>
      <div class="weights">
        ${weights
          .map(
            ([k, v]) => `<div class="w-row">
              <span style="color:var(--text-secondary)">${k.replace(/_/g, " ")}</span>
              <span class="w-bar"><i style="width:${((v / wMax) * 100).toFixed(1)}%"></i></span>
              <span class="w-val">${(v * 100).toFixed(1)}%</span>
            </div>`
          )
          .join("")}
      </div>
      <p style="font-size:10px;color:var(--text-tertiary);margin:8px 0 0;line-height:1.5">
        Weights chosen by the meta-learner from this series' diagnostic features. Class
        <em>${U.CLASS_SHORT[s.cls] || s.cls}</em>, ADI ${s.adi ?? "—"}, CV² ${s.cv2 ?? "—"}.
      </p>

      <div class="panel-h" style="margin-top:18px">economics</div>
      <dl class="kv">
        <dt>target quantile (critical fractile)</dt><dd>${s.targetQ.toFixed(3)}</dd>
        <dt>underage — lost margin / unit</dt><dd>$${s.underage.toFixed(3)}</dd>
        <dt>overage — holding + markdown</dt><dd>$${s.overage.toFixed(3)}</dd>
        <dt class="dim" style="padding-left:10px">· holding</dt><dd class="dim">$${s.holdingComp.toFixed(3)}</dd>
        <dt class="dim" style="padding-left:10px">· markdown</dt><dd class="dim">$${s.markdownComp.toFixed(3)}</dd>
        <dt class="dim" style="padding-left:10px">· spoilage</dt><dd class="dim">$${s.spoilComp.toFixed(3)}</dd>
        <dt>order-up-to level</dt><dd>${U.nf(s.orderUpTo)}</dd>
        <dt>price / cost</dt><dd>$${s.price.toFixed(2)} / $${s.cost.toFixed(2)}</dd>
        <dt>shelf life</dt><dd>${s.shelfLife > 60 ? "durable" : s.shelfLife + "d"}</dd>
      </dl>

      <div class="panel-h" style="margin-top:18px">last cycle outcome</div>
      <dl class="kv">
        <dt>fill rate</dt><dd>${U.pct(s.ledger.fillRate, 1)}</dd>
        <dt>realised margin</dt><dd>${U.money(s.ledger.realizedMargin)}</dd>
        <dt>lost margin</dt><dd style="color:var(--state-risk)">${U.money(s.ledger.lostMargin)}</dd>
        <dt>holding + waste</dt><dd>${U.money(s.ledger.holdingCost + s.ledger.wasteCost)}</dd>
      </dl>
    `;
  }

  const FEATURE_LABELS = {
    ylag_1: "demand yesterday", ylag_7: "demand 7d ago", ylag_28: "demand 28d ago",
    ylag_364: "same day last year", rmean_7: "7d mean", rmean_28: "28d mean",
    rmean_56: "56d mean", rmean_180: "180d mean", rstd_28: "28d volatility",
    rzero_28: "28d zero share", snap: "SNAP benefit day", dayofweek: "day of week",
    future_price_ratio: "planned price vs today", price_ratio_28: "price vs 28d mean",
    days_to_holiday: "days to holiday", horizon: "days ahead",
    dept_store_agg_mean_28: "department demand", store_agg_mean_28: "store traffic",
    days_since_nonzero: "days since a sale", health_score: "data health",
    seasonal_strength: "seasonality strength", discount_flag: "on discount",
  };
  const prettyFeature = (f) =>
    FEATURE_LABELS[f] || f.replace(/_/g, " ").replace(/^enc /, "encoded ");

  /* ============================================================ override */

  function currentWeek() { return parseInt($("ovWeek").value, 10) || 0; }

  function overrideImpact(r, w, v) {
    // Expected newsvendor cost of the override versus the model, evaluated
    // against the model's own predictive distribution. Stated plainly: this is
    // what the change costs *if the forecast is right*. It cannot be what the
    // change costs if the planner is right — nobody knows that yet — and
    // pretending otherwise would be the dishonest version of this number.
    const s = r._s;
    const q = s.weekly[w];
    const base = q[Q50];
    if (!(base > 0) || !(v >= 0)) return 0;

    const under = s.underage, over = s.overage;
    const cost = (S) => {
      let short = 0, excess = 0;
      for (let k = 0; k < q.length; k++) {
        short += QW[k] * Math.max(q[k] - S, 0);
        excess += QW[k] * Math.max(S - q[k], 0);
      }
      return under * short + over * excess;
    };
    const S_model = interpQ(q, s.targetQ);
    const ratio = v / base;
    const S_over = interpQ(q.map((x) => x * ratio), s.targetQ);
    return cost(S_model) - cost(S_over);
  }

  function refreshOverrideUI() {
    const r = Grid.selected;
    if (!r) return;
    const w = currentWeek();
    const pend = state.pending.get(r.id + ":" + w);
    const modelVal = Math.round(r.weekly[w][Q50]);
    $("ovValue").value = pend !== undefined ? pend : modelVal;
    $("ovValue").placeholder = modelVal;

    const v = parseFloat($("ovValue").value);
    const changed = Number.isFinite(v) && Math.abs(v - modelVal) > 0.5;
    const impact = changed ? overrideImpact(r, w, v) : 0;

    const el = $("ovImpact");
    el.textContent = changed ? (impact >= 0 ? "+" : "") + U.money(impact) : "—";
    el.dataset.sign = !changed ? "flat" : impact >= 0 ? "pos" : "neg";
    $("ovCommit").disabled = !changed || !$("ovReason").value;
    $("ovCommit").textContent =
      state.pending.size > 0 ? `Commit ${state.pending.size} edit${state.pending.size > 1 ? "s" : ""}` : "Commit";
  }

  function stageOverride() {
    const r = Grid.selected;
    if (!r) return;
    const w = currentWeek();
    const v = parseFloat($("ovValue").value);
    const modelVal = Math.round(r.weekly[w][Q50]);
    if (!Number.isFinite(v) || Math.abs(v - modelVal) < 0.5) {
      state.pending.delete(r.id + ":" + w);
      Grid.setOverride(r.id, w, null);
    } else {
      state.pending.set(r.id + ":" + w, v);
      Grid.setOverride(r.id, w, v);
    }
    applyOverrideToChart(r);
    refreshOverrideUI();
  }

  function applyOverrideToChart(r) {
    const s = r._s;
    const daily = new Array(D.run.horizon).fill(null);
    let any = false;
    for (let w = 0; w < 4; w++) {
      const p = state.pending.get(r.id + ":" + w);
      if (p === undefined) continue;
      any = true;
      const base = s.weekly[w][Q50] || 1;
      for (let d = 0; d < 7; d++) daily[w * 7 + d] = s.fan[w * 7 + d][Q50] * (p / base);
    }
    s._override = any ? daily : null;
    Chart.setSeries(s, D.dates, D.events, { showPrior: $("priorToggle").checked });
  }

  function commitOverrides() {
    const n = state.pending.size;
    if (!n) return;
    // A prototype: commits are announced and cleared rather than persisted,
    // because there is no write path to a system of record here.
    toast(`${n} override${n > 1 ? "s" : ""} committed · reason "${$("ovReason").value}" · logged to the override ledger`);
    announce(`${n} overrides committed`);
    state.pending.clear();
    Grid.clearOverrides();
    const r = Grid.selected;
    if (r) { r._s._override = null; applyOverrideToChart(r); }
    refreshOverrideUI();
  }

  /* ============================================================ selection */

  function onSelect(r) {
    if (!r) return;
    state.selected = r.id;
    const s = r._s;
    $("chartTitle").textContent = `${s.item} · ${s.store}`;
    $("chartSub").innerHTML =
      `${U.CLASS_SHORT[s.cls] || s.cls} · health <span style="color:${U.healthColor(s.health)}">${s.health.toFixed(2)}</span>` +
      ` · ${s.censoredDays} censored days · ${s.ooaDays} not carried` +
      ` · prior version ${D.run.priorOrigin}` +
      (s.reasons.length ? ` · ${s.reasons.map((x) => U.REASON_LABEL[x]).join(", ")}` : "");

    $("ovWeek").innerHTML = [0, 1, 2, 3]
      .map((w) => `<option value="${w}">W${U.isoWeek(D.dates.future[w * 7])}</option>`)
      .join("");

    applyOverrideToChart(r);
    renderDetail(r);
    refreshOverrideUI();

    document.querySelectorAll(".wl-item").forEach((n) => {
      const on = n.dataset.id === r.id;
      n.setAttribute("aria-current", on);
      n.setAttribute("aria-selected", on);
      if (on) n.scrollIntoView({ block: "nearest" });
    });
  }

  function refresh() {
    const rows = visibleRows();
    Grid.setRows(rows);
    renderWorklist(rows);
    $("crumbCount").textContent = `${U.nf(rows.length)} series`;
    $("gridEmpty").hidden = rows.length > 0;
  }

  /* ============================================================ palette */

  let palIdx = 0, palItems = [];
  function openPalette() {
    $("palette").dataset.open = "true";
    $("paletteBackdrop").dataset.open = "true";
    $("paletteInput").value = "";
    $("paletteInput").focus();
    palSearch("");
  }
  function closePalette() {
    $("palette").dataset.open = "false";
    $("paletteBackdrop").dataset.open = "false";
  }
  function palSearch(q) {
    q = q.trim().toLowerCase();
    palItems = (q
      ? ALL_ROWS.filter(
          (r) =>
            r.item.toLowerCase().includes(q) ||
            r.store.toLowerCase().includes(q) ||
            r.cls.includes(q) ||
            r.dept.toLowerCase().includes(q)
        )
      : ALL_ROWS.slice().sort((a, b) => b.priority - a.priority)
    ).slice(0, 40);
    palIdx = 0;
    $("paletteList").innerHTML = palItems
      .map(
        (r, i) =>
          `<div class="palette-item" role="option" aria-selected="${i === 0}" data-id="${r.id}">
             <span class="p-id">${r.item} · ${r.store}</span>
             <span class="p-meta">${U.CLASS_SHORT[r.cls] || r.cls} · health ${r.health.toFixed(2)} · ${U.nf(Math.round(r.total))}u</span>
           </div>`
      )
      .join("");
    $("paletteList").querySelectorAll(".palette-item").forEach((n) =>
      n.addEventListener("click", () => jumpTo(n.dataset.id))
    );
  }
  function jumpTo(id) {
    closePalette();
    state.scope = "all";
    state.filters.clear();
    document.querySelectorAll("[data-scope]").forEach((b) =>
      b.setAttribute("aria-pressed", b.dataset.scope === "all")
    );
    renderFilters();
    refresh();
    const idx = Grid.rows.findIndex((r) => r.id === id);
    if (idx >= 0) Grid.select(idx);
  }

  /* ============================================================ chrome */

  function toast(msg) {
    const t = $("toast");
    t.textContent = msg;
    t.dataset.show = "true";
    clearTimeout(t._h);
    t._h = setTimeout(() => (t.dataset.show = "false"), 3200);
  }
  function announce(msg) {
    // Cell commits are announced for screen-reader users, stating what changed.
    const a = $("gridAria");
    const n = document.createElement("p");
    n.textContent = msg;
    a.appendChild(n);
    setTimeout(() => n.remove(), 1500);
  }

  function setView(v) {
    state.view = v;
    document.querySelectorAll(".tab").forEach((t) =>
      t.setAttribute("aria-selected", t.dataset.view === v)
    );
    document.querySelectorAll(".view").forEach((s) => {
      s.dataset.active = s.id === "view-" + v;
    });
    if (v !== "workspace") Views.render(v);
    else requestAnimationFrame(() => { Grid.resize(); Chart.resize(); });
  }

  const DENSITIES = ["dense", "default", "comfortable"];
  function cycleDensity() {
    const cur = document.documentElement.dataset.density;
    const next = DENSITIES[(DENSITIES.indexOf(cur) + 1) % DENSITIES.length];
    document.documentElement.dataset.density = next;
    localStorage.setItem("dq.density", next);
    $("densityBtn").textContent = "≡ " + next;
    window.dispatchEvent(new Event("dq:density"));
  }
  function toggleTheme() {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("dq.theme", next);
    U.refresh();
    window.dispatchEvent(new Event("dq:theme"));
    Views.invalidate();
    if (state.view !== "workspace") Views.render(state.view, true);
  }

  /* ============================================================ keyboard */
  /* Spreadsheet conventions, wholesale. These users came from Excel and are fast
     in it; inventing a new keyboard language would make the tool slower than the
     thing it replaced. */

  document.addEventListener("keydown", (e) => {
    const mod = e.metaKey || e.ctrlKey;
    const inField = /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName);

    if (mod && e.key.toLowerCase() === "k") { e.preventDefault(); openPalette(); return; }
    if ($("palette").dataset.open === "true") {
      if (e.key === "Escape") { closePalette(); return; }
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        palIdx = U.clamp(palIdx + (e.key === "ArrowDown" ? 1 : -1), 0, palItems.length - 1);
        $("paletteList").querySelectorAll(".palette-item").forEach((n, i) => {
          n.setAttribute("aria-selected", i === palIdx);
          if (i === palIdx) n.scrollIntoView({ block: "nearest" });
        });
        return;
      }
      if (e.key === "Enter" && palItems[palIdx]) { jumpTo(palItems[palIdx].id); return; }
      return;
    }

    if (mod && e.key === "Enter") { e.preventDefault(); commitOverrides(); return; }
    if (inField) {
      if (e.key === "Escape") { document.activeElement.blur(); $("gridScroll").focus(); }
      return;
    }
    if (state.view !== "workspace") return;

    switch (e.key) {
      case "ArrowDown": e.preventDefault(); Grid.move(1); break;
      case "ArrowUp": e.preventDefault(); Grid.move(-1); break;
      case "PageDown": e.preventDefault(); Grid.move(20); break;
      case "PageUp": e.preventDefault(); Grid.move(-20); break;
      case "Home": e.preventDefault(); Grid.select(0); break;
      case "End": e.preventDefault(); Grid.select(Grid.rows.length - 1); break;
      case "j": Grid.move(1); break;
      case "k": Grid.move(-1); break;
      case "e":
        $("detailPanel").scrollTo({ top: 0 });
        toast("Explanation panel is adjacent to the number — never a separate tab");
        break;
      case "Enter": e.preventDefault(); $("ovValue").focus(); $("ovValue").select(); break;
      case "Escape":
        if (state.pending.size) {
          state.pending.clear();
          Grid.clearOverrides();
          const r = Grid.selected;
          if (r) { r._s._override = null; applyOverrideToChart(r); }
          refreshOverrideUI();
          toast("Pending edits discarded");
        }
        break;
    }
  });

  /* ============================================================ boot */

  function boot() {
    const savedTheme = localStorage.getItem("dq.theme");
    const savedDensity = localStorage.getItem("dq.density");
    if (savedTheme) document.documentElement.dataset.theme = savedTheme;
    if (savedDensity) document.documentElement.dataset.density = savedDensity;
    $("densityBtn").textContent = "≡ " + document.documentElement.dataset.density;
    U.refresh();

    $("originLabel").textContent = D.run.origin;
    $("horizonLabel").textContent = D.run.horizon + "d";
    $("crumbScope").textContent =
      `${U.nf(D.run.nSeries)} item-locations · ${U.nf(D.run.nPanelRows)} panel rows`;

    Grid.init({
      futureDates: D.dates.future,
      onSelect,
      onEditWeek: (r, w) => { $("ovWeek").value = w; $("ovValue").focus(); $("ovValue").select(); },
    });
    Chart.init();
    Views.init(D);

    renderFilters();
    refresh();

    document.querySelectorAll(".tab").forEach((t) =>
      t.addEventListener("click", () => setView(t.dataset.view))
    );
    document.querySelectorAll("[data-scope]").forEach((b) =>
      b.addEventListener("click", () => {
        state.scope = b.dataset.scope;
        document.querySelectorAll("[data-scope]").forEach((x) =>
          x.setAttribute("aria-pressed", x === b)
        );
        refresh();
      })
    );
    $("themeBtn").addEventListener("click", toggleTheme);
    $("densityBtn").addEventListener("click", cycleDensity);
    $("priorToggle").addEventListener("change", () => {
      const r = Grid.selected;
      if (r) applyOverrideToChart(r);
    });
    $("ovWeek").addEventListener("change", refreshOverrideUI);
    $("ovValue").addEventListener("input", stageOverride);
    $("ovReason").addEventListener("change", refreshOverrideUI);
    $("ovCommit").addEventListener("click", commitOverrides);
    $("ovReset").addEventListener("click", () => {
      const r = Grid.selected;
      if (!r) return;
      [0, 1, 2, 3].forEach((w) => {
        state.pending.delete(r.id + ":" + w);
        Grid.setOverride(r.id, w, null);
      });
      r._s._override = null;
      applyOverrideToChart(r);
      refreshOverrideUI();
    });
    $("searchInput").addEventListener("focus", openPalette);
    $("paletteInput").addEventListener("input", (e) => palSearch(e.target.value));
    $("paletteBackdrop").addEventListener("click", closePalette);

    // Sorting the grid is reflected in the breadcrumb, so the current ordering is
    // never ambiguous.
    const origSort = Grid.sortBy;
    Grid.sortBy = (k) => { origSort(k); $("crumbSort").textContent = k; };
  }

  document.readyState === "loading"
    ? document.addEventListener("DOMContentLoaded", boot)
    : boot();
})();
