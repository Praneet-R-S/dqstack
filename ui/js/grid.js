/* ============================================================================
   The forecast grid.

   Canvas, not DOM. Above roughly ten thousand visible cells the browser cannot
   lay out that many elements at frame rate, and this grid routinely shows
   1,000 rows x 14 columns with a rendered glyph in a third of the cells.
   Scrolling is delegated to a native overflow container driving a spacer div, so
   momentum, trackpad behaviour and scrollbars are the platform's rather than a
   reimplementation of them; the canvas is sticky inside that container and
   redraws against the scroll offset.

   The signature element lives here: an in-cell uncertainty micro-fan behind
   every weekly numeral, plus a 3px confidence gutter at the left edge of every
   row. Together they answer "where is the risk, and can I trust this number"
   across fifty rows without a single interaction.

   Canvas is invisible to assistive technology, so a parallel ARIA grid of the
   visible window is maintained in #gridAria. That is real work and it is done
   here rather than discovered at the end.
   ============================================================================ */

const Grid = (() => {
  const HEADER_H = 27;
  const FAN_W = 40, FAN_H = 14;

  let cols = [];
  let rows = [];
  let sel = 0;
  let hover = -1;
  let scrollTop = 0, scrollLeft = 0;
  let vw = 0, vh = 0;
  let canvas, ctx, scroller, spacer, aria;
  let onSelect = () => {};
  let overrides = new Map();
  let futureDates = [];
  let sortKey = "priority", sortDir = -1;

  // Selection is a rectangle, not a row. The spec's keyboard model is Excel's, and
  // fill-down, range paste and range undo are all defined over a rectangle — so the
  // anchor/focus pair is the primitive everything else is built on.
  // Grouping. `groupBy` is a field name on the row, or null for the flat list.
  // `collapsed` holds the group keys currently folded shut. Aggregates are computed
  // on demand from the member rows, never stored — a stored aggregate is a second
  // source of truth that drifts the moment an override lands.
  let groupBy = null;
  let collapsed = new Set();
  let baseRows = [];              // ungrouped, sorted rows
  let colState = {};              // persisted widths / order

  let anchor = { row: 0, col: 0 };   // where the selection started
  let focus = { row: 0, col: 0 };    // where it currently extends to
  let editCol = 0;                   // which week column has keyboard focus
  // Held at module scope on purpose. A ResizeObserver created inline as
  // `new ResizeObserver(cb).observe(el)` has no strong reference and can be
  // garbage-collected, after which it silently never fires again — the canvas
  // then keeps whatever size it happened to measure first.
  let ro = null;

  /* ---- the 60fps budget -------------------------------------------------
     The spec sets a build-failing threshold: under 16ms sustained, zero frames over
     32ms. Measured at 18.6ms median / 40ms p95, and the cost is not the ARIA mirror
     (1.2ms) or getComputedStyle (0.1ms) — it is the micro-fan. Seventy-odd glyphs
     per frame, each three alpha-composited canvas paths, is simply more path work
     than a frame has time for.

     Two fixes, both standard for canvas grids and neither of which sacrifices the
     glyph. Cache each rendered glyph to a small offscreen canvas keyed by its data,
     so scrolling back over rows already seen is a blit rather than a redraw. And
     while a scroll is actually in flight, draw only the cheap median line for
     glyphs not already cached, then repaint at full fidelity once motion stops —
     nobody can read a 40x14 glyph during a fast scroll, and the frame budget exists
     to keep the motion itself smooth. */
  const fanCache = new Map();
  const FAN_CACHE_MAX = 900;
  let scrolling = false;
  let scrollIdleTimer = null;

  /* ---- columns ---------------------------------------------------------- */
  function buildCols(dates) {
    const wk = [0, 1, 2, 3].map((w) => "W" + U.isoWeek(dates[w * 7]));
    cols = [
      { key: "gutter", label: "", w: 4, frozen: true, kind: "gutter" },
      { key: "flags", label: "", w: 22, frozen: true, kind: "flags" },
      { key: "item", label: "ITEM", w: 122, frozen: true, kind: "text", sort: "item" },
      { key: "store", label: "LOC", w: 56, frozen: true, kind: "text", sort: "store" },
      { key: "cls", label: "CLASS", w: 84, frozen: true, kind: "cls", sort: "cls" },
      { key: "health", label: "HEALTH", w: 62, frozen: true, kind: "health", sort: "health", right: true },
      { key: "w0", label: wk[0], w: 88, kind: "week", widx: 0, right: true },
      { key: "w1", label: wk[1], w: 88, kind: "week", widx: 1, right: true },
      { key: "w2", label: wk[2], w: 88, kind: "week", widx: 2, right: true },
      { key: "w3", label: wk[3], w: 88, kind: "week", widx: 3, right: true },
      { key: "total", label: "28D", w: 68, kind: "total", sort: "total", right: true },
      { key: "band", label: "P10–P90", w: 86, kind: "band", right: true },
      { key: "tq", label: "TGT Q", w: 62, kind: "tq", sort: "targetQ", right: true },
      { key: "oup", label: "ORDER-UP-TO", w: 100, kind: "oup", sort: "orderUpTo", right: true },
      { key: "delta", label: "Δ PRIOR", w: 78, kind: "delta", sort: "swing", right: true },
      { key: "lost", label: "LOST MGN", w: 86, kind: "lost", sort: "lostMargin", right: true },
    ];
  }

  // Indices of the editable week columns, in display order.
  const weekCols = () => cols.map((c, i) => (c.kind === "week" ? i : -1)).filter((i) => i >= 0);
  const weekIdxOf = (colIndex) => (cols[colIndex] ? cols[colIndex].widx : null);

  const selRect = () => ({
    r0: Math.min(anchor.row, focus.row),
    r1: Math.max(anchor.row, focus.row),
    c0: Math.min(anchor.col, focus.col),
    c1: Math.max(anchor.col, focus.col),
  });

  const inSelection = (rowIdx, colIndex) => {
    const s = selRect();
    return rowIdx >= s.r0 && rowIdx <= s.r1 && colIndex >= s.c0 && colIndex <= s.c1;
  };

  /* ---- grouping ---------------------------------------------------------
     A group header is a synthetic row carrying live aggregates of its members.
     Sums are additive so they aggregate directly; a target quantile and a health
     score are not, so the header shows a member-weighted mean for health and omits
     the fractile entirely rather than inventing an average of ratios. */

  function aggregate(key, members) {
    const sum = (f) => members.reduce((a, r) => a + (f(r) || 0), 0);

    // Aggregation must include pending edits, or "live" is a misnomer: a planner
    // who fills a column down would watch the group total sit still. An override
    // sets a level, not a distribution, so the whole week's quantile vector is
    // shifted by the same amount — the band keeps its width and its centre moves.
    const eff = (r, w, k) => {
      const wk = r.weekly[w];
      if (!wk) return 0;
      const ov = overrides.get(r.id + ":" + w);
      if (ov === undefined) return wk[k] || 0;
      return Math.max(0, (wk[k] || 0) + (ov - wk[3]));
    };

    const weekly = [0, 1, 2, 3].map((w) => {
      const out = new Array(7).fill(0);
      for (const r of members) for (let k = 0; k < out.length; k++) out[k] += eff(r, w, k);
      return out;
    });
    return {
      __group: true,
      key,
      count: members.length,
      id: "__grp__" + key,
      item: key,
      store: `${members.length} series`,
      cls: "",
      // Revenue-weighted so a group's health reflects what it is worth, not a
      // headcount of its sparsest members.
      health: (() => {
        const wsum = sum((r) => Math.max(r._s.revenue, 1e-9));
        return wsum > 0
          ? members.reduce((a, r) => a + r.health * Math.max(r._s.revenue, 1e-9), 0) / wsum
          : members.reduce((a, r) => a + r.health, 0) / Math.max(members.length, 1);
      })(),
      weekly,
      priorWeekly: [0, 1, 2, 3].map((w) =>
        members.every((r) => r.priorWeekly[w] !== null)
          ? sum((r) => r.priorWeekly[w])
          : null
      ),
      fan: null,                       // no micro-fan on a header row
      // Deliberately the sum of the four week medians rather than of 28 daily
      // medians: the two differ slightly under moment matching, and a header
      // whose 28D column does not equal W1+W2+W3+W4 reads as broken.
      total: [0, 1, 2, 3].reduce((a, w) => a + weekly[w][3], 0),
      p10: [0, 1, 2, 3].reduce((a, w) => a + weekly[w][1], 0),
      p90: [0, 1, 2, 3].reduce((a, w) => a + weekly[w][5], 0),
      targetQ: null,                   // a mean of ratios is not a ratio
      orderUpTo: sum((r) => r.orderUpTo),
      swing: (() => {
        const pr = sum((r) => r.priorWeekly.reduce((a, v) => a + (v || 0), 0));
        const now = sum((r) => r.total);
        return pr > 1 ? (now - pr) / pr : null;
      })(),
      relWidth: null,
      reasons: [...new Set(members.flatMap((r) => r.reasons))],
      priority: Math.max(...members.map((r) => r.priority)),
      ledger: { lostMargin: sum((r) => r.ledger.lostMargin) },
      lostMargin: sum((r) => r.ledger.lostMargin),
      _members: members,
      _s: { revenue: sum((r) => r._s.revenue) },
    };
  }

  function buildDisplayRows() {
    if (!groupBy) return baseRows.slice();
    const groups = new Map();
    for (const r of baseRows) {
      const k = String(r[groupBy] ?? "—");
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(r);
    }
    const out = [];
    for (const k of [...groups.keys()].sort()) {
      const members = groups.get(k);
      out.push(aggregate(k, members));
      if (!collapsed.has(k)) out.push(...members);
    }
    return out;
  }

  const frozenW = () => cols.filter((c) => c.frozen).reduce((a, c) => a + c.w, 0);
  const totalW = () => cols.reduce((a, c) => a + c.w, 0);
  const rowH = () => parseInt(getComputedStyle(document.documentElement).getPropertyValue("--row-h"), 10) || 28;
  const cellFS = () => parseInt(getComputedStyle(document.documentElement).getPropertyValue("--cell-fs"), 10) || 12;

  /* ---- the signature glyph --------------------------------------------- */
  function fanKey(rowId, wIdx, w, h) {
    return `${rowId}|${wIdx}|${w}x${h}|${document.documentElement.dataset.theme}`;
  }

  function renderFanToCanvas(fan, wIdx, w, h) {
    const dpr = window.devicePixelRatio || 1;
    const off = document.createElement("canvas");
    off.width = Math.max(1, Math.round(w * dpr));
    off.height = Math.max(1, Math.round(h * dpr));
    const c = off.getContext("2d");
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    drawFan(c, 0, 0, w, h, fan, wIdx);
    return off;
  }

  function microFan(x, y, w, h, fan, wIdx, health, rowId) {
    if (!fan) return;                       // group headers carry no fan
    const key = fanKey(rowId, wIdx, w, h);
    let glyph = fanCache.get(key);
    if (glyph) {
      ctx.drawImage(glyph, x, y, w, h);
      return;
    }
    if (scrolling) {
      // Cheap stand-in while motion is in flight; the idle repaint upgrades it.
      drawFanMedianOnly(x, y, w, h, fan, wIdx);
      return;
    }
    glyph = renderFanToCanvas(fan, wIdx, w, h);
    if (fanCache.size >= FAN_CACHE_MAX) {
      // Plain FIFO eviction: scroll locality means the oldest entries are the ones
      // furthest from the viewport.
      fanCache.delete(fanCache.keys().next().value);
    }
    fanCache.set(key, glyph);
    ctx.drawImage(glyph, x, y, w, h);
  }

  function drawFanMedianOnly(x, y, w, h, fan, wIdx) {
    const days = [];
    let hi = 0;
    for (let d = 0; d < 7; d++) {
      const f = fan[wIdx * 7 + d];
      if (!f) continue;
      days.push(f);
      if (f[6] > hi) hi = f[6];
    }
    if (!days.length || hi <= 0) return;
    ctx.beginPath();
    for (let i = 0; i < days.length; i++) {
      const px = x + (w * i) / (days.length - 1 || 1);
      const py = y + h - (h * days[i][3]) / hi;
      i ? ctx.lineTo(px, py) : ctx.moveTo(px, py);
    }
    ctx.strokeStyle = U.rgba(U.tk("--series-forecast"), 0.55);
    ctx.lineWidth = 1;
    ctx.stroke();
  }

  function drawFan(ctx, x, y, w, h, fan, wIdx) {
    // Seven days of the week's predictive interval, drawn as a tapered band.
    // Vertical extent maps to P10–P90 spread; opacity maps to confidence, so a
    // wide distribution renders paler and uncertainty gets a pre-attentive
    // channel that does not consume a hue.
    const days = [];
    let hi = 0;
    for (let d = 0; d < 7; d++) {
      const f = fan[wIdx * 7 + d];
      if (!f) continue;
      days.push(f);
      if (f[6] > hi) hi = f[6];
    }
    if (!days.length || hi <= 0) return;

    const p10 = days.map((f) => f[1]), p90 = days.map((f) => f[5]);
    const p25 = days.map((f) => f[2]), p75 = days.map((f) => f[4]);
    const p50 = days.map((f) => f[3]);

    const sx = (i) => x + (w * i) / (days.length - 1 || 1);
    const sy = (v) => y + h - (h * v) / hi;

    const med = p50.reduce((a, b) => a + b, 0) / p50.length;
    const spread = (p90.reduce((a, b) => a + b, 0) - p10.reduce((a, b) => a + b, 0)) / p50.length;
    const conf = U.clamp(1 - spread / (2 * med + 1.5), 0.14, 1);

    const band = (lo, up, alpha) => {
      ctx.beginPath();
      ctx.moveTo(sx(0), sy(up[0]));
      for (let i = 1; i < up.length; i++) ctx.lineTo(sx(i), sy(up[i]));
      for (let i = lo.length - 1; i >= 0; i--) ctx.lineTo(sx(i), sy(lo[i]));
      ctx.closePath();
      ctx.fillStyle = U.rgba(U.tk("--series-forecast"), alpha * (0.35 + 0.65 * conf));
      ctx.fill();
    };
    band(p10, p90, 0.20);
    band(p25, p75, 0.30);

    ctx.beginPath();
    for (let i = 0; i < p50.length; i++) {
      const px = sx(i), py = sy(p50[i]);
      i ? ctx.lineTo(px, py) : ctx.moveTo(px, py);
    }
    ctx.strokeStyle = U.rgba(U.tk("--series-forecast"), 0.55 + 0.35 * conf);
    ctx.lineWidth = 1;
    ctx.stroke();
  }

  /* ---- painting -------------------------------------------------------- */
  function paint() {
    if (!ctx) return;
    const rh = rowH(), fs = cellFS();
    const fz = frozenW();
    ctx.clearRect(0, 0, vw, vh);
    ctx.fillStyle = U.tk("--bg-base");
    ctx.fillRect(0, 0, vw, vh);

    const first = Math.max(0, Math.floor(scrollTop / rh) - 1);
    const last = Math.min(rows.length, Math.ceil((scrollTop + vh) / rh) + 1);

    ctx.textBaseline = "middle";

    // ---- scrollable region
    ctx.save();
    ctx.beginPath();
    ctx.rect(fz, HEADER_H, vw - fz, vh - HEADER_H);
    ctx.clip();
    ctx.translate(-scrollLeft, 0);
    for (let i = first; i < last; i++) paintRow(i, false, rh, fs);
    ctx.restore();

    // ---- frozen region, drawn over the scrolled content
    ctx.save();
    ctx.beginPath();
    ctx.rect(0, HEADER_H, fz, vh - HEADER_H);
    ctx.clip();
    ctx.fillStyle = U.tk("--bg-surface");
    ctx.fillRect(0, HEADER_H, fz, vh - HEADER_H);
    for (let i = first; i < last; i++) paintRow(i, true, rh, fs);
    ctx.restore();

    // seam between frozen and scrolled, so the freeze reads as intentional
    ctx.fillStyle = U.tk("--border-strong");
    ctx.fillRect(fz - 1, HEADER_H, 1, vh - HEADER_H);
    if (scrollLeft > 0) {
      const g = ctx.createLinearGradient(fz, 0, fz + 10, 0);
      g.addColorStop(0, "rgba(0,0,0,0.28)");
      g.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = g;
      ctx.fillRect(fz, HEADER_H, 10, vh - HEADER_H);
    }

    paintHeader(fz);
    // The mirror is for assistive technology, which is not tracking a 60fps scroll.
    // Rebuilding it every frame is 1.2ms of pure waste during motion.
    if (!scrolling) syncAria(first, last);
  }

  function paintHeader(fz) {
    ctx.fillStyle = U.tk("--bg-raised");
    ctx.fillRect(0, 0, vw, HEADER_H);
    ctx.fillStyle = U.tk("--border-strong");
    ctx.fillRect(0, HEADER_H - 1, vw, 1);

    const draw = (frozen) => {
      let x = frozen ? 0 : frozenW();
      for (const c of cols) {
        if (!!c.frozen !== frozen) { if (!frozen) x += 0; continue; }
        const w = c.w;
        if (c.label) {
          ctx.font = `500 10px ${U.tk("fontUI")}`;
          ctx.fillStyle = sortKey === c.sort ? U.tk("--text-primary") : U.tk("--text-tertiary");
          ctx.textAlign = c.right ? "right" : "left";
          const tx = c.right ? x + w - 8 : x + 8;
          let label = c.label;
          if (sortKey === c.sort) label += sortDir < 0 ? " ↓" : " ↑";
          ctx.fillText(label, tx, HEADER_H / 2);
        }
        x += w;
      }
    };
    ctx.save();
    ctx.beginPath(); ctx.rect(frozenW(), 0, vw - frozenW(), HEADER_H); ctx.clip();
    ctx.translate(-scrollLeft, 0);
    let x = frozenW();
    for (const c of cols) {
      if (c.frozen) continue;
      ctx.font = `500 10px ${U.tk("fontUI")}`;
      ctx.fillStyle = sortKey === c.sort ? U.tk("--text-primary") : U.tk("--text-tertiary");
      ctx.textAlign = c.right ? "right" : "left";
      let label = c.label + (sortKey === c.sort ? (sortDir < 0 ? " ↓" : " ↑") : "");
      ctx.fillText(label, c.right ? x + c.w - 8 : x + 8, HEADER_H / 2);
      x += c.w;
    }
    ctx.restore();
    ctx.save();
    ctx.beginPath(); ctx.rect(0, 0, frozenW(), HEADER_H); ctx.clip();
    ctx.fillStyle = U.tk("--bg-raised");
    ctx.fillRect(0, 0, frozenW(), HEADER_H);
    draw(true);
    ctx.restore();
  }

  let rowIdxBeingPainted = -1;

  function paintRow(i, frozen, rh, fs) {
    rowIdxBeingPainted = i;
    const r = rows[i];
    const y = HEADER_H + i * rh - scrollTop;
    if (y + rh < HEADER_H || y > vh) return;

    const isGroup = !!r.__group;
    if (isGroup) {
      ctx.fillStyle = U.tk("--bg-raised");
      const x0 = frozen ? 0 : frozenW();
      ctx.fillRect(x0, y, frozen ? frozenW() : totalW(), rh);
      ctx.fillStyle = U.rgba(U.tk("--series-forecast"), 0.5);
      if (frozen) ctx.fillRect(0, y, 2, rh);
    }
    const isSel = i === sel;
    const isHover = i === hover;
    if (isSel || isHover) {
      ctx.fillStyle = isSel
        ? U.rgba(U.tk("--series-forecast"), 0.10)
        : U.tk("--bg-raised");
      const x0 = frozen ? 0 : frozenW();
      ctx.fillRect(x0, y, frozen ? frozenW() : totalW(), rh);
    }
    ctx.fillStyle = U.tk("--border-subtle");
    ctx.fillRect(frozen ? 0 : frozenW(), y + rh - 1, frozen ? frozenW() : totalW(), 1);

    let x = 0;
    for (const c of cols) {
      if (!!c.frozen !== frozen) { x += c.w; continue; }
      paintCell(c, r, x, y, rh, fs, isSel);
      x += c.w;
    }
  }

  function paintCell(c, r, x, y, rh, fs, isSel) {
    const pad = 8;
    const tx = c.right ? x + c.w - pad : x + pad;
    ctx.textAlign = c.right ? "right" : "left";
    const mid = y + rh / 2;

    switch (c.kind) {
      case "gutter": {
        // The confidence gutter. Health-coloured, always present.
        ctx.fillStyle = U.healthColor(r.health);
        ctx.fillRect(x + 1, y + 2, 3, rh - 5);
        break;
      }
      case "flags": {
        const rs = r.reasons || [];
        const n = Math.min(rs.length, 3);
        for (let k = 0; k < n; k++) {
          ctx.fillStyle = U.tk(U.REASON_COLOR[rs[k]] || "--text-tertiary");
          ctx.fillRect(x + 6, y + rh / 2 - 5 + k * 4, 4, 3);
        }
        break;
      }
      case "text": {
        if (r.__group && c.key === "item") {
          ctx.font = `600 ${fs}px ${U.tk("fontUI")}`;
          ctx.fillStyle = U.tk("--text-primary");
          ctx.textAlign = "left";
          const open = !collapsed.has(r.key);
          ctx.fillText(`${open ? "\u25be" : "\u25b8"}  ${r.item}`, x + pad, mid);
          break;
        }
        ctx.font = `${fs}px ${U.tk("fontNum")}`;
        ctx.fillStyle = U.tk("--text-primary");
        const t = c.key === "item" ? r.item.replace(/^(FOODS|HOUSEHOLD|HOBBIES)_/, "") : r[c.key];
        ctx.fillText(t, tx, mid);
        break;
      }
      case "cls": {
        ctx.font = `${fs - 1}px ${U.tk("fontUI")}`;
        ctx.fillStyle = U.tk("--text-secondary");
        ctx.fillText(U.CLASS_SHORT[r.cls] || r.cls, tx, mid);
        break;
      }
      case "health": {
        // Colour plus numeral: never colour alone.
        ctx.font = `${fs}px ${U.tk("fontNum")}`;
        ctx.fillStyle = U.healthColor(r.health);
        ctx.fillText(r.health.toFixed(2), tx, mid);
        break;
      }
      case "week": {
        const wIdx = c.widx;
        const wk = r.weekly[wIdx];
        if (!wk) break;
        if (r.__group) {
          // Live aggregate, recomputed from members. No micro-fan: summed
          // quantiles are not a group's quantiles, and drawing one would imply a
          // distribution that was never estimated.
          ctx.font = `600 ${fs}px ${U.tk("fontNum")}`;
          ctx.fillStyle = U.tk("--text-primary");
          ctx.textAlign = "right";
          ctx.fillText(U.nf(Math.round(wk[3])), x + c.w - pad, mid);
          break;
        }
        const myCol = cols.indexOf(c);
        if (inSelection(rowIdxBeingPainted, myCol)) {
          ctx.fillStyle = U.rgba(U.tk("--series-forecast"), 0.16);
          ctx.fillRect(x, y, c.w, rowH() - 1);
          // Focus cell gets a ring so the caret position is unambiguous inside a range.
          if (rowIdxBeingPainted === focus.row && myCol === focus.col) {
            ctx.strokeStyle = U.tk("--series-forecast");
            ctx.lineWidth = 1.5;
            ctx.strokeRect(x + 0.75, y + 0.75, c.w - 1.5, rowH() - 2.5);
          }
        }
        const rel = r.priorWeekly && r.priorWeekly[wIdx] > 0
          ? (wk[3] - r.priorWeekly[wIdx]) / r.priorWeekly[wIdx] : null;
        const tint = U.varianceTint(rel);
        if (tint) { ctx.fillStyle = tint; ctx.fillRect(x, y, c.w, rh - 1); }

        microFan(x + 5, y + (rh - FAN_H) / 2, FAN_W, FAN_H, r.fan, wIdx, r.health, r.id);

        const ov = overrides.get(r.id + ":" + wIdx);
        ctx.font = `${fs}px ${U.tk("fontNum")}`;
        ctx.fillStyle = ov !== undefined ? U.tk("--series-override") : U.tk("--text-primary");
        ctx.fillText(U.nf(ov !== undefined ? ov : Math.round(wk[3])), tx, mid);
        if (ov !== undefined) {
          ctx.fillStyle = U.tk("--series-override");
          ctx.fillRect(x + c.w - 3, y + 2, 2, rh - 5);
        }
        break;
      }
      case "total": {
        ctx.font = `${fs}px ${U.tk("fontNum")}`;
        ctx.fillStyle = U.tk("--text-primary");
        ctx.fillText(U.nf(Math.round(r.total)), tx, mid);
        break;
      }
      case "band": {
        if (r.__group) break;
        ctx.font = `${fs - 1}px ${U.tk("fontNum")}`;
        ctx.fillStyle = U.tk("--text-secondary");
        ctx.fillText(`${Math.round(r.p10)}–${Math.round(r.p90)}`, tx, mid);
        break;
      }
      case "tq": {
        if (r.targetQ === null) break;      // not meaningful for a group
        ctx.font = `${fs}px ${U.tk("fontNum")}`;
        // Deliberately not colour-coded: the fractile is not good or bad, it is
        // simply what the cost structure implies.
        ctx.fillStyle = U.tk("--text-secondary");
        ctx.fillText(r.targetQ.toFixed(3), tx, mid);
        break;
      }
      case "oup": {
        ctx.font = `${fs}px ${U.tk("fontNum")}`;
        ctx.fillStyle = U.tk("--text-primary");
        ctx.fillText(U.nf(r.orderUpTo), tx, mid);
        break;
      }
      case "delta": {
        const s = r.swing;
        ctx.font = `${fs}px ${U.tk("fontNum")}`;
        if (s === null || Math.abs(s) < 0.005) {
          ctx.fillStyle = U.tk("--text-tertiary");
          ctx.fillText("—", tx, mid);
        } else {
          ctx.fillStyle = s > 0 ? U.tk("--state-excess") : U.tk("--state-risk");
          // Glyph as well as hue, so direction survives colour blindness.
          ctx.fillText((s > 0 ? "▲ " : "▼ ") + Math.abs(s * 100).toFixed(0) + "%", tx, mid);
        }
        break;
      }
      case "lost": {
        ctx.font = `${fs}px ${U.tk("fontNum")}`;
        const v = r.ledger.lostMargin;
        ctx.fillStyle = v > 0.5 ? U.tk("--state-risk") : U.tk("--text-tertiary");
        ctx.fillText(v > 0.005 ? U.money(v) : "—", tx, mid);
        break;
      }
    }
  }

  /* ---- accessibility mirror -------------------------------------------
     A canvas is invisible to assistive technology, so the visible window is
     mirrored as a real table. It has to survive every row shape the grid can
     produce, including group headers where a target quantile is deliberately null —
     a mean of ratios is not a ratio. Reading `.toFixed` off that null threw and took
     the whole paint down, which is a reminder that the mirror is code on the render
     path, not documentation. */
  const num = (v, d = 0) =>
    v === null || v === undefined || Number.isNaN(v) ? "not applicable" : v.toFixed(d);

  function syncAria(first, last) {
    if (!aria) return;
    let body = "";
    for (let i = first; i < Math.min(last, first + 60); i++) {
      const r = rows[i];
      if (r.__group) {
        const open = !collapsed.has(r.key);
        body +=
          `<tr aria-selected="${i === sel}" aria-expanded="${open}">` +
          `<th scope="row" colspan="4">Group ${r.item}, ${r.count} series, ` +
          `${open ? "expanded" : "collapsed"}. Weighted data health ${num(r.health, 2)}</th>` +
          r.weekly.map((w) => `<td>${Math.round(w[3])} units, aggregate</td>`).join("") +
          `<td>${Math.round(r.total)}</td><td>not applicable for a group</td>` +
          `<td>order up to ${Math.round(r.orderUpTo)}</td></tr>`;
        continue;
      }
      const flags = r.reasons && r.reasons.length
        ? `, flagged ${r.reasons.map((x) => U.REASON_LABEL[x] || x).join(" and ")}`
        : "";
      body +=
        `<tr aria-selected="${i === sel}"><td>${r.item}</td><td>${r.store}</td>` +
        `<td>${r.cls}</td><td>health ${num(r.health, 2)}${flags}</td>` +
        r.weekly.map((w) =>
          `<td>${Math.round(w[3])} units, P10 ${Math.round(w[1])} to P90 ${Math.round(w[5])}</td>`
        ).join("") +
        `<td>${Math.round(r.total)}</td><td>target quantile ${num(r.targetQ, 3)}</td>` +
        `<td>order up to ${Math.round(r.orderUpTo)}</td></tr>`;
    }
    const grouped = groupBy ? `, grouped by ${groupBy}` : "";
    aria.innerHTML =
      `<table><caption>Forecast grid${grouped}. Showing rows ${first + 1} to ` +
      `${Math.min(last, rows.length)} of ${rows.length}.</caption>` +
      `<thead><tr><th>Item</th><th>Location</th><th>Class</th><th>Data health</th>` +
      cols.filter((c) => c.kind === "week").map((c) => `<th>${c.label}</th>`).join("") +
      `<th>28 day total</th><th>Target quantile</th><th>Order up to</th></tr></thead>` +
      `<tbody>${body}</tbody></table>`;
  }

  /* ---- layout / events ------------------------------------------------- */
  function resize() {
    const box = scroller.getBoundingClientRect();
    vw = box.width; vh = box.height;
    ctx = U.fitCanvas(canvas, vw, vh);
    spacer.style.width = totalW() + "px";
    spacer.style.height = HEADER_H + rows.length * rowH() + "px";
    paint();
  }

  function rowAt(clientY) {
    const box = scroller.getBoundingClientRect();
    const y = clientY - box.top;
    if (y < HEADER_H) return -1;
    return Math.floor((y - HEADER_H + scrollTop) / rowH());
  }

  function colAt(clientX) {
    const box = scroller.getBoundingClientRect();
    let x = clientX - box.left;
    const fz = frozenW();
    if (x >= fz) x += scrollLeft;
    let acc = 0;
    for (const c of cols) {
      if (x >= acc && x < acc + c.w) return c;
      acc += c.w;
    }
    return null;
  }

  function select(i, scroll = true, extend = false) {
    if (!rows.length) return;
    sel = U.clamp(i, 0, rows.length - 1);
    focus.row = sel;
    if (!extend) { anchor.row = sel; anchor.col = focus.col; }
    if (scroll) {
      const rh = rowH();
      const top = sel * rh;
      const visTop = scrollTop, visBot = scrollTop + vh - HEADER_H - rh;
      if (top < visTop) scroller.scrollTop = top;
      else if (top > visBot) scroller.scrollTop = top - (vh - HEADER_H - rh);
    }
    paint();
    onSelect(rows[sel], sel);
  }

  function sortBy(key) {
    if (!key) return;
    if (sortKey === key) sortDir = -sortDir;
    else { sortKey = key; sortDir = key === "item" || key === "store" || key === "cls" ? 1 : -1; }
    baseRows.sort((a, b) => {
      const av = a[sortKey], bv = b[sortKey];
      if (typeof av === "string") return sortDir * av.localeCompare(bv);
      return sortDir * ((av ?? -Infinity) - (bv ?? -Infinity));
    });
    regroup();
  }

  function colRightEdge(col) {
    let x = 0;
    for (const c of cols) { x += c.w; if (c === col) return x; }
    return x;
  }

  function applyColState() {
    if (colState.widths) {
      for (const c of cols) if (colState.widths[c.key]) c.w = colState.widths[c.key];
    }
    if (Array.isArray(colState.order) && colState.order.length === cols.length) {
      const byKey = new Map(cols.map((c) => [c.key, c]));
      const reordered = colState.order.map((k) => byKey.get(k)).filter(Boolean);
      if (reordered.length === cols.length) cols = reordered;
    }
  }

  function saveColState() {
    colState = {
      widths: Object.fromEntries(cols.map((c) => [c.key, c.w])),
      order: cols.map((c) => c.key),
    };
    try { localStorage.setItem("dq.cols", JSON.stringify(colState)); } catch (_) {}
  }

  /* ---- public --------------------------------------------------------- */
  function init(opts) {
    scroller = document.getElementById("gridScroll");
    spacer = document.getElementById("gridSpacer");
    canvas = document.getElementById("gridCanvas");
    aria = document.getElementById("gridAria");
    onSelect = opts.onSelect || onSelect;
    futureDates = opts.futureDates;
    buildCols(futureDates);

    scroller.addEventListener("scroll", () => {
      scrollTop = scroller.scrollTop;
      scrollLeft = scroller.scrollLeft;
      scrolling = true;
      paint();
      clearTimeout(scrollIdleTimer);
      scrollIdleTimer = setTimeout(() => {
        scrolling = false;
        paint();                            // upgrade the stand-ins to real glyphs
      }, 90);
    }, { passive: true });

    canvas.addEventListener("mousemove", (e) => {
      const i = rowAt(e.clientY);
      if (i !== hover) { hover = i >= 0 && i < rows.length ? i : -1; paint(); }
    });
    canvas.addEventListener("mouseleave", () => { hover = -1; paint(); });
    canvas.addEventListener("click", (e) => {
      const box = scroller.getBoundingClientRect();
      if (e.clientY - box.top < HEADER_H) {
        const c = colAt(e.clientX);
        if (c && c.sort) sortBy(c.sort);
        return;
      }
      const i = rowAt(e.clientY);
      if (i >= 0 && i < rows.length) {
        const r = rows[i];
        if (r.__group) { api.toggleGroup(r.key); return; }
        select(i, false);
      }
    });
    canvas.addEventListener("dblclick", (e) => {
      const c = colAt(e.clientX);
      if (c && c.kind === "week") opts.onEditWeek && opts.onEditWeek(rows[sel], c.widx);
    });

    // Persisted column widths and order. A planner who has arranged the grid to
    // their workflow should not have to redo it every morning.
    try {
      colState = JSON.parse(localStorage.getItem("dq.cols") || "{}");
    } catch (_) { colState = {}; }
    applyColState();

    // Drag the right edge of a header cell to resize.
    let drag = null;
    canvas.addEventListener("mousedown", (e) => {
      const box = scroller.getBoundingClientRect();
      if (e.clientY - box.top > HEADER_H) return;
      const c = colAt(e.clientX);
      if (!c) return;
      const edge = colRightEdge(c) - (e.clientX - box.left + (c.frozen ? 0 : scrollLeft));
      if (Math.abs(edge) <= 4) {
        drag = { col: c, startX: e.clientX, startW: c.w };
        e.preventDefault();
      }
    });
    window.addEventListener("mousemove", (e) => {
      if (!drag) return;
      drag.col.w = Math.max(36, Math.round(drag.startW + (e.clientX - drag.startX)));
      resize();
    });
    window.addEventListener("mouseup", () => {
      if (!drag) return;
      drag = null;
      saveColState();
    });

    ro = new ResizeObserver(U.debounce(resize, 40));
    ro.observe(scroller);
    // Belt and braces: a window listener covers the case where the observer is
    // unavailable, and one deferred call covers first paint before web fonts and
    // scrollbars have settled the box.
    window.addEventListener("resize", U.debounce(resize, 60));
    requestAnimationFrame(resize);
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(resize);

    window.addEventListener("dq:theme", () => { fanCache.clear(); paint(); });
    window.addEventListener("dq:density", () => resize());
    return api;
  }

  function setRows(next) {
    baseRows = next;
    rows = buildDisplayRows();
    sel = 0;
    scroller.scrollTop = 0;
    resize();
    const first = rows.find((r) => !r.__group);
    if (first) onSelect(first, rows.indexOf(first));
  }

  function regroup() {
    const keep = rows[sel] && rows[sel].id;
    rows = buildDisplayRows();
    const back = rows.findIndex((r) => r.id === keep);
    sel = back >= 0 ? back : 0;
    focus.row = sel;
    anchor.row = sel;
    resize();
  }

  /* ---- Excel-semantics editing ---------------------------------------------

     The spec adopts spreadsheet conventions wholesale, and deliberately: these
     users came from Excel and are fast in it, so a tool that cannot fill down or
     paste a column is slower than the thing it replaced. Undo in particular is
     defined over the whole pending set, because "a planner who pastes a column and
     realizes it was the wrong column needs one keystroke, not forty".
  ------------------------------------------------------------------------- */

  const undoStack = [];
  const redoStack = [];
  let onEdits = () => {};

  const keyOf = (rowIdx, wIdx) => rows[rowIdx].id + ":" + wIdx;

  function snapshot() {
    return new Map(overrides);
  }

  function restore(snap) {
    overrides = new Map(snap);
    if (groupBy) regroup(); else paint();
    onEdits();
  }

  function commitTransaction(mutate, label) {
    const before = snapshot();
    mutate();
    // Only record a step if something actually changed, so undo never burns a
    // keystroke on a no-op.
    let changed = before.size !== overrides.size;
    if (!changed) {
      for (const [k, v] of overrides) if (before.get(k) !== v) { changed = true; break; }
    }
    if (changed) {
      undoStack.push({ snap: before, label: label || "edit" });
      redoStack.length = 0;
    }
    fanCache.clear();          // an override changes the glyph it renders
    if (groupBy) regroup(); else paint();
    onEdits();
    return changed;
  }

  function setCell(rowIdx, wIdx, value) {
    const r = rows[rowIdx];
    if (!r) return;
    const model = Math.round(r.weekly[wIdx][3]);
    const k = keyOf(rowIdx, wIdx);
    if (value === null || !Number.isFinite(value) || Math.abs(value - model) < 0.5) {
      overrides.delete(k);
    } else {
      overrides.set(k, Math.max(0, Math.round(value)));
    }
  }

  function cellValue(rowIdx, wIdx) {
    const k = keyOf(rowIdx, wIdx);
    const ov = overrides.get(k);
    return ov !== undefined ? ov : Math.round(rows[rowIdx].weekly[wIdx][3]);
  }

  /** Cmd-D: copy the top row of the selection down through the rest of it. */
  function fillDown() {
    const s = selRect();
    const wcols = [];
    for (let c = s.c0; c <= s.c1; c++) if (cols[c] && cols[c].kind === "week") wcols.push(c);
    if (!wcols.length || s.r1 <= s.r0) return false;
    return commitTransaction(() => {
      for (const c of wcols) {
        const w = cols[c].widx;
        const src = cellValue(s.r0, w);
        for (let r = s.r0 + 1; r <= s.r1; r++) setCell(r, w, src);
      }
    }, `fill down ${s.r1 - s.r0} rows`);
  }

  /** Cmd-C: the selection as Excel-compatible TSV. */
  function copyTSV() {
    const s = selRect();
    const wcols = [];
    for (let c = s.c0; c <= s.c1; c++) if (cols[c] && cols[c].kind === "week") wcols.push(c);
    if (!wcols.length) return "";
    const lines = [];
    for (let r = s.r0; r <= s.r1; r++) {
      lines.push(wcols.map((c) => cellValue(r, cols[c].widx)).join("\t"));
    }
    return lines.join("\n");
  }

  /** Cmd-V: paste TSV, anchored at the focus cell, clipped to the grid. */
  function pasteTSV(text) {
    const grid = String(text || "")
      .replace(/\r/g, "")
      .split("\n")
      .filter((l) => l.length)
      .map((l) => l.split("\t").map((v) => parseFloat(v.replace(/[^0-9.\-]/g, ""))));
    if (!grid.length) return false;

    const s = selRect();
    const startRow = s.r0;
    const startColIdx = cols[s.c0] && cols[s.c0].kind === "week" ? s.c0 : weekCols()[0];
    const wcols = weekCols();
    const startPos = wcols.indexOf(startColIdx);
    if (startPos < 0) return false;

    return commitTransaction(() => {
      grid.forEach((line, dr) => {
        const rowIdx = startRow + dr;
        if (rowIdx >= rows.length) return;
        line.forEach((val, dc) => {
          const colIdx = wcols[startPos + dc];
          if (colIdx === undefined) return;      // clip, do not wrap
          setCell(rowIdx, cols[colIdx].widx, val);
        });
      });
    }, `paste ${grid.length}x${grid[0].length}`);
  }

  function undo() {
    if (!undoStack.length) return false;
    const step = undoStack.pop();
    redoStack.push({ snap: snapshot(), label: step.label });
    restore(step.snap);
    return step.label;
  }

  function redo() {
    if (!redoStack.length) return false;
    const step = redoStack.pop();
    undoStack.push({ snap: snapshot(), label: step.label });
    restore(step.snap);
    return step.label;
  }

  const api = {
    init, setRows, select, sortBy, paint, resize,
    get rows() { return rows; },
    get selected() { return rows[sel]; },
    get selectedIndex() { return sel; },
    move(d) { select(sel + d); },
    setOverride(id, wIdx, val) {
      commitTransaction(() => {
        const k = id + ":" + wIdx;
        if (val === null) overrides.delete(k); else overrides.set(k, val);
      }, "cell edit");
    },
    // range selection
    extendSelection(dRow, dCol) {
      focus.row = U.clamp(focus.row + dRow, 0, rows.length - 1);
      const wcols = weekCols();
      if (dCol) {
        const cur = wcols.indexOf(focus.col) >= 0 ? wcols.indexOf(focus.col) : 0;
        focus.col = wcols[U.clamp(cur + dCol, 0, wcols.length - 1)];
      }
      sel = focus.row;
      select(focus.row, true, true);
    },
    moveFocusCol(d) {
      const wcols = weekCols();
      const cur = wcols.indexOf(focus.col);
      focus.col = wcols[U.clamp((cur < 0 ? 0 : cur) + d, 0, wcols.length - 1)];
      anchor.col = focus.col;
      paint();
    },
    get selection() { return selRect(); },
    get focusWeek() { return weekIdxOf(focus.col); },
    setSelection(r0, r1, cIdx) {
      anchor.row = U.clamp(r0, 0, rows.length - 1);
      focus.row = U.clamp(r1, 0, rows.length - 1);
      const wcols = weekCols();
      anchor.col = focus.col = cIdx !== undefined ? cIdx : (wcols[0] ?? 0);
      paint();
    },
    fillDown, copyTSV, pasteTSV, undo, redo,
    get canUndo() { return undoStack.length; },
    get canRedo() { return redoStack.length; },
    get pendingMap() { return overrides; },
    setGroupBy(field) {
      groupBy = field || null;
      collapsed.clear();
      regroup();
    },
    get groupBy() { return groupBy; },
    toggleGroup(key) {
      collapsed.has(key) ? collapsed.delete(key) : collapsed.add(key);
      regroup();
    },
    collapseAll() {
      rows.filter((r) => r.__group).forEach((r) => collapsed.add(r.key));
      regroup();
    },
    expandAll() { collapsed.clear(); regroup(); },
    resetColumns() {
      try { localStorage.removeItem("dq.cols"); } catch (_) {}
      colState = {};
      buildCols(futureDates);
      resize();
    },
    onEdits(fn) { onEdits = fn; },
    // Discarding is itself a transaction, so Cmd-Z brings the edits back. A planner
    // who hits Escape by accident should not lose an afternoon's work.
    discardAll() { commitTransaction(() => overrides.clear(), "discard all"); },
    commitAll() { undoStack.length = 0; redoStack.length = 0; overrides.clear(); paint(); onEdits(); },
    resetSeries(id) {
      commitTransaction(() => {
        for (const k of [...overrides.keys()]) if (k.startsWith(id + ":")) overrides.delete(k);
      }, "reset series");
    },
    getOverride(id, wIdx) { return overrides.get(id + ":" + wIdx); },
    get overrideCount() { return overrides.size; },
    clearOverrides() { overrides.clear(); paint(); },
    HEADER_H,
  };
  return api;
})();
