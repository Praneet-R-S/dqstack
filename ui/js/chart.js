/* ============================================================================
   The forecast chart. The emotional centre of the product.

   Non-negotiables, all of them from the specification:

   - Nested fan bands at three levels, not one grey interval. A single band tells
     a planner nothing about the *shape* of the risk.
   - An unmistakable origin rule. Everything to its right is prediction.
   - Actual, forecast and override distinguished by colour *and* dash pattern,
     never colour alone.
   - The prior forecast version available as a ghost line, so a planner can see
     what changed since last cycle. Most requested feature in real planning
     tools, almost always missing.
   - Hover crosshair with the full quantile readout, not just the median.
   - Event annotations from the calendar as axis ticks.

   Also drawn, because Stage 1 exists: censored and out-of-assortment days are
   marked on the history line. A planner looking at a dip needs to know whether
   demand fell or the shelf was empty, and those are opposite conclusions.
   ============================================================================ */

const Chart = (() => {
  const PAD = { t: 14, r: 16, b: 26, l: 46 };
  let canvas, ctx, host, readout;
  let data = null;
  let W = 0, H = 0;
  let hoverX = null;
  let showPrior = true;
  let ro = null;   // retained; see the note in grid.js

  function init() {
    host = document.getElementById("chartHost");
    canvas = document.getElementById("chartCanvas");
    readout = document.getElementById("crosshair");
    ro = new ResizeObserver(U.debounce(resize, 40));
    ro.observe(host);
    window.addEventListener("resize", U.debounce(resize, 60));
    requestAnimationFrame(resize);
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(resize);
    canvas.addEventListener("mousemove", (e) => {
      const box = canvas.getBoundingClientRect();
      hoverX = e.clientX - box.left;
      draw();
    });
    canvas.addEventListener("mouseleave", () => {
      hoverX = null;
      readout.dataset.show = "false";
      draw();
    });
    window.addEventListener("dq:theme", () => draw());
    return api;
  }

  function resize() {
    const box = host.getBoundingClientRect();
    W = box.width; H = box.height;
    ctx = U.fitCanvas(canvas, W, H);
    draw();
  }

  function setSeries(series, dates, events, opts = {}) {
    data = { series, dates, events };
    showPrior = opts.showPrior !== false;
    draw();
  }

  /* ---- scales ---------------------------------------------------------- */
  function scales() {
    const { series, dates } = data;
    const nH = dates.history.length;
    const nF = dates.future.length;
    const n = nH + nF;

    let hi = 0;
    for (const v of series.histRaw) if (v > hi) hi = v;
    for (const f of series.fan) if (f[6] > hi) hi = f[6];
    if (showPrior) for (const v of series.prior) if (v > 0 && v > hi) hi = v;
    hi = hi <= 0 ? 1 : hi * 1.12;

    const plotW = W - PAD.l - PAD.r;
    const plotH = H - PAD.t - PAD.b;
    const dx = plotW / (n - 1);
    return {
      n, nH, nF, hi, plotW, plotH, dx,
      x: (i) => PAD.l + i * dx,
      y: (v) => PAD.t + plotH - (plotH * v) / hi,
      originX: PAD.l + (nH - 1) * dx,
    };
  }

  function niceTicks(hi, count = 4) {
    const raw = hi / count;
    const mag = Math.pow(10, Math.floor(Math.log10(Math.max(raw, 1e-9))));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || mag * 10;
    const out = [];
    for (let v = 0; v <= hi; v += step) out.push(v);
    return out;
  }

  /* ---- paint ----------------------------------------------------------- */
  function draw() {
    if (!ctx) return;
    ctx.clearRect(0, 0, W, H);
    if (!data) return;
    const s = scales();
    const { series, dates, events } = data;

    // ---- grid lines and y axis
    ctx.font = `10px ${U.tk("fontNum")}`;
    ctx.textBaseline = "middle";
    ctx.textAlign = "right";
    for (const t of niceTicks(s.hi)) {
      const y = s.y(t);
      ctx.strokeStyle = U.rgba(U.tk("--border-subtle"), 0.85);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(PAD.l, Math.round(y) + 0.5);
      ctx.lineTo(W - PAD.r, Math.round(y) + 0.5);
      ctx.stroke();
      ctx.fillStyle = U.tk("--text-tertiary");
      ctx.fillText(U.compact(t), PAD.l - 7, y);
    }

    // ---- future region wash: prediction is visually a different place
    ctx.fillStyle = U.rgba(U.tk("--series-forecast"), 0.035);
    ctx.fillRect(s.originX, PAD.t, W - PAD.r - s.originX, s.plotH);

    // ---- nested fan bands. 50%, 80% then 98%, opacity decreasing outward.
    const bands = [
      [2, 4, 0.30],   // p25 - p75
      [1, 5, 0.19],   // p10 - p90
      [0, 6, 0.11],   // p05 - p95
    ];
    for (const [lo, up, alpha] of bands) {
      ctx.beginPath();
      // Anchor the fan at the last actual so it grows out of the history line
      // rather than appearing to start from nowhere.
      const anchor = lastObserved(series);
      ctx.moveTo(s.originX, s.y(anchor));
      for (let i = 0; i < s.nF; i++) ctx.lineTo(s.x(s.nH + i), s.y(series.fan[i][up]));
      for (let i = s.nF - 1; i >= 0; i--) ctx.lineTo(s.x(s.nH + i), s.y(series.fan[i][lo]));
      ctx.closePath();
      ctx.fillStyle = U.rgba(U.tk("--series-forecast"), alpha);
      ctx.fill();
    }

    // ---- prior forecast version, ghosted
    if (showPrior) {
      ctx.save();
      ctx.setLineDash([1, 3]);
      ctx.strokeStyle = U.tk("--series-prior");
      ctx.lineWidth = 1.25;
      ctx.beginPath();
      let started = false;
      for (let i = 0; i < s.nF; i++) {
        const v = series.prior[i];
        if (v < 0) { started = false; continue; }
        const px = s.x(s.nH + i), py = s.y(v);
        started ? ctx.lineTo(px, py) : (ctx.moveTo(px, py), (started = true));
      }
      ctx.stroke();
      ctx.restore();
    }

    // ---- history: actual demand. Gaps where the observation is not signal.
    ctx.strokeStyle = U.tk("--series-actual");
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    let open = false;
    for (let i = 0; i < s.nH; i++) {
      const v = series.hist[i];
      if (v === null) { open = false; continue; }
      const px = s.x(i), py = s.y(v);
      open ? ctx.lineTo(px, py) : (ctx.moveTo(px, py), (open = true));
    }
    ctx.stroke();

    // Masked days: recorded sales are drawn faintly and the day is ticked, so a
    // dip is legible as "shelf was empty" rather than "demand fell".
    for (let i = 0; i < s.nH; i++) {
      if (series.hist[i] !== null) continue;
      const px = s.x(i);
      const notCarried = series.carried && series.carried[i] === false;
      /* --state-attention, not --series-override. This chart was the only surface
         that drew masked days at all, and it borrowed the colour that means "a
         planner edited this" for a condition of the data. Today and Products now
         shade the same two exclusions, and they take their colour from the tone of
         the flag that reports them, which is warn. Override amber goes back to
         meaning one thing. */
      ctx.fillStyle = U.rgba(
        notCarried ? U.tk("--text-tertiary") : U.tk("--state-attention"), 0.5
      );
      ctx.fillRect(px - 0.5, PAD.t + s.plotH - 5, 1.4, 5);
    }

    // ---- forecast median, dashed
    ctx.save();
    ctx.setLineDash([5, 3]);
    ctx.strokeStyle = U.tk("--series-forecast");
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(s.originX, s.y(lastObserved(series)));
    for (let i = 0; i < s.nF; i++) ctx.lineTo(s.x(s.nH + i), s.y(series.fan[i][3]));
    ctx.stroke();
    ctx.restore();

    // ---- override line, if any pending edits exist for this series
    if (series._override && series._override.length) {
      ctx.save();
      ctx.setLineDash([2, 2]);
      ctx.strokeStyle = U.tk("--series-override");
      ctx.lineWidth = 2;
      ctx.beginPath();
      let st = false;
      for (let i = 0; i < s.nF; i++) {
        const v = series._override[i];
        if (v === null || v === undefined) { st = false; continue; }
        const px = s.x(s.nH + i), py = s.y(v);
        st ? ctx.lineTo(px, py) : (ctx.moveTo(px, py), (st = true));
      }
      ctx.stroke();
      ctx.restore();
    }

    // ---- origin rule: a hairline with a label. Unmistakable.
    ctx.strokeStyle = U.tk("--series-forecast");
    ctx.lineWidth = 1;
    ctx.setLineDash([]);
    ctx.beginPath();
    ctx.moveTo(Math.round(s.originX) + 0.5, PAD.t - 4);
    ctx.lineTo(Math.round(s.originX) + 0.5, PAD.t + s.plotH);
    ctx.stroke();
    ctx.fillStyle = U.tk("--series-forecast");
    ctx.font = `600 9px ${U.tk("fontUI")}`;
    ctx.textAlign = "left";
    ctx.fillText("ORIGIN", s.originX + 4, PAD.t + 3);

    // ---- regime break marker, if Stage 1 truncated the training window
    if (series.regimeStart) {
      const ri = dates.history.indexOf(series.regimeStart);
      if (ri > 0) {
        ctx.save();
        ctx.setLineDash([2, 4]);
        ctx.strokeStyle = U.rgba(U.tk("--state-attention"), 0.8);
        ctx.beginPath();
        ctx.moveTo(s.x(ri), PAD.t);
        ctx.lineTo(s.x(ri), PAD.t + s.plotH);
        ctx.stroke();
        ctx.restore();
        ctx.fillStyle = U.tk("--state-attention");
        ctx.font = `9px ${U.tk("fontUI")}`;
        ctx.fillText("regime break", s.x(ri) + 3, PAD.t + 14);
      }
    }

    // ---- x axis: dates plus event ticks
    ctx.textAlign = "center";
    ctx.font = `10px ${U.tk("fontNum")}`;
    const all = dates.history.concat(dates.future);
    const step = Math.max(1, Math.round(s.n / 9));
    for (let i = 0; i < s.n; i += step) {
      ctx.fillStyle = U.tk("--text-tertiary");
      ctx.fillText(U.shortDate(all[i]), s.x(i), H - PAD.b + 12);
    }
    const evByDate = new Map(events.map((e) => [e.date, e]));
    for (let i = 0; i < s.n; i++) {
      const e = evByDate.get(all[i]);
      if (!e) continue;
      ctx.fillStyle = U.rgba(U.tk("--state-attention"), 0.9);
      ctx.fillRect(s.x(i) - 1, H - PAD.b - 1, 2, 5);
    }

    if (hoverX !== null) crosshair(s, all);
  }

  function lastObserved(series) {
    for (let i = series.hist.length - 1; i >= 0; i--)
      if (series.hist[i] !== null) return series.hist[i];
    return series.fan[0][3];
  }

  function crosshair(s, all) {
    const i = Math.round((hoverX - PAD.l) / s.dx);
    if (i < 0 || i >= s.n) { readout.dataset.show = "false"; return; }
    const px = s.x(i);

    ctx.save();
    ctx.strokeStyle = U.rgba(U.tk("--text-secondary"), 0.45);
    ctx.setLineDash([2, 2]);
    ctx.beginPath();
    ctx.moveTo(px, PAD.t);
    ctx.lineTo(px, PAD.t + s.plotH);
    ctx.stroke();
    ctx.restore();

    const { series, events } = data;
    const isFuture = i >= s.nH;
    let html = `<div class="cr-date">${U.shortDate(all[i])}${isFuture ? " · forecast" : ""}</div><table>`;

    if (isFuture) {
      const f = series.fan[i - s.nH];
      const labels = ["P05", "P10", "P25", "P50", "P75", "P90", "P95"];
      // Full quantile readout, not just the median — the whole point.
      [5, 4, 3, 2, 1, 0].forEach(() => {});
      for (let k = labels.length - 1; k >= 0; k--) {
        const strong = labels[k] === "P50";
        html += `<tr><td${strong ? ' style="color:var(--text-primary)"' : ""}>${labels[k]}</td>` +
                `<td${strong ? ' style="font-weight:600"' : ""}>${U.nf(Math.round(f[k]))}</td></tr>`;
      }
      const pr = series.prior[i - s.nH];
      if (pr >= 0)
        html += `<tr><td style="color:var(--series-prior)">prior P50</td><td>${U.nf(Math.round(pr))}</td></tr>`;
      ctx.fillStyle = U.tk("--series-forecast");
      ctx.beginPath();
      ctx.arc(px, s.y(f[3]), 3, 0, Math.PI * 2);
      ctx.fill();
    } else {
      const v = series.hist[i];
      const raw = series.histRaw[i];
      html += `<tr><td>actual</td><td>${v === null ? "masked" : U.nf(v)}</td></tr>`;
      if (v === null) {
        const nc = series.carried && series.carried[i] === false;
        html += `<tr><td style="color:var(--series-override)">${nc ? "not carried" : "suspected stockout"}</td>` +
                `<td>recorded ${U.nf(raw)}</td></tr>`;
      }
      if (v !== null) {
        ctx.fillStyle = U.tk("--series-actual");
        ctx.beginPath();
        ctx.arc(px, s.y(v), 3, 0, Math.PI * 2);
        ctx.fill();
      }
    }
    const ev = events.find((e) => e.date === all[i]);
    if (ev) html += `<tr><td style="color:var(--state-attention)">event</td><td>${ev.name}</td></tr>`;
    html += "</table>";

    readout.innerHTML = html;
    readout.dataset.show = "true";
    const rw = readout.offsetWidth || 176;
    readout.style.left = Math.min(Math.max(px + 12, 4), W - rw - 8) + "px";
    readout.style.top = PAD.t + 6 + "px";
  }

  const api = { init, setSeries, resize, draw, set showPrior(v) { showPrior = v; draw(); } };
  return api;
})();
