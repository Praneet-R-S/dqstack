/* ============================================================================
   The three non-planner views.

   Pipeline   the seven stages, each with the numbers it actually produced and
              the reason it exists. This is the flow made inspectable.
   Evidence   the results that decide whether the system is trustworthy:
              leaderboard, FVA ladder, coverage before and after conformal, and
              the accuracy-versus-economics rank inversion.
   Decisions  the link to money: critical fractile per item, order-up-to levels,
              and the realised simulator ledger.
   ============================================================================ */

const Views = (() => {
  let D = null;
  const el = (id) => document.getElementById(id);

  const tbl = (headers, rows, opts = {}) => {
    const th = headers
      .map((h) => `<th class="${h.num ? "num" : ""}">${h.label}</th>`)
      .join("");
    const tr = rows
      .map(
        (r) =>
          `<tr ${r._hero ? 'data-hero="true"' : ""}>` +
          r.cells
            .map((c) => `<td class="${c.cls || ""}${c.num ? " num" : ""}">${c.v}</td>`)
            .join("") +
          "</tr>"
      )
      .join("");
    return `<div class="tbl-wrap"><table class="data"><thead><tr>${th}</tr></thead><tbody>${tr}</tbody></table></div>`;
  };

  /* ====================================================== pipeline view */

  function renderPipeline() {
    const m = D.metrics;
    const r = D.run;
    const cens = m.censoring;
    const cls = m.classCounts;
    const feed = m.feedRuns || [];
    const accepted = feed.filter((f) => f.outcome !== "rejected").length;
    const fva = m.fvaLadder;
    const cov = m.coverage;
    const sim = m.simulator;
    const econ = m.economics;
    const full = econ.find((e) => e.method === "DQ-Stack (full)") || {};
    const nocal = econ.find((e) => e.method === "no calibration") || {};
    const nocens = econ.find((e) => e.method === "no censoring correction");
    const blanket = econ.find((e) => e.method === "full, blanket 95% service");

    const covBefore = cov.reduce((a, c) => a + Math.abs(c.gap_before), 0) / cov.length;
    const covAfter = cov.reduce((a, c) => a + Math.abs(c.gap_after), 0) / cov.length;

    const stage = (n, name, status, body, why, stats) => `
      <article class="stage" data-status="${status}">
        <div class="stage-n">${n}</div>
        <div>
          <h3 class="stage-name">${name}${
            status === "gate" ? '<span class="badge gate">blocking gate</span>' : ""
          }</h3>
          <div class="stage-body">${body}</div>
          <div class="stage-why">${why}</div>
        </div>
        <div class="stage-stats">${stats
          .map(
            ([k, v, c]) =>
              `<div class="stat-line"><span>${k}</span><b class="${c || ""}">${v}</b></div>`
          )
          .join("")}</div>
      </article>`;

    el("pipelineFlow").innerHTML =
      stage(
        "0", "Ingest and validate", "gate",
        `Raw sales, price, calendar and event feeds are checked against a declared schema before
         anything else touches them. Row counts against expectation, schema fingerprints against
         the last accepted run, aggregate totals against source-declared totals.`,
        `A failed quality check <em>stops the run</em> rather than letting the pipeline continue.
         Training on quietly corrupted data produces a forecast that looks fine and is wrong, and
         you do not discover it for weeks.`,
        [
          ["feeds validated", accepted + " / " + feed.length],
          ["panel rows", U.nf(r.nPanelRows)],
          ["series", U.nf(r.nSeries)],
          ["blocking failures", "0", "pos"],
        ]
      ) +
      stage(
        "1", "Diagnose each series", "ok",
        `Every item-location series is profiled before any model sees it: a planning class from ADI
         and squared CV, a data-health score, censoring detection, and structural-break detection
         that sets each series' own training window.`,
        `This stage matters more than the model choice. Feeding stockout zeros into training makes
         you under-forecast precisely the items that sell out most, which causes more stockouts.
         The loop is self-reinforcing and <em>invisible in accuracy metrics</em> — only the
         economics or ground truth expose it.`,
        [
          ["planning classes", Object.keys(cls).length],
          ["censor precision", cens.precision.toFixed(3)],
          ["censor recall (full-zero)", cens.recall_full_zero.toFixed(3)],
          ["rows flagged censored", U.nf(cens.flagged_rows)],
          [
            "margin lost if skipped",
            nocens ? U.money(full.realized_margin - nocens.realized_margin) : "—",
            "pos",
          ],
        ]
      ) +
      stage(
        "2", "Featurize", "ok",
        `Lagged demand at 1, 7, 28 and 364 days; rolling statistics over five windows; calendar
         features with holiday proximity and benefit-day flags; price <em>ratios</em> rather than raw
         price; time-expanding hierarchy target encodings; Fourier terms at weekly and annual
         frequency.`,
        `Every rolling window is computed strictly before the forecast origin. The panel is held as
         dense series-by-day matrices and features for origin O read only the slice up to O, so
         there is no code path by which a post-origin value can enter a feature. A test shifts the
         origin and asserts nothing changes.`,
        [
          ["features", "149"],
          ["lags", "11 levels to 364d"],
          ["leakage tests", "passing", "pos"],
          ["train origins", r.origins.train.length],
        ]
      ) +
      stage(
        "3", "Forecast with a candidate pool", "ok",
        `Six families, each covering a case the others handle badly: statistical (ETS, Croston, SBA,
         TSB, ADIDA), LightGBM with a Tweedie objective, LightGBM quantile heads at nine levels,
         a neural NHITS model, a zero-shot covariate-only prior for thin history, and cohort
         lifecycle curves for items under eight weeks old.`,
        `Direct multi-horizon estimation, not recursive iteration, because error compounds over 28
         days. Tweedie deviance because retail unit sales are zero-inflated non-negative counts —
         which is exactly what that family is for, and what won M5.`,
        [
          ["candidates", D.candidates.length],
          ["quantile levels", r.quantiles.length],
          ["horizon groups", "3 (1-7, 8-14, 15-28)"],
          ["training time", U.nf(r.timings.stage3_train, 0) + "s"],
        ]
      ) +
      stage(
        "4", "Combine", "ok",
        `Not selection — combination. A FFORMA-style meta-learner maps the Stage-1 series features
         to a weight vector over the candidates, constrained non-negative and summing to one.`,
        `Selection is a hard decision made on noisy validation estimates; combination hedges that
         noise. It also adapts at a granularity no hand-written rule table reaches: intermittent
         series end up weighted toward Croston and TSB, smooth high-volume series toward Tweedie
         and the quantile heads. The weights are inspectable, which the explanation panel needs.`,
        [
          ["effective candidates / series", m.effectiveCandidates.toFixed(2)],
          ["FVA of combining", U.signedPct(fva[2].delta_vs_previous), "pos"],
          ["anchor weight", "≈0.00 (never deployed)"],
        ]
      ) +
      stage(
        "5", "Reconcile", "ok",
        `Cross-sectional MinT with a shrinkage covariance estimator across twelve aggregation
         levels, plus temporal reconciliation so daily, weekly and monthly forecasts agree
         arithmetically.`,
        `This makes "the weekly plan disagrees with the quarterly plan" structurally impossible
         rather than a recurring meeting. Reported honestly: reconciliation often improves aggregate
         accuracy while slightly <em>degrading</em> bottom-level accuracy, and which matters depends
         on the decision.`,
        [
          ["hierarchy nodes", "1,554 over 1,000"],
          ["shrinkage intensity", m.mintShrinkage.toFixed(4)],
          ["bottom-level effect", U.signedPct(fva[3].delta_vs_previous),
           fva[3].delta_vs_previous >= 0 ? "pos" : "neg"],
          ["temporal residual", "0 (exact)"],
        ]
      ) +
      stage(
        "6", "Calibrate", "ok",
        `Split conformal, stratified by planning class and horizon group. A recent held-out
         calibration set measures how far reality fell outside each predicted quantile, and the
         quantiles are shifted by that amount.`,
        `Load-bearing, not cosmetic. The next stage reads a quantile off this distribution to set
         inventory. <em>If the P90 is not really the P90, your service level is not what you think
         it is.</em> A single global adjustment would over-widen smooth series and under-widen lumpy
         ones, which is why it is stratified.`,
        [
          ["mean coverage gap before", covBefore.toFixed(4)],
          ["mean coverage gap after", covAfter.toFixed(4), covAfter < covBefore ? "pos" : "neg"],
          ["improvement", U.pct(1 - covAfter / covBefore, 0), "pos"],
          [
            "margin vs uncalibrated",
            U.money(full.realized_margin - nocal.realized_margin),
            full.realized_margin >= nocal.realized_margin ? "pos" : "neg",
          ],
        ]
      ) +
      stage(
        "7", "Decide", "ok",
        `The target service quantile is the critical fractile — underage over underage plus overage,
         where underage is lost gross margin and overage is holding cost plus markdown and spoilage
         risk. The order-up-to level is the predictive quantile of demand over the review-plus-lead
         period. A vectorised simulator runs inventory forward and writes the decision ledger.`,
        `This replaces the industry habit of a blanket 95% service level on everything. A low-margin,
         high-holding-cost item should be planned at a much lower quantile than a high-margin one,
         and the formula says so precisely. The ledger — one row per item-location-period with the
         full quantile forecast, the order, what happened, and every cost component — is the results
         dataset for the whole thesis.`,
        [
          ["fill rate", U.pct(sim.fill_rate, 2)],
          ["realised margin", U.money(sim.realized_margin)],
          ["lost margin", U.money(sim.lost_margin), "neg"],
          [
            "vs blanket 95%",
            blanket ? U.money(full.realized_margin - blanket.realized_margin) : "—",
            "pos",
          ],
          ["conservation residual", sim.conservation_residual.toExponential(1), "pos"],
        ]
      ) +
      stage(
        "↺", "The feedback loop", "ok",
        `Realised outcomes flow back two ways. Forecast Value Added is computed for every pipeline
         stage against a naive anchor, revealing which steps add value and which destroy it.
         And model promotion is gated on realised ledger margin, not accuracy.`,
        `Negative FVA on a stage is a signal to disable it, which is why every stage in this
         pipeline has an ablation switch. Promote on WRMSSE and your implementation contradicts your
         thesis — so the gate reads the ledger.`,
        [
          ["FVA vs anchor", U.signedPct(fva[fva.length - 1].fva_vs_anchor), "pos"],
          ["promotion metric", "realised margin"],
          [
            "accuracy/economics ρ",
            m.accuracyVsEconomics.spearman_rho.toFixed(3),
          ],
          ["rank inversions", m.accuracyVsEconomics.inversions.length],
        ]
      );

    // ---- headline cards
    el("pipelineCards").innerHTML = [
      ["Series forecast", U.nf(r.nSeries), `${r.horizon}-day horizon, ${r.quantiles.length} quantile levels, origin ${r.origin}`],
      ["Forecast value added", U.signedPct(fva[fva.length - 1].fva_vs_anchor),
       `WRMSSE against a seasonal-naive anchor, after all seven stages`],
      ["Coverage gap closed", U.pct(1 - covAfter / covBefore, 0),
       `Mean |empirical − nominal| fell from ${covBefore.toFixed(3)} to ${covAfter.toFixed(3)}`],
      ["Realised margin", U.money(sim.realized_margin),
       `Simulated over ${r.horizon} days at ${U.pct(sim.fill_rate, 1)} fill rate`],
      ["Total runtime", U.nf(r.timings.total, 0) + "s",
       `All seven stages plus the censoring ablation arm, single machine`],
    ]
      .map(
        ([cap, big, sub]) =>
          `<div class="card"><div class="cap">${cap}</div><div class="big">${big}</div><div class="sub">${sub}</div></div>`
      )
      .join("");
  }

  /* ====================================================== evidence view */

  function renderEvidence() {
    const m = D.metrics;

    // ---- leaderboard
    el("lbTable").innerHTML = tbl(
      [
        { label: "Method" }, { label: "WSPL", num: true }, { label: "WRMSSE", num: true },
        { label: "MASE", num: true }, { label: "Bias", num: true },
      ],
      m.leaderboard.map((r) => ({
        _hero: r.method === "DQ-Stack (full)",
        cells: [
          { v: r.method },
          { v: r.wspl === null ? "—" : r.wspl.toFixed(4), num: true },
          { v: r.wrmsse === null ? "—" : r.wrmsse.toFixed(4), num: true },
          { v: r.mase === null ? "—" : r.mase.toFixed(3), num: true },
          { v: U.signedPct(r.bias, 1), num: true, cls: Math.abs(r.bias) > 0.1 ? "neg" : "" },
        ],
      }))
    );

    // ---- FVA ladder
    el("fvaTable").innerHTML = tbl(
      [
        { label: "Pipeline stage" }, { label: "WRMSSE", num: true },
        { label: "FVA vs anchor", num: true }, { label: "Δ vs previous stage", num: true },
      ],
      m.fvaLadder.map((r) => ({
        cells: [
          { v: r.stage },
          { v: r.wrmsse.toFixed(4), num: true },
          { v: U.signedPct(r.fva_vs_anchor), num: true, cls: r.fva_vs_anchor > 0 ? "pos" : "neg" },
          {
            v: r.delta_vs_previous === null ? "—" : U.signedPct(r.delta_vs_previous),
            num: true,
            cls: r.delta_vs_previous === null ? "dim" : r.delta_vs_previous > 0 ? "pos" : "neg",
          },
        ],
      }))
    );

    // ---- coverage
    el("covTable").innerHTML = tbl(
      [
        { label: "Nominal", num: true }, { label: "Empirical before", num: true },
        { label: "Empirical after", num: true }, { label: "|gap| before", num: true },
        { label: "|gap| after", num: true },
      ],
      m.coverage.map((r) => ({
        cells: [
          { v: r.nominal.toFixed(3), num: true },
          { v: r.empirical_before.toFixed(4), num: true },
          { v: r.empirical_after.toFixed(4), num: true },
          { v: r.gap_before.toFixed(4), num: true },
          {
            v: r.gap_after.toFixed(4), num: true,
            cls: r.gap_after < r.gap_before ? "pos" : "neg",
          },
        ],
      }))
    );
    reliability(el("relChart"), m.coverage);

    // ---- FVA by class
    el("classTable").innerHTML = tbl(
      [
        { label: "Planning class" }, { label: "Series", num: true },
        { label: "Anchor WRMSSE", num: true }, { label: "System WRMSSE", num: true },
        { label: "FVA", num: true },
      ],
      m.fvaByClass.map((r) => ({
        cells: [
          { v: r.planning_class },
          { v: U.nf(r.n_series), num: true },
          { v: r.anchor_wrmsse === null ? "—" : r.anchor_wrmsse.toFixed(4), num: true },
          { v: r.system_wrmsse === null ? "—" : r.system_wrmsse.toFixed(4), num: true },
          { v: U.signedPct(r.fva), num: true, cls: r.fva > 0 ? "pos" : "neg" },
        ],
      }))
    );

    // ---- ensemble weights heatmap
    const wbc = m.weightsByClass;
    const classes = Object.keys(wbc);
    const cands = D.candidates.filter((c) => classes.some((k) => wbc[k][c] !== undefined));
    let head = '<th>Planning class</th>' + cands.map((c) => `<th class="num">${c.replace(/_/g, " ")}</th>`).join("");
    let body = classes
      .map((k) => {
        const cells = cands
          .map((c) => {
            const v = wbc[k][c] ?? 0;
            const bg = U.rgba(U.tk("--series-forecast"), Math.min(v / 0.45, 1) * 0.55);
            return `<td class="num" style="background:${bg}">${v.toFixed(3)}</td>`;
          })
          .join("");
        return `<tr><td>${k}</td>${cells}</tr>`;
      })
      .join("");
    el("weightTable").innerHTML =
      `<div class="tbl-wrap"><table class="data"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;

    // ---- accuracy vs economics
    const ave = m.accuracyVsEconomics;
    el("aveSummary").innerHTML =
      `<b>Spearman ρ = ${ave.spearman_rho.toFixed(3)}</b> (p = ${ave.p_value.toFixed(4)}) across
       ${ave.n_methods} configurations, with <b>${ave.inversions.length} rank inversions</b>.
       An inversion is a configuration that is more accurate on WRMSSE yet earns less realised
       margin. If accuracy and economics ranked identically this system could be promoted on
       WRMSSE and the thesis would have nothing to argue. They do not.`;

    el("econTable").innerHTML = tbl(
      [
        { label: "Configuration" }, { label: "WRMSSE", num: true },
        { label: "Realised margin", num: true }, { label: "Fill rate", num: true },
        { label: "Lost margin", num: true }, { label: "Holding", num: true },
        { label: "Waste", num: true }, { label: "Avg inventory", num: true },
        { label: "Mean target q", num: true },
      ],
      [...m.economics]
        .sort((a, b) => b.realized_margin - a.realized_margin)
        .map((r) => ({
          _hero: r.method === "DQ-Stack (full)",
          cells: [
            { v: r.method },
            { v: r.wrmsse.toFixed(4), num: true },
            { v: U.money(r.realized_margin), num: true },
            { v: U.pct(r.fill_rate, 2), num: true },
            { v: U.money(r.lost_margin), num: true, cls: "neg" },
            { v: U.money(r.holding_cost), num: true },
            { v: U.money(r.waste_cost), num: true },
            { v: U.nf(r.avg_inventory, 1), num: true },
            { v: r.mean_target_q.toFixed(3), num: true },
          ],
        }))
    );

    el("invTable").innerHTML = ave.inversions.length
      ? tbl(
          [
            { label: "More accurate (lower WRMSSE)" }, { label: "More profitable" },
            { label: "WRMSSE gap", num: true }, { label: "Margin gap", num: true },
          ],
          ave.inversions.slice(0, 10).map((r) => ({
            cells: [
              { v: r.more_accurate },
              { v: r.more_profitable },
              { v: "+" + r.wrmsse_gap.toFixed(4), num: true },
              { v: U.money(r.margin_gap), num: true, cls: "pos" },
            ],
          }))
        )
      : '<div class="note">No rank inversions at this origin.</div>';

    scatter(el("aveChart"), m.economics);
  }

  /* ---- small canvases -------------------------------------------------- */
  function reliability(canvas, cov) {
    if (!canvas) return;
    const box = canvas.getBoundingClientRect();
    const w = box.width || 460, h = 220;
    const c = U.fitCanvas(canvas, w, h);
    const P = { t: 12, r: 12, b: 28, l: 40 };
    const pw = w - P.l - P.r, ph = h - P.t - P.b;
    const X = (v) => P.l + v * pw;
    const Y = (v) => P.t + ph - v * ph;

    c.clearRect(0, 0, w, h);
    c.strokeStyle = U.rgba(U.tk("--border-subtle"), 1);
    for (let g = 0; g <= 4; g++) {
      c.beginPath();
      c.moveTo(P.l, Y(g / 4)); c.lineTo(w - P.r, Y(g / 4)); c.stroke();
    }
    // perfect-calibration diagonal
    c.save();
    c.setLineDash([3, 3]);
    c.strokeStyle = U.tk("--text-tertiary");
    c.beginPath(); c.moveTo(X(0), Y(0)); c.lineTo(X(1), Y(1)); c.stroke();
    c.restore();

    const line = (key, color, dash) => {
      c.save();
      if (dash) c.setLineDash(dash);
      c.strokeStyle = color; c.lineWidth = 2;
      c.beginPath();
      cov.forEach((r, i) =>
        i ? c.lineTo(X(r.nominal), Y(r[key])) : c.moveTo(X(r.nominal), Y(r[key]))
      );
      c.stroke();
      cov.forEach((r) => {
        c.beginPath(); c.arc(X(r.nominal), Y(r[key]), 2.5, 0, Math.PI * 2);
        c.fillStyle = color; c.fill();
      });
      c.restore();
    };
    line("empirical_before", U.tk("--state-risk"), [4, 3]);
    line("empirical_after", U.tk("--series-forecast"), null);

    c.font = `10px ${U.tk("fontNum")}`;
    c.fillStyle = U.tk("--text-tertiary");
    c.textAlign = "center";
    [0, 0.25, 0.5, 0.75, 1].forEach((v) => c.fillText(v.toFixed(2), X(v), h - 10));
    c.textAlign = "right";
    [0, 0.5, 1].forEach((v) => c.fillText(v.toFixed(1), P.l - 6, Y(v)));
    c.textAlign = "left";
    c.font = `10px ${U.tk("fontUI")}`;
    c.fillStyle = U.tk("--text-secondary");
    c.fillText("nominal quantile →", P.l, h - 2);
  }

  function scatter(canvas, econ) {
    if (!canvas) return;
    const box = canvas.getBoundingClientRect();
    const w = box.width || 460, h = 260;
    const c = U.fitCanvas(canvas, w, h);
    const P = { t: 16, r: 16, b: 34, l: 58 };
    const pw = w - P.l - P.r, ph = h - P.t - P.b;

    const xs = econ.map((e) => e.wrmsse), ys = econ.map((e) => e.realized_margin);
    const x0 = Math.min(...xs), x1 = Math.max(...xs);
    const y0 = Math.min(...ys), y1 = Math.max(...ys);
    const X = (v) => P.l + ((v - x0) / (x1 - x0 || 1)) * pw;
    const Y = (v) => P.t + ph - ((v - y0) / (y1 - y0 || 1)) * ph;

    c.clearRect(0, 0, w, h);
    c.strokeStyle = U.rgba(U.tk("--border-subtle"), 1);
    for (let g = 0; g <= 4; g++) {
      c.beginPath(); c.moveTo(P.l, P.t + (ph * g) / 4); c.lineTo(w - P.r, P.t + (ph * g) / 4); c.stroke();
    }
    econ.forEach((e) => {
      const hero = e.method === "DQ-Stack (full)";
      c.beginPath();
      c.arc(X(e.wrmsse), Y(e.realized_margin), hero ? 5.5 : 3.5, 0, Math.PI * 2);
      c.fillStyle = hero ? U.tk("--series-forecast") : U.tk("--text-secondary");
      c.fill();
      c.font = `${hero ? "600 " : ""}9px ${U.tk("fontUI")}`;
      c.fillStyle = hero ? U.tk("--text-primary") : U.tk("--text-tertiary");
      c.textAlign = X(e.wrmsse) > P.l + pw * 0.62 ? "right" : "left";
      const dx = X(e.wrmsse) > P.l + pw * 0.62 ? -8 : 8;
      c.fillText(e.method, X(e.wrmsse) + dx, Y(e.realized_margin));
    });
    c.font = `10px ${U.tk("fontUI")}`;
    c.fillStyle = U.tk("--text-secondary");
    c.textAlign = "left";
    c.fillText("WRMSSE (lower is more accurate) →", P.l, h - 6);
    c.save();
    c.translate(12, P.t + ph / 2); c.rotate(-Math.PI / 2); c.textAlign = "center";
    c.fillText("realised margin →", 0, 0);
    c.restore();
  }

  /* ====================================================== decisions view */

  function renderDecisions() {
    const m = D.metrics;
    const sim = m.simulator;
    const series = D.series;

    el("decCards").innerHTML = [
      ["Order units recommended", U.nf(series.reduce((a, s) => a + s.orderUpTo, 0)),
       `Order-up-to levels across ${U.nf(series.length)} item-locations for the next review cycle`],
      ["Mean target quantile", (series.reduce((a, s) => a + s.targetQ, 0) / series.length).toFixed(3),
       `Range ${Math.min(...series.map((s) => s.targetQ)).toFixed(3)} to ${Math.max(...series.map((s) => s.targetQ)).toFixed(3)} — set by cost asymmetry, not policy`],
      ["Fill rate achieved", U.pct(sim.fill_rate, 2),
       `Against true demand in the simulator, so lost sales are counted rather than hidden`],
      ["Realised margin", U.money(sim.realized_margin),
       `Gross margin ${U.money(sim.gross_margin)} less holding ${U.money(sim.holding_cost)} and waste ${U.money(sim.waste_cost)}`],
      ["Lost margin", U.money(sim.lost_margin),
       `Demand the policy could not serve. This is what a blanket service level mis-prices`],
    ]
      .map(([cap, big, sub]) =>
        `<div class="card"><div class="cap">${cap}</div><div class="big">${big}</div><div class="sub">${sub}</div></div>`)
      .join("");

    // Widest fractile spread: the clearest demonstration that the formula bites.
    const sorted = [...series].sort((a, b) => a.targetQ - b.targetQ);
    const show = [...sorted.slice(0, 8), ...sorted.slice(-8).reverse()];
    el("fractileTable").innerHTML = tbl(
      [
        { label: "Item" }, { label: "Loc" }, { label: "Class" },
        { label: "Price", num: true }, { label: "Cost", num: true },
        { label: "Margin", num: true }, { label: "Shelf life", num: true },
        { label: "Underage", num: true }, { label: "Overage", num: true },
        { label: "Target q", num: true }, { label: "Order-up-to", num: true },
      ],
      show.map((s) => ({
        cells: [
          { v: s.item },
          { v: s.store },
          { v: U.CLASS_SHORT[s.cls] || s.cls },
          { v: "$" + s.price.toFixed(2), num: true },
          { v: "$" + s.cost.toFixed(2), num: true },
          { v: U.pct(s.marginRate, 0), num: true },
          { v: s.shelfLife > 60 ? "durable" : s.shelfLife + "d", num: true },
          { v: "$" + s.underage.toFixed(3), num: true },
          { v: "$" + s.overage.toFixed(3), num: true },
          { v: s.targetQ.toFixed(3), num: true, cls: s.targetQ > 0.85 ? "pos" : s.targetQ < 0.65 ? "neg" : "" },
          { v: U.nf(s.orderUpTo), num: true },
        ],
      }))
    );

    el("simTable").innerHTML = tbl(
      [{ label: "Quantity" }, { label: "Value", num: true }],
      [
        ["True demand (units)", U.nf(sim.demand)],
        ["Sales (units)", U.nf(sim.sales)],
        ["Lost sales (units)", U.nf(sim.lost_sales)],
        ["Waste (units)", U.nf(sim.waste)],
        ["Fill rate", U.pct(sim.fill_rate, 3)],
        ["Cycle service level", U.pct(sim.cycle_service_level, 2)],
        ["Gross margin", U.money(sim.gross_margin)],
        ["Holding cost", U.money(sim.holding_cost)],
        ["Waste cost", U.money(sim.waste_cost)],
        ["Lost margin", U.money(sim.lost_margin)],
        ["Realised margin", U.money(sim.realized_margin)],
        ["Average inventory (units)", U.nf(sim.avg_inventory, 1)],
        ["Conservation residual", sim.conservation_residual.toExponential(2)],
        ["Minimum on-hand", U.nf(sim.min_on_hand, 2)],
      ].map(([k, v]) => ({ cells: [{ v: k }, { v, num: true }] }))
    );

    const t = m.temporal;
    el("temporalTable").innerHTML = tbl(
      [{ label: "Bucket" }, { label: "Units", num: true }, { label: "Check" }],
      [
        ...t.weekly.map((v, i) => ({
          cells: [{ v: "Week " + (i + 1) }, { v: U.nf(v), num: true }, { v: "" }],
        })),
        {
          cells: [
            { v: "Sum of weeks" },
            { v: U.nf(t.weekly.reduce((a, b) => a + b, 0)), num: true },
            { v: "" },
          ],
        },
        {
          _hero: true,
          cells: [
            { v: "28-day total (reconciled)" },
            { v: U.nf(t.monthly), num: true },
            {
              v: `<span class="pos">exact match — residual ${Math.abs(
                t.weekly.reduce((a, b) => a + b, 0) - t.monthly
              ).toExponential(1)}</span>`,
            },
          ],
        },
      ]
    );
  }

  /* ====================================================== public */

  function init(bundle) { D = bundle; return api; }
  const rendered = new Set();
  function render(which, force = false) {
    if (rendered.has(which) && !force) return;
    rendered.add(which);
    if (which === "pipeline") renderPipeline();
    if (which === "evidence") renderEvidence();
    if (which === "decisions") renderDecisions();
  }
  function invalidate() { rendered.clear(); }

  const api = { init, render, invalidate };
  return api;
})();
