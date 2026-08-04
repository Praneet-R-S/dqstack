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

  const frozenW = () => cols.filter((c) => c.frozen).reduce((a, c) => a + c.w, 0);
  const totalW = () => cols.reduce((a, c) => a + c.w, 0);
  const rowH = () => parseInt(getComputedStyle(document.documentElement).getPropertyValue("--row-h"), 10) || 28;
  const cellFS = () => parseInt(getComputedStyle(document.documentElement).getPropertyValue("--cell-fs"), 10) || 12;

  /* ---- the signature glyph --------------------------------------------- */
  function microFan(x, y, w, h, fan, wIdx, health) {
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
    syncAria(first, last);
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

  function paintRow(i, frozen, rh, fs) {
    const r = rows[i];
    const y = HEADER_H + i * rh - scrollTop;
    if (y + rh < HEADER_H || y > vh) return;

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
        const rel = r.priorWeekly && r.priorWeekly[wIdx] > 0
          ? (wk[3] - r.priorWeekly[wIdx]) / r.priorWeekly[wIdx] : null;
        const tint = U.varianceTint(rel);
        if (tint) { ctx.fillStyle = tint; ctx.fillRect(x, y, c.w, rh - 1); }

        microFan(x + 5, y + (rh - FAN_H) / 2, FAN_W, FAN_H, r.fan, wIdx, r.health);

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
        ctx.font = `${fs - 1}px ${U.tk("fontNum")}`;
        ctx.fillStyle = U.tk("--text-secondary");
        ctx.fillText(`${Math.round(r.p10)}–${Math.round(r.p90)}`, tx, mid);
        break;
      }
      case "tq": {
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

  /* ---- accessibility mirror ------------------------------------------- */
  function syncAria(first, last) {
    if (!aria) return;
    const head = cols.filter((c) => c.label).map((c) => `<th>${c.label}</th>`).join("");
    let body = "";
    for (let i = first; i < Math.min(last, first + 60); i++) {
      const r = rows[i];
      body +=
        `<tr aria-selected="${i === sel}"><td>${r.item}</td><td>${r.store}</td>` +
        `<td>${r.cls}</td><td>health ${r.health.toFixed(2)}</td>` +
        r.weekly.map((w) => `<td>${Math.round(w[3])} units, P10 ${Math.round(w[1])} to P90 ${Math.round(w[5])}</td>`).join("") +
        `<td>${Math.round(r.total)}</td><td>target quantile ${r.targetQ.toFixed(3)}</td>` +
        `<td>order up to ${r.orderUpTo}</td></tr>`;
    }
    aria.innerHTML =
      `<table><caption>Forecast grid, rows ${first + 1} to ${Math.min(last, rows.length)} of ${rows.length}</caption>` +
      `<thead><tr><th>Item</th><th>Location</th><th>Class</th><th>Data health</th>` +
      cols.filter((c) => c.kind === "week").map((c) => `<th>${c.label}</th>`).join("") +
      `<th>28 day total</th><th>Target quantile</th><th>Order up to</th></tr></thead><tbody>${body}</tbody></table>`;
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

  function select(i, scroll = true) {
    if (!rows.length) return;
    sel = U.clamp(i, 0, rows.length - 1);
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
    const cur = rows[sel] && rows[sel].id;
    rows.sort((a, b) => {
      const av = a[sortKey], bv = b[sortKey];
      if (typeof av === "string") return sortDir * av.localeCompare(bv);
      return sortDir * ((av ?? -Infinity) - (bv ?? -Infinity));
    });
    const back = rows.findIndex((r) => r.id === cur);
    sel = back >= 0 ? back : 0;
    paint();
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
      paint();
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
      if (i >= 0 && i < rows.length) select(i, false);
    });
    canvas.addEventListener("dblclick", (e) => {
      const c = colAt(e.clientX);
      if (c && c.kind === "week") opts.onEditWeek && opts.onEditWeek(rows[sel], c.widx);
    });

    new ResizeObserver(U.debounce(resize, 40)).observe(scroller);
    window.addEventListener("dq:theme", () => paint());
    window.addEventListener("dq:density", () => resize());
    return api;
  }

  function setRows(next) {
    rows = next;
    sel = 0;
    scroller.scrollTop = 0;
    resize();
    if (rows.length) onSelect(rows[0], 0);
  }

  const api = {
    init, setRows, select, sortBy, paint, resize,
    get rows() { return rows; },
    get selected() { return rows[sel]; },
    get selectedIndex() { return sel; },
    move(d) { select(sel + d); },
    setOverride(id, wIdx, val) {
      const k = id + ":" + wIdx;
      if (val === null) overrides.delete(k); else overrides.set(k, val);
      paint();
    },
    getOverride(id, wIdx) { return overrides.get(id + ":" + wIdx); },
    get overrideCount() { return overrides.size; },
    clearOverrides() { overrides.clear(); paint(); },
    HEADER_H,
  };
  return api;
})();
