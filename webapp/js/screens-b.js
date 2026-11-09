/* Screens, group b: Today, Stock, Product, Orders, Purchase order, Suppliers, Reports,
   Model performance. See js/core.js for the helpers and js/start.js for the frame.

   Every screen names the store it shows, says how fresh its numbers are, shows a
   skeleton while loading, an error that says what to do, and an empty state that
   teaches. Quantities arrive in thousandths and money in paise; both are only ever
   shown through DQ.fmt. Charts are inline SVG with a text summary. */
(function () {
  "use strict";
  var DQ = window.DQ, h = DQ.h, fmt = DQ.fmt, ui = DQ.ui;

  /* ================================================================ shared bits */
  var SVGNS = "http://www.w3.org/2000/svg";
  function sv(tag, attrs) {
    var el = document.createElementNS(SVGNS, tag);
    Object.keys(attrs || {}).forEach(function (k) {
      var v = attrs[k];
      if (v === null || v === undefined || v === false) return;
      if (k === "text") el.textContent = v;
      else el.setAttribute(k, String(v));
    });
    for (var i = 2; i < arguments.length; i++) {
      var c = arguments[i];
      if (c === null || c === undefined || c === false) continue;
      if (Array.isArray(c)) c.forEach(function (x) { if (x) el.appendChild(x); });
      else el.appendChild(c);
    }
    return el;
  }
  function r1(n) { return Math.round(n * 10) / 10; }
  function pts(list) { return list.map(function (p) { return r1(p[0]) + "," + r1(p[1]); }).join(" "); }

  function addDays(iso, n) {
    var d = new Date(iso.slice(0, 10) + "T00:00:00Z");
    d.setUTCDate(d.getUTCDate() + n);
    return d.toISOString().slice(0, 10);
  }
  function dayDiff(a, b) { return Math.round((new Date(b + "T00:00:00Z") - new Date(a + "T00:00:00Z")) / 86400000); }
  function nowIso() { return new Date().toISOString(); }
  /* The server sends some timestamps without a zone; they are UTC. Show them in IST,
     and take the IST calendar date from the UTC fields of the shifted time, which is
     right whatever zone the browser is in. */
  function utc(iso) { return typeof iso === "string" && iso.length > 10 && !/(Z|[+-]\d\d:?\d\d)$/.test(iso) ? iso + "Z" : iso; }
  function istDate(iso) { return new Date(new Date(utc(iso)).getTime() + 330 * 60000).toISOString().slice(0, 10); }
  function todayIst() { return istDate(nowIso()); }
  function tTime(iso) { return iso ? fmt.time(utc(iso)) : "—"; }
  function tDate(iso) { return !iso ? "—" : iso.length <= 10 ? fmt.date(iso) : fmt.date(istDate(iso)); }
  function tDateTime(iso) { return iso ? tDate(iso) + ", " + tTime(iso) + " IST" : "—"; }
  function fresh(label, iso) { return h("span", { class: "fresh", title: iso ? tDateTime(iso) : "" }, label + " " + tTime(iso)); }
  function storeName() { var s = DQ.storeObj(); return s ? s.name : "No store"; }
  function storeById(id) { return DQ.stores().filter(function (s) { return s.id === id; })[0] || null; }
  function cap(s) { s = String(s || ""); return s.charAt(0).toUpperCase() + s.slice(1); }
  function plural(n, one, many) { return fmt.num(n) + " " + (n === 1 ? one : (many || one + "s")); }
  function units(milli, unit) { return fmt.qty(milli, unit); }
  function rupeesFromRupees(r, opts) { return r === null || r === undefined ? "—" : fmt.rupees(Math.round(r * 100), opts); }

  function loading(main, title, sub) {
    DQ.clear(main);
    if (title) main.appendChild(ui.page(title, { sub: sub }));
    main.appendChild(ui.skeleton(6));
  }
  function failed(main, alive, title) {
    return function (err) {
      if (!alive()) return;
      DQ.clear(main);
      if (title) main.appendChild(ui.page(title, { sub: storeName() }));
      main.appendChild(ui.error(err, DQ.render));
    };
  }
  function noStore(main, title) {
    DQ.clear(main);
    main.appendChild(ui.page(title));
    main.appendChild(ui.empty({ title: "There is no store yet", text: "Add your first store, then this screen fills in.", action: { label: "Open settings", href: "#/settings" } }));
  }
  function section(title, opts) {
    opts = opts || {};
    return h("section", { class: "card b-section", "aria-label": title },
      h("div", { class: "b-section-head" }, h("h2", { class: "title-3" }, title),
        opts.aside ? h("div", { class: "b-section-aside" }, opts.aside) : null),
      opts.sub ? h("p", { class: "text-2 b-section-sub" }, opts.sub) : null);
  }
  function fact(label, value, opts) {
    opts = opts || {};
    return h("div", { class: "b-fact" + (opts.cls ? " " + opts.cls : "") },
      h("dt", { title: opts.title || null }, label),
      h("dd", { class: "mono" }, value),
      opts.note ? h("dd", { class: "b-fact-note text-3" }, opts.note) : null);
  }
  function chip(text, title) { return h("span", { class: "b-chip-text", title: title || null }, text); }
  function setBusy(btn, busy, label) {
    if (busy) { btn.dataset.label = btn.textContent; btn.disabled = true; btn.textContent = label || "Please wait…"; }
    else { btn.disabled = false; if (btn.dataset.label) btn.textContent = btn.dataset.label; }
  }
  function apiErr(err) { return err.message + (err.problems && err.problems.length ? " " + err.problems.join("; ") : ""); }
  function legend(items) {
    return h("ul", { class: "b-legend", "aria-hidden": "true" }, items.map(function (it) {
      return h("li", null, h("span", { class: "b-swatch " + it[0] }), it[1]);
    }));
  }
  function niceMax(v) {
    if (!(v > 0)) return 1;
    var p = Math.pow(10, Math.floor(Math.log10(v)));
    var m = v / p;
    var n = m <= 1 ? 1 : m <= 2 ? 2 : m <= 2.5 ? 2.5 : m <= 5 ? 5 : 10;
    return n * p;
  }

  function niceStep(v) {
    if (!(v > 0)) return 1;
    var p = Math.pow(10, Math.floor(Math.log10(v))), m = v / p;
    return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10) * p;
  }

  /* A product search that lists matches as buttons; used for order lines and terms. */
  function productSearch(onPick, opts) {
    opts = opts || {};
    var input = ui.input({ type: "search", placeholder: "Type a name, SKU or barcode", autocomplete: "off" });
    var list = h("ul", { class: "b-results", "aria-live": "polite" });
    var timer = null, seq = 0;
    function run() {
      var q = input.value.trim();
      if (q.length < 2) { DQ.clear(list); return; }
      var my = ++seq;
      DQ.get("/api/products" + DQ.qs({ q: q, limit: 8 })).then(function (r) {
        if (my !== seq) return;
        DQ.clear(list);
        if (!r.rows.length) { list.appendChild(h("li", { class: "text-3 b-result-none" }, "No product matches " + q + ".")); return; }
        r.rows.forEach(function (p) {
          list.appendChild(h("li", null, h("button", { type: "button", class: "b-result", onclick: function () {
            onPick(p); input.value = ""; DQ.clear(list); input.focus();
          } }, h("span", null, p.name), h("span", { class: "text-3 mono" }, p.sku), p.status === "discontinued" ? ui.badge("Discontinued") : null)));
        });
      }, function (err) { if (my === seq) { DQ.clear(list); list.appendChild(h("li", { class: "field-error" }, err.message)); } });
    }
    input.addEventListener("input", function () { clearTimeout(timer); timer = setTimeout(run, 250); });
    input.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); clearTimeout(timer); run(); } });
    return h("div", { class: "b-search" }, ui.field(opts.label || "Add a product", input, opts.help), list);
  }

  /* ================================================================ Today */
  var LEVELS = [0.05, 0.10, 0.25, 0.5, 0.75, 0.90, 0.95, 0.975, 0.99];
  var REASON_LABELS = { local_event: "A local event", promotion: "A promotion", supplier_issue: "A supplier problem",
    new_product: "A new product", known_order: "A known large order", weather: "The weather", other: "Something else" };

  /* Demand over the risk period as 400 equally likely values, read off the quantiles
     by straight lines between them, with the tails carried on at the end slopes. */
  function demandSamples(qMilli) {
    var q = [], hi = 0;
    qMilli.forEach(function (v) { hi = Math.max(hi, v / 1000); q.push(hi); });
    var lo0 = Math.max(0, q[0] - (q[1] - q[0]) / (LEVELS[1] - LEVELS[0]) * LEVELS[0]);
    var n = q.length - 1;
    var hi1 = q[n] + (q[n] - q[n - 1]) / (LEVELS[n] - LEVELS[n - 1]) * (1 - LEVELS[n]);
    var P = [0].concat(LEVELS, [1]), X = [lo0].concat(q, [hi1]);
    var out = [], N = 400;
    for (var i = 0; i < N; i++) {
      var p = (i + 0.5) / N, j = 1;
      while (j < P.length - 1 && P[j] < p) j++;
      var t = (p - P[j - 1]) / (P[j] - P[j - 1] || 1);
      out.push(X[j - 1] + t * (X[j] - X[j - 1]));
    }
    return out;
  }
  function expectedCost(samples, S, under, over) {
    var c = 0;
    for (var i = 0; i < samples.length; i++) { var d = samples[i]; c += d > S ? under * (d - S) : over * (S - d); }
    return c / samples.length;          // paise
  }

  function costCurve(row) {
    var q = row.risk_q;
    if (!q || q.length !== LEVELS.length || !(row.underage_paise > 0 || row.overage_paise > 0)) return null;
    var samples = demandSamples(q);
    var under = row.underage_paise || 0, over = row.overage_paise || 0;
    var target = (row.target_milli || 0) / 1000;
    var chosen = row.decision && row.decision.action === "change" && row.decision.value_milli !== null ? row.decision.value_milli / 1000 : null;
    var xmax = niceMax(Math.max(samples[samples.length - 1] * 1.1, target * 1.2, chosen ? chosen * 1.1 : 0, 1));
    var N = 60, curve = [], best = { s: 0, c: Infinity };
    for (var i = 0; i <= N; i++) {
      var s = xmax * i / N, c = expectedCost(samples, s, under, over);
      curve.push([s, c]);
      if (c < best.c) best = { s: s, c: c };
    }
    var ymax = niceMax(Math.max.apply(null, curve.map(function (p) { return p[1]; })));
    var W = 340, H = 168, L = 54, R = 10, T = 14, B = 40;
    function X(s) { return L + (W - L - R) * s / xmax; }
    function Y(c) { return T + (H - T - B) * (1 - c / ymax); }
    var tc = expectedCost(samples, target, under, over);
    var u = row.unit, one = u === "each" ? "a unit" : "a " + u;
    var label = "Expected cost of being wrong for " + row.name + " at each stock level over the next " + row.risk_days +
      " days. It is lowest near " + units(Math.round(best.s * 1000), u) + ". At the stock target of " + units(row.target_milli, u) +
      " the expected cost is " + fmt.rupees(Math.round(tc), { whole: true }) + ". " + cap(one) + " short loses " + fmt.rupees(under) +
      "; " + one + " left over costs " + fmt.rupees(over) + ".";
    var g = sv("svg", { viewBox: "0 0 " + W + " " + H, class: "b-chart b-cost", role: "img", "aria-label": label, preserveAspectRatio: "xMidYMid meet" },
      sv("title", { text: label }),
      sv("line", { x1: L, y1: Y(0), x2: W - R, y2: Y(0), class: "b-axis" }),
      sv("line", { x1: L, y1: T, x2: L, y2: Y(0), class: "b-axis" }),
      sv("line", { x1: L, y1: Y(ymax / 2), x2: W - R, y2: Y(ymax / 2), class: "b-grid" }),
      sv("text", { x: L - 6, y: Y(0) + 4, class: "b-tick", "text-anchor": "end", text: "₹0" }),
      sv("text", { x: L - 6, y: Y(ymax) + 4, class: "b-tick", "text-anchor": "end", text: fmt.rupees(Math.round(ymax), { whole: true, short: true }) }),
      sv("text", { x: L, y: Y(0) + 15, class: "b-tick", "text-anchor": "start", text: "0" }),
      sv("text", { x: W - R, y: Y(0) + 15, class: "b-tick", "text-anchor": "end", text: units(Math.round(xmax * 1000), u) }),
      sv("text", { x: L + (W - L - R) / 2, y: H - 4, class: "b-axis-label", "text-anchor": "middle", text: "Stock level for the next " + row.risk_days + " days" }),
      sv("text", { x: 12, y: T + (H - T - B) / 2, class: "b-axis-label", "text-anchor": "middle", transform: "rotate(-90 12 " + (T + (H - T - B) / 2) + ")", text: "Expected cost" }),
      sv("polyline", { points: pts(curve.map(function (p) { return [X(p[0]), Y(p[1])]; })), class: "b-line-forecast" }),
      sv("line", { x1: X(target), y1: T, x2: X(target), y2: Y(0), class: "b-mark-target" }),
      sv("circle", { cx: X(target), cy: Y(tc), r: 4, class: "b-dot-target" }),
      sv("text", { x: Math.min(X(target) + 5, W - R - 2), y: T + 10, class: "b-tick b-tick-strong", "text-anchor": X(target) > W - 90 ? "end" : "start", text: "Target " + units(row.target_milli, u) }),
      chosen !== null ? [
        sv("line", { x1: X(chosen), y1: T + 14, x2: X(chosen), y2: Y(0), class: "b-mark-override" }),
        sv("rect", { x: X(chosen) - 4, y: Y(expectedCost(samples, chosen, under, over)) - 4, width: 8, height: 8, class: "b-dot-override" }),
        sv("text", { x: Math.min(X(chosen) + 5, W - R - 2), y: T + 24, class: "b-tick", "text-anchor": X(chosen) > W - 90 ? "end" : "start", text: "Yours " + units(row.decision.value_milli, u) })
      ] : null);
    return h("figure", { class: "b-cost-fig" }, g,
      h("figcaption", { class: "text-3 b-caption" }, "Cost of being wrong: lowest near " + units(Math.round(best.s * 1000), u) +
        ". " + cap(one) + " short loses " + fmt.rupees(under) + ", " + one + " left over costs " + fmt.rupees(over) + "."));
  }

  function decisionText(d, unit) {
    var when = d.at ? " at " + tTime(d.at) + (istDate(d.at) !== todayIst() ? " on " + tDate(d.at) : "") : "";
    if (d.action === "approve") return "Approved" + when + ". The order uses the stock target shown.";
    if (d.action === "skip") return "Skipped" + when + ". This product is left off this order.";
    return "Changed to a stock target of " + units(d.value_milli, unit) + when + ". Reason: " + (REASON_LABELS[d.reason] || d.reason || "not given").toLowerCase() + (d.note ? ". Note: " + d.note : "") + ".";
  }

  function todayCard(row, ctx) {
    var card = h("article", { class: "card b-today-card", "aria-label": row.name });
    function draw() {
      DQ.clear(card);
      var u = row.unit, d = row.decision;
      card.setAttribute("data-decided", d ? d.action : "none");
      var cases = row.case_pack > 1 && row.suggested_milli ? plural(Math.round(row.suggested_milli / 1000 / row.case_pack), "case") + " of " + row.case_pack : null;
      card.appendChild(h("header", { class: "b-card-head" },
        h("div", null,
          h("h2", { class: "title-3" }, h("a", { href: "#/products/" + row.product_id, class: "b-plain-link" }, row.name)),
          h("p", { class: "text-3 b-card-sub" }, h("span", { class: "mono" }, row.sku), row.category ? " · " + row.category : "", row.supplier ? " · " + row.supplier : "")),
        h("div", { class: "b-card-badges" },
          ui.badge(fmt.rupees(row.money_at_risk_paise, { whole: true }) + " at risk", "risk"),
          row.estimate ? h("span", { class: "badge", "data-kind": "attention", title: "Cold start: no sales history, so this is an estimate" }, "Estimate, no sales history") : null,
          d ? ui.badge(d.action === "approve" ? "Approved" : d.action === "skip" ? "Skipped" : "Changed", d.action === "change" ? "attention" : "ok") : null)));
      card.appendChild(h("dl", { class: "b-facts" },
        fact("Forecast, next 7 days", units(row.week.mid_milli, u), { note: "likely " + units(row.week.lo_milli, u) + " to " + units(row.week.hi_milli, u), cls: "b-fact-forecast" }),
        fact("Stock target", units(row.target_milli, u), { title: "Enough for " + row.risk_days + " days until the next delivery can arrive; covers demand " + fmt.pct(row.service_level) + " of the time (service level)", note: "for " + row.risk_days + " days", cls: "b-fact-target" }),
        fact("On hand", row.stock_known ? units(row.on_hand_milli, u) : "Not known", { note: row.stock_known ? null : "count it first" }),
        fact("On order", units(row.on_order_milli, u)),
        fact("Suggested order", units(row.suggested_milli, u), { note: cases })));
      var reasons = (row.reasons || []).map(function (r) { return h("li", { class: "b-chip" }, typeof r === "string" ? r : r.text); });
      if (reasons.length) card.appendChild(h("ul", { class: "b-chips", "aria-label": "Why this needs a look" }, reasons));
      var curve = costCurve(row);
      var meta = h("p", { class: "fresh b-card-fresh" }, "Forecast from data to " + fmt.dayShort(ctx.cutoff) + (row.history_days ? " · " + plural(row.history_days, "day") + " of sales history" : ""));
      card.appendChild(h("div", { class: "b-card-body" }, curve, meta));
      if (d) {
        card.appendChild(h("div", { class: "b-decided", role: "status" }, h("p", null, decisionText(d, u)),
          ctx.canDecide ? ui.button("Decide again", function () { row.decision = null; row._again = d; draw(); }, "ghost", { class: "btn btn-ghost btn-sm" }) : null));
        return;
      }
      if (!ctx.canDecide) return;
      var changeBox = h("div", { class: "b-change", hidden: true });
      var actions = h("div", { class: "row b-actions" },
        ui.button("Approve", function (e) { decide(e.target, { action: "approve" }); }, "primary"),
        ui.button("Change", function () { changeBox.hidden = !changeBox.hidden; if (!changeBox.hidden) val.focus(); }, "ghost", { "aria-expanded": "false" }),
        ui.button("Skip", function (e) { decide(e.target, { action: "skip" }); }, "ghost"),
        row._again ? ui.button("Keep my earlier decision", function () { row.decision = row._again; row._again = null; draw(); }, "ghost", { class: "btn btn-ghost btn-sm" }) : null);
      var val = ui.input({ type: "number", min: 0, step: u === "each" ? 1 : 0.001, inputmode: "decimal", value: String((row.target_milli || 0) / 1000) });
      var reason = ui.select([{ value: "", label: "Choose a reason" }].concat((ctx.reasons || []).map(function (k) { return { value: k, label: REASON_LABELS[k] || cap(k.replace(/_/g, " ")) }; })), "");
      var note = h("textarea", { class: "input", rows: 2, maxlength: 500 });
      var err = h("p", { class: "field-error", role: "alert", hidden: true });
      changeBox.appendChild(h("div", { class: "grid-3 b-change-grid" },
        ui.field("New stock target" + (u !== "each" ? " (" + u + ")" : ""), val, "The system's target is " + units(row.target_milli, u) + "."),
        ui.field("Reason", reason, "The forecast learns from it."),
        ui.field("Note (optional)", note)));
      changeBox.appendChild(err);
      changeBox.appendChild(h("div", { class: "row" },
        ui.button("Save the change", function (e) {
          var v = fmt.toMilli(val.value);
          err.hidden = true;
          if (v === null || v < 0) { err.textContent = "Type the stock target you want, as a number."; err.hidden = false; val.focus(); return; }
          if (u === "each" && v % 1000) { err.textContent = row.name + " is counted in whole units."; err.hidden = false; val.focus(); return; }
          if (!reason.value) { err.textContent = "Choose a reason, so the forecast can learn from it."; err.hidden = false; reason.focus(); return; }
          decide(e.target, { action: "change", value_milli: v, reason: reason.value, note: note.value.trim() || null });
        }, "primary"),
        ui.button("Cancel", function () { changeBox.hidden = true; }, "ghost")));
      actions.children[1].addEventListener("click", function () { actions.children[1].setAttribute("aria-expanded", String(!changeBox.hidden)); });
      card.appendChild(actions);
      card.appendChild(changeBox);
    }
    function decide(btn, body) {
      body.recommendation_id = row.id;
      setBusy(btn, true, "Saving…");
      DQ.post("/api/decisions", body).then(function (res) {
        row._again = null;
        row.decision = { id: res.id, action: res.action, value_milli: res.value_milli, reason: body.reason || null, note: body.note || null, at: nowIso() };
        draw();
        ctx.changed();
        var word = res.action === "approve" ? "Approved" : res.action === "skip" ? "Skipped" : "Changed the stock target for";
        DQ.toast(word + " " + row.name + ".", { undo: function () {
          DQ.post("/api/decisions/" + res.id + "/undo").then(function () {
            row.decision = null; draw(); ctx.changed();
            DQ.toast("Undone. " + row.name + " is waiting for a decision again.");
          }, function (err2) { DQ.toast("Could not undo: " + err2.message, { kind: "risk" }); });
        } });
      }, function (err) {
        setBusy(btn, false);
        DQ.toast("Not saved. " + apiErr(err), { kind: "risk" });
      });
    }
    draw();
    return card;
  }

  DQ.route("#/today", { title: "Today", nav: "Today", render: function (main, params, query, alive) {
    var sid = DQ.store();
    if (!sid) return noStore(main, "Today");
    loading(main, "Today", storeName());
    return DQ.get("/api/today" + DQ.qs({ store_id: sid })).then(function (t) {
      if (!alive()) return;
      DQ.clear(main);
      var canDecide = !!t.can_decide && DQ.can("forecast.review", sid);
      var cutoff = t.run && t.run.data_cutoff;
      var sub = h("span", null, storeName(), cutoff ? " · forecast from data to " + fmt.dayShort(cutoff) : "", " · ",
        t.run ? fresh("made at", t.run.finished_at || t.run.started_at) : null);
      var summary = h("p", { class: "b-summary", role: "status" });
      var bulkBtn = canDecide ? ui.button("Approve the small ones", bulk, "ghost", { title: "Approves every product still waiting whose money at risk is " + fmt.rupees(50000, { whole: true }) + " or less" }) : null;
      main.appendChild(ui.page("Today", { sub: sub, actions: bulkBtn ? [bulkBtn] : null }));
      if (!t.run) {
        main.appendChild(ui.empty({ title: "No forecast yet", text: "Forecasts start once there are enough days of sales to learn from. The setup checklist shows what is still missing and how long it takes.", action: { label: "See what the forecast needs", href: "#/setup/forecast" } }));
        return;
      }
      if (!t.rows.length) {
        main.appendChild(ui.empty({ title: "Nothing needs a look in " + storeName() + " today", text: "Every forecast is within its usual range and stock covers its target. Suggested orders are ready on the Orders screen.", action: { label: "Open suggested orders", href: "#/orders" } }));
        return;
      }
      main.appendChild(summary);
      if (!canDecide) main.appendChild(h("p", { class: "notice" }, "You can read these forecasts. A store manager, Owner or Admin decides on them."));
      var list = h("div", { class: "b-today-list" });
      var ctx = { cutoff: cutoff, reasons: t.change_reasons, canDecide: canDecide, changed: paint };
      t.rows.forEach(function (row) { list.appendChild(todayCard(row, ctx)); });
      main.appendChild(list);
      function paint() {
        var left = t.rows.filter(function (r) { return !r.decision; });
        var small = left.filter(function (r) { return r.money_at_risk_paise <= 50000; }).length;
        summary.textContent = left.length ? plural(left.length, "product") + " waiting for a decision, " + fmt.num(t.rows.length - left.length) + " decided. Most money at risk first." :
          "All " + plural(t.rows.length, "product") + " decided. The suggested orders now use these decisions.";
        if (bulkBtn) { bulkBtn.disabled = !small; bulkBtn.textContent = small ? "Approve the small ones (" + small + ")" : "No small ones left"; }
      }
      paint();
      function bulk() {
        setBusy(bulkBtn, true, "Approving…");
        DQ.post("/api/decisions/bulk-approve", { store_id: sid, max_value_paise: 50000 }).then(function (r) {
          DQ.toast("Approved " + plural(r.approved, "product") + " with " + fmt.rupees(50000, { whole: true }) + " or less at risk.");
          if (alive()) DQ.render();
        }, function (err) { setBusy(bulkBtn, false); DQ.toast("Not approved. " + err.message, { kind: "risk" }); });
      }
    }, failed(main, alive, "Today"));
  } });

  /* ================================================================ Stock */
  var STATUSES = ["Count needed", "Out", "Expiring", "Low", "Overstocked", "OK"];

  DQ.route("#/stock", { title: "Stock", nav: "Stock", render: function (main, params, query, alive) {
    var sid = DQ.store();
    if (!sid) return noStore(main, "Stock");
    var status = STATUSES.indexOf(query.status) >= 0 ? query.status : "";
    loading(main, "Stock", storeName());
    return Promise.all([
      DQ.get("/api/stock" + DQ.qs({ store_id: sid, q: query.q, category: query.category })),
      DQ.get("/api/products/categories").catch(function () { return []; })
    ]).then(function (res) {
      if (!alive()) return;
      var st = res[0], cats = res[1];
      DQ.clear(main);
      function go(o) { DQ.go("#/stock" + DQ.qs(Object.assign({ q: query.q, status: status, category: query.category }, o))); }
      main.appendChild(ui.page("Stock", {
        sub: h("span", null, st.store.name, " · ", fresh("Stock as of", st.as_of)),
        actions: [ui.button("Export to a spreadsheet", function (e) { DQ.download("/api/stock/export.csv" + DQ.qs({ store_id: sid }), e.currentTarget); }, "ghost", { title: "Downloads every product in this store as a CSV file" })]
      }));
      var q = ui.input({ type: "search", value: query.q || "", placeholder: "Name, SKU or barcode" });
      var cat = ui.select([{ value: "", label: "All categories" }].concat(cats.map(function (c) { return { value: c.category, label: c.category + " (" + c.products + ")" }; })), query.category || "", { onchange: function () { go({ category: cat.value }); } });
      var form = h("form", { class: "b-filters", role: "search", onsubmit: function (e) { e.preventDefault(); go({ q: q.value.trim() }); } },
        ui.field("Search this store's stock", q), ui.field("Category", cat),
        h("div", { class: "b-filter-btns" }, h("button", { class: "btn btn-primary", type: "submit" }, "Search"),
          query.q || query.category || status ? h("a", { class: "btn btn-ghost", href: "#/stock" }, "Clear") : null));
      main.appendChild(form);
      var counts = st.status_counts || {};
      main.appendChild(h("div", { class: "b-chipbar", role: "group", "aria-label": "Show by status" },
        h("button", { type: "button", class: "b-filter-chip", "aria-pressed": status ? "false" : "true", onclick: function () { go({ status: "" }); } }, "All ", h("span", { class: "mono" }, fmt.num(st.total))),
        STATUSES.map(function (s) {
          return h("button", { type: "button", class: "b-filter-chip", "data-kind": ui.statusKind(s), "aria-pressed": status === s ? "true" : "false", onclick: function () { go({ status: status === s ? "" : s }); } },
            h("span", { class: "b-chip-dot", "aria-hidden": "true" }), s + " ", h("span", { class: "mono" }, fmt.num(counts[s] || 0)));
        })));
      var rows = status ? st.rows.filter(function (r) { return r.status === status; }) : st.rows;
      if (!st.total && !query.q && !query.category) {
        main.appendChild(ui.empty({ title: "No products yet", text: "Upload your product list and opening stock, and every product in " + st.store.name + " shows here with its status.", action: { label: "Upload products", href: "#/imports" } }));
        return;
      }
      if (status === "Count needed" && rows.length) main.appendChild(h("p", { class: "notice" }, "These products have no trusted stock figure, so no order is suggested for them. ", h("a", { href: "#/count" }, "Count them now"), "."));
      main.appendChild(ui.table([
        { key: "name", label: "Product", render: function (r) { return h("div", { class: "b-prodcell" }, h("a", { href: "#/products/" + r.product_id, class: "b-plain-link", onclick: function (e) { e.stopPropagation(); } }, r.name), h("div", { class: "text-3 mono b-small" }, r.sku, r.category ? " · " + r.category : "")); } },
        { key: "on_hand_milli", label: "On hand", kind: "qty", unit: "unit", render: function (r) { return r.known ? units(r.on_hand_milli, r.unit) : h("span", { title: "Not counted yet" }, units(r.on_hand_milli, r.unit) + "?"); } },
        { key: "in_open_bills_milli", label: "In open bills", kind: "qty", unit: "unit" },
        { key: "on_order_milli", label: "On order", kind: "qty", unit: "unit" },
        { key: "in_transit_milli", label: "In transit", kind: "qty", unit: "unit" },
        { key: "days_of_cover", label: "Days of cover", kind: "num", render: function (r) { return r.days_of_cover === null ? "—" : r.days_of_cover.toFixed(1); } },
        { key: "last_counted_at", label: "Last counted", kind: "date", render: function (r) { return r.last_counted_at ? tDate(r.last_counted_at) : "Never"; } },
        { key: "value_paise", label: "Value at cost", kind: "money" },
        { key: "status", label: "Status", render: function (r) { return h("span", null, ui.badge(r.status, ui.statusKind(r.status)), r.status === "Expiring" && r.expiring_on ? h("div", { class: "text-3 b-small" }, units(r.expiring_milli, r.unit) + " by " + fmt.dayShort(r.expiring_on)) : null); } }
      ], rows, {
        caption: "Stock in " + st.store.name,
        onRow: function (r) { DQ.go("#/products/" + r.product_id); },
        empty: { title: "Nothing matches", text: "No product in " + st.store.name + " matches these filters.", action: { label: "Show all stock", href: "#/stock" } }
      }));
      var value = rows.reduce(function (a, r) { return a + (r.value_paise || 0); }, 0);
      if (rows.length) main.appendChild(h("p", { class: "text-3 b-foot" }, "Showing " + plural(rows.length, "product") + " worth " + fmt.rupees(value, { whole: true }) + " at cost." + (st.total > st.rows.length ? " The first " + fmt.num(st.rows.length) + " of " + fmt.num(st.total) + " are listed; search to narrow." : "")));
    }, failed(main, alive, "Stock"));
  } });

  /* ================================================================ Product */
  function salesChart(o) {
    var sales = o.sales || [], fc = o.forecast, u = o.product.unit;
    var cutoff = fc && fc.data_cutoff ? fc.data_cutoff : (sales.length ? sales[sales.length - 1].day : null);
    if (!sales.length && !(fc && fc.daily && fc.daily.length)) return null;
    var start = sales.length ? sales[0].day : addDays(cutoff, -27);
    var daily = fc && fc.daily ? fc.daily : [];
    var end = daily.length ? addDays(cutoff, daily.length) : (sales.length ? sales[sales.length - 1].day : cutoff);
    var span = Math.max(dayDiff(start, end), 1);
    var ymaxRaw = 0;
    sales.forEach(function (s) { ymaxRaw = Math.max(ymaxRaw, s.units_milli); });
    daily.forEach(function (d) { ymaxRaw = Math.max(ymaxRaw, d[2]); });
    var raw = Math.max(ymaxRaw / 1000, 1);
    var ystep = niceStep(raw / 5);
    var ymax = Math.ceil(raw / ystep) * ystep;
    var W = 820, H = 280, L = 52, R = 14, T = 16, B = 40;
    function X(day) { return L + (W - L - R) * dayDiff(start, day) / span; }
    function Y(m) { return T + (H - T - B) * (1 - m / 1000 / ymax); }
    var g = [];
    for (var k = 0; k * ystep <= ymax + 1e-9; k++) {
      var yv = k * ystep;
      g.push(sv("line", { x1: L, y1: Y(yv * 1000), x2: W - R, y2: Y(yv * 1000), class: k ? "b-grid" : "b-axis" }));
      g.push(sv("text", { x: L - 6, y: Y(yv * 1000) + 4, class: "b-tick", "text-anchor": "end", text: Number.isInteger(yv) ? fmt.num(yv) : String(r1(yv)) }));
    }
    var step = span > 100 ? 21 : 14;
    for (var dd = 0; dd <= span; dd += step) {
      var day = addDays(start, dd);
      g.push(sv("text", { x: X(day), y: H - B + 16, class: "b-tick", "text-anchor": "middle", text: fmt.dayShort(day) }));
    }
    if (daily.length) {
      var first = addDays(cutoff, 1);
      var upper = daily.map(function (d, i) { return [X(addDays(first, i)), Y(d[2])]; });
      var lower = daily.map(function (d, i) { return [X(addDays(first, i)), Y(d[0])]; }).reverse();
      g.push(sv("polygon", { points: pts(upper.concat(lower)), class: "b-band" }));
      g.push(sv("polyline", { points: pts(daily.map(function (d, i) { return [X(addDays(first, i)), Y(d[1])]; })), class: "b-line-forecast b-dashed" }));
    }
    if (cutoff) {
      g.push(sv("line", { x1: X(cutoff), y1: T, x2: X(cutoff), y2: H - B, class: "b-mark-now" }));
      g.push(sv("text", { x: X(cutoff) - 4, y: T + 10, class: "b-tick", "text-anchor": "end", text: "Data to " + fmt.dayShort(cutoff) }));
    }
    if (sales.length) {
      g.push(sv("polyline", { points: pts(sales.map(function (s) { return [X(s.day), Y(s.units_milli)]; })), class: "b-line-actual" }));
      sales.forEach(function (s) {
        if (s.out_of_stock) g.push(sv("path", { d: "M" + r1(X(s.day)) + " " + (H - B - 2) + " l-4 -8 h8 z", class: "b-oos" }, sv("title", { text: "Out of stock on " + fmt.date(s.day) })));
      });
    }
    g.push(sv("text", { x: 12, y: T + (H - T - B) / 2, class: "b-axis-label", "text-anchor": "middle", transform: "rotate(-90 12 " + (T + (H - T - B) / 2) + ")", text: "Units a day" + (u !== "each" ? " (" + u + ")" : "") }));
    var last28 = sales.slice(-28), sold = last28.reduce(function (a, s) { return a + s.units_milli; }, 0);
    var oos = sales.filter(function (s) { return s.out_of_stock; }).length;
    var summary = (sales.length ? "Over the last " + plural(last28.length, "day") + " it sold " + units(Math.round(sold / Math.max(last28.length, 1)), u) + " a day on average" + (oos ? ", and was out of stock on " + plural(oos, "day") + " in this chart" : "") + ". " : "No sales recorded in this period. ") +
      (fc ? "Forecast for the next 7 days: " + units(fc.week1_mid_milli, u) + ", likely " + units(fc.week1_lo_milli, u) + " to " + units(fc.week1_hi_milli, u) + "; stock target " + units(fc.target_milli, u) + "." : "No forecast for this product in this store yet.");
    return { svg: sv("svg", { viewBox: "0 0 " + W + " " + H, class: "b-chart b-sales", role: "img", "aria-label": "Daily sales and forecast. " + summary }, sv("title", { text: "Daily sales and forecast" }), g), summary: summary };
  }

  function productForm(p, onSaved) {
    var f = {
      name: ui.input({ value: p.name, required: true, maxlength: 300 }),
      brand: ui.input({ value: p.brand || "" }),
      category: ui.input({ value: p.category || "" }),
      pack_size: ui.input({ value: p.pack_size || "" }),
      unit: ui.select([{ value: "each", label: "Each" }, { value: "kg", label: "Kilogram" }, { value: "litre", label: "Litre" }], p.unit),
      mrp: ui.input({ type: "number", min: 0, step: 0.01, inputmode: "decimal", value: p.mrp_paise === null ? "" : String(p.mrp_paise / 100) }),
      price: ui.input({ type: "number", min: 0, step: 0.01, inputmode: "decimal", value: p.price_paise === null ? "" : String(p.price_paise / 100) }),
      cost: ui.input({ type: "number", min: 0, step: 0.01, inputmode: "decimal", value: p.cost_paise === null ? "" : String(p.cost_paise / 100) }),
      gst: ui.input({ type: "number", min: 0, max: 40, step: 0.01, inputmode: "decimal", value: p.gst_rate_bp === null ? "" : String(p.gst_rate_bp / 100) }),
      hsn: ui.input({ value: p.hsn || "", inputmode: "numeric", maxlength: 8 }),
      shelf: ui.input({ type: "number", min: 0, step: 1, value: p.shelf_life_days === null ? "" : String(p.shelf_life_days) }),
      case_pack: ui.input({ type: "number", min: 1, step: 1, value: String(p.case_pack || 1) }),
      status: ui.select([{ value: "new", label: "New" }, { value: "active", label: "Active" }, { value: "discontinued", label: "Discontinued" }], p.status),
      perishable: h("input", { type: "checkbox", class: "b-check", checked: !!p.perishable })
    };
    var err = h("div", { class: "form-error", role: "alert", hidden: true });
    var save = h("button", { class: "btn btn-primary", type: "submit" }, "Save the product");
    function opt(v) { v = v.trim(); return v === "" ? null : v; }
    function money(v) { return v.trim() === "" ? null : fmt.toPaise(v); }
    var form = h("form", { class: "stack", novalidate: true, onsubmit: function (e) {
      e.preventDefault();
      var body = {
        name: f.name.value.trim(), brand: opt(f.brand.value), category: opt(f.category.value), pack_size: opt(f.pack_size.value),
        unit: f.unit.value, mrp_paise: money(f.mrp.value), price_paise: money(f.price.value), cost_paise: money(f.cost.value),
        gst_rate_bp: f.gst.value.trim() === "" ? null : Math.round(parseFloat(f.gst.value) * 100), hsn: opt(f.hsn.value),
        shelf_life_days: f.shelf.value.trim() === "" ? null : parseInt(f.shelf.value, 10), case_pack: parseInt(f.case_pack.value, 10) || 1,
        status: f.status.value, perishable: f.perishable.checked
      };
      if (!body.name) { err.textContent = "Give the product a name."; err.hidden = false; f.name.focus(); return; }
      var changes = {};
      Object.keys(body).forEach(function (k) { var before = p[k] === undefined ? null : p[k]; if (body[k] !== before) changes[k] = body[k]; });
      if (!Object.keys(changes).length) { err.textContent = "Nothing has changed yet."; err.hidden = false; return; }
      err.hidden = true; setBusy(save, true, "Saving…");
      DQ.api("PATCH", "/api/products/" + p.id, changes).then(function () { DQ.toast("Saved " + body.name + "."); onSaved(); },
        function (e2) { setBusy(save, false); err.textContent = apiErr(e2); err.hidden = false; });
    } },
      h("div", { class: "grid-3" }, ui.field("Name", f.name), ui.field("Brand", f.brand), ui.field("Category", f.category),
        ui.field("Pack size", f.pack_size, "For example 500 g."), ui.field("Sold by", f.unit), ui.field("Status", f.status),
        ui.field("MRP (₹)", f.mrp), ui.field("Selling price (₹)", f.price), ui.field("Cost (₹)", f.cost, "Before GST."),
        ui.field("GST rate (%)", f.gst), ui.field("HSN code", f.hsn, "4 to 8 digits."), ui.field("Shelf life (days)", f.shelf),
        ui.field("Units in a case", f.case_pack)),
      h("label", { class: "b-checkline" }, f.perishable, " Perishable: it goes off, so it is stocked for fewer days"),
      err, h("div", { class: "row" }, save));
    return form;
  }

  DQ.route("#/products/:id", { title: "Product", render: function (main, params, query, alive) {
    var sid = DQ.store();
    loading(main, "Product", storeName());
    return DQ.get("/api/products/" + encodeURIComponent(params.id) + "/overview" + DQ.qs({ store_id: sid })).then(function (o) {
      if (!alive()) return;
      DQ.clear(main);
      var p = o.product, s = o.stock, u = p.unit, loadedAt = nowIso();
      var isAdmin = DQ.can("imports.upload");
      var editBox = h("div", { hidden: true });
      document.title = p.name + " · DQ-Stack";
      main.appendChild(ui.page(p.name, {
        sub: h("span", null, h("span", { class: "mono" }, p.sku), " · sold by " + (u === "each" ? "the unit" : "the " + u) + (p.pack_size ? " · " + p.pack_size : "") + (p.category ? " · " + p.category : "") + " · ", o.store ? o.store.name : "no store"),
        actions: [h("a", { class: "btn btn-ghost", href: "#/stock" }, "Back to stock"),
          isAdmin ? ui.button("Edit the product", function (e) { editBox.hidden = !editBox.hidden; e.target.setAttribute("aria-expanded", String(!editBox.hidden)); }, "ghost", { "aria-expanded": "false" }) : null]
      }));
      if (isAdmin) {
        editBox.appendChild(section("Edit the product", { sub: "Changes apply in every store." }));
        editBox.firstChild.appendChild(productForm(p, DQ.render));
        main.appendChild(editBox);
      }
      main.appendChild(h("div", { class: "tiles b-tiles" },
        h("div", { class: "tile" }, h("div", { class: "tile-label" }, "MRP"), h("div", { class: "tile-value" }, fmt.rupees(p.mrp_paise))),
        h("div", { class: "tile" }, h("div", { class: "tile-label" }, "Selling price"), h("div", { class: "tile-value" }, fmt.rupees(p.price_paise))),
        h("div", { class: "tile" }, h("div", { class: "tile-label" }, "Cost"), h("div", { class: "tile-value" }, fmt.rupees(p.cost_paise)), h("div", { class: "tile-note" }, p.gst_rate_bp !== null ? "GST " + (p.gst_rate_bp / 100) + "%" : "GST not set")),
        h("div", { class: "tile" }, h("div", { class: "tile-label" }, "Status"), h("div", { class: "tile-value b-tile-text" }, cap(p.status)), h("div", { class: "tile-note" }, (p.perishable ? "Perishable" + (p.shelf_life_days ? ", keeps " + plural(p.shelf_life_days, "day") : "") : "Not perishable") + " · case of " + p.case_pack))));

      var stockCard = section("Stock now in " + (o.store ? o.store.name : "this store"), { aside: fresh("Stock as of", loadedAt) });
      if (s) {
        stockCard.appendChild(h("dl", { class: "b-facts" },
          fact("On hand", (s.known ? "" : "about ") + units(s.on_hand_milli, u), { note: h("span", null, ui.badge(s.status, ui.statusKind(s.status))) }),
          fact("On order", units(s.on_order_milli, u)),
          fact("In transit", units(s.in_transit_milli, u)),
          fact("In open bills", units(s.in_open_bills_milli, u)),
          fact("Days of cover", s.days_of_cover === null ? "—" : s.days_of_cover.toFixed(1)),
          fact("Stock target", units(s.target_milli, u), { cls: "b-fact-target", note: o.forecast && o.forecast.data_cutoff ? "from data to " + fmt.dayShort(o.forecast.data_cutoff) : null }),
          fact("Last counted", s.last_counted_at ? tDate(s.last_counted_at) : "Never"),
          fact("Value at cost", fmt.rupees(s.value_paise))));
        if (!s.known) stockCard.appendChild(h("p", { class: "notice" }, "This stock figure is not trusted yet, so no order is suggested. ", h("a", { href: "#/count" }, "Count it"), "."));
      } else stockCard.appendChild(h("p", { class: "text-2" }, "No stock record in this store yet."));
      main.appendChild(stockCard);

      var chartCard = section("Sales and forecast", { sub: o.forecast ? "Daily sales, then the next " + o.forecast.daily.length + " days forecast from data to " + fmt.dayShort(o.forecast.data_cutoff) + "." : "Daily sales in " + (o.store ? o.store.name : "this store") + "." });
      var ch = salesChart(o);
      if (ch) {
        chartCard.appendChild(h("div", { class: "b-chart-wrap" }, ch.svg));
        chartCard.appendChild(legend([["b-sw-actual", "Sales a day"], ["b-sw-forecast", "Forecast, middle"], ["b-sw-band", "Likely range"], ["b-sw-oos", "Out of stock day"]]));
        chartCard.appendChild(h("p", { class: "text-2" }, ch.summary));
        if (o.forecast && o.forecast.reasons && o.forecast.reasons.length) chartCard.appendChild(h("ul", { class: "b-chips", "aria-label": "What the forecast noticed" }, o.forecast.reasons.map(function (r) { return h("li", { class: "b-chip" }, r.text || r); })));
      } else chartCard.appendChild(ui.empty({ title: "No sales yet", text: "Sales from Checkout or your daily upload appear here, and the forecast follows once there is enough history.", action: { label: "Upload sales", href: "#/imports" } }));
      main.appendChild(chartCard);

      var hist = section("Stock history", { sub: "Every movement in " + (o.store ? o.store.name : "this store") + ", newest first, with the balance after it." });
      hist.appendChild(ui.table([
        { key: "at", label: "When", render: function (m) { return tDateTime(m.at); } },
        { key: "label", label: "What", render: function (m) { return h("span", null, m.label, m.reason ? h("div", { class: "text-3 b-small" }, m.reason) : null); } },
        { key: "qty_milli", label: "Change", kind: "qty", render: function (m) { return (m.qty_milli > 0 ? "+" : "") + units(m.qty_milli, u); } },
        { key: "balance_milli", label: "Balance", kind: "qty", render: function (m) { return units(m.balance_milli, u); } },
        { key: "person", label: "By", render: function (m) { return m.person || m.device || "System"; } }
      ], o.movements, { caption: "Stock movements", empty: { title: "No movements yet", text: "Deliveries, sales, counts and transfers show here as they happen." } }));
      var others = section("In other stores");
      others.appendChild(ui.table([
        { key: "name", label: "Store" },
        { key: "on_hand_milli", label: "On hand", kind: "qty", render: function (r) { return (r.known ? "" : "about ") + units(r.on_hand_milli, u); } }
      ], o.other_stores, { caption: "Stock in other stores", empty: { title: "Not stocked in another store you can see" } }));
      main.appendChild(h("div", { class: "grid-2 b-grid-top" }, hist, others));

      var supCard = section("Suppliers and terms");
      supCard.appendChild(ui.table([
        { key: "supplier", label: "Supplier", render: function (r) { return h("a", { href: "#/suppliers/" + r.supplier_id }, r.supplier); } },
        { key: "supplier_code", label: "Their code" },
        { key: "cost_paise", label: "Cost", kind: "money" },
        { key: "case_pack", label: "Case", kind: "num", render: function (r) { return r.case_pack ? fmt.num(r.case_pack) : fmt.num(p.case_pack); } },
        { key: "min_order_qty_milli", label: "Smallest order", kind: "qty", render: function (r) { return r.min_order_qty_milli ? units(r.min_order_qty_milli, u) : "—"; } },
        { key: "lead_time_days", label: "Delivery takes", kind: "num", render: function (r) { return plural(r.lead_time_days, "day"); } },
        { key: "preferred", label: "Preferred", render: function (r) { return r.preferred ? "Yes" : "No"; } }
      ], p.suppliers || [], { caption: "Suppliers", empty: { title: "No supplier yet", text: "Link a supplier so orders know who to buy from.", action: { label: "Open suppliers", href: "#/suppliers" } } }));
      var priceCard = section("Price history");
      priceCard.appendChild(ui.table([
        { key: "price_paise", label: "Price", kind: "money" },
        { key: "start_date", label: "From", kind: "date" },
        { key: "end_date", label: "To", kind: "date", render: function (r) { return r.end_date ? fmt.date(r.end_date) : "Now"; } },
        { key: "promo_type", label: "Offer", render: function (r) { return r.promo_type ? cap(String(r.promo_type).replace(/_/g, " ")) : "Regular"; } },
        { key: "store_id", label: "Where", render: function (r) { return r.store_id ? (storeById(r.store_id) || {}).name || "This store" : "All stores"; } }
      ], o.prices, { caption: "Price history", empty: { title: "No price changes recorded", text: "The selling price above applies everywhere." } }));
      main.appendChild(h("div", { class: "grid-2 b-grid-top" }, supCard, priceCard));

      var bc = section("Barcodes");
      bc.appendChild(p.barcodes && p.barcodes.length ? h("ul", { class: "b-chips" }, p.barcodes.map(function (b) {
        return h("li", { class: "b-chip mono" }, b.code + (b.units_per_scan > 1 ? " · case of " + b.units_per_scan : ""));
      })) : h("p", { class: "text-2" }, "No barcode yet. Checkout finds this product by its SKU, " + p.sku + "."));
      if (isAdmin) {
        var code = ui.input({ inputmode: "numeric", autocomplete: "off", maxlength: 64 });
        var per = ui.input({ type: "number", min: 1, step: 1, value: "1" });
        var berr = h("p", { class: "field-error", role: "alert", hidden: true });
        var add = h("button", { class: "btn btn-primary", type: "submit" }, "Add the barcode");
        DQ.scan.attach(code, function (c) { code.value = c; });
        bc.appendChild(h("form", { class: "b-inline-form", novalidate: true, onsubmit: function (e) {
          e.preventDefault();
          var c = code.value.trim();
          if (c.length < 4) { berr.textContent = "Type or scan the barcode."; berr.hidden = false; code.focus(); return; }
          berr.hidden = true; setBusy(add, true, "Adding…");
          DQ.post("/api/products/" + p.id + "/barcodes", { code: c, units_per_scan: parseInt(per.value, 10) || 1 }).then(function () {
            DQ.toast("Added barcode " + c + " to " + p.name + "."); DQ.render();
          }, function (e2) { setBusy(add, false); berr.textContent = e2.message; berr.hidden = false; });
        } }, h("div", { class: "grid-3" }, ui.field("New barcode", code, "Scan it or type it."), ui.field("Units per scan", per, "More than 1 for a case barcode."), h("div", { class: "b-form-end" }, add)), berr));
      }
      main.appendChild(bc);
    }, failed(main, alive, "Product"));
  } });

  /* ================================================================ Orders */
  var PO_STATUS = { draft: ["Draft", "neutral"], approved: ["Approved", "data"], sent: ["Sent", "attention"], partly_received: ["Partly received", "attention"], received: ["Received", "ok"], cancelled: ["Cancelled", "neutral"] };
  var PO_ORDER = ["draft", "approved", "sent", "partly_received", "received", "cancelled"];
  function poBadge(st) { var x = PO_STATUS[st] || [cap(st), "neutral"]; return ui.badge(x[0], x[1]); }
  function casesText(qty, caseMilli) {
    if (!caseMilli || caseMilli <= 1000) return null;
    var n = qty / caseMilli;
    return (Number.isInteger(n) ? fmt.num(n) : n.toFixed(1)) + (n === 1 ? " case" : " cases") + " of " + fmt.num(caseMilli / 1000);
  }

  function progress(total, min) {
    var met = total >= min, share = Math.min(1, total / min);
    return h("div", { class: "b-progress-wrap" },
      h("div", { class: "b-progress", role: "progressbar", "aria-valuemin": 0, "aria-valuemax": 100, "aria-valuenow": Math.round(share * 100), "aria-label": "Order value against the supplier's minimum", "data-met": met ? "yes" : "no" },
        h("div", { class: "b-progress-fill", style: { width: (share * 100).toFixed(1) + "%" } })),
      h("p", { class: "b-progress-text" }, fmt.rupees(total, { whole: true }) + " of the " + fmt.rupees(min, { whole: true }) + " minimum: " +
        (met ? "met" : fmt.rupees(min - total, { whole: true }) + " short")));
  }

  function suggestedTab(body, sid, alive, switchTab) {
    body.appendChild(ui.skeleton(5));
    /* Suggested lines carry no unit, so the units come from the stock list. */
    return Promise.all([DQ.get("/api/orders/suggestions" + DQ.qs({ store_id: sid })),
      DQ.get("/api/stock" + DQ.qs({ store_id: sid })).catch(function () { return { rows: [] }; })]).then(function (res) {
      if (!alive()) return;
      var s = res[0], unitOf = {};
      res[1].rows.forEach(function (r) { unitOf[r.product_id] = r.unit; });
      function lu(l) { return unitOf[l.product_id] || "each"; }
      DQ.clear(body);
      var canMake = DQ.can("orders.create", sid);
      if (!s.run) {
        body.appendChild(ui.empty({ title: "No suggested orders yet", text: s.message || "Order suggestions appear after the first forecast.", action: { label: "See what the forecast needs", href: "#/setup/forecast" } }));
        return;
      }
      var make = canMake && s.groups.length ? ui.button("Make draft orders", function () { makeDrafts(make, null); }, "primary", { title: "One draft per supplier; drafts made earlier from suggestions are refreshed, not doubled" }) : null;
      body.appendChild(h("div", { class: "b-bar" },
        h("p", { class: "text-2" }, "Suggested for " + storeName() + " from the forecast with data to " + fmt.dayShort(s.run.data_cutoff) + ", after Today's decisions. ", fresh("Stock as of", nowIso())),
        make));
      function makeDrafts(btn, supplierId) {
        setBusy(btn, true, "Making drafts…");
        var payload = { store_id: sid };
        if (supplierId !== null) payload.supplier_ids = [supplierId];
        DQ.post("/api/orders/from-suggestions", payload).then(function (made) {
          DQ.toast("Made " + plural(made.length, "draft order") + ". Check and approve them under Purchase orders.");
          switchTab("po");
        }, function (err) { setBusy(btn, false); DQ.toast("No drafts made. " + err.message, { kind: "risk" }); });
      }
      if (!s.groups.length) {
        body.appendChild(ui.empty({ title: "Nothing to order for " + storeName() + " right now", text: s.message || "Stock and what is already on order cover every stock target." }));
      }
      s.groups.forEach(function (g) {
        var card = section(g.supplier, { aside: canMake ? ui.button("Make a draft for " + g.supplier, function (e) { makeDrafts(e.target, g.supplier_id); }, "ghost", { class: "btn btn-ghost btn-sm" }) : null });
        card.appendChild(h("p", { class: "text-2 b-section-sub" },
          (g.lead_time_days !== null ? "Delivers in " + plural(g.lead_time_days, "day") + ". " : "") +
          (g.can_order_today ? "Takes orders today." : "Does not take orders today" + (g.next_order_day ? "; next order day " + fmt.date(g.next_order_day) : "") + ".")));
        if (g.min_order_paise > 0) card.appendChild(progress(g.total_paise, g.min_order_paise));
        else card.appendChild(h("p", { class: "b-progress-text" }, "Order value " + fmt.rupees(g.total_paise, { whole: true }) + "; this supplier has no minimum order."));
        if (g.raised) card.appendChild(h("p", { class: "notice b-raised" }, h("strong", null, g.raised + ". "), g.alternative ? cap(g.alternative) + "." : ""));
        card.appendChild(ui.table([
          { key: "name", label: "Product", render: function (l) { return h("div", { class: "b-prodcell" }, h("a", { href: "#/products/" + l.product_id, class: "b-plain-link" }, l.name), h("div", { class: "text-3 mono b-small" }, l.sku)); } },
          { key: "target_milli", label: "Stock target", kind: "qty", unit: lu },
          { key: "on_hand_milli", label: "On hand", kind: "qty", unit: lu },
          { key: "on_order_milli", label: "On order", kind: "qty", unit: lu },
          { key: "need_milli", label: "Need", kind: "qty", unit: lu },
          { key: "qty_milli", label: "Order", kind: "qty", render: function (l) { var c = lu(l) === "each" ? casesText(l.qty_milli, l.case_milli) : null; return h("span", null, h("strong", null, units(l.qty_milli, lu(l))), c ? h("div", { class: "text-3 b-small" }, c) : null); } },
          { key: "value_paise", label: "Value", kind: "money" },
          { key: "reasons", label: "Why", render: function (l) { return h("div", { class: "b-why" }, l.reasons.length ? cap(l.reasons.join("; ")) : "To reach the stock target"); } }
        ], g.lines, { caption: "Suggested lines for " + g.supplier }));
        body.appendChild(card);
      });
      if (s.skipped.length || s.stock_unknown.length) {
        var left = section("Left off these orders");
        if (s.skipped.length) left.appendChild(h("p", null, h("strong", null, "Skipped on Today: "), s.skipped.join(", "), ". ", h("a", { href: "#/today" }, "Review on Today"), "."));
        if (s.stock_unknown.length) left.appendChild(h("p", null, h("strong", null, "Stock not known: "), s.stock_unknown.join(", "), ". An order on a guess is worse than no order. ", h("a", { href: "#/count" }, "Count them"), " or ", h("a", { href: "#/stock?status=" + encodeURIComponent("Count needed") }, "see them in Stock"), "."));
        body.appendChild(left);
      }
    }, function (err) { if (alive()) { DQ.clear(body); body.appendChild(ui.error(err, DQ.render)); } });
  }

  function poTab(body, sid, alive) {
    body.appendChild(ui.skeleton(5));
    return DQ.get("/api/orders" + DQ.qs({ store_id: sid, limit: 200 })).then(function (list) {
      if (!alive()) return;
      DQ.clear(body);
      body.appendChild(h("p", { class: "text-2 b-bar" }, "Purchase orders for " + storeName() + ". ", fresh("As of", nowIso())));
      if (!list.length) {
        body.appendChild(ui.empty({ title: "No purchase orders yet", text: "Make drafts from the suggested orders, or make one by hand. Drafts are checked and approved before they go to the supplier.", action: { label: "See suggested orders", href: "#/orders" } }));
        return;
      }
      PO_ORDER.forEach(function (st) {
        var rows = list.filter(function (o) { return o.status === st; });
        if (!rows.length) return;
        var card = section(PO_STATUS[st][0] + " (" + rows.length + ")");
        card.appendChild(ui.table([
          { key: "number", label: "Order", render: function (o) { return h("a", { href: "#/orders/" + o.id, class: "mono", onclick: function (e) { e.stopPropagation(); } }, o.number); } },
          { key: "supplier", label: "Supplier", render: function (o) { return o.supplier || "No supplier set"; } },
          { key: "lines", label: "Lines", kind: "num", render: function (o) { return fmt.num(o.lines.length); } },
          { key: "total_paise", label: "Value", kind: "money" },
          { key: "origin", label: "Made", render: function (o) { return o.origin === "suggested" ? "From suggestions" : "By hand"; } },
          { key: "expected_date", label: "Expected", kind: "date" },
          { key: "created_at", label: "Created", kind: "date", render: function (o) { return tDate(o.created_at); } },
          { key: "status", label: "Status", render: function (o) { return poBadge(o.status); } }
        ], rows, { caption: PO_STATUS[st][0] + " orders", onRow: function (o) { DQ.go("#/orders/" + o.id); } }));
        body.appendChild(card);
      });
    }, function (err) { if (alive()) { DQ.clear(body); body.appendChild(ui.error(err, DQ.render)); } });
  }

  /* Lines editor for a new order or a draft: quantity and cost per line. */
  function linesEditor(initial, opts) {
    opts = opts || {};
    var lines = initial.map(function (l) { return Object.assign({}, l); });
    var holder = h("div");
    function draw() {
      DQ.clear(holder);
      if (!lines.length) { holder.appendChild(h("p", { class: "text-2 b-lines-empty" }, "No lines yet. Search for a product below to add it.")); return; }
      var tbody = h("tbody");
      lines.forEach(function (l, i) {
        var q = ui.input({ type: "number", min: 0, step: l.unit === "each" ? 1 : 0.001, inputmode: "decimal", value: l.qty_milli ? String(l.qty_milli / 1000) : "", "aria-label": "Quantity of " + l.name, class: "input b-num-input" });
        q.addEventListener("input", function () { l.qty_milli = fmt.toMilli(q.value); });
        var c = ui.input({ type: "number", min: 0, step: 0.01, inputmode: "decimal", value: l.unit_cost_paise === null || l.unit_cost_paise === undefined ? "" : String(l.unit_cost_paise / 100), placeholder: "Supplier's", "aria-label": "Unit cost of " + l.name + " in rupees", class: "input b-num-input" });
        c.addEventListener("input", function () { l.unit_cost_paise = c.value.trim() === "" ? null : fmt.toPaise(c.value); });
        tbody.appendChild(h("tr", null,
          h("td", null, h("div", { class: "b-prodcell" }, l.name, h("div", { class: "text-3 mono b-small" }, l.sku, l.case_pack > 1 ? " · case of " + l.case_pack : "", l.unit && l.unit !== "each" ? " · in " + l.unit : ""),
            l.reason ? h("div", { class: "text-3 b-small" }, cap(l.reason)) : null)),
          h("td", { class: "num" }, q),
          h("td", { class: "num" }, c),
          h("td", null, h("button", { type: "button", class: "btn btn-ghost btn-sm", "aria-label": "Remove " + l.name, onclick: function () { lines.splice(i, 1); draw(); } }, "Remove"))));
      });
      holder.appendChild(h("div", { class: "table-wrap" }, h("table", { class: "table" },
        h("caption", { class: "sr-only" }, "Order lines"),
        h("thead", null, h("tr", null, h("th", { scope: "col" }, "Product"), h("th", { scope: "col", class: "num" }, "Quantity"), h("th", { scope: "col", class: "num" }, "Unit cost (₹)"), h("th", { scope: "col" }, h("span", { class: "sr-only" }, "Remove")))),
        tbody)));
    }
    draw();
    var search = productSearch(function (p) {
      if (lines.some(function (l) { return l.product_id === p.id; })) { DQ.toast(p.name + " is already on this order; change its quantity instead.", { kind: "attention" }); return; }
      if (p.status === "discontinued") { DQ.toast(p.name + " is discontinued, so it cannot be ordered.", { kind: "attention" }); return; }
      lines.push({ product_id: p.id, sku: p.sku, name: p.name, unit: p.unit, case_pack: p.case_pack, qty_milli: (p.case_pack || 1) * 1000, unit_cost_paise: null, reason: null });
      draw();
    }, { label: opts.searchLabel || "Add a product to the order" });
    return {
      el: h("div", { class: "stack" }, holder, search),
      read: function () {
        var bad = lines.filter(function (l) { return !(l.qty_milli > 0); })[0];
        if (bad) throw new Error("Give " + bad.name + " a quantity above zero, or remove it.");
        var frac = lines.filter(function (l) { return l.unit === "each" && l.qty_milli % 1000; })[0];
        if (frac) throw new Error(frac.name + " is ordered in whole units.");
        return lines.map(function (l) { return { product_id: l.product_id, qty_milli: l.qty_milli, unit_cost_paise: l.unit_cost_paise === undefined ? null : l.unit_cost_paise, reason: l.reason || null }; });
      },
      count: function () { return lines.length; }
    };
  }

  function newOrderTab(body, sid, alive) {
    if (!DQ.can("orders.create", sid)) {
      body.appendChild(ui.empty({ title: "Your role cannot make orders in " + storeName(), text: "A Purchaser, Store manager, Owner or Admin makes orders." }));
      return null;
    }
    body.appendChild(ui.skeleton(3));
    return DQ.get("/api/suppliers").then(function (sups) {
      if (!alive()) return;
      DQ.clear(body);
      var sup = ui.select([{ value: "", label: "No supplier yet" }].concat(sups.map(function (x) { return { value: x.id, label: x.name }; })), "");
      var exp = ui.input({ type: "date", min: todayIst() });
      var notes = h("textarea", { class: "input", rows: 2, maxlength: 2000 });
      var ed = linesEditor([]);
      var err = h("div", { class: "form-error", role: "alert", hidden: true });
      var save = h("button", { class: "btn btn-primary", type: "submit" }, "Make the draft order");
      var card = section("A new order for " + storeName(), { sub: "It starts as a draft. Leave the cost empty to use the supplier's agreed cost." });
      card.appendChild(h("form", { class: "stack", novalidate: true, onsubmit: function (e) {
        e.preventDefault();
        var lines;
        try { lines = ed.read(); } catch (x) { err.textContent = x.message; err.hidden = false; return; }
        if (!lines.length) { err.textContent = "Add at least one product."; err.hidden = false; return; }
        err.hidden = true; setBusy(save, true, "Making…");
        DQ.post("/api/orders", { store_id: sid, supplier_id: sup.value || null, lines: lines, expected_date: exp.value || null, notes: notes.value.trim() || null, origin: "manual" }).then(function (po) {
          DQ.toast("Made draft order " + po.number + ".");
          DQ.go("#/orders/" + po.id);
        }, function (e2) { setBusy(save, false); err.textContent = apiErr(e2); err.hidden = false; });
      } },
        h("div", { class: "grid-3" }, ui.field("Supplier", sup), ui.field("Expected delivery (optional)", exp), ui.field("Note for the supplier (optional)", notes)),
        ed.el, err, h("div", { class: "row" }, save)));
      body.appendChild(card);
    }, function (err) { if (alive()) { DQ.clear(body); body.appendChild(ui.error(err, DQ.render)); } });
  }

  DQ.route("#/orders", { title: "Orders", nav: "Orders", render: function (main, params, query, alive) {
    var sid = DQ.store();
    if (!sid) return noStore(main, "Orders");
    var tab = ["suggested", "po", "new"].indexOf(query.tab) >= 0 ? query.tab : "suggested";
    DQ.clear(main);
    main.appendChild(ui.page("Orders", { sub: storeName() }));
    function switchTab(v) { DQ.go("#/orders" + (v === "suggested" ? "" : "?tab=" + v)); }
    main.appendChild(ui.tabs([{ value: "suggested", label: "Suggested" }, { value: "po", label: "Purchase orders" }].concat(DQ.can("orders.create", sid) ? [{ value: "new", label: "New order" }] : []), tab, switchTab));
    var body = h("div", { class: "b-tab-body", role: "tabpanel" });
    main.appendChild(body);
    if (tab === "po") return poTab(body, sid, alive);
    if (tab === "new") return newOrderTab(body, sid, alive);
    return suggestedTab(body, sid, alive, switchTab);
  } });

  /* ================================================================ Purchase order */
  DQ.route("#/orders/:id", { title: "Purchase order", render: function (main, params, query, alive) {
    loading(main, "Purchase order");
    return DQ.get("/api/orders/" + encodeURIComponent(params.id)).then(function (po) {
      if (!alive()) return;
      var supP = po.supplier_id ? DQ.get("/api/suppliers/" + po.supplier_id).catch(function () { return null; }) : Promise.resolve(null);
      return supP.then(function (sup) { if (alive()) draw(po, sup); });
    }, failed(main, alive, "Purchase order"));

    function draw(po, sup) {
      DQ.clear(main);
      document.title = po.number + " · DQ-Stack";
      var st = po.status, store = po.store_id;
      var canCreate = DQ.can("orders.create", store), canApprove = DQ.can("orders.approve", store);
      var msg = h("div", { "aria-live": "polite" });
      function act(btn, path, body, done) {
        setBusy(btn, true);
        return DQ.post("/api/orders/" + po.id + path, body || {}).then(function (r) { if (done) done(r); else DQ.render(); }, function (err) { setBusy(btn, false); DQ.toast(apiErr(err), { kind: "risk" }); });
      }
      var actions = [ui.button("Download PDF", function (e) { DQ.download("/api/orders/" + po.id + "/pdf", e.currentTarget); }, "ghost")];
      if (st === "draft" && canApprove) actions.push(ui.button("Approve", function (e) { act(e.target, "/approve", null, function () { DQ.toast("Approved " + po.number + ". Send it to the supplier next."); DQ.render(); }); }, "primary"));
      if ((st === "approved" || st === "sent") && canCreate) {
        actions.push(ui.button(st === "sent" ? "Email again" : "Send by email", function (e) {
          var to = sup && sup.email;
          DQ.confirm({ title: "Email " + po.number + "?", message: to ? "The order goes to " + to + " with the PDF attached." : "This supplier has no email address on file. Add one on the supplier's page, or send by WhatsApp.", confirmLabel: to ? "Send the email" : "Try anyway" }).then(function (r) {
            if (!r.ok) return;
            act(e.target, "/send", { channel: "email" }, function (res) { DQ.toast("Emailed " + po.number + " to " + res.sent_to + "."); DQ.render(); });
          });
        }, "ghost"));
        actions.push(ui.button(st === "sent" && po.whatsapp_api ? "WhatsApp again" : "Send by WhatsApp", function (e) {
          var supName = po.supplier || "the supplier";
          if (po.whatsapp_api) {
            var ph = sup && sup.phone;
            DQ.confirm({ title: "Send " + po.number + " on WhatsApp?", message: ph ? "The order PDF goes to " + supName + " on WhatsApp at " + ph + "." : "This supplier has no phone number on file. Add one on the supplier's page, or send by email.", confirmLabel: ph ? "Send on WhatsApp" : "Try anyway" }).then(function (r) {
              if (!r.ok) return;
              act(e.target, "/send", { channel: "whatsapp" }, function (res) {
                if (res.whatsapp_link) { window.open(res.whatsapp_link, "_blank", "noopener"); DQ.render(); return; }
                DQ.toast("Sent " + po.number + " to " + (res.supplier || supName) + " on WhatsApp."); DQ.render();
              });
            });
            return;
          }
          var win = null;
          try { win = window.open("", "_blank"); } catch (x) { win = null; }
          act(e.target, "/send", { channel: "whatsapp" }, function (res) {
            if (!res.whatsapp_link) { DQ.toast("Sent " + po.number + " to " + (res.supplier || supName) + " on WhatsApp."); DQ.render(); return; }
            if (win && !win.closed) { try { win.opener = null; win.location.href = res.whatsapp_link; } catch (x) { win = null; } }
            DQ.render();
            setTimeout(function () {
              var m = document.getElementById("b-wa-msg");
              if (m) m.appendChild(h("p", { class: "notice" }, "WhatsApp opens with the order filled in; press send there. ", h("a", { href: res.whatsapp_link, target: "_blank", rel: "noopener noreferrer" }, "Open WhatsApp again"), "."));
            }, 300);
          }).then(function () { if (win && !win.closed && win.location.href === "about:blank") win.close(); });
        }, "ghost"));
      }
      if (st === "approved" && canCreate) actions.push(ui.button("Mark as sent", function (e) { act(e.target, "/mark-sent", null, function () { DQ.toast(po.number + " marked as sent."); DQ.render(); }); }, "ghost", { title: "For an order phoned in or handed over outside the app" }));
      if (st === "partly_received" && canCreate) actions.push(ui.button("Close short", function (e) {
        DQ.confirm({ title: "Close " + po.number + " short?", message: "The rest of this order stops counting as on order, and cannot be received against it. This cannot be undone.", reason: true, reasonLabel: "Why the rest will not come", confirmLabel: "Close it short", danger: true }).then(function (r) {
          if (r.ok) act(e.target, "/close-short", { reason: r.reason }, function () { DQ.toast("Closed " + po.number + " short."); DQ.render(); });
        });
      }, "ghost"));
      var anyReceived = po.lines.some(function (l) { return l.received_milli > 0; });
      if (["draft", "approved", "sent"].indexOf(st) >= 0 && canApprove && !anyReceived) actions.push(ui.button("Cancel the order", function (e) {
        DQ.confirm({ title: "Cancel " + po.number + "?", message: "A cancelled order cannot be reopened. If the supplier already has it, tell them too.", reason: true, reasonLabel: "Why it is cancelled", confirmLabel: "Cancel the order", cancelLabel: "Keep it", danger: true }).then(function (r) {
          if (r.ok) act(e.target, "/cancel", { reason: r.reason }, function () { DQ.toast("Cancelled " + po.number + "."); DQ.render(); });
        });
      }, "danger"));

      main.appendChild(ui.page(h("span", null, "Purchase order ", h("span", { class: "mono" }, po.number), " ", poBadge(st)), {
        sub: h("span", null, (po.supplier || "No supplier set") + " · deliver to " + (po.store || "") + " · ", fresh("As of", nowIso())),
        actions: actions
      }));
      main.appendChild(h("div", { id: "b-wa-msg" }, msg));
      main.appendChild(h("dl", { class: "b-facts b-facts-wide card" },
        fact("Value", fmt.rupees(po.total_paise), { note: "excluding GST" }),
        fact("Supplier minimum", po.supplier_min_order_paise ? fmt.rupees(po.supplier_min_order_paise, { whole: true }) : "None", { note: po.supplier_min_order_paise ? (po.total_paise >= po.supplier_min_order_paise ? "met" : fmt.rupees(po.supplier_min_order_paise - po.total_paise, { whole: true }) + " short") : null }),
        fact("Expected", po.expected_date ? fmt.date(po.expected_date) : "Not set"),
        fact("Made", po.created_at ? tDate(po.created_at) : "—", { note: (po.created_by ? "by " + po.created_by + ", " : "") + (po.origin === "suggested" ? "from suggestions" : "by hand") }),
        fact("Approved", po.approved_at ? tDate(po.approved_at) : "Not yet", { note: po.approved_by ? "by " + po.approved_by : null }),
        fact("Sent", po.sent_at ? tDateTime(po.sent_at) : "Not yet")));
      if (sup) main.appendChild(h("p", { class: "text-2" }, "Supplier contact: ", sup.contact ? sup.contact + ", " : "", sup.email || "no email", ", ", sup.phone || "no phone", ". ", h("a", { href: "#/suppliers/" + sup.id }, "Supplier details")));
      if (po.notes) main.appendChild(h("p", { class: "notice" }, h("strong", null, "Note: "), po.notes));
      if (po.closed_short) main.appendChild(h("p", { class: "notice" }, "This order was closed short: the rest will not come."));

      var linesCard = section("Lines", { sub: st === "draft" ? (canCreate ? "A draft can still be changed. Save before approving." : "A draft; a Purchaser or manager can change it.") : null });
      if (st === "draft" && canCreate) {
        var ed = linesEditor(po.lines);
        var err = h("div", { class: "form-error", role: "alert", hidden: true });
        var save = h("button", { class: "btn btn-primary", type: "submit" }, "Save changes");
        linesCard.appendChild(h("form", { class: "stack", novalidate: true, onsubmit: function (e) {
          e.preventDefault();
          var lines;
          try { lines = ed.read(); } catch (x) { err.textContent = x.message; err.hidden = false; return; }
          if (!lines.length) { err.textContent = "An order needs at least one line. Cancel it instead."; err.hidden = false; return; }
          err.hidden = true; setBusy(save, true, "Saving…");
          DQ.api("PATCH", "/api/orders/" + po.id, { lines: lines }).then(function () { DQ.toast("Saved " + po.number + "."); DQ.render(); },
            function (e2) { setBusy(save, false); err.textContent = apiErr(e2); err.hidden = false; });
        } }, ed.el, err, h("div", { class: "row" }, save)));
      } else {
        linesCard.appendChild(ui.table([
          { key: "name", label: "Product", render: function (l) { return h("div", { class: "b-prodcell" }, h("a", { href: "#/products/" + l.product_id, class: "b-plain-link" }, l.name), h("div", { class: "text-3 mono b-small" }, l.sku)); } },
          { key: "qty_milli", label: "Ordered", kind: "qty", render: function (l) { var c = casesText(l.qty_milli, l.case_pack * 1000); return h("span", null, units(l.qty_milli, l.unit), c ? h("div", { class: "text-3 b-small" }, c) : null); } },
          { key: "received_milli", label: "Received", kind: "qty", unit: "unit" },
          { key: "due_milli", label: "Still due", kind: "qty", unit: "unit" },
          { key: "unit_cost_paise", label: "Unit cost", kind: "money" },
          { key: "value_paise", label: "Value", kind: "money" },
          { key: "reason", label: "Why", render: function (l) { return h("div", { class: "b-why" }, l.reason ? cap(l.reason) : "—"); } }
        ], po.lines, { caption: "Lines on " + po.number }));
      }
      main.appendChild(linesCard);

      var unitOf = {};
      po.lines.forEach(function (l) { unitOf[l.product_id] = l.unit; });
      function lineUnit(l) { return unitOf[l.product_id] || "each"; }
      var del = section("Deliveries against this order");
      if (!po.deliveries || !po.deliveries.length) {
        del.appendChild(h("p", { class: "text-2" }, st === "sent" || st === "partly_received" ? "Nothing received yet. " : "Nothing received. ",
          (st === "sent" || st === "partly_received") ? h("a", { href: "#/receive" }, "Receive a delivery") : null));
      } else po.deliveries.forEach(function (d) {
        del.appendChild(h("h3", { class: "title-3 b-sub-head" }, "Received " + tDateTime(d.created_at) + (d.invoice_number ? " · invoice " + d.invoice_number : "")));
        if (d.note) del.appendChild(h("p", { class: "text-2" }, d.note));
        del.appendChild(ui.table([
          { key: "name", label: "Product" },
          { key: "received_milli", label: "Received", kind: "qty", unit: lineUnit },
          { key: "damaged_milli", label: "Damaged", kind: "qty", unit: lineUnit },
          { key: "reject_reason", label: "Why rejected" },
          { key: "expiry", label: "Expires", kind: "date" }
        ], d.lines, { caption: "Delivery lines" }));
      });
      main.appendChild(del);
    }
  } });

  /* ================================================================ Suppliers */
  var WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  var WEEKDAYS_LONG = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
  function canManageSuppliers() { return DQ.can("imports.upload") || DQ.can("suppliers.manage"); }
  function orderDaysText(days) { return !days || !days.length ? "Any day" : days.slice().sort().map(function (d) { return WEEKDAYS[d]; }).join(", "); }

  function supplierForm(s, onSaved) {
    s = s || {};
    var f = {
      name: ui.input({ value: s.name || "", required: true, maxlength: 200, autocomplete: "organization" }),
      contact: ui.input({ value: s.contact || "", maxlength: 200, autocomplete: "name" }),
      email: ui.input({ type: "email", value: s.email || "", maxlength: 254, autocomplete: "email" }),
      phone: ui.input({ type: "tel", value: s.phone || "", maxlength: 20, autocomplete: "tel", inputmode: "tel" }),
      gstin: ui.input({ value: s.gstin || "", maxlength: 15, autocapitalize: "characters" }),
      lead: ui.input({ type: "number", min: 0, max: 120, step: 1, value: String(s.lead_time_days === undefined ? 2 : s.lead_time_days) }),
      min: ui.input({ type: "number", min: 0, step: 1, inputmode: "decimal", value: String((s.min_order_paise || 0) / 100) })
    };
    var days = WEEKDAYS_LONG.map(function (d, i) { return h("input", { type: "checkbox", class: "b-check", value: i, checked: (s.order_days || []).indexOf(i) >= 0, "aria-label": d }); });
    var err = h("div", { class: "form-error", role: "alert", hidden: true });
    var save = h("button", { class: "btn btn-primary", type: "submit" }, s.id ? "Save the supplier" : "Add the supplier");
    function opt(v) { v = v.trim(); return v === "" ? null : v; }
    return h("form", { class: "stack", novalidate: true, onsubmit: function (e) {
      e.preventDefault();
      var body = { name: f.name.value.trim(), contact: opt(f.contact.value), email: opt(f.email.value), phone: opt(f.phone.value), gstin: opt(f.gstin.value.toUpperCase()),
        lead_time_days: parseInt(f.lead.value, 10), min_order_paise: fmt.toPaise(f.min.value || "0"),
        order_days: days.filter(function (d) { return d.checked; }).map(function (d) { return +d.value; }) };
      if (!body.name) { err.textContent = "Give the supplier a name."; err.hidden = false; f.name.focus(); return; }
      if (isNaN(body.lead_time_days) || body.lead_time_days < 0) { err.textContent = "Lead time is a number of days, 0 or more."; err.hidden = false; f.lead.focus(); return; }
      if (body.min_order_paise === null || body.min_order_paise < 0) { err.textContent = "The minimum order is an amount in rupees, 0 or more."; err.hidden = false; f.min.focus(); return; }
      err.hidden = true; setBusy(save, true, "Saving…");
      (s.id ? DQ.api("PATCH", "/api/suppliers/" + s.id, body) : DQ.post("/api/suppliers", body)).then(function (r) {
        DQ.toast((s.id ? "Saved " : "Added ") + body.name + "."); onSaved(r);
      }, function (e2) { setBusy(save, false); err.textContent = apiErr(e2); err.hidden = false; });
    } },
      h("div", { class: "grid-3" }, ui.field("Name", f.name), ui.field("Contact person", f.contact), ui.field("GSTIN", f.gstin, "15 characters, optional."),
        ui.field("Email for orders", f.email), ui.field("Phone or WhatsApp", f.phone, "10 digits for an Indian mobile."),
        ui.field("Delivery takes (days)", f.lead, "From order to delivery."), ui.field("Minimum order (₹)", f.min, "0 if there is none. Orders are raised to reach it.")),
      h("fieldset", { class: "b-days" }, h("legend", null, "Days they take orders"),
        h("div", { class: "b-day-list" }, days.map(function (d, i) { return h("label", { class: "b-day" }, d, WEEKDAYS[i]); })),
        h("p", { class: "field-help" }, "Tick none if they take orders any day.")),
      err, h("div", { class: "row" }, save, s.id ? h("a", { class: "btn btn-ghost", href: "#/suppliers/" + s.id }, "Cancel") : h("a", { class: "btn btn-ghost", href: "#/suppliers" }, "Cancel")));
  }

  function termsForm(sup, existing, onSaved, onCancel) {
    var t = existing || {};
    var picked = existing ? { id: existing.product_id, name: existing.name, sku: existing.sku } : null;
    var who = h("p", { class: "b-picked" }, picked ? h("span", null, h("strong", null, picked.name), " ", h("span", { class: "mono text-3" }, picked.sku)) : "No product chosen yet.");
    var code = ui.input({ value: t.supplier_code || "", maxlength: 64 });
    var cost = ui.input({ type: "number", min: 0, step: 0.01, inputmode: "decimal", value: t.cost_paise === null || t.cost_paise === undefined ? "" : String(t.cost_paise / 100) });
    var cpk = ui.input({ type: "number", min: 1, step: 1, value: t.case_pack ? String(t.case_pack) : "" });
    var moq = ui.input({ type: "number", min: 0, step: 1, value: t.min_order_qty_milli ? String(t.min_order_qty_milli / 1000) : "" });
    var err = h("div", { class: "form-error", role: "alert", hidden: true });
    var save = h("button", { class: "btn btn-primary", type: "submit" }, existing ? "Save the terms" : "Add the product");
    var search = existing ? null : productSearch(function (p) {
      picked = p; DQ.clear(who); who.appendChild(h("span", null, h("strong", null, p.name), " ", h("span", { class: "mono text-3" }, p.sku)));
      if (!cpk.value) cpk.value = String(p.case_pack || 1);
      if (!cost.value && p.cost_paise !== null) cost.value = String(p.cost_paise / 100);
      code.focus();
    }, { label: "Find the product" });
    return h("form", { class: "stack b-terms-form", novalidate: true, onsubmit: function (e) {
      e.preventDefault();
      if (!picked) { err.textContent = "Find and choose the product first."; err.hidden = false; return; }
      var body = { supplier_code: code.value.trim() || null, cost_paise: cost.value.trim() === "" ? null : fmt.toPaise(cost.value),
        case_pack: cpk.value.trim() === "" ? null : parseInt(cpk.value, 10), min_order_qty_milli: moq.value.trim() === "" ? null : fmt.toMilli(moq.value), preferred: existing ? existing.preferred !== false : true };
      err.hidden = true; setBusy(save, true, "Saving…");
      DQ.api("PUT", "/api/suppliers/" + sup.id + "/products/" + picked.id, body).then(function () {
        DQ.toast("Saved the terms for " + picked.name + " from " + sup.name + "."); onSaved();
      }, function (e2) { setBusy(save, false); err.textContent = apiErr(e2); err.hidden = false; });
    } }, search, who,
      h("div", { class: "grid-3 b-terms-grid" }, ui.field("Their code (optional)", code), ui.field("Cost from them (₹)", cost, "Per unit, before GST."),
        ui.field("Units in their case", cpk), ui.field("Smallest order (units, optional)", moq)),
      err, h("div", { class: "row" }, save, onCancel ? ui.button("Cancel", onCancel, "ghost") : null));
  }

  DQ.route("#/suppliers", { title: "Suppliers", nav: "Suppliers", render: function (main, params, query, alive) {
    loading(main, "Suppliers");
    return DQ.get("/api/suppliers").then(function (list) {
      if (!alive()) return;
      DQ.clear(main);
      var can = canManageSuppliers();
      main.appendChild(ui.page("Suppliers", { sub: h("span", null, "Shared by every store. ", fresh("As of", nowIso())), actions: can ? [h("a", { class: "btn btn-primary", href: "#/suppliers/new" }, "Add a supplier")] : null }));
      main.appendChild(ui.table([
        { key: "name", label: "Supplier", render: function (s) { return h("a", { href: "#/suppliers/" + s.id, class: "b-plain-link", onclick: function (e) { e.stopPropagation(); } }, s.name); } },
        { key: "products", label: "Products", kind: "num" },
        { key: "order_days", label: "Order days", render: function (s) { return orderDaysText(s.order_days); } },
        { key: "lead_time_days", label: "Delivery takes", kind: "num", render: function (s) { return plural(s.lead_time_days, "day"); } },
        { key: "min_order_paise", label: "Minimum order", kind: "money", render: function (s) { return s.min_order_paise ? fmt.rupees(s.min_order_paise, { whole: true }) : "None"; } },
        { key: "contact", label: "Contact", render: function (s) { return [s.contact, s.phone, s.email].filter(Boolean).join(" · ") || "—"; } }
      ], list, { caption: "Suppliers", onRow: function (s) { DQ.go("#/suppliers/" + s.id); },
        empty: { title: "No suppliers yet", text: "Add the people you buy from, with their order days, delivery time and minimum order. Suggested orders are grouped by supplier.", action: can ? { label: "Add a supplier", href: "#/suppliers/new" } : { label: "Upload a supplier list", href: "#/imports" } } }));
    }, failed(main, alive, "Suppliers"));
  } });

  DQ.route("#/suppliers/:id", { title: "Supplier", render: function (main, params, query, alive) {
    var can = canManageSuppliers();
    if (params.id === "new") {
      DQ.clear(main);
      main.appendChild(ui.page("Add a supplier", { sub: "Shared by every store." }));
      if (!can) { main.appendChild(ui.empty({ title: "Your role cannot add suppliers", text: "An Owner, Admin or Purchaser adds them.", action: { label: "Back to suppliers", href: "#/suppliers" } })); return; }
      var c = section("Supplier details");
      c.appendChild(supplierForm(null, function (r) { DQ.go("#/suppliers/" + r.id); }));
      main.appendChild(c);
      return;
    }
    loading(main, "Supplier");
    return DQ.get("/api/suppliers/" + encodeURIComponent(params.id)).then(function (s) {
      if (!alive()) return;
      DQ.clear(main);
      document.title = s.name + " · DQ-Stack";
      var editing = query.edit === "1" && can;
      main.appendChild(ui.page(s.name, { sub: h("span", null, "Supplier, shared by every store · ", fresh("As of", nowIso())),
        actions: [h("a", { class: "btn btn-ghost", href: "#/suppliers" }, "All suppliers"), can && !editing ? h("a", { class: "btn btn-ghost", href: "#/suppliers/" + s.id + "?edit=1" }, "Edit details") : null,
          h("a", { class: "btn btn-ghost", href: "#/orders?tab=po" }, "Orders")] }));
      if (editing) {
        var ec = section("Edit " + s.name);
        ec.appendChild(supplierForm(s, function () { DQ.go("#/suppliers/" + s.id); }));
        main.appendChild(ec);
      } else {
        main.appendChild(h("dl", { class: "b-facts b-facts-wide card" },
          fact("Order days", orderDaysText(s.order_days)),
          fact("Delivery takes", plural(s.lead_time_days, "day")),
          fact("Minimum order", s.min_order_paise ? fmt.rupees(s.min_order_paise, { whole: true }) : "None"),
          fact("GSTIN", s.gstin || "Not given"),
          fact("Contact", s.contact || "Not given"),
          fact("Email", s.email || "Not given", { cls: "b-fact-wrap" }),
          fact("Phone", s.phone || "Not given")));
      }
      var prods = section("Products and case packs", { sub: "What " + s.name + " supplies, at what cost and in what case." });
      var addBox = h("div", { class: "b-add-terms", hidden: true });
      if (can) {
        var addBtn = ui.button("Add a product", function () { addBox.hidden = !addBox.hidden; addBtn.setAttribute("aria-expanded", String(!addBox.hidden)); if (!addBox.hidden) { var i = addBox.querySelector("input"); if (i) i.focus(); } }, "ghost", { "aria-expanded": "false" });
        prods.querySelector(".b-section-head").appendChild(addBtn);
        addBox.appendChild(termsForm(s, null, DQ.render));
        prods.appendChild(addBox);
      }
      var editRow = null;
      prods.appendChild(ui.table([
        { key: "name", label: "Product", render: function (l) { return h("div", { class: "b-prodcell" }, h("a", { href: "#/products/" + l.product_id, class: "b-plain-link" }, l.name), h("div", { class: "text-3 mono b-small" }, l.sku)); } },
        { key: "supplier_code", label: "Their code" },
        { key: "cost_paise", label: "Cost", kind: "money" },
        { key: "case_pack", label: "Case", kind: "num", render: function (l) { var n = l.effective_case_pack || l.case_pack; return n ? fmt.num(n) + (l.case_pack ? "" : " (product's)") : "—"; } },
        { key: "min_order_qty_milli", label: "Smallest order", kind: "qty", render: function (l) { return l.min_order_qty_milli ? units(l.min_order_qty_milli) : "—"; } },
        can ? { key: "act", label: "Change", render: function (l) {
          return h("div", { class: "row b-row-actions" },
            ui.button("Edit", function (e) {
              if (editRow) editRow.remove();
              var tr = e.target.closest("tr");
              editRow = h("tr", { class: "b-edit-row" }, h("td", { colspan: 6 }, termsForm(s, l, DQ.render, function () { if (editRow) editRow.remove(); editRow = null; e.target.focus(); })));
              tr.parentNode.insertBefore(editRow, tr.nextSibling);
              var first = editRow.querySelector("input"); if (first) first.focus();
            }, "ghost", { class: "btn btn-ghost btn-sm", "aria-label": "Edit terms for " + l.name }),
            ui.button("Remove", function (e) { removeTerms(e.target, s, l); }, "ghost", { class: "btn btn-ghost btn-sm", "aria-label": "Remove " + l.name + " from " + s.name }));
        } } : null
      ].filter(Boolean), s.product_list, { caption: "Products from " + s.name, empty: { title: "No products linked yet", text: can ? "Add the products " + s.name + " supplies, so orders for them go to " + s.name + "." : "An Owner, Admin or Purchaser links products to suppliers." } }));
      main.appendChild(prods);
    }, failed(main, alive, "Supplier"));
  } });

  function removeTerms(btn, s, l) {
    setBusy(btn, true, "Removing…");
    DQ.api("DELETE", "/api/suppliers/" + s.id + "/products/" + l.product_id).then(function () {
      DQ.render();
      DQ.toast("Removed " + l.name + " from " + s.name + ".", { undo: function () {
        DQ.api("PUT", "/api/suppliers/" + s.id + "/products/" + l.product_id, { supplier_code: l.supplier_code, cost_paise: l.cost_paise, case_pack: l.case_pack, min_order_qty_milli: l.min_order_qty_milli, preferred: l.preferred !== false })
          .then(function () { DQ.toast("Put " + l.name + " back."); DQ.render(); }, function (err) { DQ.toast("Could not undo: " + err.message, { kind: "risk" }); });
      } });
    }, function (err) { setBusy(btn, false); DQ.toast(err.message, { kind: "risk" }); });
  }

  /* ================================================================ Reports */
  var REPORT_HELP = {
    sales: "Units and revenue by day and store.", sales_by_category: "Where the money comes from.", best_worst: "What sells most and least.",
    stock_value: "What the stock on the shelves cost.", slow_dead_stock: "Stock that is not moving.", shrinkage: "Stock lost without a sale.",
    wastage: "Thrown away, damaged or expired.", forecast_accuracy: "How close the forecast came to what sold.",
    supplier_performance: "On time and in full, by supplier.", lost_sales: "Sales missed while out of stock.",
    profit_comparison: "Stock targets from real costs against one blanket service level.", accounting: "Totals for your accountant."
  };

  DQ.route("#/reports", { title: "Reports", nav: "Reports", can: "reports.view", render: function (main, params, query, alive) {
    loading(main, "Reports");
    return DQ.get("/api/reports").then(function (list) {
      if (!alive()) return;
      DQ.clear(main);
      main.appendChild(ui.page("Reports", { sub: "Open a report to read it on screen, or download it as Excel, PDF or CSV for the period and stores chosen here." }));
      /* One period and store choice for every download on this page. */
      var to = /^\d{4}-\d{2}-\d{2}$/.test(query.to || "") ? query.to : todayIst();
      var from = /^\d{4}-\d{2}-\d{2}$/.test(query.from || "") ? query.from : addDays(to, -27);
      var fromIn = ui.input({ type: "date", value: from, max: todayIst() });
      var toIn = ui.input({ type: "date", value: to, max: todayIst() });
      var stores = DQ.stores();
      var storeIn = ui.select([{ value: "all", label: stores.length > 1 ? "All my stores" : "All stores" }].concat(stores.map(function (s) { return { value: s.id, label: s.name }; })),
        query.store || DQ.store() || "all");
      var periodErr = h("p", { class: "field-error", role: "alert", hidden: true });
      function params(extra) {
        var f = fromIn.value, t = toIn.value;
        if (!f || !t || f > t) { periodErr.textContent = "Choose a start date on or before the end date."; periodErr.hidden = false; return null; }
        periodErr.hidden = true;
        return DQ.qs(Object.assign({ from: f, to: t, store_id: storeIn.value === "all" ? null : storeIn.value }, extra || {}));
      }
      function quick(days) {
        return ui.button(days === 7 ? "Last 7 days" : days === 28 ? "Last 28 days" : "Last 90 days", function () {
          toIn.value = todayIst(); fromIn.value = addDays(todayIst(), -(days - 1)); periodErr.hidden = true;
        }, "ghost", { class: "btn btn-ghost btn-sm" });
      }
      var allBtn = ui.button("Download all reports (Excel)", function (e) {
        var q = params(); if (q === null) return;
        DQ.download("/api/reports/all.xlsx" + q, e.currentTarget);
      }, "primary", { title: "One Excel workbook with every report, a sheet each" });
      main.appendChild(h("section", { class: "card b-dl-card", "aria-label": "Downloads" },
        h("h2", { class: "title-3" }, "Period and stores for downloads"),
        h("div", { class: "b-dl-grid" }, ui.field("From", fromIn), ui.field("To", toIn), ui.field("Stores", storeIn)),
        h("div", { class: "row b-dl-quick" }, quick(7), quick(28), quick(90)),
        periodErr,
        h("div", { class: "row" }, allBtn, h("span", { class: "text-3" }, "Every report in one workbook, with a contents sheet."))));
      main.appendChild(h("div", { class: "tiles b-report-tiles" }, list.map(function (r) {
        function dl(fmtName, label, title) {
          return ui.button(label, function (e) {
            var q = params({ format: fmtName }); if (q === null) return;
            DQ.download("/api/reports/" + r.name + q, e.currentTarget);
          }, "ghost", { class: "btn btn-ghost btn-sm", title: title, "aria-label": label + ": " + r.title });
        }
        return h("div", { class: "tile b-report-tile" },
          h("a", { class: "title-3 b-report-link", href: "#/reports/" + r.name }, r.title),
          h("div", { class: "tile-note" }, REPORT_HELP[r.name] || ""),
          h("div", { class: "row b-report-dl" },
            h("a", { class: "btn btn-ghost btn-sm", href: "#/reports/" + r.name, "aria-label": "Open " + r.title }, "Open"),
            dl("xlsx", "Excel", "Download as an Excel workbook"), dl("pdf", "PDF", "Download as a PDF"), dl("csv", "CSV", "Download as a CSV file")));
      })));
    }, failed(main, alive, "Reports"));
  } });

  DQ.route("#/reports/:name", { title: "Report", can: "reports.view", render: function (main, params, query, alive) {
    var to = /^\d{4}-\d{2}-\d{2}$/.test(query.to || "") ? query.to : todayIst();
    var from = /^\d{4}-\d{2}-\d{2}$/.test(query.from || "") ? query.from : addDays(to, -27);
    var storeSel = query.store === "all" ? "all" : (query.store && storeById(query.store) ? query.store : (DQ.store() || "all"));
    var apiStore = storeSel === "all" ? null : storeSel;
    var qsBase = { store_id: apiStore, from: from, to: to };
    loading(main, "Report");
    return DQ.get("/api/reports/" + encodeURIComponent(params.name) + DQ.qs(qsBase)).then(function (rep) {
      if (!alive()) return;
      DQ.clear(main);
      document.title = rep.title + " · DQ-Stack";
      var where = apiStore ? (storeById(apiStore) || {}).name : "All stores";
      main.appendChild(ui.page(rep.title, {
        sub: h("span", null, where + " · " + fmt.date(rep.from) + " to " + fmt.date(rep.to) + " · ", fresh("Made at", nowIso())),
        actions: [h("a", { class: "btn btn-ghost", href: "#/reports" }, "All reports"),
          ui.button("Excel", function (e) { DQ.download("/api/reports/" + params.name + DQ.qs(Object.assign({ format: "xlsx" }, qsBase)), e.currentTarget); }, "ghost", { title: "Download as an Excel workbook" }),
          ui.button("PDF", function (e) { DQ.download("/api/reports/" + params.name + DQ.qs(Object.assign({ format: "pdf" }, qsBase)), e.currentTarget); }, "ghost", { title: "Download as a PDF" }),
          ui.button("CSV", function (e) { DQ.download("/api/reports/" + params.name + DQ.qs(Object.assign({ format: "csv" }, qsBase)), e.currentTarget); }, "ghost", { title: "Download as a CSV file" })]
      }));
      var f = ui.input({ type: "date", value: from, max: todayIst() });
      var t = ui.input({ type: "date", value: to, max: todayIst() });
      var s = ui.select([{ value: "all", label: "All stores" }].concat(DQ.stores().map(function (x) { return { value: x.id, label: x.name }; })), storeSel);
      var ferr = h("p", { class: "field-error", role: "alert", hidden: true });
      function quick(days) { return ui.button("Last " + days + " days", function () { DQ.go("#/reports/" + params.name + DQ.qs({ from: addDays(todayIst(), -(days - 1)), to: todayIst(), store: storeSel })); }, "ghost", { class: "btn btn-ghost btn-sm" }); }
      main.appendChild(h("form", { class: "b-filters", onsubmit: function (e) {
        e.preventDefault();
        if (!f.value || !t.value || f.value > t.value) { ferr.textContent = "Choose a start date on or before the end date."; ferr.hidden = false; return; }
        DQ.go("#/reports/" + params.name + DQ.qs({ from: f.value, to: t.value, store: s.value }));
      } }, ui.field("From", f), ui.field("To", t), ui.field("Store", s),
        h("div", { class: "b-filter-btns" }, h("button", { class: "btn btn-primary", type: "submit" }, "Show")), ferr));
      main.appendChild(h("div", { class: "row b-quick" }, quick(7), quick(28), quick(90)));
      main.appendChild(reportTable(rep));
      (rep.notes || []).forEach(function (n) { main.appendChild(h("p", { class: "text-2 b-note" }, n)); });
    }, failed(main, alive, "Report"));
  } });

  function reportTable(rep) {
    var cols = rep.columns || [], rows = rep.rows || [];
    if (!rows.length) return ui.empty({ title: "Nothing in this period", text: "No records fall between these dates for this choice of store. Try a longer period or all stores." });
    var LIMIT = 1000;
    function cell(r, c) {
      var v = r[c.key];
      if (c.kind === "qty") return ui.cell(v, "qty", r.unit || null);
      if (c.kind === "num" && typeof v === "number" && !Number.isInteger(v)) return v.toFixed(1);
      return ui.cell(v, c.kind);
    }
    var isNum = function (c) { return c.kind && c.kind !== "text"; };
    var thead = h("thead", null, h("tr", null, cols.map(function (c) { return h("th", { scope: "col", class: isNum(c) ? "num" : null }, c.label || h("span", { class: "sr-only" }, c.key)); })));
    var tbody = h("tbody", null, rows.slice(0, LIMIT).map(function (r) {
      return h("tr", null, cols.map(function (c) { return h("td", { class: isNum(c) ? "num" : null }, cell(r, c)); }));
    }));
    var tfoot = rep.totals && Object.keys(rep.totals).length ? h("tfoot", null, h("tr", { class: "b-total" }, cols.map(function (c, i) {
      var v = rep.totals[c.key];
      return h(i === 0 ? "th" : "td", { scope: i === 0 ? "row" : null, class: isNum(c) ? "num" : null }, i === 0 && v === undefined ? "Total" : (v === undefined ? "" : cell(rep.totals, c)));
    }))) : null;
    return h("div", null,
      h("div", { class: "table-wrap" }, h("table", { class: "table" }, h("caption", { class: "sr-only" }, rep.title), thead, tbody, tfoot)),
      h("p", { class: "text-3 b-foot" }, rows.length > LIMIT ? "Showing the first " + fmt.num(LIMIT) + " of " + fmt.num(rows.length) + " rows. Download Excel for all of them." : plural(rows.length, "row") + "."));
  }

  /* ================================================================ Model performance */
  var RUN_STATUS = { succeeded: ["Done", "ok"], failed: ["Failed", "risk"], running: ["Running", "data"], queued: ["Waiting", "data"] };
  var MODEL_STATUS = { active: ["In use", "ok"], retired: ["Retired", "neutral"], rejected: ["Not used", "attention"], training: ["Training", "data"] };
  function tierName(t) { return t === "engine" ? "Full forecast" : t === "starter" ? "Starter estimate" : cap(t || "—"); }

  function backtestWords(bt) {
    if (!bt) return [h("p", { class: "text-2" }, "No test results yet.")];
    if (bt.early) return [h("p", { class: "text-2" }, "This is the starter forecast, which learns as sales arrive. A full test runs once there is enough sales history.")];
    var out = [];
    function li(text, tip) { return h("li", { title: tip || null }, text); }
    var items = [];
    var weeks = bt.weeks ? plural(bt.weeks, "week") : "recent weeks";
    if (bt.wape !== null && bt.wape !== undefined) items.push(li("Tested on the last " + weeks + " it had not seen, the forecast was off by " + fmt.pct(bt.wape) + " of the units sold" +
      (bt.naive_wape ? "; simply repeating last year's pattern was off by " + fmt.pct(bt.naive_wape) + (bt.better_than_naive_pct !== undefined && bt.better_than_naive_pct !== null ? ", so this is " + bt.better_than_naive_pct + "% closer" : "") : "") + ".", "Weighted absolute percentage error (WAPE) against a seasonal naive forecast"));
    if (bt.inside_range !== null && bt.inside_range !== undefined) items.push(li("What actually sold fell inside the forecast's likely range " + fmt.pct(bt.inside_range) + " of the time.", "Interval coverage"));
    if (bt.fill_rate !== null && bt.fill_rate !== undefined) items.push(li("Stocking to its targets would have met " + fmt.pct(bt.fill_rate) + " of what customers wanted.", "Fill rate"));
    if (bt.bias !== null && bt.bias !== undefined) items.push(li(Math.abs(bt.bias) < 0.02 ? "It forecast neither too high nor too low on average." : "On average it forecast " + fmt.pct(Math.abs(bt.bias)) + " too " + (bt.bias < 0 ? "low" : "high") + ".", "Bias"));
    if (bt.margin !== null && bt.margin !== undefined) items.push(li("Ordering by it would have earned " + rupeesFromRupees(bt.margin, { whole: true }) + " in margin over those weeks, after the cost of waste and missed sales.", "Realised margin in the backtest"));
    out.push(h("ul", { class: "b-words" }, items));
    var pv = bt.profit_vs_blanket;
    if (pv && pv.targets !== null && pv.blanket_95 !== null) {
      var diff = pv.targets - pv.blanket_95;
      out.push(h("div", { class: "b-compare" },
        h("h3", { class: "title-3" }, "Profit against one 95% service level"),
        h("p", { class: "text-2", title: "Newsvendor stock targets from each product's own costs, against a blanket 95% service level for every product" },
          "Stock targets set from each product's own costs earned " + rupeesFromRupees(pv.targets, { whole: true }) + " on the same weeks, against " + rupeesFromRupees(pv.blanket_95, { whole: true }) +
          " if every product were stocked to cover demand 95% of the time: " + rupeesFromRupees(Math.abs(diff), { whole: true }) + (diff >= 0 ? " more." : " less.")),
        compareBars(pv.targets, pv.blanket_95)));
    }
    return out;
  }
  function compareBars(a, b) {
    var m = Math.max(a, b, 1);
    function bar(label, v, cls) {
      return h("div", { class: "b-cbar" }, h("span", { class: "b-cbar-label" }, label),
        h("span", { class: "b-cbar-track" }, h("span", { class: "b-cbar-fill " + cls, style: { width: (100 * Math.max(v, 0) / m).toFixed(1) + "%" } })),
        h("span", { class: "mono b-cbar-value" }, rupeesFromRupees(v, { whole: true, short: true })));
    }
    return h("div", { class: "b-cbars", role: "img", "aria-label": "Own-cost targets " + rupeesFromRupees(a, { whole: true }) + ", blanket 95% " + rupeesFromRupees(b, { whole: true }) },
      bar("Own-cost targets", a, "b-fill-forecast"), bar("One 95% level", b, "b-fill-prior"));
  }

  function monitorChart(days) {
    var W = 820, H = 240, L = 48, R = 14, T = 16, B = 36;
    var n = days.length;
    function X(i) { return L + (W - L - R) * (n > 1 ? i / (n - 1) : 0.5); }
    function Y(v) { return T + (H - T - B) * (1 - Math.min(Math.max(v, 0), 1)); }
    var g = [];
    [0, 0.25, 0.5, 0.75, 1].forEach(function (v) {
      g.push(sv("line", { x1: L, y1: Y(v), x2: W - R, y2: Y(v), class: v ? "b-grid" : "b-axis" }));
      g.push(sv("text", { x: L - 6, y: Y(v) + 4, class: "b-tick", "text-anchor": "end", text: Math.round(v * 100) + "%" }));
    });
    var step = Math.max(1, Math.ceil(n / 7));
    days.forEach(function (d, i) { if (i % step === 0 || (i === n - 1 && i % step >= step / 2)) g.push(sv("text", { x: X(i), y: H - B + 16, class: "b-tick", "text-anchor": "middle", text: fmt.dayShort(d.day) })); });
    function line(key, cls, marker) {
      var p = [];
      days.forEach(function (d, i) { if (d[key] !== null && d[key] !== undefined) p.push([X(i), Y(d[key])]); });
      if (!p.length) return;
      g.push(sv("polyline", { points: pts(p), class: cls }));
      p.forEach(function (q) { g.push(marker === "square" ? sv("rect", { x: r1(q[0] - 3), y: r1(q[1] - 3), width: 6, height: 6, class: cls + "-dot" }) : sv("circle", { cx: r1(q[0]), cy: r1(q[1]), r: 3, class: cls + "-dot" })); });
    }
    line("wape", "b-line-risk", "circle");
    line("inside_range", "b-line-forecast", "square");
    var valid = days.filter(function (d) { return d.wape !== null; });
    var avgErr = valid.length ? valid.reduce(function (a, d) { return a + d.wape; }, 0) / valid.length : null;
    var vin = days.filter(function (d) { return d.inside_range !== null; });
    var avgIn = vin.length ? vin.reduce(function (a, d) { return a + d.inside_range; }, 0) / vin.length : null;
    var summary = "Over " + plural(n, "day") + ", the forecast was off by " + fmt.pct(avgErr) + " of units sold on an average day, and sales fell inside the likely range " + fmt.pct(avgIn) + " of the time.";
    return { svg: sv("svg", { viewBox: "0 0 " + W + " " + H, class: "b-chart", role: "img", "aria-label": "Daily forecast error and share inside the range. " + summary }, sv("title", { text: "Forecast error and inside range share by day" }), g), summary: summary };
  }

  DQ.route("#/model", { title: "Model performance", nav: "Model performance", can: "reports.view", render: function (main, params, query, alive) {
    loading(main, "Model performance");
    return Promise.all([
      DQ.get("/api/forecast/status"), DQ.get("/api/forecast/models"), DQ.get("/api/forecast/runs?limit=20"), DQ.get("/api/forecast/monitoring?days=28")
    ]).then(function (res) {
      if (!alive()) return;
      var st = res[0], models = res[1], runs = res[2], mon = res[3];
      DQ.clear(main);
      var admin = DQ.can("company.settings");
      var busy = runs.some(function (r) { return r.status === "queued" || r.status === "running"; });
      var actions = [h("a", { class: "btn btn-ghost", href: "/planner/", title: "The analysts' planner workspace, with every chart and the full evidence" }, "Open the planner workspace")];
      function queue(kind, btn) {
        setBusy(btn, true, kind === "train" ? "Training…" : "Forecasting…");
        DQ.post("/api/forecast/run", { kind: kind }).then(function (r) {
          DQ.toast(kind === "train" ? (r.status === "succeeded" ? "Training finished: " + (r.message || "done") : "Training started. It takes a few minutes; this page shows it under recent runs.") :
            (r.status === "succeeded" ? "The forecast is refreshed. " + (r.message || "") : "The forecast refresh has started."));
          if (alive()) DQ.render();
        }, function (err) { setBusy(btn, false); DQ.toast(err.message, { kind: "risk" }); if (alive()) DQ.render(); });
      }
      if (admin) {
        actions.push(ui.button("Refresh the forecast now", function (e) { queue("nightly", e.target); }, "ghost", { disabled: busy, title: "Forecasts again with the current model and the latest sales and stock" }));
        actions.push(ui.button("Retrain now", function (e) {
          var b = e.target;
          DQ.confirm({ title: "Retrain the model now?", message: "Training takes several minutes. The new model is used only if it would have earned more than the current one on recent weeks.", confirmLabel: "Retrain now" }).then(function (r) { if (r.ok) queue("train", b); });
        }, "primary", { disabled: busy }));
      }
      var cur = st.current, model = st.model;
      main.appendChild(ui.page("Model performance", {
        sub: h("span", null, cur ? "Forecast from data to " + fmt.dayShort(cur.data_cutoff) + " · " : "", cur ? fresh("made at", cur.finished_at) : "No forecast yet", st.stale ? h("span", null, " · ", ui.badge("Older than 36 hours", "attention")) : null),
        actions: actions
      }));
      if (admin) main.appendChild(h("p", { class: "text-3 b-note" }, busy ? "A forecast is running now; the buttons come back when it finishes." : "Refreshing takes under a minute. Retraining takes several minutes, and the forecast keeps working meanwhile."));

      var act = section("The model in use");
      if (!model) {
        act.appendChild(ui.empty({ title: "No model yet", text: st.days_needed ? "The forecast needs " + plural(st.days_needed, "more day") + " of sales before the first model. It has " + plural(st.history_days, "day") + " so far." : "The first model trains once sales are uploaded.", action: { label: "See what the forecast needs", href: "#/setup/forecast" } }));
      } else {
        act.appendChild(h("dl", { class: "b-facts b-facts-wide" },
          fact("Version", model.version), fact("Kind", tierName(model.tier)), fact("Learned from data to", fmt.date(model.data_cutoff)),
          fact("Sales history", plural(st.history_days, "day"))));
        act.appendChild(h("h3", { class: "title-3 b-sub-head" }, "How it did when tested"));
        backtestWords(model.backtest).forEach(function (n) { act.appendChild(n); });
      }
      main.appendChild(act);

      var mv = section("Every model version", { sub: "A new model is used only if it would have earned more margin than the one in use, on the same recent weeks." });
      mv.appendChild(ui.table([
        { key: "version", label: "Version", render: function (m) { return h("span", { class: "mono" }, m.version); } },
        { key: "status", label: "Status", render: function (m) { var x = MODEL_STATUS[m.status] || [cap(m.status), "neutral"]; return ui.badge(x[0], x[1]); } },
        { key: "tier", label: "Kind", render: function (m) { return tierName(m.tier); } },
        { key: "data_cutoff", label: "Data to", kind: "date" },
        { key: "created_at", label: "Trained", render: function (m) { return tDateTime(m.created_at); } },
        { key: "train_seconds", label: "Took", kind: "num", render: function (m) { return m.train_seconds ? (m.train_seconds >= 90 ? Math.round(m.train_seconds / 60) + " min" : Math.round(m.train_seconds) + " s") : "—"; } },
        { key: "promotion", label: "Decision", render: function (m) {
          var p = m.promotion;
          if (!p) return "—";
          var txt = p.decision === "promoted" ? "Put in use" : "Kept the current model";
          var detail = p.current_margin !== null && p.current_margin !== undefined ?
            "New earned " + rupeesFromRupees(p.new_margin, { whole: true, short: true }) + " against " + rupeesFromRupees(p.current_margin, { whole: true, short: true }) + " for " + (p.current_version || "the current one") :
            (p.new_margin !== null && p.new_margin !== undefined ? "First model; earned " + rupeesFromRupees(p.new_margin, { whole: true, short: true }) + " in the test" : "");
          return h("span", null, txt, detail ? h("div", { class: "text-3 b-small" }, detail + (p.origins ? " over " + plural(p.origins, "week") : "")) : null);
        } }
      ], models, { caption: "Model versions", empty: { title: "No model versions yet", text: "The first one appears after the first training." } }));
      main.appendChild(mv);

      var rr = section("Recent runs", { sub: "Nightly forecasts and training, failures included." });
      rr.appendChild(ui.table([
        { key: "started_at", label: "Started", render: function (r) { return r.started_at ? tDateTime(r.started_at) : "Not yet"; } },
        { key: "kind", label: "What", render: function (r) { return r.kind === "train" ? "Training" : "Forecast"; } },
        { key: "trigger", label: "Why", render: function (r) { return { manual: "Started by hand", nightly: "Nightly", schedule: "Nightly", onboarding: "First setup", after_training: "After training" }[r.trigger] || cap(String(r.trigger || "").replace(/_/g, " ")); } },
        { key: "status", label: "Result", render: function (r) { var x = RUN_STATUS[r.status] || [cap(r.status), "neutral"]; return ui.badge(x[0], x[1]); } },
        { key: "data_cutoff", label: "Data to", kind: "date" },
        { key: "message", label: "Message", render: function (r) { return h("span", { class: r.status === "failed" ? "b-fail-msg" : null }, r.message || "—"); } }
      ], runs, { caption: "Recent forecast runs", empty: { title: "No runs yet" } }));
      main.appendChild(rr);

      var mo = section("Checked against sales, last 28 days", { sub: "Each night yesterday's forecasts are scored against what sold." });
      if (!mon.by_day.length) {
        mo.appendChild(ui.empty({ title: "No scores yet", text: "Scores appear after the first night with sales after a forecast. Upload or ring up the day's sales and check back tomorrow." }));
      } else {
        var mc = monitorChart(mon.by_day);
        mo.appendChild(h("div", { class: "b-chart-wrap" }, mc.svg));
        mo.appendChild(legend([["b-sw-risk", "Error: share of units the forecast was off by"], ["b-sw-forecast b-sw-square", "Inside range: share of sales inside the likely range"]]));
        mo.appendChild(h("p", { class: "text-2" }, mc.summary));
        mo.appendChild(h("h3", { class: "title-3 b-sub-head" }, "By category"));
        mo.appendChild(ui.table([
          { key: "category", label: "Category", render: function (c) { return c.category || "No category"; } },
          { key: "actual_milli", label: "Sold", kind: "qty" },
          { key: "forecast_milli", label: "Forecast", kind: "qty" },
          { key: "wape", label: "Off by", kind: "pct", render: function (c) { return h("span", { title: "WAPE" }, fmt.pct(c.wape)); } },
          { key: "bias", label: "Leaning", render: function (c) { return c.bias === null ? "—" : Math.abs(c.bias) < 0.02 ? "Even" : fmt.pct(Math.abs(c.bias)) + (c.bias < 0 ? " low" : " high"); } },
          { key: "inside_range", label: "Inside range", kind: "pct", render: function (c) { return fmt.pct(c.inside_range); } }
        ], mon.by_category, { caption: "Forecast scores by category" }));
      }
      main.appendChild(mo);
      main.appendChild(h("p", { class: "text-2 b-note" }, "For every chart and the full evidence behind these numbers, open the ", h("a", { href: "/planner/" }, "planner workspace"), "."));
    }, failed(main, alive, "Model performance"));
  } });
})();
