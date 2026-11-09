/* Screens, group a: Home, the setup checklist (with readiness and the first forecast),
   Settings, Imports, Alerts and the Activity log. See js/core.js for the helpers and
   js/start.js for the frame and router. Every screen builds its DOM with DQ.h. */
(function () {
  "use strict";
  var DQ = window.DQ, h = DQ.h, fmt = DQ.fmt, ui = DQ.ui;

  /* ================================================================ helpers */

  /* The server sends some times without a zone; they are UTC. Read them as UTC so the
     IST shown does not depend on the zone the browser happens to be set to. */
  function utc(iso) {
    if (!iso) return iso;
    return /(Z|[+-]\d\d:?\d\d)$/.test(iso) ? iso : iso + "Z";
  }
  function ist(iso) { return new Date(Date.parse(utc(iso)) + 330 * 60000).toISOString(); }
  function when(iso) { if (!iso) return "—"; var s = ist(iso); return fmt.date(s.slice(0, 10)) + ", " + s.slice(11, 16) + " IST"; }
  function clock(iso) { return iso ? ist(iso).slice(11, 16) : "—"; }
  function ago(iso) { return iso ? fmt.ago(utc(iso)) : "never"; }
  function todayIST() { return ist(new Date().toISOString()).slice(0, 10); }
  function addDays(day, n) { var d = new Date(day + "T00:00:00Z"); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); }
  function daysFrom(a, b) { return Math.round((Date.parse(b + "T00:00:00Z") - Date.parse(a + "T00:00:00Z")) / 86400000); }
  function plural(n, one, many) { return fmt.num(n) + " " + (n === 1 ? one : (many || one + "s")); }
  function cap(s) { s = String(s || ""); return s.charAt(0).toUpperCase() + s.slice(1); }
  function settle(p) {
    return p.then(function (v) { return { ok: true, value: v }; }, function (e) { return { ok: false, error: e }; });
  }
  function asOf(iso) { return h("span", { class: "fresh", title: when(iso) }, "as of " + clock(iso) + " IST"); }
  function isAdmin() { return DQ.can("company.settings"); }
  function role() { return DQ.me && DQ.me.person ? DQ.me.person.role : null; }
  function storeName(id, list) {
    var s = (list || DQ.stores()).filter(function (x) { return x.id === id; })[0];
    return s ? s.name : "Another store";
  }
  /* Menu highlight for sub pages (#/settings/stores lights up Settings). */
  function markNav(pattern) {
    var a = document.querySelector('.sidebar a.nav-item[href="' + pattern + '"]');
    if (a) a.setAttribute("aria-current", "page");
  }
  /* The checklist links sales history as #/imports/new/sales; the kind is sales_history. */
  var KIND_ALIAS = { sales: "sales_history" };
  function stepRoute(r) {
    var m = /^#\/imports\/new\/(\w+)$/.exec(r || "");
    return m && KIND_ALIAS[m[1]] ? "#/imports/new/" + KIND_ALIAS[m[1]] : r;
  }
  function sectionCard(title, children, opts) {
    opts = opts || {};
    var id = "a-h-" + Math.random().toString(36).slice(2, 8);
    return h("section", { class: "card " + (opts.cls || ""), "aria-labelledby": id },
      h("div", { class: "a-card-head" },
        h("h2", { id: id, class: "title-3" }, title),
        opts.aside || null),
      opts.sub ? h("p", { class: "text-2 a-card-sub" }, opts.sub) : null,
      children);
  }
  function notice(kind, children) { return h("div", { class: "a-notice", "data-kind": kind, role: kind === "risk" ? "alert" : null }, children); }
  function progress(done, total, label) {
    var pct = total ? Math.round(100 * done / total) : 0;
    return h("div", { class: "a-progress", role: "progressbar", "aria-valuemin": 0, "aria-valuemax": total, "aria-valuenow": done, "aria-label": label || "Progress" },
      h("span", { style: { width: pct + "%" } }));
  }
  function meter(value, label) {
    var v = Math.max(0, Math.min(100, value || 0));
    return h("div", { class: "a-meter" },
      h("div", { class: "a-meter-top" }, h("span", null, label), h("span", { class: "num" }, v + " of 100")),
      h("div", { class: "a-progress", role: "img", "aria-label": label + ": " + v + " of 100", "data-kind": v >= 80 ? "ok" : v >= 50 ? "attention" : "risk" },
        h("span", { style: { width: v + "%" } })));
  }
  function copyBox(label, text) {
    var input = ui.input({ readonly: true, value: text });
    var btn = ui.button("Copy", function () {
      function done() { btn.textContent = "Copied"; setTimeout(function () { btn.textContent = "Copy"; }, 2000); }
      if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, fallback);
      else fallback();
      function fallback() {
        input.select();
        try { document.execCommand("copy"); done(); } catch (e) { DQ.toast("Select the text and copy it by hand.", { kind: "attention" }); }
      }
    }, "ghost");
    input.addEventListener("focus", function () { input.select(); });
    var f = ui.field(label, input);
    f.appendChild(h("div", { class: "row" }, btn));
    return f;
  }
  function checkGroup(legend, options, selected, help) {
    var sel = (selected || []).map(String);
    var boxes = options.map(function (o) {
      var cb = h("input", { type: "checkbox", value: String(o.value) });
      cb.checked = sel.indexOf(String(o.value)) >= 0;
      if (o.disabled) cb.disabled = true;
      return { cb: cb, el: h("label", { class: "a-check" }, cb, h("span", null, o.label)) };
    });
    var fs = h("fieldset", { class: "a-fieldset" }, h("legend", null, legend),
      help ? h("p", { class: "field-help" }, help) : null,
      h("div", { class: "a-checks" }, boxes.map(function (b) { return b.el; })));
    return { el: fs, values: function () { return boxes.filter(function (b) { return b.cb.checked; }).map(function (b) { return b.cb.value; }); },
      set: function (vals) { vals = vals.map(String); boxes.forEach(function (b) { b.cb.checked = vals.indexOf(b.cb.value) >= 0; }); } };
  }
  function checkbox(label, checked, help) {
    var cb = h("input", { type: "checkbox" });
    cb.checked = !!checked;
    var wrap = h("div", { class: "field" }, h("label", { class: "a-check" }, cb, h("span", null, label)), help ? h("p", { class: "field-help" }, help) : null);
    return { el: wrap, input: cb };
  }
  /* A form that checks required boxes, disables its button while saving and shows the
     server's sentence when something goes wrong. onSubmit returns a promise. */
  function form(children, submitLabel, onSubmit, opts) {
    opts = opts || {};
    var err = h("div", { class: "form-error", role: "alert", hidden: true });
    var btn = h("button", { class: "btn btn-primary", type: "submit" }, submitLabel);
    var f = h("form", { class: "stack a-form", novalidate: true }, children, err,
      h("div", { class: "row" }, btn, opts.cancel ? ui.button("Cancel", opts.cancel, "ghost") : null, opts.extra || null));
    if (opts.disabled) { btn.disabled = true; Array.prototype.forEach.call(f.querySelectorAll("input, select, textarea"), function (i) { i.disabled = true; }); }
    f.addEventListener("submit", function (e) {
      e.preventDefault();
      var bad = Array.prototype.filter.call(f.querySelectorAll("[required]"), function (i) { return !i.disabled && !String(i.value).trim(); });
      if (bad.length) {
        var lab = f.querySelector('label[for="' + bad[0].id + '"]');
        err.textContent = "Fill in " + (lab ? lab.textContent.toLowerCase() : "every box") + ".";
        err.hidden = false; bad[0].focus(); return;
      }
      var old = btn.textContent;
      btn.disabled = true; btn.textContent = opts.busyLabel || "Saving…"; err.hidden = true;
      Promise.resolve().then(onSubmit).then(function () {
        btn.disabled = false; btn.textContent = old;
      }, function (e2) {
        btn.disabled = false; btn.textContent = old;
        err.textContent = (e2 && e2.message ? e2.message : String(e2)) + (e2 && e2.problems && e2.problems.length ? " " + e2.problems.join("; ") : "");
        err.hidden = false;
      });
    });
    return f;
  }
  function busy(btn, label, promise) {
    var old = btn.textContent;
    btn.disabled = true; btn.textContent = label;
    return promise.then(function (v) { btn.disabled = false; btn.textContent = old; return v; },
      function (e) { btn.disabled = false; btn.textContent = old; DQ.toast(e.message, { kind: "risk" }); throw e; });
  }
  function swallow() { /* already shown to the person */ }
  function refreshMe() {
    return DQ.get("/api/auth/me").then(function (me) { DQ.me = me; DQ.render(); });
  }
  function downloadText(name, text, type) {
    var blob = new Blob(["﻿" + text], { type: type || "text/csv;charset=utf-8" });
    var url = URL.createObjectURL(blob);
    var a = h("a", { href: url, download: name, hidden: true });
    document.body.appendChild(a); a.click();
    setTimeout(function () { URL.revokeObjectURL(url); a.remove(); }, 1000);
  }
  function csvCell(v) {
    if (v === null || v === undefined) return "";
    var s = typeof v === "string" ? v : JSON.stringify(v);
    return /[",\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }

  var DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
  var MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  var STATES = ["Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh", "Goa", "Gujarat",
    "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka", "Kerala", "Madhya Pradesh", "Maharashtra",
    "Manipur", "Meghalaya", "Mizoram", "Nagaland", "Odisha", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu",
    "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal", "Delhi", "Jammu and Kashmir",
    "Ladakh", "Puducherry", "Chandigarh", "Andaman and Nicobar Islands",
    "Dadra and Nagar Haveli and Daman and Diu", "Lakshadweep"];
  var ROLES = [
    { value: "owner", text: "Everything, including billing and support access." },
    { value: "admin", text: "Everything except billing, in every store." },
    { value: "store_manager", text: "Runs their stores: approves orders and count differences, refunds, day close." },
    { value: "purchaser", text: "Makes orders and looks after suppliers for their stores." },
    { value: "stock_clerk", text: "Receives deliveries, counts, moves and writes off stock." },
    { value: "cashier", text: "Bills customers at the till." },
    { value: "viewer", text: "Looks at stock and reports; changes nothing." }
  ];
  function roleName(r) { return DQ.roleName ? DQ.roleName(r) : r; }

  /* Forecast accuracy in a shop owner's words. */
  function inTen(x) {
    var n = Math.round(10 * x);
    return n >= 10 && x < 0.995 ? Math.round(100 * x) + " times in 100" : n + " times in 10";
  }
  function accuracyLines(bt) {
    if (!bt) return [];
    var head = [];
    if (bt.wape !== null && bt.wape !== undefined) head.push("Off by " + Math.round(100 * bt.wape) + "% on average over the last " + plural(bt.weeks || 0, "week"));
    if (bt.inside_range !== null && bt.inside_range !== undefined) head.push("inside its range " + inTen(bt.inside_range));
    var out = [];
    if (head.length) out.push(cap(head.join("; ")) + ".");
    if (typeof bt.better_than_naive_pct === "number") {
      out.push(bt.better_than_naive_pct > 0 ?
        Math.round(bt.better_than_naive_pct) + "% closer than simply repeating recent sales." :
        "Not yet closer than simply repeating recent sales; it improves as more history arrives.");
    }
    if (typeof bt.fill_rate === "number") out.push("Following its advice, shelves would have met " + Math.round(100 * bt.fill_rate) + "% of what customers wanted.");
    return out;
  }
  function accuracyBlock(bt, noHead) {
    var lines = accuracyLines(bt);
    if (!lines.length) return null;
    return h("div", { class: "a-accuracy" },
      noHead ? null : h("h3", { class: "title-3" }, "How accurate it has been"),
      h("ul", { class: "a-plain" }, lines.map(function (l) { return h("li", null, l); })),
      h("p", { class: "field-help" }, "Tested on your own last " + plural(bt.weeks || 0, "week") +
        " of sales, which it had not seen while learning. The range is the low to high estimate shown with each forecast."));
  }

  /* ================================================================ Home */

  function setupBanner(s) {
    var next = s.steps.filter(function (x) { return x.state === "todo" || x.state === "in_progress"; })[0];
    return h("section", { class: "card a-banner", "aria-labelledby": "a-setup-h" },
      h("div", { class: "a-banner-text" },
        h("h2", { id: "a-setup-h", class: "title-3" }, "Setup: " + s.done + " of " + s.total + " steps done"),
        progress(s.done, s.total, "Setup progress"),
        next ? h("p", { class: "text-2" }, "Next: " + next.title + (next.minutes ? ", about " + next.minutes + " min." : ".")) : null),
      h("div", { class: "row" },
        h("a", { class: "btn btn-primary", href: next ? stepRoute(next.route) : "#/setup" }, "Continue setup"),
        h("a", { class: "btn btn-ghost", href: "#/setup" }, "See the checklist")));
  }

  function forecastCard(res, admin) {
    if (!res) return null;
    if (!res.ok) return sectionCard("Forecast", ui.error(res.error, DQ.render));
    var st = res.value, cur = st.current, latest = st.latest, kids = [];
    var running = latest && (latest.status === "queued" || latest.status === "running");
    if (!cur) {
      if (latest && latest.status === "failed") kids.push(notice("risk", "The last forecast attempt did not finish: " + (latest.message || "no reason given") + "."));
      if (running) {
        kids.push(h("p", null, "The first forecast is being made now. It usually takes a few minutes; come back to this page to see it."));
        kids.push(h("a", { class: "btn btn-ghost", href: admin ? "#/setup/forecast" : "#/home" }, "See progress"));
      } else if (st.days_needed > 0) {
        kids.push(h("p", null, "Forecasts start after 8 weeks of daily sales. " + plural(st.history_days, "day") +
          " of sales are loaded so far; " + plural(st.days_needed, "more day") + " needed."));
        if (DQ.can("imports.upload")) kids.push(h("a", { class: "btn btn-primary", href: "#/imports/new/sales_history" }, "Load sales history"));
      } else {
        kids.push(h("p", null, "There is enough sales history to start. " + (admin ?
          "Start the first forecast; it takes a few minutes and you are emailed when it is ready." :
          "An Owner or Admin starts the first forecast.")));
        if (admin) kids.push(h("a", { class: "btn btn-primary", href: "#/setup/forecast" }, "Start the first forecast"));
      }
      return sectionCard("No forecast yet", kids, { cls: "a-forecast" });
    }
    kids.push(h("p", { class: "a-big" }, "Forecast from data to " + fmt.dayShort(cur.data_cutoff)));
    kids.push(h("p", { class: "text-2" }, "Made " + when(cur.finished_at) + ", " + ago(cur.finished_at) + "."));
    if (st.stale) kids.push(notice("attention", "This forecast is more than 36 hours old, so it does not know about the latest sales. " +
      (admin ? "Check why last night's run did not finish." : "Your Owner or Admin has been told.")));
    if (latest && latest.id !== cur.id) {
      if (latest.status === "failed") kids.push(notice("risk", [
        "Last night's forecast did not run: " + (latest.message || "no reason given") + " The numbers shown are from the forecast made " + when(cur.finished_at) + ". ",
        admin ? h("a", { href: "#/model" }, "See what happened") : null]));
      else if (running) kids.push(notice("data", "A newer forecast is being made now. This page shows the current one until it is ready."));
    }
    kids.push(accuracyBlock(st.model && st.model.backtest));
    return sectionCard("Forecast", kids, { cls: "a-forecast", aside: st.stale ? ui.badge("Out of date", "attention") : ui.badge("Up to date", "ok") });
  }

  function tile(label, value, note, href, kind) {
    return h(href ? "a" : "div", { class: "tile a-tile", href: href || null, "data-kind": kind || null },
      h("div", { class: "tile-label" }, label),
      h("div", { class: "tile-value" }, value),
      note ? h("div", { class: "tile-note" }, note) : null);
  }
  function failTile(label, err, href) { return tile(label, "—", "Could not load: " + err.message, href, null); }

  function homeTiles(r, store) {
    var out = [], today = todayIST();
    if (r.today) {
      if (!r.today.ok) out.push(failTile("Decisions waiting today", r.today.error, "#/today"));
      else {
        var t = r.today.value;
        if (!t.run) out.push(tile("Decisions waiting today", "—", "No forecast yet, so nothing to decide.", "#/today"));
        else out.push(tile("Decisions waiting today", fmt.num(t.to_review),
          t.to_review ? "From the forecast to " + fmt.dayShort(t.run.data_cutoff) + "." : "All decided for today.",
          "#/today", t.to_review ? "attention" : "ok"));
      }
    }
    if (r.drafts) {
      if (!r.drafts.ok) out.push(failTile("Orders to approve", r.drafts.error, "#/orders"));
      else {
        var d = r.drafts.value, total = d.reduce(function (a, o) { return a + (o.total_paise || 0); }, 0);
        out.push(tile("Orders to approve", fmt.num(d.length),
          d.length ? "Draft orders worth " + fmt.rupees(total, { short: true, whole: true }) + "." : "No draft orders waiting.",
          "#/orders?status=draft", d.length ? "attention" : "ok"));
      }
    }
    if (r.stock) {
      if (!r.stock.ok) out.push(failTile("Stock to look at", r.stock.error, "#/stock"));
      else {
        var sc = r.stock.value.status_counts || {};
        var out_ = sc.Out || 0, low = sc.Low || 0, cnt = sc["Count needed"] || 0, exp = sc.Expiring || 0;
        var parts = [];
        if (out_) parts.push(out_ + " out");
        if (low) parts.push(low + " low");
        if (cnt) parts.push(cnt + " to count");
        if (exp) parts.push(exp + " expiring");
        var n = out_ + low + cnt + exp;
        out.push(tile("Stock to look at", fmt.num(n),
          [parts.length ? cap(parts.join(", ")) + ". " : "Everything is in range. ", asOf(r.stock.value.as_of)],
          n ? "#/stock" : "#/stock", out_ || cnt ? "risk" : n ? "attention" : "ok"));
      }
    }
    if (r.alerts) {
      if (!r.alerts.ok) out.push(failTile("Open alerts", r.alerts.error, "#/alerts"));
      else {
        var a = r.alerts.value;
        out.push(tile("Open alerts", fmt.num(a.open), a.risk ? plural(a.risk, "urgent alert") + "." : a.open ? "None urgent." : "Nothing needs attention.",
          "#/alerts", a.risk ? "risk" : a.open ? "attention" : "ok"));
      }
    }
    if (r.due) {
      if (!r.due.ok) out.push(failTile("Counts due", r.due.error, "#/count"));
      else out.push(tile("Counts due", fmt.num(r.due.value.total),
        r.due.value.total ? "Products due a count in this store." : "No counts due.", "#/count", r.due.value.total ? "attention" : "ok"));
    }
    if (r.incoming) {
      if (!r.incoming.ok) out.push(failTile("Deliveries expected", r.incoming.error, "#/receive"));
      else {
        var list = r.incoming.value;
        var late = list.filter(function (o) { return o.expected_date && o.expected_date < today; }).length;
        var next = list.map(function (o) { return o.expected_date; }).filter(function (x) { return x && x >= today; }).sort()[0];
        var note = late ? plural(late, "order") + " overdue." : next ? "Next expected " + (next === today ? "today" : fmt.dayShort(next)) + "." :
          list.length ? "No delivery date given." : "Nothing on the way.";
        out.push(tile("Deliveries expected", fmt.num(list.length), note, "#/receive", late ? "attention" : null));
      }
    }
    return h("section", { "aria-label": "Where things stand at " + store.name, class: "tiles a-tiles" }, out);
  }

  function freshnessCard(r, store) {
    var items = [];
    var st = r.status && r.status.ok ? r.status.value : null;
    var cut = st && st.current ? st.current.data_cutoff : null;
    if (cut) {
      var yesterday = addDays(todayIST(), -1), gap = daysFrom(cut, yesterday);
      items.push(h("li", null, "Sales in the forecast run to " + fmt.date(cut) + (gap <= 0 ? " (yesterday)." : ".")));
      if (gap > 0) {
        items.push(h("li", null, notice("attention", [
          plural(gap, "day") + " of sales after " + fmt.dayShort(cut) + " are not in the forecast yet. Suggestions are less reliable until they are. ",
          store.mode === "connected" ? h("a", { href: "#/sales" }, "Check the sales") :
            DQ.can("imports.upload") ? h("a", { href: "#/imports/new/daily_sales" }, "Upload the day's sales") : null])));
      }
    } else if (st) {
      items.push(h("li", null, plural(st.history_days || 0, "day") + " of sales history loaded."));
    }
    if (r.stock && r.stock.ok) items.push(h("li", null, "Stock figures for " + store.name + " are live, read " + clock(r.stock.value.as_of) + " IST."));
    if (st && st.model) items.push(h("li", null, "Model " + st.model.version + " learned from sales to " + fmt.date(st.model.data_cutoff) + "."));
    if (!items.length) return null;
    return sectionCard("How fresh the numbers are", h("ul", { class: "a-plain" }, items));
  }

  DQ.route("#/home", { title: "Home", nav: "Home", render: function (main, params, query, alive) {
    var store = DQ.storeObj(), admin = isAdmin();
    main.appendChild(ui.page("Home", { sub: store ? "Where things stand at " + store.name + " today." : "Where things stand today." }));
    var body = h("div", { class: "stack" }, ui.skeleton(6));
    main.appendChild(body);
    var sid = store && store.id;
    var want = {
      setup: admin ? DQ.get("/api/setup") : null,
      status: DQ.can("data.read") ? DQ.get("/api/forecast/status") : null,
      today: sid && DQ.can("forecast.review", sid) ? DQ.get("/api/today" + DQ.qs({ store_id: sid, include_decided: "false", limit: 500 })) : null,
      drafts: sid && (DQ.can("orders.approve", sid) || DQ.can("orders.create", sid)) ? DQ.get("/api/orders" + DQ.qs({ store_id: sid, status: "draft" })) : null,
      incoming: sid && (DQ.can("stock.receive", sid) || DQ.can("orders.create", sid)) ? DQ.get("/api/orders" + DQ.qs({ store_id: sid, status: "approved,sent,partly_received" })) : null,
      alerts: DQ.get("/api/alerts/count"),
      stock: sid ? DQ.get("/api/stock" + DQ.qs({ store_id: sid, limit: 1 })) : null,
      due: sid && DQ.can("stock.count", sid) ? DQ.get("/api/counts/due" + DQ.qs({ store_id: sid })) : null
    };
    var keys = Object.keys(want);
    return Promise.all(keys.map(function (k) { return want[k] ? settle(want[k]) : Promise.resolve(null); })).then(function (res) {
      if (!alive()) return;
      var r = {};
      keys.forEach(function (k, i) { r[k] = res[i]; });
      DQ.clear(body);
      if (r.setup && r.setup.ok && !r.setup.value.complete) body.appendChild(setupBanner(r.setup.value));
      if (!store) {
        body.appendChild(ui.empty({
          title: "No store yet",
          text: admin ? "Everything in DQ-Stack belongs to a store: stock, orders and forecasts. Add your first store to begin." :
            "You are not assigned to a store yet. Ask an Owner or Admin to add you to one.",
          action: admin ? { label: "Add a store", href: "#/settings/stores" } : null }));
        return;
      }
      [forecastCard(r.status, admin), homeTiles(r, store), freshnessCard(r, store)].forEach(function (x) { if (x) body.appendChild(x); });
    });
  } });

  /* ================================================================ Setup checklist */

  var STEP_STATE = { done: ["Done", "ok"], in_progress: ["In progress", "data"], todo: ["To do", "neutral"], later: ["Left for later", "neutral"] };
  var MARK = { done: "Mark as done", later: "Leave for later", unknown: "Start with stock unknown", none: "Start with no sales history" };
  var MARK_BY_KEY = { people: "Nobody else needs an account yet", calendar: "I have checked the calendar", rules: "The rules are right", readiness: "Mark as reviewed", go_live: "Go live" };
  var MARKED = { done: "marked as done", later: "left for later", unknown: "starting with stock unknown", none: "starting with no sales history" };
  /* Details the server gives a step that was marked by hand; a step done because its
     data is loaded cannot be reopened, so it gets no Reopen button. */
  var MANUAL_DETAILS = ["", "Nobody else yet", "Company defaults in use", "Starting with stock unknown", "No history: new store mode", "Skipped for now", "Live"];
  function markLabel(step, m) { return (m === "done" && MARK_BY_KEY[step.key]) || MARK[m] || cap(m); }

  DQ.route("#/setup", { title: "Setup checklist", nav: "Setup checklist", can: "company.settings", render: function (main, params, query, alive) {
    main.appendChild(ui.page("Setup checklist", {
      sub: "Thirteen steps from an empty company to the first forecast. Steps tick themselves once their data is loaded.",
      actions: [h("a", { class: "btn btn-ghost", href: "#/setup/readiness" }, "Readiness"), h("a", { class: "btn btn-ghost", href: "#/setup/forecast" }, "First forecast")] }));
    var body = h("div", null, ui.skeleton(8));
    main.appendChild(body);

    function load() {
      return DQ.get("/api/setup").then(function (s) { if (alive()) draw(s); }, function (err) {
        if (!alive()) return;
        DQ.clear(body); body.appendChild(ui.error(err, load));
      });
    }
    function mark(step, state, modes) {
      var payload = { state: state };
      if (modes) payload.modes = modes;
      return DQ.post("/api/setup/" + step.key, payload).then(function (s2) {
        if (alive()) draw(s2);
        return s2;
      });
    }
    function onMark(step, m, btn) {
      busy(btn, "Saving…", mark(step, m)).then(function () {
        DQ.toast(step.title + ": " + (MARKED[m] || m) + ".", { undo: function () { mark(step, "reopen").catch(function (e) { DQ.toast(e.message, { kind: "risk" }); }); } });
      }, swallow);
    }
    function onReopen(step, btn) {
      busy(btn, "Saving…", mark(step, "reopen")).then(function (s2) {
        var now = s2.steps.filter(function (x) { return x.key === step.key; })[0];
        if (now && now.state === step.state) DQ.toast("This step stays " + (STEP_STATE[now.state] || [now.state])[0].toLowerCase() + " because its data is loaded.", { kind: "attention" });
        else DQ.toast(step.title + " reopened.");
      }, swallow);
    }
    function goLivePanel(step, holder) {
      DQ.clear(holder);
      var stores = DQ.stores();
      var selects = stores.map(function (s) {
        return { id: s.id, sel: ui.select([
          { value: "standalone", label: "Standalone: upload or type each day's sales" },
          { value: "connected", label: "Connected: sales come from the tills or a billing system" }], s.mode || "standalone") };
      });
      holder.appendChild(h("div", { class: "a-inline-panel" },
        h("p", null, "Choose how each store sends its daily sales. You can change this later in Settings, Stores."),
        selects.map(function (x) { return ui.field(storeName(x.id, stores), x.sel); }),
        form([], "Go live", function () {
          var modes = {};
          selects.forEach(function (x) { modes[x.id] = x.sel.value; });
          return mark(step, "done", modes).then(function () {
            DQ.toast("You are live. Daily forecasts and suggestions now run every night.", { undo: function () { mark(step, "reopen").catch(swallow); } });
            refreshMe().catch(swallow);
          });
        }, { cancel: function () { DQ.clear(holder); } })));
    }
    function stepItem(st, isNext) {
      var b = STEP_STATE[st.state] || [st.state, "neutral"];
      var holder = h("div");
      var actions = h("div", { class: "a-step-actions" });
      actions.appendChild(h("a", { class: "btn btn-sm " + (isNext ? "btn-primary" : "btn-ghost"), href: stepRoute(st.route) },
        st.state === "done" ? "Open" : "Continue"));
      if (st.state === "todo" || st.state === "in_progress") {
        st.manual.forEach(function (m) {
          actions.appendChild(ui.button(markLabel(st, m), function (e) {
            if (st.key === "go_live") goLivePanel(st, holder); else onMark(st, m, e.currentTarget);
          }, "ghost", { class: "btn btn-ghost btn-sm" }));
        });
      } else if (st.manual.length && MANUAL_DETAILS.indexOf(st.detail || "") >= 0) {
        actions.appendChild(ui.button("Reopen", function (e) { onReopen(st, e.currentTarget); }, "ghost", { class: "btn btn-ghost btn-sm" }));
      }
      return h("li", { class: "a-step", "data-state": st.state },
        h("span", { class: "a-step-n", "aria-hidden": "true" }, st.state === "done" ? "✓" : String(st.n)),
        h("div", { class: "a-step-body" },
          h("div", { class: "a-step-title" },
            h("h3", { class: "title-3" }, h("span", { class: "sr-only" }, "Step " + st.n + ": "), st.title),
            ui.badge(b[0], b[1]), st.optional ? ui.badge("Optional", "neutral") : null),
          st.detail ? h("p", { class: "a-step-detail" }, st.detail) : null,
          h("p", { class: "text-2 a-step-what" }, st.what),
          h("p", { class: "field-help" }, st.minutes ? "About " + st.minutes + " min" : "Time depends on how much data you have"),
          actions, holder));
    }
    function draw(s) {
      DQ.clear(body);
      var nextKey = (s.steps.filter(function (x) { return x.state === "todo" || x.state === "in_progress"; })[0] || {}).key;
      body.appendChild(h("div", { class: "card a-setup-summary" },
        h("p", { class: "title-3" }, s.complete ? "Setup is complete." : s.done + " of " + s.total + " steps done"),
        progress(s.done, s.total, "Setup progress"),
        s.complete ? h("p", { class: "text-2" }, "Every step is done. The checklist leaves Home now; it stays here for reference.") : null));
      body.appendChild(h("ol", { class: "a-steps" }, s.steps.map(function (st) { return stepItem(st, st.key === nextKey); })));
    }
    return load();
  } });

  /* ---------------------------------------------------------------- readiness */
  DQ.route("#/setup/readiness", { title: "Readiness", can: "company.settings", render: function (main, params, query, alive) {
    markNav("#/setup");
    main.appendChild(ui.page("Readiness", { sub: "What is loaded, what is missing, and a data quality score for each store.",
      actions: [h("a", { class: "btn btn-ghost", href: "#/setup" }, "Back to the checklist")] }));
    var body = h("div", { class: "stack" }, ui.skeleton(6));
    main.appendChild(body);
    function load() {
      return DQ.get("/api/setup/readiness").then(function (r) {
        if (!alive()) return;
        DQ.clear(body);
        var loaded = sectionCard("What is loaded", h("dl", { class: "a-kv" },
          kv("Products", fmt.num(r.products)),
          kv("With price, cost and GST rate", fmt.num(r.products_priced) + " of " + fmt.num(r.products)),
          kv("With a barcode", fmt.num(r.products_with_barcode) + " of " + fmt.num(r.products)),
          kv("Upcoming calendar entries", fmt.num(r.upcoming_calendar_events)),
          kv("Products with sales, all stores", fmt.num(r.product_store_pairs))), { aside: asOf(new Date().toISOString()) });
        body.appendChild(loaded);
        body.appendChild(sectionCard("What is missing", r.missing.length ?
          h("ul", { class: "a-plain a-missing" }, r.missing.map(function (m) {
            return h("li", null, h("span", null, m.what), h("a", { class: "btn btn-ghost btn-sm", href: stepRoute(m.route) }, "Fix this"));
          })) : h("p", null, "Nothing important is missing.")));
        if (!r.stores.length) {
          body.appendChild(ui.empty({ title: "No stores yet", text: "Scores are worked out per store. Add a store first.", action: { label: "Add a store", href: "#/settings/stores" } }));
        } else {
          body.appendChild(h("div", { class: "grid-2 a-cards" }, r.stores.map(function (s) {
            var k = s.score >= 80 ? ["Ready", "ok"] : s.score >= 50 ? ["Usable, will improve", "attention"] : ["Thin data", "risk"];
            return sectionCard(s.name + " (" + s.code + ")", [
              h("p", { class: "a-score" }, h("span", { class: "num" }, String(s.score)), h("span", { class: "text-2" }, " out of 100")),
              meter(s.score_parts.history, "Sales history: " + s.weeks_of_sales + " weeks"),
              meter(s.score_parts.catalogue, "Products with price, cost and GST rate"),
              meter(s.score_parts.stock, "Stock known: " + fmt.num(s.stock_known) + " products"),
              h("p", { class: "field-help" }, fmt.num(s.products_with_sales) + " products have sales here. Daily mode: " + s.mode + ".")
            ], { aside: ui.badge(k[0], k[1]) });
          })));
          body.appendChild(h("p", { class: "field-help" }, "How the score is worked out: " + r.score_explained));
        }
        var est = r.first_forecast_minutes;
        body.appendChild(sectionCard("First forecast", [
          h("p", null, est ? "The first forecast should take about " + est.low + " to " + est.high + " minutes for " + fmt.num(r.product_store_pairs) + " products across your stores." :
            "There is no sales history yet, so the first forecast starts in new store mode."),
          h("div", { class: "row" },
            h("a", { class: "btn btn-primary", href: "#/setup/forecast" }, "Go to the first forecast"),
            ui.button("Mark this page as reviewed", function (e) {
              busy(e.currentTarget, "Saving…", DQ.post("/api/setup/readiness", { state: "done" })).then(function () {
                DQ.toast("Readiness marked as reviewed.", { undo: function () { DQ.post("/api/setup/readiness", { state: "reopen" }).catch(swallow); } });
              }, swallow);
            }, "ghost"))]));
      }, function (err) { if (!alive()) return; DQ.clear(body); body.appendChild(ui.error(err, load)); });
    }
    return load();
  } });
  function kv(k, v) { return h("div", { class: "a-kv-row" }, h("dt", null, k), h("dd", { class: "num" }, v)); }

  /* ---------------------------------------------------------------- first forecast */
  DQ.route("#/setup/forecast", { title: "First forecast", can: "company.settings", render: function (main, params, query, alive) {
    markNav("#/setup");
    main.appendChild(ui.page("First forecast", { sub: "The engine learns from your own sales, then tests itself on the weeks it did not see.",
      actions: [h("a", { class: "btn btn-ghost", href: "#/setup" }, "Back to the checklist")] }));
    var body = h("div", { class: "stack" }, ui.skeleton(5));
    main.appendChild(body);
    var timer = null, starting = false;
    function load() {
      return Promise.all([settle(DQ.get("/api/forecast/status")), settle(DQ.get("/api/setup/readiness"))]).then(function (res) {
        if (!alive()) return;
        DQ.clear(body);
        if (!res[0].ok) { body.appendChild(ui.error(res[0].error, load)); return; }
        draw(res[0].value, res[1].ok ? res[1].value : null);
      });
    }
    function draw(st, ready) {
      var latest = st.latest, running = latest && (latest.status === "queued" || latest.status === "running");
      var est = ready && ready.first_forecast_minutes;
      body.appendChild(sectionCard("Sales history", [
        h("p", null, plural(st.history_days || 0, "day") + " of sales are loaded (" + plural(Math.floor((st.history_days || 0) / 7), "week") + ")."),
        st.days_needed > 0 ? notice("attention", ["Forecasts start after 8 weeks of sales: " + plural(st.days_needed, "more day") + " needed. ",
          h("a", { href: "#/imports/new/sales_history" }, "Load sales history")]) :
          h("p", { class: "text-2" }, "That is enough to start. A full year lets it learn festivals; two to three years is best.")]));

      var kids = [];
      if (latest) {
        var kind = { succeeded: ["Finished", "ok"], failed: ["Did not finish", "risk"], queued: ["Waiting to start", "data"], running: ["Running", "data"] }[latest.status] || [latest.status, "neutral"];
        kids.push(h("div", { class: "row" }, ui.badge(kind[0], kind[1]),
          h("span", { class: "text-2" }, (latest.kind === "train" ? "Training" : "Nightly forecast") + ", started " + when(latest.started_at))));
        if (latest.message) kids.push(h("p", null, latest.message));
        if (latest.finished_at) kids.push(h("p", { class: "field-help" }, "Finished " + when(latest.finished_at) +
          (latest.timings && latest.timings.seconds ? ", took " + Math.max(1, Math.round(latest.timings.seconds)) + " seconds." : ".")));
      } else {
        kids.push(h("p", null, "No forecast has been made yet."));
      }
      if (isAdmin()) {
        var label = st.model ? "Train again now" : "Start the first forecast";
        var btn = ui.button(running ? "Running…" : label, start, "primary");
        if (running || st.days_needed > 0 || starting) btn.disabled = true;
        kids.push(h("div", { class: "row" }, btn,
          h("span", { class: "field-help" }, est ? "Takes about " + est.low + " to " + est.high + " minutes. You can leave this page; you are emailed when it is ready." :
            "Takes a few minutes. You can leave this page; you are emailed when it is ready.")));
      }
      body.appendChild(sectionCard("Forecast run", kids));
      if (st.model) {
        body.appendChild(sectionCard("How accurate it has been", [
          h("p", null, "Model " + st.model.version + ", learned from sales to " + fmt.date(st.model.data_cutoff) + "."),
          accuracyBlock(st.model.backtest, true) || h("p", { class: "text-2" }, "The accuracy test results are not available for this model.")]));
        body.appendChild(h("div", { class: "row" }, h("a", { class: "btn btn-primary", href: "#/today" }, "See today's decisions"),
          h("a", { class: "btn btn-ghost", href: "#/model" }, "Model performance")));
      }
      if (running) { clearTimeout(timer); timer = setTimeout(function () { if (alive()) load(); }, 10000); }
    }
    function start(e) {
      var btn = e.currentTarget;
      starting = true;
      btn.disabled = true; btn.textContent = "Training… this can take a few minutes";
      DQ.post("/api/forecast/run", { kind: "train" }).then(function (run) {
        starting = false;
        if (run && run.status === "failed") DQ.toast("Training did not finish: " + (run.message || "no reason given"), { kind: "risk" });
        else if (run && run.status === "succeeded") DQ.toast("The forecast is ready.");
        else DQ.toast("Training has started. This page updates by itself.");
        if (alive()) load();
      }, function (err) {
        starting = false;
        DQ.toast(err.message, { kind: "risk" });
        if (alive()) load();
      });
    }
    return load();
  } });

  /* ================================================================ Settings */

  var SETTINGS_TABS = [
    { value: "company", label: "Company", show: function () { return true; } },
    { value: "stores", label: "Stores", show: function () { return true; } },
    { value: "people", label: "People", show: function () { return DQ.can("people.manage"); } },
    { value: "rules", label: "Business rules", show: function () { return true; } },
    { value: "calendar", label: "Calendar", show: function () { return true; } },
    { value: "integrations", label: "Integrations", show: function () { return DQ.can("integrations.manage"); } },
    { value: "support", label: "Support access", show: function () { return role() === "owner"; } },
    { value: "pin", label: "Manager PIN", show: function () { return DQ.can("sales.refund"); } }
  ];
  function renderSettings(main, params, query, alive) {
    markNav("#/settings");
    var tab = params.tab || "company";
    var tabs = SETTINGS_TABS.filter(function (t) { return t.show(); });
    main.appendChild(ui.page("Settings", { sub: isAdmin() ? "Your company, stores, people and the rules the forecast works to." :
      "What your role may see. Only an Owner or Admin can change company settings." }));
    main.appendChild(ui.tabs(tabs.map(function (t) { return { value: t.value, label: t.label }; }), tab, function (v) { DQ.go("#/settings/" + v); }));
    var box = h("div", { class: "stack", role: "tabpanel" });
    main.appendChild(box);
    var def = tabs.filter(function (t) { return t.value === tab; })[0];
    if (!def) {
      box.appendChild(ui.empty({ title: "Your role does not include this part of Settings", text: "Ask an Owner or Admin if you need it.", action: { label: "Back to Settings", href: "#/settings" } }));
      return;
    }
    return SETTINGS[tab](box, alive, query);
  }
  DQ.route("#/settings", { title: "Settings", nav: "Settings", render: function (m, p, q, a) { return renderSettings(m, { tab: "company" }, q, a); } });
  DQ.route("#/settings/:tab", { title: "Settings", render: renderSettings });

  function loadInto(box, alive, promise, draw, retry) {
    DQ.clear(box); box.appendChild(ui.skeleton(5));
    return promise.then(function (v) { if (!alive()) return; DQ.clear(box); draw(v); },
      function (err) { if (!alive()) return; DQ.clear(box); box.appendChild(ui.error(err, retry)); });
  }

  var SETTINGS = {};

  /* ---------------------------------------------------------------- company */
  SETTINGS.company = function (box, alive) {
    function run() {
      return loadInto(box, alive, DQ.get("/api/company"), function (co) {
        var edit = isAdmin();
        var name = ui.input({ required: true, maxlength: 200, value: co.name, autocomplete: "organization" });
        var gstin = ui.input({ maxlength: 15, value: co.gstin || "", autocapitalize: "characters", spellcheck: "false" });
        var tzs = [{ value: "Asia/Kolkata", label: "India Standard Time (Asia/Kolkata)" }];
        if (co.timezone && co.timezone !== "Asia/Kolkata") tzs.push({ value: co.timezone, label: co.timezone });
        var tz = ui.select(tzs, co.timezone || "Asia/Kolkata");
        var fy = ui.select(MONTH_NAMES.map(function (m, i) { return { value: i + 1, label: m + (i === 3 ? " (most Indian businesses)" : "") }; }), co.fy_start_month);
        box.appendChild(sectionCard("Company details", [
          edit ? null : notice("data", "Only an Owner or Admin can change these."),
          form([
            ui.field("Company name", name),
            ui.field("GSTIN", gstin, "15 characters, for example 27AAPFU0939F1ZV. Leave empty if you are not registered."),
            ui.field("Time zone", tz, "All times in DQ-Stack are shown in this zone."),
            ui.field("Financial year starts in", fy, "Used for year to date figures in reports.")
          ], "Save company details", function () {
            var patch = {};
            if (name.value.trim() !== co.name) patch.name = name.value.trim();
            if (gstin.value.trim().toUpperCase() !== (co.gstin || "")) patch.gstin = gstin.value.trim().toUpperCase();
            if (tz.value !== co.timezone) patch.timezone = tz.value;
            if (+fy.value !== co.fy_start_month) patch.fy_start_month = +fy.value;
            if (!Object.keys(patch).length) { DQ.toast("Nothing has changed."); return; }
            return DQ.api("PATCH", "/api/company", patch).then(function (co2) {
              co = co2;
              if (DQ.me && DQ.me.company) DQ.me.company.name = co2.name;
              DQ.toast("Company details saved.");
            });
          }, { disabled: !edit })]));
        box.appendChild(sectionCard("Plan", h("dl", { class: "a-kv" }, kv("Plan", cap(co.plan || "—")), kv("Currency", co.currency || "INR"))));
      }, run);
    }
    return run();
  };

  /* ---------------------------------------------------------------- stores */
  function storeForm(s, onSaved, onCancel) {
    var isNew = !s;
    s = s || {};
    var code = ui.input({ required: true, maxlength: 32, value: s.code || "", autocapitalize: "characters", spellcheck: "false" });
    if (!isNew) code.disabled = true;
    var name = ui.input({ required: true, maxlength: 200, value: s.name || "" });
    var address = h("textarea", { class: "input", rows: 2, maxlength: 2000 });
    address.value = s.address || "";
    var state = ui.select([{ value: "", label: "Choose a state or union territory" }].concat(STATES.map(function (x) { return { value: x, label: x }; })), s.state || "");
    var hours = ui.input({ maxlength: 64, value: s.opening_hours || "", placeholder: "08:00 to 22:00" });
    var close = ui.input({ type: "time", required: true, value: s.day_close_time || "22:00" });
    var off = checkGroup("Weekly off days", DAYS.map(function (d, i) { return { value: i, label: d }; }), s.weekly_off || [],
      "Days the store is closed every week. Leave all unticked if it opens every day.");
    var sizes = [{ value: "", label: "Not set" }, { value: "small", label: "Small" }, { value: "medium", label: "Medium" }, { value: "large", label: "Large" }];
    if (s.size && !sizes.some(function (x) { return x.value === s.size; })) sizes.push({ value: s.size, label: s.size });
    var size = ui.select(sizes, s.size || "");
    var mode = ui.select([
      { value: "standalone", label: "Standalone: upload or type each day's sales" },
      { value: "connected", label: "Connected: sales come from the tills or a billing system" }], s.mode || "standalone");
    var active = isNew ? null : checkbox("This store is open", s.active !== false, "Untick for a store that has closed for good. Its history is kept.");
    return sectionCard(isNew ? "Add a store" : "Change " + s.name, form([
      h("div", { class: "grid-2" }, ui.field("Store code", code, isNew ? "A short code, unique, for example PUN1. It cannot be changed later." : "The code cannot be changed."), ui.field("Store name", name)),
      ui.field("Address", address),
      h("div", { class: "grid-2" }, ui.field("State", state, "Decides which state festivals apply."), ui.field("Opening hours", hours)),
      h("div", { class: "grid-2" }, ui.field("Day close time", close, "When the day's sales are closed off."), ui.field("Size", size)),
      off.el,
      ui.field("Daily mode", mode, "How this store's daily sales reach DQ-Stack."),
      active ? active.el : null
    ], isNew ? "Add the store" : "Save the store", function () {
      var body = {
        name: name.value.trim(), address: address.value.trim() || null, state: state.value || null,
        opening_hours: hours.value.trim() || null, day_close_time: close.value,
        weekly_off: off.values().map(Number), size: size.value || null, mode: mode.value
      };
      if (isNew) body.code = code.value.trim().toUpperCase();
      else body.active = active.input.checked;
      var p = isNew ? DQ.post("/api/stores", body) : DQ.api("PATCH", "/api/stores/" + s.id, body);
      return p.then(function (saved) { DQ.toast(isNew ? saved.name + " added." : saved.name + " saved."); onSaved(saved); });
    }, { cancel: onCancel }), { cls: "a-editor" });
  }
  SETTINGS.stores = function (box, alive) {
    var edit = isAdmin();
    function run() {
      return loadInto(box, alive, DQ.get("/api/stores"), function (stores) {
        var editorSlot = h("div");
        function openEditor(s) {
          DQ.clear(editorSlot);
          editorSlot.appendChild(storeForm(s, function () { refreshMe().catch(swallow); }, function () { DQ.clear(editorSlot); }));
          var first = editorSlot.querySelector("input:not([disabled]), select");
          if (first) first.focus();
          editorSlot.scrollIntoView({ block: "start", behavior: "smooth" });
        }
        box.appendChild(h("div", { class: "a-toolbar" },
          h("p", { class: "text-2" }, plural(stores.length, "store") + (edit ? "." : ". Only an Owner or Admin can add or change stores.")),
          edit ? ui.button("Add a store", function () { openEditor(null); }) : null));
        box.appendChild(editorSlot);
        if (!stores.length) {
          box.appendChild(ui.empty({ title: "No stores yet", text: "Stock, orders and forecasts all belong to a store. " + (edit ? "Add your first one above, or upload a stores file." : "Ask an Owner or Admin to add one."),
            action: edit ? { label: "Upload a stores file", href: "#/imports/new/stores" } : null }));
          return;
        }
        box.appendChild(ui.table([
          { key: "code", label: "Code" },
          { key: "name", label: "Name" },
          { key: "state", label: "State" },
          { key: "opening_hours", label: "Hours" },
          { key: "day_close_time", label: "Day close" },
          { key: "weekly_off", label: "Weekly off", render: function (s) { return (s.weekly_off || []).length ? s.weekly_off.map(function (d) { return DAYS[d]; }).join(", ") : "None"; } },
          { key: "mode", label: "Daily mode", render: function (s) { return ui.badge(s.mode === "connected" ? "Connected" : "Standalone", "data"); } },
          { key: "active", label: "Status", render: function (s) { return s.active ? ui.badge("Open", "ok") : ui.badge("Closed", "neutral"); } },
          { key: "id", label: "", render: function (s) { return edit ? ui.button("Change", function () { openEditor(s); }, "ghost", { class: "btn btn-ghost btn-sm", "aria-label": "Change " + s.name }) : ""; } }
        ], stores, { caption: "Stores" }));
      }, run);
    }
    return run();
  };

  /* ---------------------------------------------------------------- people */
  function linkPanel(title, intro, link) {
    return h("div", { class: "a-notice a-link-panel", "data-kind": "data", role: "status" },
      h("p", null, h("strong", null, title)), h("p", null, intro), copyBox("Link", link));
  }
  SETTINGS.people = function (box, alive) {
    var mineOwner = role() === "owner";
    function run() {
      return loadInto(box, alive, Promise.all([DQ.get("/api/people"), DQ.get("/api/stores")]), function (res) {
        var people = res[0], stores = res[1];
        var top = h("div");
        var formSlot = h("div");
        var list = h("ul", { class: "a-list", "aria-label": "People" });
        box.appendChild(h("div", { class: "a-toolbar" },
          h("p", { class: "text-2" }, plural(people.length, "person", "people") + " in the company. Each person sets their own password; nobody else ever sees it."),
          ui.button("Invite a person", function () { openInvite(); })));
        box.appendChild(top);
        box.appendChild(formSlot);
        box.appendChild(list);
        people.forEach(function (p) { list.appendChild(personItem(p)); });

        function roleSelect(value) {
          return ui.select(ROLES.filter(function (r) { return r.value !== "owner" || mineOwner || value === "owner"; })
            .map(function (r) { return { value: r.value, label: roleName(r.value) + ": " + r.text }; }), value);
        }
        function storeChecks(selected) {
          return checkGroup("Stores", stores.map(function (s) { return { value: s.id, label: s.name + " (" + s.code + ")" }; }), selected,
            "The stores this person works in. Owners and Admins see every store.");
        }
        function wireRole(sel, group) {
          function sync() { group.el.hidden = sel.value === "owner" || sel.value === "admin"; }
          sel.addEventListener("change", sync); sync();
        }
        function openInvite() {
          DQ.clear(formSlot); DQ.clear(top);
          var name = ui.input({ required: true, maxlength: 200, autocomplete: "off" });
          var email = ui.input({ type: "email", maxlength: 254, autocomplete: "off", inputmode: "email" });
          var phone = ui.input({ type: "tel", maxlength: 20, autocomplete: "off", inputmode: "tel", placeholder: "98765 43210" });
          var rsel = roleSelect("store_manager");
          var sg = storeChecks(stores.length === 1 ? [stores[0].id] : []);
          wireRole(rsel, sg);
          formSlot.appendChild(sectionCard("Invite a person", form([
            ui.field("Name", name),
            h("div", { class: "grid-2" }, ui.field("Email", email, "The invitation is emailed here."), ui.field("Phone", phone, "If they have no email, give a phone number and share the link yourself.")),
            ui.field("Role", rsel),
            sg.el
          ], "Send the invitation", function () {
            if (!email.value.trim() && !phone.value.trim()) throw new Error("Give an email address or a phone number.");
            var isAll = rsel.value === "owner" || rsel.value === "admin";
            return DQ.post("/api/people/invite", { name: name.value.trim(), email: email.value.trim() || null, phone: phone.value.trim() || null,
              role: rsel.value, store_ids: isAll ? [] : sg.values() }).then(function (p) {
              people.push(p);
              list.insertBefore(personItem(p), list.firstChild);
              DQ.clear(formSlot);
              showLink(p, p.invite_link, "invitation");
            });
          }, { cancel: function () { DQ.clear(formSlot); } }), { cls: "a-editor" }));
          name.focus();
        }
        function showLink(p, link, what) {
          DQ.clear(top);
          var emailed = p.delivered_by === "email";
          var label = what === "invitation" ? "Invitation" : "Password link";
          if (emailed) DQ.toast(label + " emailed to " + p.email + ".");
          top.appendChild(linkPanel(
            emailed ? label + " emailed to " + p.email : label + " for " + p.name + " was not emailed",
            (emailed ? "You can also pass the link on yourself. " : (p.email ? "Email could not be sent just now. " : "There is no email address for " + p.name + ". ") +
              "Copy the link and share it by phone or WhatsApp. ") +
              (what === "invitation" ? "It works once, for 7 days." : "It works once, for 24 hours."), link));
          top.scrollIntoView({ block: "start", behavior: "smooth" });
        }
        function personItem(p) {
          var li = h("li", { class: "a-item" });
          function paint() {
            DQ.clear(li);
            var st = p.status === "active" ? ["Active", "ok"] : p.status === "invited" ? ["Invited, not joined", "data"] : ["Disabled", "risk"];
            var acts = h("div", { class: "a-item-actions" });
            var me = DQ.me.person.id === p.id;
            var ownerLocked = p.role === "owner" && !mineOwner;
            if (!ownerLocked) acts.appendChild(ui.button("Change role or stores", function () { editPerson(); }, "ghost", { class: "btn btn-ghost btn-sm" }));
            if (p.locked) acts.appendChild(ui.button("Unlock", function (e) { act(e.currentTarget, "unlock", "Unlocked. " + p.name + " can sign in again."); }, "ghost", { class: "btn btn-ghost btn-sm" }));
            if (p.status === "invited") acts.appendChild(ui.button("Send the invitation again", function (e) {
              busy(e.currentTarget, "Sending…", DQ.post("/api/people/" + p.id + "/resend-invite")).then(function (r) { Object.assign(p, r); paint(); showLink(p, r.invite_link, "invitation"); }, swallow);
            }, "ghost", { class: "btn btn-ghost btn-sm" }));
            if (p.status === "active" && !ownerLocked && !me) acts.appendChild(ui.button("Reset password", function (e) {
              var b = e.currentTarget;
              DQ.confirm({ title: "Reset " + p.name + "'s password?", message: p.name + " is signed out on every device now and gets a link to choose a new password. Nobody else sees the new password.", confirmLabel: "Reset the password" }).then(function (c) {
                if (!c.ok) return;
                busy(b, "Resetting…", DQ.post("/api/people/" + p.id + "/reset-password")).then(function (r) { Object.assign(p, r); paint(); showLink(p, r.reset_link, "reset"); }, swallow);
              });
            }, "ghost", { class: "btn btn-ghost btn-sm" }));
            if (p.status !== "disabled" && !me && !ownerLocked) acts.appendChild(ui.button("Disable", function (e) {
              var b = e.currentTarget;
              DQ.confirm({ title: "Disable " + p.name + "?", message: "They are signed out everywhere at once and cannot sign in. Everything they did stays in the records. You can enable the account again later.", confirmLabel: "Disable the account", danger: true }).then(function (c) {
                if (c.ok) act(b, "disable", p.name + " is disabled and signed out everywhere.");
              });
            }, "ghost", { class: "btn btn-ghost btn-sm a-danger-text" }));
            if (p.status === "disabled" && !ownerLocked) acts.appendChild(ui.button("Enable", function (e) { act(e.currentTarget, "enable", p.name + " can sign in again."); }, "ghost", { class: "btn btn-ghost btn-sm" }));
            var where = p.role === "owner" || p.role === "admin" ? "Every store" : (p.store_ids || []).map(function (id) { return storeName(id, stores); }).join(", ") || "No store yet";
            li.appendChild(h("div", { class: "a-item-main" },
              h("div", { class: "a-item-title" }, h("h3", { class: "title-3" }, p.name + (me ? " (you)" : "")), ui.badge(st[0], st[1]), p.locked ? ui.badge("Locked after wrong passwords", "attention") : null),
              h("p", { class: "text-2" }, roleName(p.role) + " · " + where),
              h("p", { class: "field-help" }, [p.email, p.phone].filter(Boolean).join(" · ") + " · " + (p.last_sign_in ? "Last signed in " + when(p.last_sign_in) : "Never signed in"))));
            li.appendChild(acts);
          }
          function act(btn, what, msg) {
            busy(btn, "Saving…", DQ.post("/api/people/" + p.id + "/" + what)).then(function (r) { Object.assign(p, r); paint(); DQ.toast(msg); }, swallow);
          }
          function editPerson() {
            DQ.clear(li);
            var rsel = roleSelect(p.role);
            var sg = storeChecks(p.store_ids || []);
            wireRole(rsel, sg);
            li.appendChild(h("div", { class: "a-item-main" }, h("h3", { class: "title-3" }, p.name),
              form([ui.field("Role", rsel), sg.el], "Save", function () {
                var body = {};
                if (rsel.value !== p.role) body.role = rsel.value;
                var isAll = rsel.value === "owner" || rsel.value === "admin";
                if (!isAll) {
                  var ids = sg.values();
                  if (!ids.length) throw new Error("Choose at least one store for this person.");
                  body.store_ids = ids;
                }
                return DQ.api("PATCH", "/api/people/" + p.id, body).then(function (r) { Object.assign(p, r); paint(); DQ.toast(p.name + " saved."); });
              }, { cancel: paint })));
            rsel.focus();
          }
          paint();
          return li;
        }
      }, run);
    }
    return run();
  };

  /* ---------------------------------------------------------------- business rules */
  var RULE_GROUPS = [
    { title: "Stock targets", sub: "A service level is how often a customer should find the product on the shelf. Higher means fewer empty shelves and more money tied up in stock.", items: [
      { key: "service_level_floor", label: "Lowest service level", kind: "pct", digits: 1, help: "Even the least important products aim at least this high." },
      { key: "service_level_ceiling", label: "Highest service level", kind: "pct", digits: 1, help: "Even the most important products aim no higher than this." },
      { key: "holding_rate_per_day", label: "Cost of holding stock, per day", kind: "pct", digits: 2, help: "Money tied up, space and spoilage, as a share of the cost price each day. 0.22% a day is about 80% a year." },
      { key: "perishable_cover_days", label: "Perishables: most days of stock", kind: "int", unit: "days", help: "Perishable products holding more than this many days of sales are flagged as a wastage risk." }] },
    { title: "Counting", sub: "Products are counted in turn. A products sell the most value; C products the least.", items: [
      { key: "count_interval_days", label: "Count every", kind: "interval" },
      { key: "count_auto_post_units", label: "Count differences that post by themselves, up to", kind: "int", unit: "units", help: "A difference at or below both limits is accepted at once; a bigger one waits for a manager." },
      { key: "count_auto_post_paise", label: "and up to a value of", kind: "money", help: "In rupees, at cost price." }] },
    { title: "Ordering", sub: "Used until suppliers are loaded, and for products with no supplier.", items: [
      { key: "default_lead_time_days", label: "Days from order to delivery", kind: "int", unit: "days" },
      { key: "default_review_period_days", label: "Days between orders", kind: "int", unit: "days" }] },
    { title: "Alerts", items: [
      { key: "overstock_cover_days", label: "Overstocked when stock lasts more than", kind: "int", unit: "days" },
      { key: "expiry_warning_days", label: "Warn about expiry this many days ahead", kind: "int", unit: "days" },
      { key: "dead_stock_days", label: "Dead stock after this many days without a sale", kind: "int", unit: "days" },
      { key: "shrinkage_alert_paise", label: "Alert when a store loses more than this in a week", kind: "money", help: "Stock lost in counts, in rupees at cost price." }] },
    { title: "Day close", items: [
      { key: "auto_close_time", label: "Close the day by itself at", kind: "time", help: "If nobody closes the day, it closes at this time." }] }
  ];
  function ruleShow(kind, v, digits) {
    if (v === null || v === undefined) return "";
    if (kind === "pct") return String(+(100 * v).toFixed(digits === undefined ? 1 : digits));
    if (kind === "money") return String(v / 100);
    return String(v);
  }
  function ruleText(item, v) {
    if (item.kind === "pct") return ruleShow("pct", v, item.digits) + "%";
    if (item.kind === "money") return fmt.rupees(v, { whole: true });
    if (item.kind === "interval") return "A " + v.A + ", B " + v.B + ", C " + v.C + " days";
    if (item.kind === "time") return v;
    return fmt.num(v) + (item.unit ? " " + item.unit : "");
  }
  SETTINGS.rules = function (box, alive) {
    function run() {
      return loadInto(box, alive, DQ.get("/api/company"), function (co) {
        var edit = isAdmin(), cur = co.rules, defs = co.rule_defaults, readers = [];
        function numInput(val, attrs) { return ui.input(Object.assign({ type: "text", inputmode: "decimal", value: val, required: true, autocomplete: "off" }, attrs || {})); }
        function itemField(item) {
          var help = (item.help ? item.help + " " : "") + "Default: " + ruleText(item, defs[item.key]) + ".";
          if (item.kind === "interval") {
            var ins = { A: null, B: null, C: null };
            var fields = ["A", "B", "C"].map(function (c) {
              ins[c] = numInput(String(cur[item.key][c]), { inputmode: "numeric" });
              return ui.field(c + " products, days", ins[c]);
            });
            readers.push(function (out) {
              var v = {}, changed = false;
              ["A", "B", "C"].forEach(function (c) {
                var n = Number(ins[c].value.trim());
                if (!Number.isInteger(n) || n < 1) throw new Error("Count intervals are whole numbers of days, 1 or more.");
                v[c] = n; if (n !== cur[item.key][c]) changed = true;
              });
              if (changed) out[item.key] = v;
            });
            return h("fieldset", { class: "a-fieldset" }, h("legend", null, item.label), h("div", { class: "grid-3" }, fields), h("p", { class: "field-help" }, help));
          }
          var input = item.kind === "time" ? ui.input({ type: "time", value: cur[item.key], required: true }) :
            numInput(ruleShow(item.kind, cur[item.key], item.digits), item.kind === "int" ? { inputmode: "numeric" } : null);
          var suffix = item.kind === "pct" ? "%" : item.kind === "money" ? null : item.unit || null;
          var f = ui.field(item.label + (suffix ? " (" + suffix + ")" : item.kind === "money" ? " (₹)" : ""), input, help);
          if (suffix || item.kind === "money") {
            var wrap = h("div", { class: "a-affix" });
            f.replaceChild(wrap, input);
            if (item.kind === "money") wrap.appendChild(h("span", { class: "a-affix-text", "aria-hidden": "true" }, "₹"));
            wrap.appendChild(input);
            if (suffix) wrap.appendChild(h("span", { class: "a-affix-text", "aria-hidden": "true" }, suffix));
          }
          if (edit) f.appendChild(h("button", { class: "a-linkbtn", type: "button", onclick: function () { input.value = item.kind === "time" ? defs[item.key] : ruleShow(item.kind, defs[item.key], item.digits); input.focus(); } }, "Use the default"));
          readers.push(function (out) {
            var raw = input.value.trim().replace(/,/g, ""), v;
            if (item.kind === "time") v = raw;
            else if (item.kind === "pct") { v = parseFloat(raw); if (isNaN(v)) throw new Error(item.label + ": enter a percentage."); v = Math.round(v * 1e4) / 1e6; }
            else if (item.kind === "money") { v = fmt.toPaise(raw); if (v === null || v < 0) throw new Error(item.label + ": enter an amount in rupees."); }
            else { v = Number(raw); if (!Number.isInteger(v) || v < 0) throw new Error(item.label + ": enter a whole number, 0 or more."); }
            if (v !== cur[item.key]) out[item.key] = v;
          });
          return f;
        }
        var groups = RULE_GROUPS.map(function (g) {
          return h("fieldset", { class: "a-fieldset a-rule-group" }, h("legend", { class: "title-3" }, g.title),
            g.sub ? h("p", { class: "text-2" }, g.sub) : null,
            h("div", { class: "grid-2" }, g.items.map(itemField)));
        });
        if (!edit) box.appendChild(notice("data", "These are the rules your company uses. Only an Owner or Admin can change them."));
        box.appendChild(h("div", { class: "card" }, form(groups, "Save the rules", function () {
          var out = {};
          readers.forEach(function (r) { r(out); });
          if (!Object.keys(out).length) { DQ.toast("Nothing has changed."); return; }
          var before = {};
          Object.keys(out).forEach(function (k) { before[k] = cur[k]; });
          return DQ.api("PATCH", "/api/company", { rules: out }).then(function (co2) {
            cur = co2.rules;
            DQ.toast(plural(Object.keys(out).length, "rule") + " saved. Tonight's forecast uses " + (Object.keys(out).length === 1 ? "it." : "them."), { undo: function () {
              DQ.api("PATCH", "/api/company", { rules: before }).then(function () { DQ.toast("Rules put back."); if (alive()) run(); }, function (e) { DQ.toast(e.message, { kind: "risk" }); });
            } });
          });
        }, { disabled: !edit })));
      }, run);
    }
    return run();
  };

  /* ---------------------------------------------------------------- calendar */
  var EVENT_KIND = { festival: ["Festival", "data"], holiday: ["Holiday", "data"], closure: ["Store closed", "attention"], local_event: ["Local event", "ok"] };
  function managedStores() { return DQ.stores().filter(function (s) { return DQ.can("forecast.review", s.id); }); }
  function canChangeEvent(e) {
    if (isAdmin()) return true;
    if (!e.store_ids || !e.store_ids.length || (e.kind !== "local_event" && e.kind !== "closure")) return false;
    var mine = managedStores().map(function (s) { return s.id; });
    return e.store_ids.every(function (id) { return mine.indexOf(id) >= 0; });
  }
  function eventForm(e, onSaved, onCancel) {
    var isNew = !e, admin = isAdmin();
    e = e || { name: "", day: todayIST(), kind: "local_event", store_ids: [], states: [] };
    var stores = admin ? DQ.stores() : managedStores();
    var name = ui.input({ required: true, maxlength: 200, value: e.name, placeholder: "Temple fair" });
    var day = ui.input({ type: "date", required: true, value: e.day });
    var kinds = admin ? ["local_event", "closure", "festival", "holiday"] : ["local_event", "closure"];
    var kind = ui.select(kinds.map(function (k) { return { value: k, label: EVENT_KIND[k][0] }; }), e.kind);
    var sg = checkGroup("Which stores", stores.map(function (s) { return { value: s.id, label: s.name }; }), e.store_ids,
      admin ? "Leave all unticked for every store" + (e.states && e.states.length ? " in " + e.states.join(", ") : "") + "." : "Choose the stores it affects.");
    return form([
      h("div", { class: "grid-2" }, ui.field("Name", name), ui.field("Date", day)),
      ui.field("Kind", kind, "A closure means no sales that day; an event or festival changes how much sells."),
      sg.el
    ], isNew ? "Add to the calendar" : "Save", function () {
      var ids = sg.values();
      if (!admin && !ids.length) throw new Error("Choose at least one store.");
      var body = { name: name.value.trim(), day: day.value, kind: kind.value, store_ids: ids };
      var p = isNew ? DQ.post("/api/calendar", body) : DQ.api("PATCH", "/api/calendar/" + e.id, body);
      return p.then(function (saved) { DQ.toast((isNew ? "Added: " : "Saved: ") + saved.name + ", " + fmt.date(saved.day) + ". Tomorrow's forecast includes it."); onSaved(saved); });
    }, { cancel: onCancel });
  }
  SETTINGS.calendar = function (box, alive, query) {
    var today = todayIST();
    var month = /^\d{4}-\d{2}$/.test(query.month || "") ? query.month : today.slice(0, 7);
    var y = +month.slice(0, 4), m = +month.slice(5, 7);
    var first = month + "-01";
    var last = new Date(Date.UTC(y, m, 0)).toISOString().slice(0, 10);
    var prev = new Date(Date.UTC(y, m - 2, 1)).toISOString().slice(0, 7);
    var next = new Date(Date.UTC(y, m, 1)).toISOString().slice(0, 7);
    var canAdd = isAdmin() || managedStores().length > 0;
    function run() {
      return loadInto(box, alive, DQ.get("/api/calendar" + DQ.qs({ from: first, to: last })), function (events) {
        var slot = h("div");
        box.appendChild(h("div", { class: "a-toolbar" },
          h("div", { class: "row a-month-nav" },
            h("a", { class: "btn btn-ghost btn-sm", href: "#/settings/calendar?month=" + prev, "aria-label": "Previous month" }, "‹ " + MONTH_NAMES[(m + 10) % 12].slice(0, 3)),
            h("h2", { class: "title-2", "aria-live": "polite" }, MONTH_NAMES[m - 1] + " " + y),
            h("a", { class: "btn btn-ghost btn-sm", href: "#/settings/calendar?month=" + next, "aria-label": "Next month" }, MONTH_NAMES[m % 12].slice(0, 3) + " ›"),
            month !== today.slice(0, 7) ? h("a", { class: "btn btn-ghost btn-sm", href: "#/settings/calendar" }, "This month") : null),
          canAdd ? ui.button("Add a local event or closure", function () {
            DQ.clear(slot);
            slot.appendChild(sectionCard("Add to the calendar", eventForm(null, function () { run(); }, function () { DQ.clear(slot); }), { cls: "a-editor" }));
            slot.querySelector("input").focus();
          }) : null));
        box.appendChild(h("p", { class: "text-2" }, "National and state festivals are filled in for you. Festivals that follow the moon have approximate dates: confirm each one, or correct it, so the forecast prepares for the right day."));
        box.appendChild(slot);
        if (!events.length) {
          box.appendChild(ui.empty({ title: "Nothing in the calendar for " + MONTH_NAMES[m - 1], text: "Add local events, such as a temple fair or a match day, and days a store is closed. The forecast then plans for them." }));
          return;
        }
        var approx = events.filter(function (e) { return e.approximate; }).length;
        if (approx) box.appendChild(notice("attention", plural(approx, "date") + " this month " + (approx === 1 ? "needs" : "need") + " confirming."));
        var list = h("ul", { class: "a-list", "aria-label": "Calendar for " + MONTH_NAMES[m - 1] });
        events.forEach(function (e) { list.appendChild(eventItem(e)); });
        box.appendChild(list);
        function eventItem(e) {
          var li = h("li", { class: "a-item" });
          function paint() {
            DQ.clear(li);
            var k = EVENT_KIND[e.kind] || [e.kind, "neutral"];
            var where = e.store_ids && e.store_ids.length ? e.store_ids.map(function (id) { return storeName(id); }).join(", ") :
              e.states && e.states.length ? e.states.join(", ") : "Every store";
            var wd = DAYS[(new Date(e.day + "T00:00:00Z").getUTCDay() + 6) % 7];
            var acts = h("div", { class: "a-item-actions" });
            if (canChangeEvent(e)) {
              if (e.approximate) acts.appendChild(ui.button("The date is right", function (ev) {
                busy(ev.currentTarget, "Saving…", DQ.api("PATCH", "/api/calendar/" + e.id, { day: e.day })).then(function (r) { Object.assign(e, r); paint(); DQ.toast(e.name + " confirmed for " + fmt.date(e.day) + "."); }, swallow);
              }, "ghost", { class: "btn btn-ghost btn-sm" }));
              acts.appendChild(ui.button("Change", function () {
                DQ.clear(li);
                li.appendChild(h("div", { class: "a-item-main" }, h("h3", { class: "title-3" }, "Change " + e.name), eventForm(e, function (r) { Object.assign(e, r); paint(); }, paint)));
                li.querySelector("input").focus();
              }, "ghost", { class: "btn btn-ghost btn-sm" }));
              acts.appendChild(ui.button("Remove", function (ev) {
                var b = ev.currentTarget;
                DQ.confirm({ title: "Remove " + e.name + "?", message: "It comes out of the calendar, and tomorrow's forecast no longer plans for it. " + (e.source && e.source !== "company" ? "It came from the " + e.source + "; it will not come back by itself." : ""),
                  confirmLabel: "Remove it", danger: true }).then(function (c) {
                  if (!c.ok) return;
                  busy(b, "Removing…", DQ.api("DELETE", "/api/calendar/" + e.id)).then(function () { li.remove(); DQ.toast(e.name + " removed."); }, swallow);
                });
              }, "ghost", { class: "btn btn-ghost btn-sm" }));
            }
            li.appendChild(h("div", { class: "a-item-main" },
              h("div", { class: "a-item-title" },
                h("span", { class: "a-date num" }, fmt.dayShort(e.day) + ", " + wd.slice(0, 3)),
                h("h3", { class: "title-3" }, e.name), ui.badge(k[0], k[1]),
                e.approximate ? ui.badge("Confirm the date", "attention") : null),
              h("p", { class: "text-2" }, where + (e.source ? " · From the " + (e.source === "company" ? "company" : e.source) : ""))));
            li.appendChild(acts);
          }
          paint();
          return li;
        }
      }, run);
    }
    return run();
  };

  /* ---------------------------------------------------------------- integrations */
  SETTINGS.integrations = function (box, alive) {
    function run() {
      return loadInto(box, alive, Promise.all([DQ.get("/api/integrations/api-keys"), DQ.get("/api/stores")]), function (res) {
        var keys = res[0], stores = res[1];
        var shown = h("div");
        var nameIn = ui.input({ required: true, maxlength: 100, placeholder: "Front till, Pune", autocomplete: "off" });
        var storeSel = ui.select([{ value: "", label: "Any store" }].concat(stores.map(function (s) { return { value: s.id, label: s.name }; })), "");
        box.appendChild(sectionCard("Connect a billing system", [
          h("p", { class: "text-2" }, "If your stores already bill on another system, it can send each paid sale, refund and void to DQ-Stack. Each system gets its own key."),
          shown,
          form([h("div", { class: "grid-2" }, ui.field("Name for the key", nameIn, "So you can tell keys apart."), ui.field("Store", storeSel, "Limit the key to one store, or leave it for any store."))],
            "Make a key", function () {
              return DQ.post("/api/integrations/api-keys", { name: nameIn.value.trim(), store_id: storeSel.value || null }).then(function (k) {
                DQ.clear(shown);
                shown.appendChild(h("div", { class: "a-notice", "data-kind": "attention", role: "status" },
                  h("p", null, h("strong", null, "Copy this key now. It is not shown again.")),
                  copyBox("Key for " + k.name, k.key)));
                nameIn.value = "";
                keys.push({ id: k.id, name: k.name, prefix: k.prefix, store_id: k.store_id, revoked: false, last_used_at: null });
                drawKeys();
              });
            })]));
        var keySlot = h("div");
        box.appendChild(sectionCard("Keys", keySlot));
        function drawKeys() {
          DQ.clear(keySlot);
          keySlot.appendChild(ui.table([
            { key: "name", label: "Name" },
            { key: "prefix", label: "Starts with", render: function (k) { return h("span", { class: "mono" }, k.prefix + "…"); } },
            { key: "store_id", label: "Store", render: function (k) { return k.store_id ? storeName(k.store_id, stores) : "Any store"; } },
            { key: "last_used_at", label: "Last used", render: function (k) { return k.last_used_at ? when(k.last_used_at) : "Not yet"; } },
            { key: "revoked", label: "Status", render: function (k) { return k.revoked ? ui.badge("Revoked", "neutral") : ui.badge("Working", "ok"); } },
            { key: "id", label: "", render: function (k) {
              return k.revoked ? "" : ui.button("Revoke", function (e) {
                var b = e.currentTarget;
                DQ.confirm({ title: "Revoke " + k.name + "?", message: "The system using this key stops being able to send sales at once. This cannot be undone; make a new key if you need one.", confirmLabel: "Revoke the key", danger: true }).then(function (c) {
                  if (!c.ok) return;
                  busy(b, "Revoking…", DQ.post("/api/integrations/api-keys/" + k.id + "/revoke")).then(function () { k.revoked = true; drawKeys(); DQ.toast(k.name + " revoked."); }, swallow);
                });
              }, "ghost", { class: "btn btn-ghost btn-sm", "aria-label": "Revoke " + k.name });
            } }
          ], keys, { caption: "API keys", empty: { title: "No keys yet", text: "Make a key above when you are ready to connect a billing system." } }));
        }
        drawKeys();
        var base = location.origin;
        var sample = JSON.stringify({ id: "pos-7781-000123", type: "sale", store_code: (stores[0] && stores[0].code) || "PUN1",
          bill_number: "000123", occurred_at: "2026-11-09T18:42:10+05:30",
          lines: [{ sku: "RICE-BAS-5KG", qty: 2, price: 849.00, discount: 0 }], payment_method: "upi" }, null, 2);
        box.appendChild(sectionCard("How to connect", [
          h("ol", { class: "a-howto" },
            h("li", null, "Make a key above and give it to whoever looks after your billing system."),
            h("li", null, "They check it works with GET ", h("code", null, base + "/api/pos/v1/ping"), ", sending the header ", h("code", null, "Authorization: Bearer dqk_…"), "."),
            h("li", null, "Each paid sale, refund or void is sent to POST ", h("code", null, base + "/api/pos/v1/messages"), ", up to 500 at a time. Every message has its own id; sending the same id twice is harmless, so a till can resend its queue whenever it likes."),
            h("li", null, "Only paid sales count. A refund names the original bill and says whether each item can be sold again. A void reverses a paid bill in full."),
            h("li", null, "Systems that cannot call an address can drop a CSV file each night at POST ", h("code", null, base + "/api/pos/v1/files"), ", one row per line.")),
          h("p", { class: "text-2" }, "One sale looks like this:"),
          h("pre", { class: "a-code", tabindex: 0, "aria-label": "Example sale message" }, sample)]));
      }, run);
    }
    return run();
  };

  /* ---------------------------------------------------------------- support access */
  SETTINGS.support = function (box) {
    var granted = h("div");
    /* Grants still open, from before this visit too. */
    DQ.get("/api/company/support-grants").then(function (rows) {
      rows.filter(function (g) { return g.active; }).forEach(function (g) { granted.appendChild(grantRow(g)); });
    }, function () {});
    function grantRow(g) {
      var row = h("div", { class: "a-notice", "data-kind": "data", role: "status" },
        h("p", null, "Support can see your company until " + when(g.expires_at) + "." + (g.reason ? " Reason: " + g.reason : "")),
        ui.button("End access now", function (e) {
          var b = e.currentTarget;
          DQ.confirm({ title: "End support access now?", message: "Support loses access at once.", confirmLabel: "End access", danger: true }).then(function (c) {
            if (!c.ok) return;
            busy(b, "Ending…", DQ.post("/api/company/support-grants/" + g.id + "/revoke")).then(function () { row.remove(); DQ.toast("Support access ended."); }, swallow);
          });
        }, "ghost"));
      return row;
    }
    var reason = h("textarea", { class: "input", rows: 3, required: true, minlength: 5, maxlength: 500, placeholder: "Help with the sales history upload" });
    var hours = ui.select([{ value: 4, label: "4 hours" }, { value: 24, label: "1 day" }, { value: 72, label: "3 days" }, { value: 168, label: "7 days, the longest allowed" }], 24);
    box.appendChild(sectionCard("Let DQ-Stack support see your company", [
      h("p", { class: "text-2" }, "Support staff cannot see your data unless you open the door. Access ends by itself at the time you choose, and everything they look at is recorded in the activity log."),
      granted,
      form([ui.field("Why support needs access", reason, "A few words, for the record."), ui.field("For how long", hours)], "Grant access", function () {
        if (reason.value.trim().length < 5) throw new Error("Give a reason of a few words.");
        return DQ.post("/api/company/support-grants", { reason: reason.value.trim(), hours: +hours.value }).then(function (g) {
          granted.appendChild(grantRow({ id: g.id, expires_at: g.expires_at, reason: reason.value.trim() }));
          reason.value = "";
          DQ.toast("Support access granted until " + when(g.expires_at) + ".");
        });
      }),
      h("p", { class: "field-help" }, "Grants made earlier are listed in the ", h("a", { href: "#/activity" }, "activity log"), ".")]));
  };

  /* ---------------------------------------------------------------- manager PIN */
  SETTINGS.pin = function (box) {
    var pin = ui.input({ type: "password", inputmode: "numeric", autocomplete: "off", required: true, maxlength: 8, pattern: "[0-9]*" });
    var again = ui.input({ type: "password", inputmode: "numeric", autocomplete: "off", required: true, maxlength: 8, pattern: "[0-9]*" });
    box.appendChild(sectionCard("Your manager PIN", [
      h("p", { class: "text-2" }, "At the till, your PIN approves refunds and price changes for a cashier. Keep it to yourself; it is yours alone and is recorded with each approval."),
      form([h("div", { class: "grid-2" }, ui.field("New PIN", pin, "4 to 8 digits."), ui.field("The same PIN again", again))], "Save my PIN", function () {
        if (!/^\d{4,8}$/.test(pin.value)) throw new Error("The PIN must be 4 to 8 digits.");
        if (pin.value !== again.value) throw new Error("The two PINs do not match.");
        return DQ.post("/api/people/me/manager-pin", { pin: pin.value }).then(function () {
          pin.value = ""; again.value = "";
          DQ.toast("Your manager PIN is saved.");
        });
      })]));
  };

  /* ================================================================ Imports */

  var IMPORT_STATUS = {
    uploaded: ["Uploaded", "data"], needs_mapping: ["Columns to match", "attention"], checked: ["Checked, not imported", "attention"],
    imported: ["Imported", "ok"], undone: ["Undone", "neutral"]
  };
  function importBadge(s) { var b = IMPORT_STATUS[s] || [cap(s), "neutral"]; return ui.badge(b[0], b[1]); }
  var kindsCache = null;
  function getKinds() {
    if (kindsCache) return Promise.resolve(kindsCache);
    return DQ.get("/api/imports/kinds").then(function (k) { kindsCache = k; return k; });
  }
  function kindTitle(kinds, key) { var k = kinds.filter(function (x) { return x.key === key; })[0]; return k ? k.title : cap(String(key).replace(/_/g, " ")); }

  DQ.route("#/imports", { title: "Imports", nav: "Imports", can: "imports.upload", render: function (main, params, query, alive) {
    main.appendChild(ui.page("Imports", { sub: "Every file uploaded, what it changed, and who uploaded it.",
      actions: [h("a", { class: "btn btn-primary", href: "#/imports/new" }, "Upload a file")] }));
    var body = h("div", { class: "stack" }, ui.skeleton(6));
    main.appendChild(body);
    function load() {
      return Promise.all([DQ.get("/api/imports"), getKinds()]).then(function (res) {
        if (!alive()) return;
        DQ.clear(body);
        var jobs = res[0], kinds = res[1];
        if (!jobs.length) {
          body.appendChild(ui.empty({ title: "No files uploaded yet", text: "Products, stores, suppliers, prices, the calendar, opening stock and sales all load from a spreadsheet. Download a template, fill it in and upload it; you see a check report before anything changes.",
            action: { label: "Upload a file", href: "#/imports/new" } }));
          return;
        }
        body.appendChild(ui.table([
          { key: "created_at", label: "Uploaded", render: function (j) { return when(j.created_at); } },
          { key: "kind", label: "What", render: function (j) { return kindTitle(kinds, j.kind); } },
          { key: "filename", label: "File", render: function (j) { return h("a", { href: "#/imports/" + j.id }, j.filename || "Untitled"); } },
          { key: "status", label: "Status", render: function (j) { return importBadge(j.status); } },
          { key: "rows_read", label: "Rows", kind: "num" },
          { key: "rows_new", label: "New", kind: "num" },
          { key: "rows_updated", label: "Changed", kind: "num" },
          { key: "rows_failed", label: "Problems", kind: "num" },
          { key: "person", label: "By", render: function (j) { return j.person || "—"; } }
        ], jobs, { caption: "Uploaded files", onRow: function (j) { DQ.go("#/imports/" + j.id); } }));
      }, function (err) { if (!alive()) return; DQ.clear(body); body.appendChild(ui.error(err, load)); });
    }
    return load();
  } });

  function kindPicker(kinds) {
    return h("ul", { class: "tiles a-plain", "aria-label": "What to upload" }, kinds.map(function (k) {
      return h("li", null, h("a", { class: "tile a-tile", href: "#/imports/new/" + k.key },
        h("div", { class: "title-3" }, k.title),
        h("div", { class: "tile-note" }, k.columns.length + " columns. Rows matched by " + k.natural_key + ".")));
    }));
  }
  DQ.route("#/imports/new", { title: "Upload a file", can: "imports.upload", render: function (main, params, query, alive) {
    markNav("#/imports");
    main.appendChild(ui.page("Upload a file", { sub: "Choose what the file holds.", actions: [h("a", { class: "btn btn-ghost", href: "#/imports" }, "All imports")] }));
    var body = h("div", null, ui.skeleton(4));
    main.appendChild(body);
    function load() {
      return getKinds().then(function (kinds) { if (!alive()) return; DQ.clear(body); body.appendChild(kindPicker(kinds)); },
        function (err) { if (!alive()) return; DQ.clear(body); body.appendChild(ui.error(err, load)); });
    }
    return load();
  } });

  DQ.route("#/imports/new/:kind", { title: "Upload a file", can: "imports.upload", render: function (main, params, query, alive) {
    markNav("#/imports");
    var key = KIND_ALIAS[params.kind] || params.kind;
    var head = h("div");
    main.appendChild(head);
    var body = h("div", { class: "stack" }, ui.skeleton(6));
    main.appendChild(body);
    function load() {
      return getKinds().then(function (kinds) {
        if (!alive()) return;
        DQ.clear(body);
        var k = kinds.filter(function (x) { return x.key === key; })[0];
        if (!k) {
          head.appendChild(ui.page("Upload a file", { actions: [h("a", { class: "btn btn-ghost", href: "#/imports" }, "All imports")] }));
          body.appendChild(notice("attention", "There is no upload called \"" + params.kind + "\". Choose one of these."));
          body.appendChild(kindPicker(kinds));
          return;
        }
        head.appendChild(ui.page("Upload " + k.title.toLowerCase(), { sub: "Rows are matched by " + k.natural_key + ": a row that matches changes what is there, a new one is added. Nothing changes until you have read the check report.",
          actions: [h("a", { class: "btn btn-ghost", href: "#/imports" }, "All imports")] }));
        body.appendChild(sectionCard("1. Download the template", [
          h("p", { class: "text-2" }, "Fill it in with your own data. Keep the first row of column names. Dates are DD-MM-YYYY. Your own column names also work: you match them up after uploading."),
          h("div", { class: "row" },
            h("a", { class: "btn btn-ghost", href: k.templates.xlsx, download: true }, "Excel template"),
            h("a", { class: "btn btn-ghost", href: k.templates.csv, download: true }, "CSV template"))]));
        body.appendChild(sectionCard("What each column means", ui.table([
          { key: "title", label: "Column", render: function (c) { return h("strong", null, c.title); } },
          { key: "required", label: "Needed", render: function (c) { return c.required ? ui.badge("Required", "attention") : ui.badge("Optional", "neutral"); } },
          { key: "help", label: "What it means", render: function (c) { return c.help || "—"; } },
          { key: "example", label: "Example", render: function (c) { return c.example ? h("span", { class: "mono" }, c.example) : "—"; } }
        ], k.columns, { caption: "Columns of the " + k.title + " template" })));
        var file = h("input", { type: "file", class: "input", accept: ".csv,.xlsx,.xls,.txt,text/csv", required: true });
        body.appendChild(sectionCard("2. Upload the filled in file", form([
          ui.field("File", file, "CSV or Excel, up to 25 MB.")
        ], "Upload and check", function () {
          if (!file.files || !file.files[0]) throw new Error("Choose a file first.");
          var fd = new FormData();
          fd.append("kind", k.key);
          fd.append("file", file.files[0]);
          return DQ.upload("/api/imports", fd).then(function (job) { DQ.go("#/imports/" + job.id); });
        }, { busyLabel: "Uploading and checking…" })));
      }, function (err) { if (!alive()) return; DQ.clear(body); body.appendChild(ui.error(err, load)); });
    }
    return load();
  } });

  DQ.route("#/imports/:id", { title: "Import", can: "imports.upload", render: function (main, params, query, alive) {
    markNav("#/imports");
    var head = h("div");
    var body = h("div", { class: "stack" }, ui.skeleton(6));
    main.appendChild(head);
    main.appendChild(body);
    var kinds = [];
    function load() {
      return Promise.all([DQ.get("/api/imports/" + encodeURIComponent(params.id)), settle(getKinds())]).then(function (res) {
        if (!alive()) return;
        kinds = res[1].ok ? res[1].value : [];
        draw(res[0]);
      }, function (err) {
        if (!alive()) return;
        DQ.clear(head); DQ.clear(body);
        head.appendChild(ui.page("Import", { actions: [h("a", { class: "btn btn-ghost", href: "#/imports" }, "All imports")] }));
        body.appendChild(ui.error(err, load));
      });
    }
    function kindOf(job) { return kinds.filter(function (x) { return x.key === job.kind; })[0] || null; }
    function draw(job) {
      DQ.clear(head); DQ.clear(body);
      var k = kindOf(job);
      head.appendChild(ui.page(job.filename || "Import", {
        sub: (k ? k.title : job.kind) + " · uploaded " + when(job.created_at) + (job.person ? " by " + job.person : ""),
        actions: [importBadge(job.status), h("a", { class: "btn btn-ghost", href: "#/imports" }, "All imports")] }));
      if (job.status === "needs_mapping") {
        body.appendChild(notice("attention", job.message || "Some columns need matching before the file can be checked."));
        body.appendChild(mappingCard(job, k, true));
        return;
      }
      if (job.status === "checked") {
        body.appendChild(reportCard(job, "Check report"));
        problemsCards(job).forEach(function (c) { body.appendChild(c); });
        var n = job.rows_accepted || 0;
        var go = ui.button("Import " + plural(n, "row"), function () { commit(go); }, "primary");
        if (!n) go.disabled = true;
        body.appendChild(sectionCard("Import", [
          h("p", null, n ? "That is " + fmt.num(job.rows_new || 0) + " new and " + fmt.num(job.rows_updated || 0) + " changed" +
            (job.rows_unchanged ? ", plus " + plural(job.rows_unchanged, "unchanged row") + " left as " + (job.rows_unchanged === 1 ? "it is" : "they are") : "") + "." :
            "No row can be imported yet. Fix the problems and upload the file again."),
          job.rows_failed ? h("p", { class: "text-2" }, "Rows with problems are left out. You can fix them in the problems file and upload just those afterwards.") : null,
          h("div", { class: "row" }, go, h("a", { class: "btn btn-ghost", href: "#/imports/new/" + job.kind }, "Upload a corrected file"))]));
        if (k) body.appendChild(h("details", { class: "card a-details" }, h("summary", null, "Change which column is which"), mappingCard(job, k, false)));
        return;
      }
      if (job.status === "imported") {
        body.appendChild(reportCard(job, "Imported " + when(job.imported_at)));
        problemsCards(job).forEach(function (c) { body.appendChild(c); });
        var undoBtn = ui.button("Undo this import", function () { undo(undoBtn); }, "ghost");
        body.appendChild(sectionCard("Change your mind?", [
          h("p", { class: "text-2" }, "Undo puts back everything this import added or changed. Anything that has been used since, such as a new product that has already sold, is kept and listed."),
          undoBtn]));
        return;
      }
      if (job.status === "undone") {
        var rep = job.undo_report || {};
        body.appendChild(sectionCard("Undone " + when(job.undone_at), [
          h("p", null, "Put back " + plural(rep.reverted || 0, "change") + "." + (rep.kept ? " " + plural(rep.kept, "row") + " kept because they were used since:" : "")),
          rep.reasons && rep.reasons.length ? h("ul", null, rep.reasons.map(function (r) { return h("li", null, typeof r === "string" ? r : JSON.stringify(r)); })) : null,
          h("a", { class: "btn btn-ghost", href: "#/imports/new/" + job.kind }, "Upload a new file")]));
        body.appendChild(reportCard(job, "What the import had done"));
        return;
      }
      body.appendChild(reportCard(job, "Check report"));
      body.appendChild(ui.button("Check again", load, "ghost"));
    }
    function reportCard(job, title) {
      return sectionCard(title, h("div", { class: "tiles a-mini-tiles" },
        tile("Rows read", fmt.num(job.rows_read || 0)),
        tile("New", fmt.num(job.rows_new || 0), null, null, job.rows_new ? "ok" : null),
        tile("Changed", fmt.num(job.rows_updated || 0), null, null, job.rows_updated ? "data" : null),
        tile("Unchanged", fmt.num(job.rows_unchanged || 0)),
        tile("With problems", fmt.num(job.rows_failed || 0), null, null, job.rows_failed ? "risk" : null)));
    }
    function problemsCards(job) {
      var out = [];
      var errs = job.errors || [], warns = job.warnings || [];
      if (errs.length) {
        var more = (job.rows_failed || errs.length) - errs.length;
        out.push(sectionCard("Rows with problems", [
          h("p", { class: "text-2" }, "These rows are not imported. Download the problems file, fix the rows and upload it again."),
          ui.table([
            { key: "row", label: "Row", kind: "num" },
            { key: "problems", label: "What is wrong", render: function (e) { return (e.problems || []).join(" "); } }
          ], errs.slice(0, 50), { caption: "Rows with problems" }),
          more > 0 || errs.length > 50 ? h("p", { class: "field-help" }, "Showing the first " + Math.min(50, errs.length) + " of " + fmt.num(job.rows_failed || errs.length) + ".") : null,
          h("a", { class: "btn btn-ghost", href: "/api/imports/" + job.id + "/errors.csv", download: true }, "Download the problems file")], { aside: ui.badge(plural(job.rows_failed || errs.length, "row"), "risk") }));
      }
      if (warns.length) {
        out.push(sectionCard("Worth a look", [
          h("p", { class: "text-2" }, "These rows are imported, but something looked unusual."),
          ui.table([
            { key: "row", label: "Row", kind: "num" },
            { key: "problem", label: "What we noticed" }
          ], warns.slice(0, 50), { caption: "Warnings" })], { aside: ui.badge(plural(warns.length, "warning"), "attention") }));
      }
      return out;
    }
    function mappingCard(job, k, primary) {
      if (!k) return notice("risk", "The column list for this kind of file could not be loaded. Reload the page.");
      var headers = job.headers || [];
      var opts = [{ value: "", label: "Not in this file" }].concat(headers.map(function (x) { return { value: x, label: x }; }));
      var sels = {};
      var rows = k.columns.map(function (c) {
        var sel = ui.select(opts, (job.mapping || {})[c.key] || "", { "aria-label": "Column in your file for " + c.title });
        sels[c.key] = sel;
        return { c: c, sel: sel };
      });
      var table = h("div", { class: "table-wrap" }, h("table", { class: "table" },
        h("caption", { class: "sr-only" }, "Match each template column to a column in your file"),
        h("thead", null, h("tr", null, h("th", { scope: "col" }, "Template column"), h("th", { scope: "col" }, "Column in your file"))),
        h("tbody", null, rows.map(function (r) {
          return h("tr", null,
            h("td", null, h("strong", null, r.c.title), " ", r.c.required ? ui.badge("Required", "attention") : null,
              r.c.help ? h("div", { class: "field-help" }, r.c.help) : null),
            h("td", null, r.sel));
        }))));
      var f = form([
        h("p", { class: "text-2" }, "Match each column DQ-Stack needs to the column in your file that holds it. Next time a file with the same columns arrives, this matching is remembered."),
        table
      ], "Check the file again", function () {
        var mapping = {};
        Object.keys(sels).forEach(function (key) { if (sels[key].value) mapping[key] = sels[key].value; });
        return DQ.api("PUT", "/api/imports/" + job.id + "/mapping", { mapping: mapping }).then(function (j2) {
          if (!alive()) return;
          draw(j2);
          DQ.toast(j2.status === "needs_mapping" ? "Some columns still need matching." : "Checked. Read the report, then import.", { kind: j2.status === "needs_mapping" ? "attention" : "ok" });
        });
      }, { busyLabel: "Checking…" });
      return primary ? sectionCard("Match the columns", f) : f;
    }
    function commit(btn) {
      busy(btn, "Importing…", DQ.post("/api/imports/" + params.id + "/commit")).then(function (j2) {
        if (alive()) draw(j2);
        DQ.toast("Imported " + plural(j2.rows_accepted || 0, "row") + ".", { undo: function () {
          DQ.post("/api/imports/" + params.id + "/undo").then(function (j3) { DQ.toast("Import undone."); if (alive()) draw(j3); }, function (e) { DQ.toast(e.message, { kind: "risk" }); });
        } });
      }, swallow);
    }
    function undo(btn) {
      DQ.confirm({ title: "Undo this import?", message: "Everything it added or changed is put back as it was, except rows that have been used since; those are kept and listed. You cannot redo it afterwards, but you can upload the file again.",
        confirmLabel: "Undo the import", danger: true }).then(function (c) {
        if (!c.ok) return;
        busy(btn, "Undoing…", DQ.post("/api/imports/" + params.id + "/undo")).then(function (j3) {
          if (alive()) draw(j3);
          DQ.toast("Import undone.");
        }, swallow);
      });
    }
    return load();
  } });

  /* ================================================================ Alerts */

  var SEVERITY = { risk: ["Urgent", "risk"], attention: ["Needs attention", "attention"], info: ["For information", "data"] };
  /* Where to act, by kind (dqstack/app/alerts.py KINDS), for alerts saved without a link. */
  var ALERT_LINK = { out_of_stock: "#/stock?status=Out", low_stock: "#/stock?status=Low", negative_stock: "#/count",
    expiring: "#/stock?status=Expiring", overstocked: "#/stock?status=Overstocked", count_overdue: "#/count",
    count_waiting: "#/count/approvals", shrinkage: "#/reports/shrinkage", today_ready: "#/today", orders_waiting: "#/orders",
    delivery_overdue: "#/orders", sales_missing: "#/sales", unmapped_barcode: "#/products",
    forecast_nightly_failed: "#/model", forecast_train_failed: "#/model", accuracy_drop: "#/model" };
  function bumpBell(delta) {
    var el = document.getElementById("bell-count");
    if (!el) return;
    var n = Math.max(0, (parseInt(el.textContent, 10) || 0) + delta);
    el.textContent = String(n); el.hidden = !n;
  }
  DQ.route("#/alerts", { title: "Alerts", nav: "Alerts", render: function (main, params, query, alive) {
    var show = query.show === "all" ? "all" : "open";
    var storeF = query.store || "", kindF = query.kind || "";
    function url(o) {
      var q = { store: storeF, kind: kindF, show: show === "all" ? "all" : "" };
      Object.keys(o).forEach(function (k) { q[k] = o[k]; });
      return "#/alerts" + DQ.qs(q);
    }
    main.appendChild(ui.page("Alerts", { sub: "Things that need someone's attention, newest and most urgent first.",
      actions: [h("a", { class: "btn btn-ghost", href: "#/alerts/settings" }, "My alert settings")] }));
    main.appendChild(ui.tabs([{ value: "open", label: "Open" }, { value: "all", label: "All, including done" }], show, function (v) { DQ.go(url({ show: v === "all" ? "all" : "" })); }));
    var filters = h("div", { class: "row a-filters" });
    main.appendChild(filters);
    var body = h("div", { class: "stack" }, ui.skeleton(5));
    main.appendChild(body);
    function load() {
      var qs = DQ.qs({ open: show === "all" ? "false" : "true", store_id: storeF, kind: kindF });
      return Promise.all([DQ.get("/api/alerts" + qs), settle(DQ.get("/api/alerts/settings"))]).then(function (res) {
        if (!alive()) return;
        var alerts = res[0], kinds = res[1].ok ? res[1].value.kinds : [];
        DQ.clear(filters);
        var stores = DQ.stores();
        if (stores.length > 1) filters.appendChild(ui.field("Store", ui.select([{ value: "", label: "Every store" }].concat(stores.map(function (s) { return { value: s.id, label: s.name }; })), storeF,
          { onchange: function (e) { DQ.go(url({ store: e.target.value })); } })));
        if (kinds.length) filters.appendChild(ui.field("Kind", ui.select([{ value: "", label: "Every kind" }].concat(kinds.map(function (k) { return { value: k.kind, label: k.label }; })), kindF,
          { onchange: function (e) { DQ.go(url({ kind: e.target.value })); } })));
        filters.appendChild(h("span", { class: "a-filters-fresh" }, asOf(new Date().toISOString())));
        DQ.clear(body);
        if (!alerts.length) {
          body.appendChild(ui.empty({ title: storeF || kindF ? "No alerts match these filters" : show === "open" ? "Nothing needs attention right now" : "No alerts yet",
            text: "Alerts appear here when stock runs out or runs low, a count is due, an order waits for approval, a delivery is late or the forecast did not run. Each one links to where you fix it, and clears itself once the cause is gone.",
            action: storeF || kindF ? { label: "Clear the filters", href: "#/alerts" } : { label: "Choose which alerts you are emailed", href: "#/alerts/settings" } }));
          return;
        }
        var list = h("ul", { class: "a-list", "aria-label": "Alerts" });
        alerts.forEach(function (a) { list.appendChild(alertItem(a)); });
        body.appendChild(list);
      }, function (err) { if (!alive()) return; DQ.clear(body); body.appendChild(ui.error(err, load)); });
    }
    function alertItem(a) {
      var sev = SEVERITY[a.severity] || [cap(a.severity), "neutral"];
      var li = h("li", { class: "a-item a-alert", "data-kind": a.resolved ? null : sev[1] });
      var acts = h("div", { class: "a-item-actions" });
      var link = a.link || ALERT_LINK[a.kind];
      if (link) acts.appendChild(h("a", { class: "btn btn-primary btn-sm", href: link, onclick: function () {
        if (a.store_id && DQ.stores().some(function (s) { return s.id === a.store_id; })) DQ.setStore(a.store_id);
      } }, "Go to it"));
      if (!a.resolved) {
        var done = ui.button("Done", function () {
          busy(done, "Saving…", DQ.post("/api/alerts/" + a.id + "/resolve")).then(function () {
            bumpBell(-1);
            if (show === "open") { li.remove(); if (!body.querySelector(".a-alert")) load(); }
            else { a.resolved = true; li.replaceWith(alertItem(a)); }
            DQ.toast("Marked done: " + a.label + ".", { undo: function () {
              DQ.post("/api/alerts/" + a.id + "/reopen").then(function () { bumpBell(1); load(); DQ.toast("Opened again: " + a.label + "."); },
                function (err) { DQ.toast("Could not undo: " + err.message, { kind: "risk" }); });
            } });
          }, swallow);
        }, "ghost", { class: "btn btn-ghost btn-sm", "aria-label": "Mark done: " + a.label });
        acts.appendChild(done);
      }
      li.appendChild(h("div", { class: "a-item-main" },
        h("div", { class: "a-item-title" }, a.resolved ? ui.badge("Done", "neutral") : ui.badge(sev[0], sev[1]), h("h3", { class: "title-3" }, a.label)),
        h("p", null, a.message),
        h("p", { class: "field-help" }, (a.store_id ? storeName(a.store_id) + " · " : "") + "Raised " + when(a.created_at) + ", " + ago(a.created_at))));
      li.appendChild(acts);
      return li;
    }
    return load();
  } });

  DQ.route("#/alerts/settings", { title: "My alert settings", render: function (main, params, query, alive) {
    markNav("#/alerts");
    main.appendChild(ui.page("My alert settings", { sub: "Alerts always appear in the app. Choose which ones are also emailed to you.",
      actions: [h("a", { class: "btn btn-ghost", href: "#/alerts" }, "Back to alerts")] }));
    var body = h("div", { class: "stack" }, ui.skeleton(5));
    main.appendChild(body);
    function load() {
      return DQ.get("/api/alerts/settings").then(function (s) {
        if (!alive()) return;
        DQ.clear(body);
        var hasEmail = !!(DQ.me.person && DQ.me.person.email);
        var email = checkbox("Email me alerts", s.email, hasEmail ? "Sent to " + DQ.me.person.email + "." : "Your account has no email address, so alerts stay in the app.");
        if (!hasEmail) email.input.disabled = true;
        var wa = checkbox("WhatsApp me alerts", s.whatsapp, s.whatsapp_note);
        var kinds = checkGroup("Email me about", s.kinds.map(function (k) { return { value: k.kind, label: k.label }; }),
          s.kinds.filter(function (k) { return s.muted.indexOf(k.kind) < 0; }).map(function (k) { return k.kind; }),
          "Only the alerts your role receives are listed. Unticked ones still show in the app.");
        function sync() { kinds.el.disabled = !email.input.checked; }
        email.input.addEventListener("change", sync); sync();
        body.appendChild(h("div", { class: "card" }, form([
          email.el, wa.el, kinds.el
        ], "Save my settings", function () {
          var on = kinds.values();
          var muted = s.kinds.map(function (k) { return k.kind; }).filter(function (k) { return on.indexOf(k) < 0; });
          var before = { email: s.email, whatsapp: s.whatsapp, muted: s.muted };
          return DQ.api("PUT", "/api/alerts/settings", { email: email.input.checked, whatsapp: wa.input.checked, muted: muted }).then(function (s2) {
            s = s2;
            DQ.toast("Alert settings saved.", { undo: function () {
              DQ.api("PUT", "/api/alerts/settings", before).then(function () { DQ.toast("Settings put back."); if (alive()) load(); }, function (e) { DQ.toast(e.message, { kind: "risk" }); });
            } });
          });
        })));
      }, function (err) { if (!alive()) return; DQ.clear(body); body.appendChild(ui.error(err, load)); });
    }
    return load();
  } });

  /* ================================================================ Activity log */

  var ENTITIES = { alert: "Alerts", api_key: "Integration keys", bill: "Bills", calendar_event: "Calendar", company: "Company", count: "Counts",
    day_close: "Day close", decision: "Forecast decisions", delivery: "Deliveries", forecast_run: "Forecast runs", import: "Imports", person: "People",
    product: "Products", purchase_order: "Purchase orders", recommendation: "Forecast decisions", store: "Stores", supplier: "Suppliers",
    support_grant: "Support access", transfer: "Transfers" };
  var ACTION_WORDS = { "forecast.approve": "Forecast approved", "forecast.change": "Forecast changed", "forecast.skip": "Forecast skipped",
    "person.signed_in": "Signed in", "person.reset_by_admin": "Password reset by an administrator", "person.pin_set": "Manager PIN set",
    "setup.marked": "Setup step marked", "stock.opening": "Opening stock set", "integration.key_created": "Integration key made",
    "integration.key_revoked": "Integration key revoked", "support.granted": "Support access granted", "support.revoked": "Support access ended",
    "day.closed": "Day closed", "sales.entered": "Sales entered" };
  function actionWords(a) {
    if (ACTION_WORDS[a]) return ACTION_WORDS[a];
    var p = String(a).split(".");
    return cap((p[0] || "").replace(/_/g, " ") + (p[1] ? " " + p[1].replace(/_/g, " ") : ""));
  }
  function showVal(v) {
    if (v === null || v === undefined || v === "") return "empty";
    if (Array.isArray(v) && !v.length) return "none";
    if (typeof v === "boolean") return v ? "yes" : "no";
    if (typeof v === "string") return /^\d{4}-\d{2}-\d{2}T/.test(v) ? when(v) : v;
    if (typeof v === "number") return Number.isInteger(v) ? fmt.num(v) : String(v);
    var s = JSON.stringify(v);
    return s.length > 160 ? s.slice(0, 157) + "…" : s;
  }
  function subjectOf(r) {
    var o = (r.after && typeof r.after === "object" ? r.after : null) || (r.before && typeof r.before === "object" ? r.before : null) || {};
    var v = o.name || o.number || o.filename || o.sku || null;
    return typeof v === "string" ? v : null;
  }
  function changesOf(r) {
    var b = r.before || {}, a = r.after || {};
    if (typeof b !== "object" || typeof a !== "object") return [];
    var keys = Object.keys(b).concat(Object.keys(a)).filter(function (k, i, all) { return all.indexOf(k) === i; });
    return keys.filter(function (k) { return JSON.stringify(b[k]) !== JSON.stringify(a[k]); }).map(function (k) {
      return { key: k.replace(/_/g, " "), before: r.before ? b[k] : undefined, after: r.after ? a[k] : undefined };
    });
  }
  function changesCell(r) {
    var ch = changesOf(r);
    if (!ch.length) return h("span", { class: "text-3" }, "—");
    function line(c) {
      return h("li", null, h("span", { class: "a-ch-key" }, cap(c.key) + ": "),
        c.before !== undefined ? h("span", { class: "a-ch-before" }, showVal(c.before)) : null,
        c.before !== undefined && c.after !== undefined ? " → " : null,
        c.after !== undefined ? h("span", { class: "a-ch-after" }, showVal(c.after)) : (c.before !== undefined ? " (removed)" : null));
    }
    if (ch.length <= 3) return h("ul", { class: "a-changes" }, ch.map(line));
    return h("details", { class: "a-changes-more" }, h("summary", null, plural(ch.length, "change")), h("ul", { class: "a-changes" }, ch.map(line)));
  }
  DQ.route("#/activity", { title: "Activity log", nav: "Activity log", can: "activity.view", render: function (main, params, query, alive) {
    var entity = query.entity || "", person = query.person || "", limit = ["100", "500", "1000"].indexOf(query.limit) >= 0 ? query.limit : "100";
    var serverPeople = DQ.can("people.manage");
    function url(o) {
      var q = { entity: entity, person: person, limit: limit === "100" ? "" : limit };
      Object.keys(o).forEach(function (k) { q[k] = o[k]; });
      return "#/activity" + DQ.qs(q);
    }
    var rows = [];
    var exportBtn = ui.button("Export as CSV", function () { exportCsv(); }, "ghost");
    exportBtn.disabled = true;
    main.appendChild(ui.page("Activity log", { sub: "Who did what, when and from which device. Nothing here can be changed or deleted.", actions: [exportBtn] }));
    var filters = h("div", { class: "row a-filters" });
    main.appendChild(filters);
    var body = h("div", { class: "stack" }, ui.skeleton(8));
    main.appendChild(body);
    function load() {
      var qs = DQ.qs({ limit: limit, entity: entity, person_id: serverPeople ? person : "" });
      return Promise.all([DQ.get("/api/activity" + qs), serverPeople ? settle(DQ.get("/api/people")) : Promise.resolve(null)]).then(function (res) {
        if (!alive()) return;
        rows = res[0];
        var people = res[1] && res[1].ok ? res[1].value : null;
        if (!serverPeople && person) rows = rows.filter(function (r) { return r.person === person; });
        DQ.clear(filters);
        var ents = Object.keys(ENTITIES).filter(function (k) { return k !== "recommendation"; });
        if (entity && ents.indexOf(entity) < 0) ents.push(entity);
        filters.appendChild(ui.field("What", ui.select([{ value: "", label: "Everything" }].concat(ents.map(function (k) { return { value: k, label: ENTITIES[k] || cap(k.replace(/_/g, " ")) }; }))
          .sort(function (a, b) { return a.value === "" ? -1 : b.value === "" ? 1 : a.label.localeCompare(b.label); }), entity,
          { onchange: function (e) { DQ.go(url({ entity: e.target.value })); } })));
        var personOpts = people ? people.map(function (p) { return { value: p.id, label: p.name }; }) :
          res[0].map(function (r) { return r.person; }).filter(function (n, i, all) { return n && all.indexOf(n) === i; }).sort().map(function (n) { return { value: n, label: n }; });
        if (person && !personOpts.some(function (o) { return o.value === person; })) personOpts.push({ value: person, label: person });
        filters.appendChild(ui.field("Who", ui.select([{ value: "", label: "Everyone" }].concat(personOpts), person,
          { onchange: function (e) { DQ.go(url({ person: e.target.value })); } })));
        filters.appendChild(ui.field("How many", ui.select([{ value: "100", label: "Latest 100" }, { value: "500", label: "Latest 500" }, { value: "1000", label: "Latest 1,000" }], limit,
          { onchange: function (e) { DQ.go(url({ limit: e.target.value })); } })));
        DQ.clear(body);
        exportBtn.disabled = !rows.length;
        if (!rows.length) {
          body.appendChild(ui.empty({ title: entity || person ? "Nothing matches these filters" : "Nothing recorded yet",
            text: "Every change anyone makes, from a price to a count, is recorded here with the time and the device.",
            action: entity || person ? { label: "Clear the filters", href: "#/activity" } : null }));
          return;
        }
        var mine = DQ.deviceId();
        body.appendChild(h("p", { class: "field-help" }, "Showing " + plural(rows.length, "entry", "entries") + ". Times are IST. ", asOf(new Date().toISOString())));
        body.appendChild(ui.table([
          { key: "at", label: "When", render: function (r) { return h("span", { class: "a-nowrap", title: ago(r.at) }, when(r.at)); } },
          { key: "person", label: "Who", render: function (r) { return r.person || "The system"; } },
          { key: "action", label: "What", render: function (r) {
            return h("div", null, h("div", null, actionWords(r.action)),
              h("div", { class: "field-help" }, (ENTITIES[r.entity] || cap(String(r.entity || "").replace(/_/g, " "))) + (subjectOf(r) ? " · " + subjectOf(r) : "") + (r.store_id ? " · " + storeName(r.store_id) : "")));
          } },
          { key: "changes", label: "Before and after", render: changesCell },
          { key: "device", label: "Device", render: function (r) { return r.device ? h("span", { class: "mono" }, r.device === mine ? "This device" : r.device) : "—"; } }
        ], rows, { caption: "Activity log" }));
      }, function (err) { if (!alive()) return; DQ.clear(body); body.appendChild(ui.error(err, load)); });
    }
    function exportCsv() {
      var head = ["When (IST)", "Person", "Action", "Area", "Record id", "Store", "Device", "Before", "After"];
      var lines = [head.join(",")].concat(rows.map(function (r) {
        var s = ist(r.at);
        return [s.slice(0, 10) + " " + s.slice(11, 19), r.person || "The system", actionWords(r.action), ENTITIES[r.entity] || r.entity, r.entity_id,
          r.store_id ? storeName(r.store_id) : "", r.device, r.before, r.after].map(csvCell).join(",");
      }));
      downloadText("dqstack-activity-" + todayIST() + ".csv", lines.join("\r\n"));
      DQ.toast("Exported " + plural(rows.length, "entry", "entries") + ".");
    }
    return load();
  } });
})();
