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
    sort: "priority",
    search: "",
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

  /* Row selection now goes through Query, which answers from DuckDB-WASM when the
     engine is up and from the JavaScript path when it is not. Both are held to the
     same answer by Query.selftest(); see ui/js/duck.js on why this is a resolver
     rather than a hard dependency. */
  function visibleQuery() {
    return {
      scope: state.scope,
      filters: [...state.filters],
      sort: state.sort,
      search: state.search,
    };
  }

  /* ============================================================ worklist */

  async function renderFilters() {
    const pairs = await Query.reasonCounts();
    const counts = Object.fromEntries(pairs);
    const order = pairs.map(([k]) => k);
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
    const queue = ALL_ROWS.reduce((a, r) => a + (r.reasons.length ? 1 : 0), 0);
    $("wlCount").textContent = U.nf(queue);
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
    const pend = Grid.pendingMap.get(r.id + ":" + w);
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
    const nPend = Grid.pendingMap.size;
    $("ovCommit").textContent = nPend > 0
      ? `Commit ${nPend} edit${nPend > 1 ? "s" : ""}`
      : "Commit";
    if (nPend > 1) {
      const tot = totalPendingImpact();
      $("ovTotal").textContent = `${nPend} pending · ${U.money(tot)} total`;
      $("ovTotal").dataset.sign = tot >= 0 ? "pos" : "neg";
    } else {
      $("ovTotal").textContent = "";
      $("ovTotal").dataset.sign = "flat";
    }
  }

  function stageOverride() {
    const r = Grid.selected;
    if (!r) return;
    const w = currentWeek();
    const v = parseFloat($("ovValue").value);
    const modelVal = Math.round(r.weekly[w][Q50]);
    // Grid owns the pending set so that fill-down, paste and undo all move the same
    // state. Keeping a second copy here is how the two drift apart.
    Grid.setOverride(r.id, w, Number.isFinite(v) && Math.abs(v - modelVal) >= 0.5 ? v : null);
    applyOverrideToChart(r);
    refreshOverrideUI();
  }

  /* ---- pending-edit summary, shared by every editing path ---------------- */

  function syncPending() {
    state.pending = Grid.pendingMap;
    const n = state.pending.size;
    $("pendingCount").textContent = n ? `${n} pending` : "";
    $("undoBtn").disabled = !Grid.canUndo;
    $("redoBtn").disabled = !Grid.canRedo;
    const r = Grid.selected;
    if (r) { applyOverrideToChart(r); refreshOverrideUI(); }
  }

  function totalPendingImpact() {
    let sum = 0;
    for (const [k, v] of Grid.pendingMap) {
      const [id, wStr] = k.split(":");
      const row = BY_ID.get(id);
      if (row) sum += overrideImpact(row, parseInt(wStr, 10), v);
    }
    return sum;
  }

  function applyOverrideToChart(r) {
    const s = r._s;
    const daily = new Array(D.run.horizon).fill(null);
    let any = false;
    for (let w = 0; w < 4; w++) {
      const p = Grid.pendingMap.get(r.id + ":" + w);
      if (p === undefined) continue;
      any = true;
      const base = s.weekly[w][Q50] || 1;
      for (let d = 0; d < 7; d++) daily[w * 7 + d] = s.fan[w * 7 + d][Q50] * (p / base);
    }
    s._override = any ? daily : null;
    Chart.setSeries(s, D.dates, D.events, { showPrior: $("priorToggle").checked });
  }

  function commitOverrides() {
    const n = Grid.pendingMap.size;
    if (!n) return;
    // A prototype: commits are announced and cleared rather than persisted,
    // because there is no write path to a system of record here.
    const impact = totalPendingImpact();
    toast(`${n} override${n > 1 ? "s" : ""} committed · ${U.money(impact)} expected impact · ` +
          `reason "${$("ovReason").value}" · logged to the override ledger`);
    announce(`${n} overrides committed, expected impact ${U.money(impact)}`);
    Grid.commitAll();
    ALL_ROWS.forEach((r) => (r._s._override = null));
    syncPending();
  }

  /* ---- clipboard. navigator.clipboard is unavailable on file:// in some
     browsers, so fall back to a hidden textarea and execCommand. ----------- */

  async function readClipboard() {
    try {
      if (navigator.clipboard && navigator.clipboard.readText) {
        return await navigator.clipboard.readText();
      }
    } catch (_) { /* permission denied or insecure context */ }
    return window.prompt("Paste TSV here (clipboard access unavailable on file://)") || "";
  }

  function writeClipboard(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).catch(() => legacyCopy(text));
    } else {
      legacyCopy(text);
    }
  }

  function legacyCopy(text) {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.left = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch (_) {}
    ta.remove();
  }

  function syncWeekSelector() {
    const w = Grid.focusWeek;
    if (w !== null && w !== undefined) {
      $("ovWeek").value = String(w);
      refreshOverrideUI();
    }
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

  /* One in-flight refresh at a time. A planner holding a filter chip down or typing
     into the search box issues queries faster than they resolve, and without a
     generation counter a slow early query can land after a fast later one and paint
     a stale grid. */
  let refreshGen = 0;
  async function refresh() {
    const gen = ++refreshGen;
    const ids = await Query.ids(visibleQuery());
    if (gen !== refreshGen) return;                 // superseded mid-flight
    const rows = Query.rowsFor(ids);
    Grid.setRows(rows);
    renderWorklist(rows);
    $("crumbCount").textContent = `${U.nf(rows.length)} series`;
    $("gridEmpty").hidden = rows.length > 0;
    renderEngine();
  }

  /* ------------------------------------------------------ engine readout */

  function renderEngine() {
    const sub = $("menuSqlSub");
    const dot = $("sbEngineDot");
    if (dot) dot.dataset.status = Duck.state.status;
    if (!sub) return;
    const st = Duck.state;
    const n = st.rows || ALL_ROWS.length;
    const routed = Query.useSql(n, false);
    const t = st.lastMs
      ? `${st.lastMs < 1 ? st.lastMs.toFixed(2) : Math.round(st.lastMs)} ms`
      : "";

    // Two facts, not one: whether the engine is up, and which backend answered the
    // last query. They differ by design — the engine can be ready and still not be
    // the right tool at this table size — and collapsing them into one label is how
    // a status line starts lying.
    sub.textContent = {
      ready: routed
        ? `SQL console · running in your browser${t ? ` · last query ${t}` : ""}`
        : `SQL console · engine ready${t ? ` · last query ${t}` : ""}`,
      loading: "SQL console · starting the query engine…",
      unavailable: "SQL console · engine unavailable, see why",
      idle: "SQL console · for analysts",
    }[st.status];

    sub.parentElement.title = st.status === "ready"
      ? `DuckDB-WASM is loaded — ${st.detail}.
` +
        `Row selection routes to SQL above ${Query.CROSSOVER_ROWS.toLocaleString()} ` +
        `rows; this table has ${n.toLocaleString()}, so live queries run in ` +
        `${routed ? "SQL" : "JavaScript"}, which is faster at this size.`
      : `${st.detail || "The query engine has not started."}
` +
        `Filtering, sorting and grouping still work — they run in JavaScript.`;
  }

  /* ========================================================= SQL console

     A visible SQL console is not a gimmick here. The claim being made is that
     aggregation happens in a query engine rather than in application code, and a
     console is the shortest proof of that claim a reviewer can run themselves.
     It is also the honest place to demonstrate the fallback: with the engine down
     the console says so instead of pretending. Read-only by construction — the
     table is an in-memory copy of an Arrow buffer, so the worst a mutating
     statement can do is corrupt a copy that is rebuilt on reload, but the guard
     below refuses them anyway rather than relying on that. */

  const SQL_EXAMPLES = [
    ["health by class", `
SELECT cls, count(*) AS series, round(avg(health), 3) AS health,
       round(sum(revenue)) AS revenue, round(avg(targetQ), 3) AS target_q
FROM series GROUP BY 1 ORDER BY series DESC`],

    ["censoring by store", `
SELECT store, count(*) AS series, sum(censoredDays) AS censored_days,
       round(avg(priceCompleteness), 3) AS price_completeness
FROM series GROUP BY 1 ORDER BY censored_days DESC`],

    ["fractile spread", `
SELECT cat,
       round(min(targetQ), 3)    AS lo,
       round(median(targetQ), 3) AS mid,
       round(max(targetQ), 3)    AS hi,
       round(avg(marginRate), 3) AS margin_rate
FROM series GROUP BY 1 ORDER BY mid`],

    ["widest intervals", `
SELECT id, cls, round(health, 2) AS health, round(relWidth, 3) AS rel_width,
       round(demandH, 1) AS demand_28d, reasons
FROM series ORDER BY relWidth DESC LIMIT 25`],

    ["queue composition", `
SELECT cat, dept,
       count(*) AS series,
       sum(CASE WHEN length(reasons) > 2 THEN 1 ELSE 0 END) AS flagged,
       round(100.0 * sum(CASE WHEN length(reasons) > 2 THEN 1 ELSE 0 END)
             / count(*), 1) AS pct_flagged
FROM series GROUP BY 1, 2 ORDER BY pct_flagged DESC`],

    ["decision spread", `
SELECT abc AS revenue_class, xyz AS variability_class,
       count(*) AS series,
       round(avg(targetQ), 3)  AS target_q,
       round(avg(orderUpTo), 1) AS order_up_to,
       round(avg(underage / nullif(underage + overage, 0)), 3) AS fractile_check
FROM series GROUP BY 1, 2 ORDER BY 1, 2`],
  ];

  const MUTATING =
    /^\s*(insert|update|delete|drop|create|alter|attach|detach|copy|truncate|replace|call|export|import|install|load|pragma|set|begin|commit|rollback)\b/i;

  function openSql() {
    $("sqlBox").dataset.open = "true";
    $("sqlBackdrop").dataset.open = "true";
    $("sqlEngineInfo").textContent = Duck.ready()
      ? `DuckDB-WASM · ${Duck.state.detail}`
      : `unavailable — ${Duck.state.detail || "engine not started"}`;
    $("sqlText").focus();
  }
  function closeSql() {
    $("sqlBox").dataset.open = "false";
    $("sqlBackdrop").dataset.open = "false";
  }

  async function runSql() {
    const out = $("sqlOut");
    const text = $("sqlText").value.trim();
    if (!text) return;
    if (!Duck.ready()) {
      out.innerHTML = `<div class="sql-err">The in-browser query engine is not
        available (${U.esc(Duck.state.detail || "not started")}). Filtering and
        grouping still work — they run in JavaScript — but arbitrary SQL needs the
        engine. Serve the workspace with
        <code>python -m dqstack.serve</code> and reload.</div>`;
      return;
    }
    if (MUTATING.test(text)) {
      out.innerHTML = `<div class="sql-err">Read-only console: ` +
        `<code>${U.esc(text.split(/\s+/)[0])}</code> is not accepted.</div>`;
      return;
    }
    out.innerHTML = '<div class="sql-note">running…</div>';
    try {
      const rows = await Duck.sql(text);
      const ms = Duck.state.lastMs;
      if (!rows.length) {
        out.innerHTML = `<div class="sql-note">0 rows · ${ms.toFixed(1)} ms</div>`;
        return;
      }
      const cols = Object.keys(rows[0]);
      const shown = rows.slice(0, 500);
      const cell = (v) =>
        v === null || v === undefined ? '<span class="null">·</span>'
        : typeof v === "number" ? `<span class="num">${U.esc(String(v))}</span>`
        : U.esc(String(v));
      out.innerHTML =
        `<div class="sql-note">${U.nf(rows.length)} row${rows.length === 1 ? "" : "s"} · ` +
        `${ms.toFixed(1)} ms in the browser` +
        (rows.length > shown.length ? ` · showing first ${shown.length}` : "") + `</div>` +
        `<div class="sql-table-wrap"><table class="sql-table"><thead><tr>` +
        cols.map((c) => `<th>${U.esc(c)}</th>`).join("") + `</tr></thead><tbody>` +
        shown.map((r) => `<tr>` + cols.map((c) => `<td>${cell(r[c])}</td>`).join("") +
                         `</tr>`).join("") +
        `</tbody></table></div>`;
      renderEngine();
    } catch (e) {
      out.innerHTML = `<div class="sql-err">${U.esc(String(e && e.message || e))}</div>`;
    }
  }

  /* =========================================================== More menu

     One menu holds everything that is powerful but not part of the main path:
     the glossary, the ordering-decisions sheet, theme, row height, the keyboard
     sheet and the SQL console. That is what allows the top-level navigation to be
     four plain words while keeping every feature two clicks away. */

  /* The sidebar replaced a More menu, so there is nothing left to open or close.
     closeMenu survives as a no-op called from setView because the alternative is
     scattering existence checks through the view switcher. On a narrow viewport the
     sidebar itself is the overlay, and navigating dismisses it. */
  function closeMenu() {
    if (window.matchMedia("(max-width: 1000px)").matches) setSidebar(false);
  }

  /* Collapsed state is remembered: an expert who wants the width back for the grid
     should not have to reclaim it on every reload. */
  function setSidebar(open) {
    document.documentElement.dataset.sidebar = open ? "open" : "closed";
    $("sbCollapse")?.setAttribute("aria-expanded", String(open));
    try { localStorage.setItem("dq.sidebar", open ? "open" : "closed"); } catch (_) {}
    // The canvas grid sizes itself from its container, so a width change is a resize.
    requestAnimationFrame(() => { Grid.resize(); Chart.resize(); });
  }
  const sidebarOpen = () => document.documentElement.dataset.sidebar !== "closed";

  function renderGlossary() {
    $("glossBody").innerHTML = Plain.GLOSSARY
      .map(([h, body]) => `<div class="gloss-item"><h4>${U.esc(h)}</h4>` +
                          `<p>${U.esc(body)}</p></div>`).join("");
  }

  const setModal = (box, backdrop, open) => {
    $(box).dataset.open = String(open);
    $(backdrop).dataset.open = String(open);
  };
  const openGlossary = () => { renderGlossary(); setModal("glossBox", "glossBackdrop", true); };
  const closeGlossary = () => setModal("glossBox", "glossBackdrop", false);

  function onMenu(action) {
    closeMenu();
    switch (action) {
      case "glossary": return openGlossary();
      case "decisions": return setView("decisions");
      case "theme": return toggleTheme();
      case "density": return cycleDensity();
      case "keys": return toggleKeys(true);
      case "sql": return openSql();
    }
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
  async function jumpTo(id) {
    closePalette();
    state.scope = "all";
    state.filters.clear();
    document.querySelectorAll("[data-scope]").forEach((b) =>
      b.setAttribute("aria-pressed", b.dataset.scope === "all")
    );
    renderFilters();
    await refresh();                 // the grid must hold the row before selecting it
    const idx = Grid.rows.findIndex((r) => r.id === id);
    if (idx >= 0) Grid.select(idx);
  }

  const toggleKeys = (open) => {
    $("keysSheet").dataset.open = String(open);
    $("keysBackdrop").dataset.open = String(open);
  };

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

  // Today is rendered by review.js and the three sheets by views.js, so setView
  // dispatches rather than assuming everything that is not the workspace is a sheet.
  const SHEETS = new Set(["pipeline", "evidence", "decisions"]);

  const VIEW_TITLE = {
    today: "Today", workspace: "All products", evidence: "How it's doing",
    decisions: "Ordering decisions", pipeline: "How it works",
  };

  function setView(v) {
    state.view = v;
    const t = $("chromeTitle");
    if (t) t.textContent = VIEW_TITLE[v] || v;
    document.querySelectorAll(".tab").forEach((t) =>
      t.setAttribute("aria-selected", t.dataset.view === v)
    );
    document.querySelectorAll(".view").forEach((s) => {
      s.dataset.active = s.id === "view-" + v;
    });
    if (SHEETS.has(v)) Views.render(v);
    else if (v === "workspace") requestAnimationFrame(() => { Grid.resize(); Chart.resize(); });
    else if (v === "today") Review.render();
    closeMenu();
  }

  const DENSITIES = ["dense", "default", "comfortable"];
  const DENSITY_WORD = { dense: "Compact", default: "Default", comfortable: "Roomy" };

  function paintDensityLabel() {
    const el = $("densityLabel");
    if (el) el.textContent = DENSITY_WORD[document.documentElement.dataset.density];
  }

  function cycleDensity() {
    const cur = document.documentElement.dataset.density;
    const next = DENSITIES[(DENSITIES.indexOf(cur) + 1) % DENSITIES.length];
    document.documentElement.dataset.density = next;
    localStorage.setItem("dq.density", next);
    paintDensityLabel();
    window.dispatchEvent(new Event("dq:density"));
  }
  /* A theme flip must not be animated, for two independent reasons.

     The visible one: every surface in the application would cross-fade at once,
     which is precisely the kind of large-area motion the design brief rules out.

     The one that is actually a bug: an element that declares `transition` for a
     property whose value comes from a custom property can keep painting the old
     value after the token changes on :root. Measured here — the tabs, the reason
     chips, the engine pill and the toggle switch all kept their dark background
     and border after switching to light, because each declares a transition on
     exactly the property that failed, while `.override`, which declares none,
     updated correctly. Suppressing transitions across the swap fixes the stale
     paint and removes the animation in one move. The class is dropped after two
     frames: one for the attribute change to be styled, one for it to be painted.
     Doing it after a single frame reinstates transitions too early and the stale
     paint returns. */
  function setTheme(next) {
    const root = document.documentElement;
    root.classList.add("theme-switching");
    root.dataset.theme = next;
    localStorage.setItem("dq.theme", next);
    setSidebar((localStorage.getItem("dq.sidebar") || "open") !== "closed");
    U.refresh();                        // the token cache the canvases read
    window.dispatchEvent(new Event("dq:theme"));
    Views.invalidate();
    if (state.view !== "workspace") Views.render(state.view, true);
    requestAnimationFrame(() =>
      requestAnimationFrame(() => root.classList.remove("theme-switching")));
  }

  function toggleTheme() {
    setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
    paintThemeLabel();
  }

  function paintThemeLabel() {
    const el = $("sbThemeLabel");
    if (el) el.textContent = document.documentElement.dataset.theme === "dark"
      ? "Light mode" : "Dark mode";
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

    // ---- Excel bindings. The spec adopts spreadsheet conventions wholesale
    // rather than inventing a keyboard language these users would have to learn.
    if (mod && e.key.toLowerCase() === "d") {
      e.preventDefault();
      const s = Grid.selection;
      toast(Grid.fillDown()
        ? `Filled down ${s.r1 - s.r0} row${s.r1 - s.r0 > 1 ? "s" : ""}`
        : "Select a range spanning more than one row to fill down");
      syncPending();
      return;
    }
    if (mod && e.key.toLowerCase() === "c") {
      const tsv = Grid.copyTSV();
      if (tsv) {
        writeClipboard(tsv);
        const n = tsv.split("\n").length;
        toast(`Copied ${n} row${n > 1 ? "s" : ""} as TSV`);
      }
      return;
    }
    if (mod && e.key.toLowerCase() === "v") {
      e.preventDefault();
      readClipboard().then((txt) => {
        if (!txt) { toast("Nothing on the clipboard"); return; }
        const okPaste = Grid.pasteTSV(txt);
        toast(okPaste ? "Pasted" : "Clipboard did not parse as numeric TSV");
        syncPending();
      });
      return;
    }
    if (mod && e.key.toLowerCase() === "z") {
      e.preventDefault();
      const label = e.shiftKey ? Grid.redo() : Grid.undo();
      toast(label ? `${e.shiftKey ? "Redid" : "Undid"}: ${label}` : "Nothing to undo");
      syncPending();
      return;
    }
    if (e.shiftKey && /^Arrow(Up|Down|Left|Right)$/.test(e.key)) {
      e.preventDefault();
      const dr = e.key === "ArrowDown" ? 1 : e.key === "ArrowUp" ? -1 : 0;
      const dc = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
      Grid.extendSelection(dr, dc);
      return;
    }

    switch (e.key) {
      case "ArrowRight": e.preventDefault(); Grid.moveFocusCol(1); syncWeekSelector(); break;
      case "ArrowLeft": e.preventDefault(); Grid.moveFocusCol(-1); syncWeekSelector(); break;
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
        if (Grid.pendingMap.size) {
          Grid.discardAll();
          ALL_ROWS.forEach((r) => (r._s._override = null));
          syncPending();
          toast("Pending edits discarded (Cmd-Z restores them)");
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
    U.refresh();

    $("originLabel").textContent = D.run.origin;
    paintDensityLabel();
    paintThemeLabel();
    $("crumbScope").textContent =
      `${U.nf(D.run.nSeries)} item-locations · ${U.nf(D.run.nPanelRows)} panel rows`;

    Grid.init({
      futureDates: D.dates.future,
      onSelect,
      onEditWeek: (r, w) => { $("ovWeek").value = w; $("ovValue").focus(); $("ovValue").select(); },
    });
    Chart.init();
    Views.init(D);

    Query.attach(ALL_ROWS);
    renderFilters();
    refresh();

    // Fire and forget. The workspace is already interactive on the JS path; when
    // the engine lands, the resolver starts using it and one refresh re-answers
    // the current query through SQL. Nothing blocks on the CDN.
    Duck.onChange(renderEngine);
    Duck.init({ arrowUrl: "series.arrow" }).then((ok) => {
      renderEngine();
      if (ok) { renderFilters(); refresh(); }
    });

    // Establish the initial view in code rather than trusting the markup. A
    // reloaded tab can retain the previous session's tab state, and an attribute
    // that has drifted leaves every view hidden — which presents as a blank app
    // with no error in the console.
    /* The review flow owns the landing view. Its two hooks keep it and the
       spreadsheet on one shared pending-edit set: a number changed on a card is
       the same pending override the grid shows, so a planner can start on a card
       and finish in the table without losing work. */
    Review.init(D, {
      onToast: toast,
      onCount: (n) => {
        const b = $("sbTodayBadge");
        if (!b) return;
        b.textContent = n;
        b.hidden = n === 0;
      },
      onChange: (s, value) => {
        const row = BY_ID.get(s.id);
        if (!row) return;
        // Grid owns the pending set, so a card edit and a cell edit are the same
        // state and share undo. Null when the change is a rounding no-op, which is
        // what setOverride treats as "no longer overridden".
        const modelVal = Math.round(row.weekly[0][Q50]);
        Grid.setOverride(s.id, 0, Math.abs(value - modelVal) >= 0.5 ? value : null);
        syncPending();
      },
      onUndo: (s) => {
        Grid.resetSeries(s.id);
        s._override = null;
        syncPending();
      },
    });

    setView("today");
    window.DQ = { state, Query, Duck, rows: ALL_ROWS, selftest: Query.selftest, setTheme };

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
    $("groupBy").addEventListener("change", (e) => {
      Grid.setGroupBy(e.target.value);
      $("crumbSort").textContent = e.target.value
        ? `priority, grouped by ${e.target.selectedOptions[0].textContent}`
        : "priority";
    });
    $("colReset").addEventListener("click", () => {
      Grid.resetColumns();
      toast("Column widths and order reset");
    });
    // Delegated at the document, so the sidebar, the chrome Help button and any
    // future host of a [data-menu] control all dispatch through one path.
    document.addEventListener("click", (e) => {
      const b = e.target.closest("[data-menu]");
      if (b) onMenu(b.dataset.menu);
    });
    $("sbCollapse").addEventListener("click", () => setSidebar(!sidebarOpen()));
    $("sbOpen").addEventListener("click", () => setSidebar(true));
    $("glossClose").addEventListener("click", closeGlossary);
    $("glossBackdrop").addEventListener("click", closeGlossary);

    $("sortBy").addEventListener("change", (e) => {
      state.sort = e.target.value;
      $("crumbSort").textContent = e.target.selectedOptions[0].textContent;
      refresh();
    });
    $("sqlClose").addEventListener("click", closeSql);
    $("sqlBackdrop").addEventListener("click", closeSql);
    $("sqlRun").addEventListener("click", runSql);
    $("sqlText").addEventListener("keydown", (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); runSql(); }
    });
    $("sqlExamples").innerHTML = SQL_EXAMPLES
      .map(([label], i) => `<button class="chip-sql" data-i="${i}">${label}</button>`).join("");
    $("sqlExamples").querySelectorAll(".chip-sql").forEach((b) =>
      b.addEventListener("click", () => {
        $("sqlText").value = SQL_EXAMPLES[+b.dataset.i][1].trim();
        runSql();
      })
    );

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
      Grid.resetSeries(r.id);
      r._s._override = null;
      syncPending();
    });
    $("undoBtn").addEventListener("click", () => {
      const l = Grid.undo();
      toast(l ? `Undid: ${l}` : "Nothing to undo");
      syncPending();
    });
    $("redoBtn").addEventListener("click", () => {
      const l = Grid.redo();
      toast(l ? `Redid: ${l}` : "Nothing to redo");
      syncPending();
    });
    Grid.onEdits(() => {
      $("pendingCount").textContent = Grid.pendingMap.size
        ? `${Grid.pendingMap.size} pending` : "";
      $("undoBtn").disabled = !Grid.canUndo;
      $("redoBtn").disabled = !Grid.canRedo;
    });
    $("keysBackdrop").addEventListener("click", () => toggleKeys(false));
    $("hintKeys")?.addEventListener("click", () => toggleKeys(true));
    $("keysBtn")?.addEventListener("click", () => toggleKeys(true));
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { toggleKeys(false); closeSql(); closeGlossary(); closeMenu(); }
      if (e.key === "?" && !/^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName)) {
        toggleKeys(true);
      }
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
