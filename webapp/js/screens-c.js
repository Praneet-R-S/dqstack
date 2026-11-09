/* Screens, group c: the floor and the till.

   Count, Receive, Transfers, Wastage (section 9), Checkout, Bills and returns, Day
   close and Sales (sections 7 and 12). Phone and tablet first: big numbers, an
   on-screen number pad, a scan box that keeps focus, 44px targets.

   Offline is normal. Counts, deliveries and transfers go through DQ.queue with a key
   made on the device, so a resend changes nothing. The till keeps its own list of
   paid bills and sends them in order through /api/checkout/sync. The product
   catalogue is kept on the device so a scan resolves instantly, even offline. */
(function () {
  "use strict";
  var DQ = window.DQ, h = DQ.h, fmt = DQ.fmt, ui = DQ.ui;

  /* ================================================================ small helpers */
  function lsGet(k, def) { try { var v = localStorage.getItem(k); return v ? JSON.parse(v) : def; } catch (e) { return def; } }
  function lsSet(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); return true; } catch (e) { return false; } }
  function ck(name) { return "dq.c." + ((DQ.me && DQ.me.company && DQ.me.company.id) || "x") + "." + name; }
  function money(p, o) { return fmt.rupees(p, o); }
  function qty(m, unit) { return fmt.qty(m, unit); }
  /* The API sends UTC times without an offset ("2026-11-09T12:35:45"); a browser reads
     those as local time, 5 h 30 min early in India. Mark them UTC before formatting. */
  function utc(iso) { return iso && /T\d\d:\d\d/.test(iso) && !/(Z|[+-]\d\d:?\d\d)$/.test(iso) ? iso + "Z" : iso; }
  var T = {
    dt: function (iso) { return fmt.dateTime(utc(iso)); },
    time: function (iso) { return fmt.time(utc(iso)); },
    ago: function (iso) { return fmt.ago(utc(iso)); },
    date: function (iso) { return iso && iso.length > 10 ? fmt.date(new Date(new Date(utc(iso)).getTime() + 330 * 60000).toISOString()) : fmt.date(iso); }
  };
  function bare(m, unit) { return fmt.qty(m, unit).replace(/ [a-z]+$/i, ""); }
  function unitWord(unit) { return unit && unit !== "each" ? unit : "units"; }
  function storeName(id) { var s = DQ.stores().filter(function (x) { return x.id === id; })[0]; return s ? s.name : "another store"; }
  function storeById(id) { return DQ.stores().filter(function (x) { return x.id === id; })[0] || null; }
  function words(s) { return String(s || "").replace(/_/g, " "); }
  function cap(s) { s = words(s); return s.charAt(0).toUpperCase() + s.slice(1); }
  function debounce(fn, ms) { var t; return function () { var a = arguments, self = this; clearTimeout(t); t = setTimeout(function () { fn.apply(self, a); }, ms); }; }
  function fine() { return window.matchMedia && window.matchMedia("(pointer: fine)").matches; }
  function card(title, body, opts) {
    opts = opts || {};
    return h("section", { class: "card " + (opts.cls || ""), "aria-label": title || null },
      title ? h("div", { class: "c-card-head" }, h("h2", { class: "title-3" }, title), opts.actions || null) : null, body);
  }
  function msgBox(kind, text) { return h("p", { class: "c-msg", "data-kind": kind, role: kind === "risk" ? "alert" : "status" }, text); }
  function noStore(main) {
    main.appendChild(ui.empty({ title: "No store yet", text: "Add a store first. Every screen here works on one store at a time.", action: { label: "Go to Settings", href: "#/settings" } }));
  }
  /* Whole-unit check made on the device, the same rule the server applies. */
  function qtyProblem(milli, product, allowZero) {
    if (milli === null || isNaN(milli)) return "Enter a number.";
    if (milli < 0) return "A quantity cannot be negative.";
    if (!allowZero && milli === 0) return "Enter more than zero.";
    if (product && (product.unit || "each") === "each" && milli % 1000) return product.name + " is counted in whole units.";
    return null;
  }
  function setErr(input, text) {
    var e = document.getElementById(input.id + "-err");
    input.setAttribute("aria-invalid", text ? "true" : "false");
    if (e) { e.textContent = text || ""; e.hidden = !text; }
  }

  /* Send a write now when we can; otherwise keep it in the offline queue. A network
     failure falls back to the queue with the same key, so nothing happens twice. */
  function send(label, method, path, body) {
    if (!navigator.onLine || DQ.queue.count() > 0) {
      DQ.queue.add(label, method, path, body);
      return Promise.resolve({ queued: true });
    }
    return DQ.api(method, path, body).then(function (data) { return { sent: true, data: data }; }, function (err) {
      if (err.status === 0 || err.status >= 500) { DQ.queue.add(label, method, path, body); return { queued: true }; }
      throw err;
    });
  }
  function queuedKeys() { var o = {}; DQ.queue.items().forEach(function (i) { if (i.body && i.body.key) o[i.body.key] = true; }); return o; }

  /* ================================================================ catalogue on the device */
  var cat = {
    rows: null, at: null, map: {}, codes: {}, loading: null,
    all: function () {
      if (!cat.rows) { var c = lsGet(ck("catalogue"), null); cat.rows = c ? c.rows : []; cat.at = c ? c.at : null; cat.index(); }
      return cat.rows;
    },
    index: function () {
      cat.map = {}; cat.codes = {};
      cat.rows.forEach(function (p) { cat.map[p.id] = p; if (p.sku) cat.codes["SKU:" + p.sku.toUpperCase()] = { p: p, n: 1 }; });
      cat.rows.forEach(function (p) { (p.barcodes || []).forEach(function (b) { cat.codes[b.code] = { p: p, n: b.units_per_scan || 1 }; }); });
    },
    save: function () { lsSet(ck("catalogue"), { at: cat.at, rows: cat.rows }); },
    load: function (force) {
      cat.all();
      if (cat.loading) return cat.loading;
      if (!force && cat.rows.length && cat.at && Date.now() - new Date(cat.at).getTime() < 10 * 60000) return Promise.resolve(cat.rows);
      if (!navigator.onLine && cat.rows.length) return Promise.resolve(cat.rows);
      cat.loading = DQ.get("/api/products?limit=5000").then(function (r) {
        cat.rows = r.rows; cat.at = new Date().toISOString(); cat.index(); cat.save(); cat.loading = null; return cat.rows;
      }, function (e) { cat.loading = null; if (cat.rows.length) return cat.rows; throw e; });
      return cat.loading;
    },
    byId: function (id) { cat.all(); return cat.map[id] || null; },
    put: function (p) {
      cat.all();
      var i = cat.rows.findIndex(function (x) { return x.id === p.id; });
      if (i >= 0) cat.rows[i] = Object.assign({}, cat.rows[i], p); else cat.rows.push(p);
      cat.index(); cat.save();
      return cat.map[p.id];
    },
    /* Instant, offline: a barcode (a case barcode adds the case), a scale label
       (2 + 5-digit item + 5-digit grams + check digit), or a SKU. */
    resolve: function (code) {
      cat.all(); code = String(code).trim();
      var hit = cat.codes[code];
      if (hit) return { product: hit.p, qty_milli: hit.n * 1000 };
      if (/^2\d{12}$/.test(code)) { hit = cat.codes[code.slice(0, 7)]; if (hit) return { product: hit.p, qty_milli: parseInt(code.slice(7, 12), 10), weighed: true }; }
      hit = cat.codes["SKU:" + code.toUpperCase()];
      if (hit) return { product: hit.p, qty_milli: 1000 };
      return null;
    },
    search: function (q) {
      var t = String(q).trim().toLowerCase();
      if (!t) return [];
      return cat.all().filter(function (p) {
        return (p.name || "").toLowerCase().indexOf(t) >= 0 || (p.sku || "").toLowerCase().indexOf(t) >= 0 ||
          (p.barcodes || []).some(function (b) { return b.code.indexOf(t) === 0; });
      }).sort(function (a, b) { return (a.status === "active" ? 0 : 1) - (b.status === "active" ? 0 : 1) || a.name.localeCompare(b.name); }).slice(0, 8);
    },
    lookupRemote: function (code) {
      return DQ.get("/api/products/lookup" + DQ.qs({ code: code })).then(function (r) {
        if (!r.found) return { found: false, message: r.message };
        return { found: true, product: cat.put(r.product), qty_milli: (r.units_per_scan || 1) * 1000 };
      });
    }
  };
  DQ.catalogue = cat;

  /* ================================================================ product picker
     One box for a scanner, a typed barcode or SKU, or a name search, plus the camera. */
  var pickerSeq = 0;
  function picker(opts) {
    var id = "c-pick-" + (++pickerSeq);
    var box = ui.input({ id: id, type: "search", autocomplete: "off", autocapitalize: "off", spellcheck: "false", enterkeyhint: "go",
      placeholder: opts.placeholder || "Scan, or type a name, SKU or barcode", "aria-describedby": id + "-msg",
      role: "combobox", "aria-expanded": "false", "aria-controls": id + "-list", "aria-autocomplete": "list" });
    var msg = h("p", { class: "c-pick-msg", id: id + "-msg", role: "status", "aria-live": "polite" });
    var list = h("ul", { class: "c-pick-list", id: id + "-list", role: "listbox", hidden: true, "aria-label": "Matching products" });
    var results = [], active = -1;
    var cam = h("button", { class: "btn btn-ghost c-cam", type: "button", "aria-label": "Scan with the camera", title: "Scan with the camera",
      onclick: function () { DQ.scan.camera(handle); } }, h("span", { class: "c-cam-icon", "aria-hidden": "true" }), "Camera");
    var el = h("div", { class: "c-picker" },
      h("label", { class: "c-pick-label", for: id }, opts.label || "Scan or search a product"),
      h("div", { class: "c-pick-row" }, box, cam), list, msg);

    function say(text, kind) { msg.textContent = text || ""; msg.setAttribute("data-kind", kind || ""); }
    function close() { list.hidden = true; box.setAttribute("aria-expanded", "false"); results = []; active = -1; DQ.clear(list); }
    function show(rows) {
      results = rows; active = rows.length ? 0 : -1; DQ.clear(list);
      rows.forEach(function (p, i) {
        list.appendChild(h("li", { role: "option", id: id + "-o" + i, "aria-selected": i === active ? "true" : "false" },
          h("button", { type: "button", class: "c-pick-opt", tabindex: -1, onpointerdown: function (e) { e.preventDefault(); },
            onclick: function () { choose(p, 1000); } },
            h("span", { class: "c-pick-name" }, p.name),
            h("span", { class: "c-pick-meta mono" }, p.sku + (p.barcodes && p.barcodes[0] ? " · " + p.barcodes[0].code : "")))));
      });
      list.hidden = !rows.length;
      box.setAttribute("aria-expanded", rows.length ? "true" : "false");
      box.setAttribute("aria-activedescendant", rows.length ? id + "-o0" : "");
    }
    function mark() {
      Array.prototype.forEach.call(list.children, function (li, i) { li.setAttribute("aria-selected", i === active ? "true" : "false"); });
      box.setAttribute("aria-activedescendant", active >= 0 ? id + "-o" + active : "");
    }
    function choose(p, q, meta) { close(); box.value = ""; say(""); opts.onPick(p, q, meta || {}); }
    function handle(code) {
      code = String(code).trim();
      if (!code) return;
      if (active >= 0 && results[active] && !/^\d{6,}$/.test(code)) { choose(results[active], 1000); return; }
      var hit = cat.resolve(code);
      if (hit) { choose(hit.product, hit.qty_milli, { weighed: hit.weighed, code: code }); return; }
      if (opts.onCode && opts.onCode(code) === true) { close(); return; }
      var found = cat.search(code);
      if (found.length === 1 && !/^\d{6,}$/.test(code)) { choose(found[0], 1000); return; }
      if (found.length > 1) { box.value = code; show(found); say("Choose one of these, or keep typing."); return; }
      if (!navigator.onLine) { say("“" + code + "” is not in the catalogue saved on this device. Search by name, or try again when the internet is back.", "risk"); return; }
      say("Looking up " + code + "…");
      cat.lookupRemote(code).then(function (r) {
        if (r.found) { choose(r.product, r.qty_milli, { code: code }); return; }
        if (/^\d{6,}$/.test(code) || /^[A-Z0-9-]+$/i.test(code) && code.indexOf(" ") < 0 && code.length > 3) {
          return DQ.get("/api/products" + DQ.qs({ q: code, limit: 8 })).then(function (s) {
            if (s.rows.length === 1) { choose(cat.put(s.rows[0]), 1000); return; }
            if (s.rows.length) { s.rows.forEach(cat.put); box.value = code; show(s.rows); say("Choose one of these."); return; }
            say((/^\d+$/.test(code) ? "Barcode " : "") + code + " is not in your catalogue. Check the number, or ask a manager to add the product.", "risk");
          });
        }
        say(r.message + " Ask a manager to add it.", "risk");
      }, function (err) { say(err.message, "risk"); });
    }
    DQ.scan.attach(box, handle);
    box.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown" && results.length) { e.preventDefault(); active = Math.min(active + 1, results.length - 1); mark(); }
      else if (e.key === "ArrowUp" && results.length) { e.preventDefault(); active = Math.max(active - 1, 0); mark(); }
      else if (e.key === "Escape") close();
    });
    box.addEventListener("input", debounce(function () {
      var v = box.value.trim();
      if (v.length < 2 || /^\d{6,}$/.test(v)) { close(); return; }
      var rows = cat.search(v);
      if (rows.length || !navigator.onLine) { show(rows); if (!rows.length) say("No product matches “" + v + "”."); else say(""); return; }
      DQ.get("/api/products" + DQ.qs({ q: v, limit: 8 })).then(function (s) { if (box.value.trim() === v) { show(s.rows); say(s.rows.length ? "" : "No product matches “" + v + "”."); } }, function () {});
    }, 180));
    box.addEventListener("blur", function () { setTimeout(function () { if (document.activeElement !== box) { list.hidden = true; box.setAttribute("aria-expanded", "false"); } }, 150); });
    cat.load().catch(function () { /* the picker still searches the server */ });
    return { el: el, box: box, focus: function () { box.focus({ preventScroll: true }); }, say: say, handle: handle };
  }

  /* ================================================================ number pad */
  function numpad(input, opts) {
    opts = opts || {};
    var keys = ["7", "8", "9", "4", "5", "6", "1", "2", "3", opts.decimal === false ? "" : ".", "0", "del"];
    function press(k) {
      var v = input.value;
      if (k === "del") v = v.slice(0, -1);
      else if (k === ".") { if (v.indexOf(".") < 0) v = (v || "0") + "."; }
      else v = v === "0" ? k : v + k;
      input.value = v;
      input.dispatchEvent(new Event("input", { bubbles: true }));
    }
    return h("div", { class: "c-pad", role: "group", "aria-label": "Number pad" }, keys.map(function (k) {
      if (!k) return h("span", { "aria-hidden": "true" });
      return h("button", { type: "button", class: "c-key" + (k === "del" ? " c-key-del" : ""),
        "aria-label": k === "del" ? "Delete the last digit" : k === "." ? "Decimal point" : k,
        onpointerdown: function (e) { e.preventDefault(); }, onclick: function () { press(k); } }, k === "del" ? "⌫" : k);
    }));
  }

  /* A small modal for forms that DQ.confirm does not cover. body(close) returns nodes. */
  function modal(title, build) {
    return new Promise(function (resolve) {
      var dlg = h("dialog", { class: "dialog c-dialog", "aria-labelledby": "c-dlg-title" });
      function done(v) { if (dlg.open) dlg.close(); dlg.remove(); resolve(v); }
      dlg.appendChild(h("h2", { id: "c-dlg-title", class: "title-3" }, title));
      dlg.appendChild(build(done));
      dlg.addEventListener("cancel", function (e) { e.preventDefault(); done(null); });
      document.body.appendChild(dlg);
      dlg.showModal();
      var f = dlg.querySelector("input, select, textarea");
      if (f) f.focus();
    });
  }

  /* ================================================================ COUNT */
  var LINE_STATUS = {
    posted: ["Posted", "ok", "Saved. Stock updated."],
    confirmed: ["Matches", "ok", "Saved. Matches the system."],
    waiting: ["Waits for a manager", "attention", "The difference is large, so a manager will review it."],
    conflict: ["Conflict", "risk", "Someone else counted this differently; a manager will pick one."],
    superseded: ["Replaced", "neutral", "A later count of this product replaced this one."],
    rejected: ["Rejected", "risk", "A manager rejected this count. Count it again."]
  };
  var COUNT_STATUS = { open: ["Open", "data"], submitted: ["Submitted, waiting for a manager", "attention"], closed: ["Closed", "ok"] };
  var KIND = { cycle: "Cycle count", full: "Full count", opening: "Opening count" };

  DQ.route("#/count", { title: "Count", nav: "Count", can: "stock.count", render: function (main, params, query, alive) {
    var storeId = DQ.store();
    if (!storeId) return noStore(main);
    var approvals = DQ.can("stock.approve_diff", storeId) ? h("a", { class: "btn btn-ghost", href: "#/count/approvals" }, "Differences to approve") : null;
    main.appendChild(ui.page("Count", { sub: "Count what is on the shelf in " + storeName(storeId) + ". Small differences post by themselves; large ones wait for a manager.", actions: approvals }));

    var kind = ui.select([{ value: "cycle", label: "Cycle count: what is due, most valuable first" }, { value: "full", label: "Full count: every product" }, { value: "opening", label: "Opening count: first stock for a new store" }], "cycle");
    var blind = h("input", { type: "checkbox", id: "c-blind", class: "c-check" });
    var name = ui.input({ maxlength: 120, placeholder: "For example: Dairy shelf, Monday" });
    var err = h("p", { class: "form-error", role: "alert", hidden: true });
    var key = DQ.key("count");
    var startBtn = ui.button("Start the count", start);
    function start() {
      if (!navigator.onLine) { err.textContent = "Starting a count needs the internet for a moment. Counting itself works offline once it has started."; err.hidden = false; return; }
      err.hidden = true; startBtn.disabled = true; startBtn.textContent = "Starting…";
      var k = kind.value;
      var ids = Promise.resolve([]);
      if (k !== "cycle") ids = cat.load(true).then(function (rows) { return rows.filter(function (p) { return p.status !== "discontinued"; }).map(function (p) { return p.id; }); });
      ids.then(function (pids) {
        return DQ.post("/api/counts", { store_id: storeId, kind: k, blind: blind.checked, name: name.value.trim() || null, product_ids: pids, key: key });
      }).then(function (s) { lsSet(ck("count." + s.id), s); DQ.go("#/count/" + s.id); }, function (e) {
        startBtn.disabled = false; startBtn.textContent = "Start the count"; err.textContent = e.message; err.hidden = false;
      });
    }
    main.appendChild(card("Start a count", h("div", { class: "c-form" },
      h("div", { class: "grid-2" }, ui.field("What to count", kind), ui.field("Name (optional)", name)),
      h("label", { class: "c-checkline", for: "c-blind" }, blind, h("span", null, h("strong", null, "Blind count. "), "Counters do not see the number the system expects, so they count what is really there.")),
      err, h("div", { class: "row" }, startBtn))));

    var body = h("div", { class: "c-stack" }, ui.skeleton(6));
    main.appendChild(body);
    return Promise.all([DQ.get("/api/counts/due" + DQ.qs({ store_id: storeId })), DQ.get("/api/counts" + DQ.qs({ store_id: storeId, limit: 30 }))]).then(function (r) {
      if (!alive()) return;
      var due = r[0], counts = r[1];
      DQ.clear(body);
      var open = counts.filter(function (c) { return c.status !== "closed"; });
      if (open.length) {
        body.appendChild(card("Counts in progress", h("div", { class: "c-list" }, open.map(function (c) {
          return h("a", { class: "c-list-item", href: "#/count/" + c.id },
            h("span", { class: "c-li-main" }, h("strong", null, c.name || KIND[c.kind]), h("span", { class: "text-2" }, " · started " + T.dt(c.created_at))),
            h("span", { class: "c-li-side" }, c.blind ? ui.badge("Blind", "data") : null, ui.badge(COUNT_STATUS[c.status][0], COUNT_STATUS[c.status][1]),
              h("span", { class: "mono" }, c.lines + " counted"), c.waiting ? ui.badge(c.waiting + " to approve", "attention") : null));
        }))));
      }
      var dueRows = due.rows.slice(0, 60);
      body.appendChild(card("Due for counting (" + fmt.num(due.total) + ")", due.total ? h("div", null,
        h("p", { class: "text-2 c-note" }, "Class A products sell the most and are counted most often. A cycle count takes this list."),
        ui.table([
          { key: "abc_class", label: "Class", render: function (x) { return ui.badge("Class " + x.abc_class, x.abc_class === "A" ? "data" : "neutral"); } },
          { key: "name", label: "Product", render: function (x) { return h("span", null, x.name, h("br"), h("span", { class: "text-3 mono" }, x.sku)); } },
          { key: "last_counted_at", label: "Last counted", render: function (x) { return x.last_counted_at ? T.ago(x.last_counted_at) : "Never"; } },
          { key: "value_paise", label: "Value at cost", kind: "money" }
        ], dueRows, { caption: "Products due for counting" }),
        due.total > dueRows.length ? h("p", { class: "text-3 c-note" }, "And " + fmt.num(due.total - dueRows.length) + " more.") : null)
        : ui.empty({ title: "Nothing is due for counting", text: "Every product was counted recently enough. A full count is still possible above." })));
      var done = counts.filter(function (c) { return c.status === "closed"; });
      body.appendChild(card("Recent counts", ui.table([
        { key: "name", label: "Count", render: function (c) { return c.name || KIND[c.kind]; } },
        { key: "created_at", label: "Started", render: function (c) { return T.dt(c.created_at); } },
        { key: "status", label: "Status", render: function (c) { return ui.badge(COUNT_STATUS[c.status][0], COUNT_STATUS[c.status][1]); } },
        { key: "lines", label: "Lines", kind: "num" }
      ], done, { onRow: function (c) { DQ.go("#/count/" + c.id); }, caption: "Recent counts", empty: { title: "No finished counts yet", text: "Start a cycle count above. It takes the products that are due, most valuable first." } })));
    });
  } });

  /* ---------------------------------------------------------------- approvals */
  DQ.route("#/count/approvals", { title: "Count differences", can: "stock.approve_diff", render: function (main, params, query, alive) {
    var storeId = DQ.store();
    if (!storeId) return noStore(main);
    main.appendChild(ui.page("Count differences to approve", { sub: "Large differences and disagreeing counts in " + storeName(storeId) + ". Every decision needs a reason.", actions: h("a", { class: "btn btn-ghost", href: "#/count" }, "Back to Count") }));
    var body = h("div", null, ui.skeleton(5));
    main.appendChild(body);
    function load() {
      return DQ.get("/api/counts/approvals" + DQ.qs({ store_id: storeId })).then(function (rows) {
        if (!alive()) return;
        DQ.clear(body);
        if (!rows.length) { body.appendChild(ui.empty({ title: "Nothing waits for you", text: "Every count difference has been decided. New ones appear here as people count.", action: { label: "Go to Count", href: "#/count" } })); return; }
        var groups = {};
        rows.forEach(function (r) { var g = r.session_id + ":" + r.product_id; (groups[g] = groups[g] || []).push(r); });
        Object.keys(groups).forEach(function (g) {
          var lines = groups[g], first = lines[0], conflict = lines.some(function (l) { return l.status === "conflict"; });
          body.appendChild(card(first.name, h("div", { class: "c-approve" },
            h("p", { class: "text-2" }, first.sku + (conflict ? " · Counted differently by " + lines.length + " people. Pick the right count, or reject both and count again." : " · The difference is above the automatic limit.")),
            lines.map(function (l) {
              var diff = l.difference_milli;
              return h("div", { class: "c-approve-line" },
                h("dl", { class: "c-figs" },
                  h("div", null, h("dt", null, "Counted"), h("dd", { class: "mono" }, qty(l.counted_milli, l.unit))),
                  h("div", null, h("dt", null, "System expected"), h("dd", { class: "mono" }, qty(l.system_milli, l.unit))),
                  h("div", null, h("dt", null, "Difference"), h("dd", { class: "mono", "data-kind": diff < 0 ? "risk" : "attention" }, (diff > 0 ? "+" : diff < 0 ? "−" : "") + qty(Math.abs(diff), l.unit) + " (" + money(l.difference_value_paise) + ")")),
                  h("div", null, h("dt", null, "Counted by"), h("dd", null, (l.counted_by || "Someone") + ", " + T.dt(l.counted_at)))),
                l.note ? h("p", { class: "text-2" }, "Note: " + l.note) : null,
                h("div", { class: "row" },
                  l.status === "conflict" ? ui.button("Pick this count", function () { decide(l, "pick"); }) : ui.button("Approve the difference", function () { decide(l, "approve"); }),
                  ui.button("Reject, count again", function () { decide(l, "reject"); }, "ghost")));
            })), { cls: "c-approve-card" }));
        });
      });
    }
    function decide(l, action) {
      var t = { approve: ["Approve this difference?", "The stock of " + l.name + " changes to " + qty(l.counted_milli, l.unit) + ".", "Approve"],
        pick: ["Pick this count?", "This count of " + qty(l.counted_milli, l.unit) + " stands and the other is set aside.", "Pick this count"],
        reject: ["Reject this count?", "Nothing posts. Someone should count " + l.name + " again.", "Reject"] }[action];
      DQ.confirm({ title: t[0], message: t[1], reason: true, reasonLabel: "Why? For example: recounted, found stock in the back room", confirmLabel: t[2], danger: action === "reject" }).then(function (r) {
        if (!r.ok) return;
        DQ.post("/api/counts/lines/" + l.line_id + "/decide", { action: action, reason: r.reason }).then(function (res) {
          DQ.toast(l.name + ": " + (action === "reject" ? "rejected. Count it again." : res.message));
          load();
        }, function (e) { DQ.toast(e.message, { kind: "risk" }); });
      });
    }
    return load();
  } });

  /* ---------------------------------------------------------------- counting view */
  DQ.route("#/count/:id", { title: "Counting", can: "stock.count", render: function (main, params, query, alive) {
    var id = params.id, cacheKey = ck("count." + id), localKey = ck("countlocal." + id);
    var s = null, selected = null, lastMsg = null, poll = null;
    main.appendChild(ui.skeleton(6));

    function local() { return lsGet(localKey, []); }
    function saveLocal(list) { lsSet(localKey, list); }
    function load() {
      return DQ.get("/api/counts/" + id).then(function (d) { s = d; lsSet(cacheKey, d); return d; }, function (e) {
        var c = lsGet(cacheKey, null);
        if (e.status === 0 && c) { s = c; return c; }
        throw e;
      });
    }
    function pending() {
      var q = queuedKeys();
      var list = local().filter(function (x) { return q[x.key] || x.state === "refused"; });
      return list;
    }
    function tidyLocal() {
      var q = queuedKeys(), list = local(), keep = list.filter(function (x) { return q[x.key] || x.state === "refused"; });
      if (keep.length !== list.length) saveLocal(keep);
      return list.length - keep.length;
    }

    var pick, qtyIn, recordBtn, selBox, resultBox, linesBox, todoBox, headBox, submitBtn;
    function build() {
      DQ.clear(main);
      var blindNote = s.blind ? ui.badge("Blind count", "data") : null;
      submitBtn = ui.button("Submit the count", submit, "primary");
      headBox = h("div");
      main.appendChild(ui.page(s.name || KIND[s.kind], { sub: storeName(s.store_id) + " · started " + T.dt(s.created_at),
        actions: [h("a", { class: "btn btn-ghost", href: "#/count" }, "All counts"), s.status === "open" ? submitBtn : null] }));
      main.appendChild(headBox);
      var closed = s.status !== "open";
      var entry = null;
      if (!closed) {
        pick = picker({ label: "Scan or search the product you are counting", onPick: function (p, q) {
          if (selected && selected.id === p.id) { addTo(q); return; }
          select(p, q);
        } });
        qtyIn = ui.input({ id: "c-count-qty", inputmode: "none", autocomplete: "off", class: "input c-big-input", "aria-describedby": "c-count-qty-err" });
        qtyIn.addEventListener("keydown", function (e) {
          if (e.key === "Enter") {
            e.preventDefault();
            var v = qtyIn.value.trim();
            if (/^\d{8,}$/.test(v)) { qtyIn.value = ""; pick.handle(v); return; }   // a scanner typed into the box
            record();
          }
        });
        qtyIn.addEventListener("input", function () { setErr(qtyIn, null); });
        recordBtn = ui.button("Record the count", record, "primary", { class: "btn btn-primary btn-block c-record" });
        selBox = h("div", { class: "c-selected", "aria-live": "polite" });
        resultBox = h("div", { "aria-live": "polite" });
        entry = card(null, h("div", { class: "c-entry" }, pick.el, selBox, resultBox), { cls: "c-entry-card" });
      }
      todoBox = h("div");
      linesBox = h("div");
      main.appendChild(h("div", { class: closed ? "" : "c-two" }, entry, h("div", { class: "c-stack" }, todoBox, linesBox)));
      paintHead(); paintSelected(); paintLists();
      if (pick) pick.focus();
    }
    function paintHead() {
      DQ.clear(headBox);
      var st = COUNT_STATUS[s.status];
      var waitingHere = pending().filter(function (x) { return x.state !== "refused"; }).length;
      headBox.appendChild(h("div", { class: "c-banner-row" },
        ui.badge(st[0], st[1]), s.blind ? ui.badge("Blind count: expected numbers are hidden", "data") : null,
        s.waiting ? ui.badge(s.waiting + " difference" + (s.waiting > 1 ? "s" : "") + " wait for a manager", "attention") : null,
        waitingHere ? ui.badge(waitingHere + " line" + (waitingHere > 1 ? "s" : "") + " saved on this device, waiting to send", "attention") : null));
      if (submitBtn) {
        submitBtn.disabled = waitingHere > 0;
        submitBtn.title = waitingHere ? "Wait until every line has been sent." : "";
      }
      if (s.status === "submitted") headBox.appendChild(msgBox("attention", "Submitted. It closes when a manager has decided the differences that wait."));
      if (s.status === "closed") headBox.appendChild(msgBox("ok", "This count is closed" + (s.closed_at ? " since " + T.dt(s.closed_at) : "") + ". Start a new count to count again."));
    }
    function select(p, q) {
      selected = p;
      paintSelected();
      qtyIn.value = q && q !== 1000 ? String(q / 1000) : "";
      if (q === 1000) qtyIn.value = "1";
      if (fine()) { qtyIn.focus(); qtyIn.select(); } else pick.focus();
    }
    function addTo(q) {
      var cur = fmt.toMilli(qtyIn.value) || 0;
      qtyIn.value = String((cur + q) / 1000);
      qtyIn.dispatchEvent(new Event("input"));
      lastMsg = null;
      paintResult(msgBox("ok", selected.name + ": now " + qty(cur + q, selected.unit) + ". Scan again to add one more, or record."));
    }
    function paintSelected() {
      if (!selBox) return;
      DQ.clear(selBox);
      if (!selected) {
        selBox.appendChild(h("p", { class: "text-2 c-hint" }, "Scan a product, or pick one from the list to count. Scanning the same product again adds one."));
        return;
      }
      var already = (s.lines || []).filter(function (l) { return l.product_id === selected.id && l.status !== "superseded"; }).length;
      selBox.appendChild(h("div", { class: "c-sel-head" },
        h("div", null, h("p", { class: "c-sel-name" }, selected.name), h("p", { class: "text-3 mono" }, selected.sku + " · counted in " + unitWord(selected.unit))),
        h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: function () { selected = null; paintSelected(); pick.focus(); } }, "Change")));
      if (already) selBox.appendChild(h("p", { class: "text-2" }, "Already counted " + already + " time" + (already > 1 ? "s" : "") + " in this count. A new count from you replaces your earlier one."));
      selBox.appendChild(ui.field("How many are on the shelf (" + unitWord(selected.unit) + ")", qtyIn));
      selBox.appendChild(numpad(qtyIn, { decimal: selected.unit !== "each" }));
      selBox.appendChild(recordBtn);
    }
    function paintResult(node) { DQ.clear(resultBox); if (node) resultBox.appendChild(node); }
    function record() {
      if (!selected) { pick.say("Scan or choose a product first.", "risk"); pick.focus(); return; }
      var m = fmt.toMilli(qtyIn.value);
      var problem = qtyIn.value.trim() === "" ? "Enter how many you counted. Enter 0 if there are none." : qtyProblem(m, selected, true);
      if (problem) { setErr(qtyIn, problem); qtyIn.focus(); return; }
      var p = selected, entry = { key: DQ.key("cl"), product_id: p.id, name: p.name, unit: p.unit, counted_milli: m, counted_at: new Date().toISOString(), state: "waiting" };
      var list = local(); list.push(entry); saveLocal(list);
      selected = null; qtyIn.value = ""; paintSelected();
      paintResult(msgBox("busy", p.name + ": " + qty(m, p.unit) + " recorded. Sending…"));
      pick.focus();
      send("Count of " + p.name, "POST", "/api/counts/" + id + "/lines", { product_id: p.id, counted_milli: m, counted_at: entry.counted_at, key: entry.key }).then(function (r) {
        if (!alive()) return;
        if (r.queued) {
          paintResult(msgBox("attention", p.name + ": " + qty(m, p.unit) + " saved on this device. It is sent when the internet is back."));
          startPoll();
        } else {
          saveLocal(local().filter(function (x) { return x.key !== entry.key; }));
          var st = LINE_STATUS[r.data.status] || ["", "ok"];
          var detail = r.data.difference_milli !== undefined && !s.blind && r.data.difference_milli !== 0 ? " Difference " + (r.data.difference_milli > 0 ? "+" : "−") + qty(Math.abs(r.data.difference_milli), p.unit) + "." : "";
          paintResult(msgBox(st[1] === "ok" ? "ok" : st[1], p.name + ": " + qty(m, p.unit) + ". " + r.data.message + detail));
          refresh();
        }
        paintHead(); paintLists();
      }, function (e) {
        var l2 = local(); l2.forEach(function (x) { if (x.key === entry.key) { x.state = "refused"; x.error = e.message; } }); saveLocal(l2);
        paintResult(msgBox("risk", p.name + " was not saved: " + e.message));
        paintHead(); paintLists();
      });
    }
    function refresh() { return load().then(function () { if (alive()) { paintHead(); paintLists(); paintSelected(); } }, function () {}); }
    function startPoll() {
      if (poll) return;
      poll = setInterval(function () {
        if (!alive()) { clearInterval(poll); poll = null; return; }
        var sent = tidyLocal();
        if (sent) { refresh(); paintResult(msgBox("ok", "The lines saved on this device have been sent. Their results are in the Counted list.")); }
        if (!pending().some(function (x) { return x.state !== "refused"; })) { clearInterval(poll); poll = null; if (!sent) refresh(); }
      }, 2500);
    }
    function paintLists() {
      var mine = pending();
      var localIds = {}; mine.forEach(function (x) { if (x.state !== "refused") localIds[x.product_id] = true; });
      var todo = (s.still_to_count || []).filter(function (t) { return !localIds[t.product_id]; });
      DQ.clear(todoBox);
      if (s.kind === "cycle" || todo.length || (s.still_to_count || []).length) {
        todoBox.appendChild(card((s.status === "open" ? "Still to count (" : "Not counted (") + todo.length + ")", todo.length ? h("ul", { class: "c-todo" }, todo.slice(0, 200).map(function (t) {
          return h("li", null, s.status === "open" ? h("button", { type: "button", class: "c-todo-btn", onclick: function () {
            select(cat.byId(t.product_id) || { id: t.product_id, name: t.name, sku: t.sku, unit: t.unit }, 0);
          } }, h("span", null, t.name), h("span", { class: "text-3 mono" }, t.sku)) : h("span", null, t.name + " · " + t.sku));
        })) : h("p", { class: "text-2" }, "Everything on this count has been counted. Submit the count when you are done.")));
      }
      DQ.clear(linesBox);
      var showExp = s.shows_expected && !s.blind;
      var rows = (s.lines || []).slice().reverse();
      var items = mine.map(function (x) {
        return h("li", { class: "c-line" },
          h("div", { class: "c-line-main" }, h("strong", null, x.name), h("span", { class: "mono c-line-qty" }, qty(x.counted_milli, x.unit))),
          h("div", { class: "c-line-sub" }, x.state === "refused" ? ui.badge("Not saved", "risk") : ui.badge("Waiting to send", "attention"),
            h("span", { class: "text-2" }, x.state === "refused" ? x.error + " " : "Saved on this device. ", T.time(x.counted_at)),
            x.state === "refused" ? h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: function () { saveLocal(local().filter(function (y) { return y.key !== x.key; })); paintHead(); paintLists(); } }, "Dismiss") : null));
      }).concat(rows.map(function (l) {
        var st = LINE_STATUS[l.status] || [l.status, "neutral", ""];
        return h("li", { class: "c-line" },
          h("div", { class: "c-line-main" }, h("strong", null, l.name), h("span", { class: "mono c-line-qty" }, qty(l.counted_milli, l.unit))),
          h("div", { class: "c-line-sub" }, ui.badge(st[0], st[1]),
            h("span", { class: "text-2" }, st[2] + " " + (l.counted_by || "") + ", " + T.time(l.counted_at)),
            showExp && l.system_milli !== undefined ? h("span", { class: "mono text-2" }, "Expected " + qty(l.system_milli, l.unit) + (l.difference_milli ? ", difference " + (l.difference_milli > 0 ? "+" : "−") + qty(Math.abs(l.difference_milli), l.unit) : "")) : null));
      }));
      linesBox.appendChild(card("Counted (" + items.length + ")", items.length ? h("ul", { class: "c-lines" }, items) : h("p", { class: "text-2" }, "Nothing counted yet. Each line you record appears here with its result.")));
    }
    function submit() {
      var left = (s.still_to_count || []).length;
      DQ.confirm({ title: "Submit the count?", message: (left ? left + " product" + (left > 1 ? "s are" : " is") + " still not counted and will be left as they are. " : "") + "After submitting, nobody can add lines. Differences waiting for a manager still need a decision.", confirmLabel: "Submit the count" }).then(function (r) {
        if (!r.ok) return;
        DQ.post("/api/counts/" + id + "/submit").then(function (d) { s = d; lsSet(cacheKey, d); DQ.toast(d.status === "closed" ? "The count is closed." : "Submitted. A manager decides the differences that wait."); if (alive()) build(); }, function (e) { DQ.toast(e.message, { kind: "risk" }); });
      });
    }
    return load().then(function () {
      if (!alive()) return;
      build();
      if (pending().some(function (x) { return x.state !== "refused"; })) startPoll();
    });
  } });

  /* ================================================================ RECEIVE */
  var REJECT = [{ value: "damaged", label: "Damaged" }, { value: "wrong_item", label: "Wrong item" }, { value: "expired", label: "Expired" }, { value: "short_dated", label: "Short dated" }];
  function suppliers() {
    var c = lsGet(ck("suppliers"), null);
    if (!navigator.onLine && c) return Promise.resolve(c);
    return DQ.get("/api/suppliers").then(function (r) { lsSet(ck("suppliers"), r); return r; }, function (e) { if (c) return c; throw e; });
  }

  DQ.route("#/receive", { title: "Receive", nav: "Receive", can: "stock.receive", render: function (main, params, query, alive) {
    var storeId = DQ.store();
    if (!storeId) return noStore(main);
    var tab = query.tab === "credits" ? "credits" : "orders";
    main.appendChild(ui.page("Receive", { sub: "Book in deliveries for " + storeName(storeId) + ". Good units go to stock; damaged or wrong ones are kept aside for supplier credit.",
      actions: h("a", { class: "btn btn-primary", href: "#/receive/new" }, "Receive without an order") }));
    main.appendChild(ui.tabs([{ value: "orders", label: "Open orders" }, { value: "credits", label: "Supplier credits" }], tab, function (v) { DQ.go("#/receive" + (v === "credits" ? "?tab=credits" : "")); }));
    var body = h("div", null, ui.skeleton(5));
    main.appendChild(body);
    if (tab === "orders") {
      var cKey = ck("openorders." + storeId);
      return DQ.get("/api/receive/orders" + DQ.qs({ store_id: storeId })).then(function (r) { lsSet(cKey, r); return r; }, function (e) { var c = lsGet(cKey, null); if (e.status === 0 && c) return c; throw e; }).then(function (orders) {
        if (!alive()) return;
        DQ.clear(body);
        if (!orders.length) { body.appendChild(ui.empty({ title: "No open orders for this store", text: "Approved and sent purchase orders appear here to receive against. A delivery without an order can still be received.", action: { label: "Receive without an order", href: "#/receive/new" } })); return; }
        body.appendChild(h("div", { class: "c-list" }, orders.map(function (o) {
          var due = o.lines.filter(function (l) { return l.due_milli > 0; }).length;
          return h("a", { class: "c-list-item", href: "#/receive/order/" + o.id },
            h("span", { class: "c-li-main" }, h("strong", null, o.number + " · " + (o.supplier || "No supplier")),
              h("span", { class: "text-2" }, " · " + o.lines.length + " products, " + money(o.total_paise) + (o.expected_date ? " · expected " + T.date(o.expected_date) : ""))),
            h("span", { class: "c-li-side" }, ui.badge(cap(o.status), o.status === "partly_received" ? "attention" : "data"), h("span", { class: "text-2" }, due + " still due")));
        })));
      });
    }
    return Promise.all([DQ.get("/api/deliveries/credits" + DQ.qs({ store_id: storeId })), suppliers().catch(function () { return []; })]).then(function (r) {
      if (!alive()) return;
      var sup = {}; r[1].forEach(function (x) { sup[x.id] = x.name; });
      var rows = r[0];
      DQ.clear(body);
      var canCredit = DQ.can("orders.create", storeId);
      var total = rows.reduce(function (a, x) { return a + x.value_paise; }, 0);
      if (rows.length) body.appendChild(h("p", { class: "text-2 c-note" }, rows.length + " rejected line" + (rows.length > 1 ? "s" : "") + " worth " + money(total) + " at cost " + (rows.length > 1 ? "wait" : "waits") + " for a supplier credit note."));
      body.appendChild(ui.table([
        { key: "received_on", label: "Received", render: function (x) { return T.date(x.received_on); } },
        { key: "supplier_id", label: "Supplier", render: function (x) { return sup[x.supplier_id] || "No supplier"; } },
        { key: "invoice_number", label: "Invoice" },
        { key: "name", label: "Product", render: function (x) { return h("span", null, x.name, h("br"), h("span", { class: "text-3 mono" }, x.sku)); } },
        { key: "units_milli", label: "Units", kind: "qty" },
        { key: "reason", label: "Reason", render: function (x) { return cap(x.reason || "damaged"); } },
        { key: "value_paise", label: "Value", kind: "money" },
        { key: "line_id", label: "", render: function (x) {
          return canCredit ? ui.button("Credit received", function (e) {
            var btn = e.currentTarget;
            DQ.confirm({ title: "Mark the credit as received?", message: "The supplier has given credit for " + qty(x.units_milli) + " " + x.name + " (" + money(x.value_paise) + "). It leaves this list.", confirmLabel: "Credit received" }).then(function (c) {
              if (!c.ok) return;
              btn.disabled = true;
              DQ.post("/api/deliveries/lines/" + x.line_id + "/credited").then(function () { DQ.toast("Credit recorded for " + x.name + "."); DQ.render(); }, function (err) { btn.disabled = false; DQ.toast(err.message, { kind: "risk" }); });
            });
          }, "ghost", { class: "btn btn-ghost btn-sm" }) : null;
        } }
      ], rows, { caption: "Supplier credits waiting", empty: { title: "No credits waiting", text: "When a delivery has damaged, wrong or expired units, they wait here until the supplier gives credit." } }));
    });
  } });

  function receiveView(main, alive, order) {
    var storeId = DQ.store();
    var key = DQ.key("dlv");
    var rows = [];                      // {product, ordered, due, cost, inputs...}
    var listBox = h("div", { class: "c-recv-lines" });
    var invoice = ui.input({ maxlength: 64, autocomplete: "off", placeholder: "As printed on the supplier's invoice" });
    var note = ui.input({ maxlength: 300, placeholder: "Optional" });
    var supplierSel = null;
    var closeShort = h("input", { type: "checkbox", id: "c-close-short", class: "c-check" });
    var err = h("div", { class: "form-error", role: "alert", hidden: true });
    var result = h("div", { "aria-live": "polite" });
    var saveBtn = ui.button("Save the delivery", save, "primary");
    var pick = picker({ label: "Scan or search an item in the delivery", onPick: function (p, q) { addRow(p, q); } });

    main.appendChild(ui.page(order ? "Receive " + order.number : "Receive without an order", {
      sub: order ? (order.supplier || "No supplier") + " · " + storeName(storeId) + (order.expected_date ? " · expected " + T.date(order.expected_date) : "") : storeName(storeId) + ". Use this when a delivery came without a purchase order.",
      actions: h("a", { class: "btn btn-ghost", href: "#/receive" }, "Back") }));

    var head = h("div", { class: "grid-2" }, ui.field("Supplier invoice number", invoice));
    if (!order) {
      supplierSel = ui.select([{ value: "", label: "No supplier, or not known" }], "");
      head.appendChild(ui.field("Supplier (optional)", supplierSel));
      suppliers().then(function (list) { list.forEach(function (s) { supplierSel.appendChild(h("option", { value: s.id }, s.name)); }); }, function () {});
    }
    head.appendChild(ui.field("Note", note));
    var fillBtn = order ? ui.button("Everything arrived as ordered", function () {
      rows.forEach(function (r) { if (r.due > 0 && !r.rec.value) { r.rec.value = String(r.due / 1000); } });
      pick.say("Filled the quantities still due. Change any that differ.", "ok");
    }, "ghost") : null;
    main.appendChild(card(null, h("div", { class: "c-form" }, head, pick.el, fillBtn ? h("div", { class: "row" }, fillBtn) : null)));
    main.appendChild(listBox);
    var foot = h("div", { class: "c-form" },
      order && DQ.can("orders.create", storeId) ? h("label", { class: "c-checkline", for: "c-close-short" }, closeShort, h("span", null, h("strong", null, "Close the order short. "), "Nothing more is expected from this order, even if some items are still due.")) : null,
      err, h("div", { class: "row" }, saveBtn), result);
    main.appendChild(card(null, foot));

    function addRow(p, q, ol) {
      var r = rows.filter(function (x) { return x.product.id === p.id; })[0];
      if (r) {
        if (q && !ol) { r.rec.value = String(((fmt.toMilli(r.rec.value) || 0) + q) / 1000); }
        r.el.scrollIntoView({ block: "nearest", behavior: "smooth" });
        r.rec.focus({ preventScroll: true });
        pick.focus();
        return;
      }
      var full = cat.byId(p.id) || p;
      r = { product: full, ordered: ol ? ol.qty_milli : null, due: ol ? ol.due_milli : null };
      r.rec = ui.input({ inputmode: "decimal", autocomplete: "off", class: "input c-num-input" });
      r.bad = ui.input({ inputmode: "decimal", autocomplete: "off", class: "input c-num-input", placeholder: "0" });
      r.reason = ui.select(REJECT, "damaged");
      var cost = ol && ol.unit_cost_paise !== null && ol.unit_cost_paise !== undefined ? ol.unit_cost_paise : full.cost_paise;
      r.cost = ui.input({ inputmode: "decimal", autocomplete: "off", class: "input c-num-input", value: cost !== null && cost !== undefined ? (cost / 100).toFixed(2) : "" });
      r.batch = ui.input({ maxlength: 64, autocomplete: "off" });
      r.expiry = ui.input({ type: "date" });
      if (!ol && q) r.rec.value = String(q / 1000);
      var reasonField = ui.field("Why rejected", r.reason);
      reasonField.hidden = true;
      r.bad.addEventListener("input", function () { reasonField.hidden = !(fmt.toMilli(r.bad.value) > 0); });
      var perish = full.perishable;
      r.el = h("article", { class: "card c-recv-line" },
        h("div", { class: "c-card-head" },
          h("div", null, h("h3", { class: "title-3" }, full.name), h("p", { class: "text-3 mono" }, (full.sku || "") + (perish ? " · perishable: expiry needed" : ""))),
          ol ? h("p", { class: "mono text-2 c-due" }, "Ordered " + qty(ol.qty_milli, ol.unit) + (ol.received_milli ? ", received " + qty(ol.received_milli, ol.unit) : "") + ", still due " + qty(ol.due_milli, ol.unit))
            : h("button", { type: "button", class: "btn btn-ghost btn-sm", "aria-label": "Remove " + full.name, onclick: function () { rows = rows.filter(function (x) { return x !== r; }); r.el.remove(); } }, "Remove")),
        h("div", { class: "c-recv-grid" },
          ui.field("Good units received", r.rec), ui.field("Rejected units", r.bad), reasonField,
          ui.field("Unit cost (₹)", r.cost), ui.field("Batch", r.batch), ui.field("Expiry" + (perish ? " (required)" : ""), r.expiry)));
      rows.push(r);
      listBox.appendChild(r.el);
      if (!ol) pick.focus();
    }
    if (order) {
      cat.load().catch(function () {}).then(function () {
        if (!alive()) return;
        order.lines.forEach(function (l) { addRow(cat.byId(l.product_id) || { id: l.product_id, name: l.name, sku: l.sku, unit: l.unit }, 0, l); });
        pick.focus();
      });
    } else pick.focus();

    function save() {
      err.hidden = true; DQ.clear(result);
      var problems = [], lines = [];
      rows.forEach(function (r) {
        var good = r.rec.value.trim() ? fmt.toMilli(r.rec.value) : 0, bad = r.bad.value.trim() ? fmt.toMilli(r.bad.value) : 0;
        setErr(r.rec, null); setErr(r.bad, null); setErr(r.expiry, null); setErr(r.cost, null);
        if (!good && !bad) return;
        var pr = qtyProblem(good, r.product, true) || qtyProblem(bad, r.product, true);
        if (pr) { setErr(r.rec, pr); problems.push(r.product.name + ": " + pr); return; }
        var cost = r.cost.value.trim() ? fmt.toPaise(r.cost.value) : null;
        if (r.cost.value.trim() && (cost === null || cost < 0)) { setErr(r.cost, "Enter the cost in rupees, like 42.50."); problems.push(r.product.name + ": the cost is not a number."); return; }
        if (r.product.perishable && good && !r.expiry.value) { setErr(r.expiry, "Enter the expiry date."); problems.push(r.product.name + " is perishable: enter its expiry date."); return; }
        lines.push({ product_id: r.product.id, received_milli: good, damaged_milli: bad, reject_reason: bad ? r.reason.value : null,
          unit_cost_paise: cost, batch: r.batch.value.trim() || null, expiry: r.expiry.value || null });
      });
      if (problems.length) { err.textContent = problems.join(" "); err.hidden = false; return; }
      if (!lines.length) { err.textContent = "Enter how many arrived for at least one item."; err.hidden = false; return; }
      var body = { store_id: storeId, po_id: order ? order.id : null, supplier_id: supplierSel && supplierSel.value ? supplierSel.value : null,
        invoice_number: invoice.value.trim() || null, lines: lines, close_short: closeShort.checked, note: note.value.trim() || null, key: key };
      saveBtn.disabled = true; saveBtn.textContent = "Saving…";
      send("Delivery" + (order ? " for " + order.number : "") + (body.invoice_number ? ", invoice " + body.invoice_number : ""), "POST", "/api/deliveries", body).then(function (r) {
        if (!alive()) return;
        saveBtn.textContent = "Saved";
        DQ.clear(result);
        if (r.queued) {
          result.appendChild(msgBox("attention", "Saved on this device. The delivery is sent once the internet is back, and stock updates then."));
        } else {
          var good = lines.reduce(function (a, l) { return a + l.received_milli; }, 0), bad = lines.reduce(function (a, l) { return a + l.damaged_milli; }, 0);
          result.appendChild(msgBox("ok", (r.data.duplicate ? "This delivery was already saved. Nothing changed. " : "Saved. ") + qty(good) + " good units went to stock" + (bad ? "; " + qty(bad) + " rejected units wait for supplier credit." : ".")));
          var o = r.data.order;
          if (o) {
            var still = o.lines.filter(function (l) { return l.due_milli > 0; });
            result.appendChild(h("div", { class: "c-result" },
              h("p", null, h("strong", null, o.number + " is now " + (o.status === "received" ? (o.closed_short ? "closed short" : "fully received") : words(o.status)) + ".")),
              still.length ? h("div", null, h("p", { class: "text-2" }, "Still due:"), h("ul", { class: "c-due-list" }, still.map(function (l) { return h("li", null, l.name + ": " + qty(l.due_milli, l.unit)); }))) : null));
          }
        }
        result.appendChild(h("div", { class: "row" }, h("a", { class: "btn btn-primary", href: "#/receive" }, "Done"), h("a", { class: "btn btn-ghost", href: "#/receive/new" }, "Receive another delivery")));
      }, function (e) {
        saveBtn.disabled = false; saveBtn.textContent = "Save the delivery";
        err.textContent = e.message; err.hidden = false;
      });
    }
  }
  DQ.route("#/receive/new", { title: "Receive", can: "stock.receive", render: function (main, params, query, alive) {
    if (!DQ.store()) return noStore(main);
    receiveView(main, alive, null);
  } });
  DQ.route("#/receive/order/:id", { title: "Receive", can: "stock.receive", render: function (main, params, query, alive) {
    var storeId = DQ.store();
    if (!storeId) return noStore(main);
    main.appendChild(ui.skeleton(5));
    var cKey = ck("openorders." + storeId);
    return DQ.get("/api/receive/orders" + DQ.qs({ store_id: storeId })).then(function (r) { lsSet(cKey, r); return r; }, function (e) { var c = lsGet(cKey, null); if (e.status === 0 && c) return c; throw e; }).then(function (orders) {
      if (!alive()) return;
      DQ.clear(main);
      var o = orders.filter(function (x) { return x.id === params.id; })[0];
      if (!o) { main.appendChild(ui.empty({ title: "This order is not open for receiving", text: "It may be fully received, cancelled, or for another store. Check the store at the top.", action: { label: "See open orders", href: "#/receive" } })); return; }
      receiveView(main, alive, o);
    });
  } });

  /* ================================================================ TRANSFERS */
  DQ.route("#/transfers", { title: "Transfers", nav: "Transfers", can: "stock.transfer", render: function (main, params, query, alive) {
    var storeId = DQ.store();
    if (!storeId) return noStore(main);
    main.appendChild(ui.page("Transfers", { sub: "Stock moving between " + storeName(storeId) + " and your other stores. What does not arrive is recorded as a transfer loss.",
      actions: DQ.stores().length > 1 ? h("a", { class: "btn btn-primary", href: "#/transfers/new" }, "Send stock to another store") : null }));
    var body = h("div", { class: "c-stack" }, ui.skeleton(5));
    main.appendChild(body);
    return DQ.get("/api/transfers" + DQ.qs({ store_id: storeId, limit: 100 })).then(function (list) {
      if (!alive()) return;
      DQ.clear(body);
      var incoming = list.filter(function (t) { return t.status === "in_transit" && t.to_store_id === storeId; });
      var outgoing = list.filter(function (t) { return t.status === "in_transit" && t.from_store_id === storeId; });
      var done = list.filter(function (t) { return t.status !== "in_transit"; });
      function row(t) {
        var lost = t.lines.reduce(function (a, l) { return a + (l.lost_milli || 0); }, 0);
        var inbound = t.to_store_id === storeId;
        return h("a", { class: "c-list-item", href: "#/transfers/" + t.id },
          h("span", { class: "c-li-main" }, h("strong", null, inbound ? "From " + storeName(t.from_store_id) : "To " + storeName(t.to_store_id)),
            h("span", { class: "text-2" }, " · " + t.lines.length + " product" + (t.lines.length > 1 ? "s" : "") + " · sent " + T.dt(t.created_at))),
          h("span", { class: "c-li-side" }, t.status === "in_transit" ? ui.badge(inbound ? "Arriving: confirm what came" : "On the way", inbound ? "attention" : "data") : ui.badge("Received " + T.date(t.received_at), "ok"),
            lost ? ui.badge("Loss " + qty(lost), "risk") : null));
      }
      body.appendChild(card("Coming in (" + incoming.length + ")", incoming.length ? h("div", { class: "c-list" }, incoming.map(row)) : h("p", { class: "text-2" }, "Nothing is on its way to this store.")));
      body.appendChild(card("Sent, on the way (" + outgoing.length + ")", outgoing.length ? h("div", { class: "c-list" }, outgoing.map(row)) : h("p", { class: "text-2" }, "Nothing sent from this store is on the way.")));
      body.appendChild(card("Received", done.length ? h("div", { class: "c-list" }, done.map(row)) : ui.empty({ title: "No transfers yet", text: "Send stock to another store when one has too much and the other too little.", action: DQ.stores().length > 1 ? { label: "Send stock", href: "#/transfers/new" } : null })));
    });
  } });

  DQ.route("#/transfers/new", { title: "Send stock", can: "stock.transfer", render: function (main, params, query, alive) {
    var storeId = DQ.store();
    if (!storeId) return noStore(main);
    var others = DQ.stores().filter(function (s) { return s.id !== storeId; });
    main.appendChild(ui.page("Send stock from " + storeName(storeId), { sub: "Stock leaves this store now and arrives when the other store confirms it.", actions: h("a", { class: "btn btn-ghost", href: "#/transfers" }, "Back") }));
    if (!others.length) { main.appendChild(ui.empty({ title: "There is no other store to send to", text: "Add a second store in Settings first.", action: { label: "Go to Settings", href: "#/settings" } })); return; }
    var to = ui.select(others.map(function (s) { return { value: s.id, label: s.name }; }), others[0].id);
    var note = ui.input({ maxlength: 300, placeholder: "Optional, for example the driver's name" });
    var rows = [], key = DQ.key("trf");
    var listBox = h("div", { class: "c-lines-edit" });
    var err = h("div", { class: "form-error", role: "alert", hidden: true });
    var result = h("div", { "aria-live": "polite" });
    var sendBtn = ui.button("Send the stock", go);
    var pick = picker({ label: "Scan or search what you are sending", onPick: function (p, q) {
      var r = rows.filter(function (x) { return x.p.id === p.id; })[0];
      if (r) { r.q.value = String(((fmt.toMilli(r.q.value) || 0) + q) / 1000); pick.focus(); return; }
      r = { p: p, q: ui.input({ inputmode: "decimal", class: "input c-num-input", value: String(q / 1000), "aria-label": "Quantity of " + p.name }) };
      r.el = h("li", { class: "c-edit-line" }, h("div", { class: "c-li-main" }, h("strong", null, p.name), h("span", { class: "text-3 mono" }, " " + p.sku + " · " + unitWord(p.unit))),
        h("div", { class: "c-edit-ctl" }, r.q, h("p", { class: "field-error", id: "", hidden: true }),
          h("button", { type: "button", class: "btn btn-ghost btn-sm", "aria-label": "Remove " + p.name, onclick: function () { rows = rows.filter(function (x) { return x !== r; }); r.el.remove(); } }, "Remove")));
      r.q.id = "c-trf-" + p.id; r.el.querySelector(".field-error").id = r.q.id + "-err";
      rows.push(r); listBox.appendChild(r.el); pick.focus();
    } });
    main.appendChild(card(null, h("div", { class: "c-form" }, h("div", { class: "grid-2" }, ui.field("Send to", to), ui.field("Note", note)), pick.el)));
    main.appendChild(card("Items to send", h("div", null, h("ul", { class: "c-edit-list" }, listBox), err, h("div", { class: "row" }, sendBtn), result)));
    pick.focus();
    function go() {
      err.hidden = true;
      var bad = null, lines = rows.map(function (r) {
        var m = fmt.toMilli(r.q.value), pr = qtyProblem(m, r.p);
        setErr(r.q, pr); if (pr && !bad) bad = r.p.name + ": " + pr;
        return { product_id: r.p.id, qty_milli: m };
      });
      if (!lines.length) { err.textContent = "Add at least one item to send."; err.hidden = false; return; }
      if (bad) { err.textContent = bad; err.hidden = false; return; }
      DQ.confirm({ title: "Send to " + storeName(to.value) + "?", message: lines.length + (lines.length > 1 ? " products leave" : " product leaves") + " the stock of " + storeName(storeId) + " now. " + storeName(to.value) + " confirms what arrives.", confirmLabel: "Send the stock" }).then(function (c) {
        if (!c.ok) return;
        sendBtn.disabled = true;
        send("Transfer to " + storeName(to.value), "POST", "/api/transfers", { from_store_id: storeId, to_store_id: to.value, lines: lines, note: note.value.trim() || null, key: key }).then(function (r) {
          if (!alive()) return;
          DQ.clear(result);
          result.appendChild(msgBox(r.queued ? "attention" : "ok", r.queued ? "Saved on this device. The transfer is sent once the internet is back." : (r.data.duplicate ? "This transfer was already sent. Nothing changed." : "Sent. It shows as on the way until " + storeName(to.value) + " confirms it.")));
          result.appendChild(h("div", { class: "row" }, h("a", { class: "btn btn-primary", href: "#/transfers" }, "Done")));
        }, function (e) { sendBtn.disabled = false; err.textContent = e.message; err.hidden = false; });
      });
    }
  } });

  DQ.route("#/transfers/:id", { title: "Transfer", can: "stock.transfer", render: function (main, params, query, alive) {
    main.appendChild(ui.skeleton(4));
    return Promise.all([DQ.get("/api/transfers/" + params.id), cat.load().catch(function () {})]).then(function (r) {
      if (!alive()) return;
      var t = r[0];
      DQ.clear(main);
      var unit = function (pid) { var p = cat.byId(pid); return p ? p.unit : "each"; };
      var canReceive = t.status === "in_transit" && DQ.can("stock.transfer", t.to_store_id);
      main.appendChild(ui.page("Transfer from " + storeName(t.from_store_id) + " to " + storeName(t.to_store_id), {
        sub: "Sent " + T.dt(t.created_at) + (t.received_at ? " · received " + T.dt(t.received_at) : "") + (t.note ? " · " + t.note : ""),
        actions: h("a", { class: "btn btn-ghost", href: "#/transfers" }, "Back") }));
      var key = DQ.key("trfin");
      var inputs = {};
      var cols = [
        { key: "name", label: "Product", render: function (l) { return h("span", null, l.name, h("br"), h("span", { class: "text-3 mono" }, l.sku)); } },
        { key: "sent_milli", label: "Sent", kind: "qty", unit: function (l) { return unit(l.product_id); } }];
      if (canReceive) cols.push({ key: "x", label: "Arrived", kind: "num", render: function (l) {
        var i = ui.input({ inputmode: "decimal", class: "input c-num-input", value: String(l.sent_milli / 1000), "aria-label": "Arrived of " + l.name, id: "c-arr-" + l.product_id });
        inputs[l.product_id] = { input: i, line: l };
        return i;
      } });
      else cols.push({ key: "received_milli", label: "Arrived", kind: "qty", unit: function (l) { return unit(l.product_id); } },
        { key: "lost_milli", label: "Lost on the way", kind: "num", render: function (l) { return l.lost_milli ? h("span", { class: "c-risk-text" }, "−" + qty(l.lost_milli, unit(l.product_id))) : "None"; } });
      main.appendChild(ui.table(cols, t.lines, { caption: "Transfer lines" }));
      if (canReceive) {
        var err = h("div", { class: "form-error", role: "alert", hidden: true }), result = h("div", { "aria-live": "polite" });
        var btn = ui.button("Confirm what arrived", function () {
          err.hidden = true;
          var lines = [], lost = [], bad = null;
          Object.keys(inputs).forEach(function (pid) {
            var x = inputs[pid], m = fmt.toMilli(x.input.value);
            if (m === null || m < 0 || m > x.line.sent_milli) { bad = x.line.name + ": enter between 0 and " + qty(x.line.sent_milli, unit(pid)) + "."; return; }
            lines.push({ product_id: pid, qty_milli: m });
            if (m < x.line.sent_milli) lost.push(x.line.name + " " + qty(x.line.sent_milli - m, unit(pid)));
          });
          if (bad) { err.textContent = bad; err.hidden = false; return; }
          DQ.confirm({ title: "Confirm what arrived?", message: lost.length ? "Short: " + lost.join(", ") + ". The shortfall is recorded as a transfer loss and cannot be received later." : "Everything sent arrived. It goes into the stock of " + storeName(t.to_store_id) + ".", confirmLabel: "Confirm" }).then(function (c) {
            if (!c.ok) return;
            btn.disabled = true;
            DQ.post("/api/transfers/" + t.id + "/receive", { lines: lines, key: key }).then(function (res) {
              DQ.toast(res.losses && res.losses.length ? "Received. Loss recorded: " + res.losses.map(function (l) { return l.name + " " + qty(l.lost_milli) + " (" + money(l.value_paise) + ")"; }).join(", ") : "Received. Everything arrived.", { kind: res.losses && res.losses.length ? "attention" : "ok" });
              DQ.render();
            }, function (e) { btn.disabled = false; err.textContent = e.message; err.hidden = false; });
          });
        });
        main.appendChild(card(null, h("div", { class: "c-form" }, h("p", { class: "text-2" }, "Change any number that differs from what was sent, then confirm."), err, h("div", { class: "row" }, btn), result)));
      } else if (t.status === "in_transit") {
        main.appendChild(msgBox("attention", "On the way. " + storeName(t.to_store_id) + " confirms what arrives."));
      }
    });
  } });

  /* ================================================================ WASTAGE */
  var WASTE = [{ value: "expired", label: "Expired" }, { value: "damaged", label: "Damaged" }, { value: "spoiled", label: "Spoiled" }, { value: "theft_found", label: "Theft found" }, { value: "other", label: "Other" }];
  function resizePhoto(file) {
    return new Promise(function (resolve, reject) {
      var url = URL.createObjectURL(file), img = new Image();
      img.onload = function () {
        var s = Math.min(1, 1280 / Math.max(img.naturalWidth, img.naturalHeight));
        var c = document.createElement("canvas");
        c.width = Math.max(1, Math.round(img.naturalWidth * s)); c.height = Math.max(1, Math.round(img.naturalHeight * s));
        c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
        URL.revokeObjectURL(url);
        resolve(c.toDataURL("image/jpeg", 0.8));
      };
      img.onerror = function () { URL.revokeObjectURL(url); reject(new Error("That file could not be read as a photo. Take the photo again.")); };
      img.src = url;
    });
  }
  DQ.route("#/wastage", { title: "Wastage", nav: "Wastage", can: "stock.wastage", render: function (main, params, query, alive) {
    var storeId = DQ.store();
    if (!storeId) return noStore(main);
    main.appendChild(ui.page("Wastage", { sub: "Write off stock that cannot be sold in " + storeName(storeId) + ". It leaves stock at cost and never counts as demand." }));
    var product = null, photo = null, key = DQ.key("waste");
    var prodBox = h("div", { class: "c-selected" });
    var q = ui.input({ inputmode: "decimal", autocomplete: "off", class: "input c-big-input", id: "c-waste-qty" });
    var reasonName = "c-wr-" + Date.now();
    var reasons = h("fieldset", { class: "c-radios" }, h("legend", null, "Why is it written off?"), WASTE.map(function (r, i) {
      return h("label", { class: "c-radio" }, h("input", { type: "radio", name: reasonName, value: r.value, checked: i === 0 }), h("span", null, r.label));
    }));
    var note = ui.input({ maxlength: 300, id: "c-waste-note" });
    var noteField = ui.field("What happened", note, "Needed when the reason is other.");
    var file = h("input", { type: "file", accept: "image/*", capture: "environment", class: "c-file", id: "c-waste-photo" });
    var preview = h("div", { class: "c-photo-prev" });
    var value = h("p", { class: "c-value", "aria-live": "polite" });
    var err = h("div", { class: "form-error", role: "alert", hidden: true });
    var btn = ui.button("Write off", submit);
    var pick = picker({ label: "Scan or search the product", onPick: function (p, qm) { product = p; if (qm) q.value = String(qm / 1000); paintProduct(); paintValue(); (fine() ? q : pick.box).focus(); } });
    function reason() { var r = reasons.querySelector("input:checked"); return r ? r.value : "expired"; }
    function paintProduct() {
      DQ.clear(prodBox);
      if (!product) { prodBox.appendChild(h("p", { class: "text-2 c-hint" }, "Scan or search the product first.")); return; }
      prodBox.appendChild(h("div", { class: "c-sel-head" }, h("div", null, h("p", { class: "c-sel-name" }, product.name), h("p", { class: "text-3 mono" }, product.sku + " · " + unitWord(product.unit) + (product.cost_paise !== null && product.cost_paise !== undefined ? " · cost " + money(product.cost_paise) : ""))),
        h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: function () { product = null; paintProduct(); paintValue(); pick.focus(); } }, "Change")));
    }
    function paintValue() {
      var m = fmt.toMilli(q.value);
      value.textContent = product && m > 0 && product.cost_paise ? "Value at cost: about " + money(Math.floor(m * product.cost_paise / 1000)) : "";
    }
    q.addEventListener("input", function () { setErr(q, null); paintValue(); });
    file.addEventListener("change", function () {
      DQ.clear(preview); photo = null;
      var f = file.files && file.files[0];
      if (!f) return;
      preview.appendChild(h("p", { class: "text-2" }, "Preparing the photo…"));
      resizePhoto(f).then(function (data) {
        photo = data;
        DQ.clear(preview);
        preview.appendChild(h("img", { src: data, alt: "Photo of the written off stock" }));
        preview.appendChild(h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: function () { photo = null; file.value = ""; DQ.clear(preview); } }, "Remove the photo"));
      }, function (e) { DQ.clear(preview); preview.appendChild(msgBox("risk", e.message)); });
    });
    main.appendChild(h("div", { class: "c-two" },
      card("Write off stock", h("div", { class: "c-form" }, pick.el, prodBox,
        ui.field("Quantity", q), numpad(q),
        reasons, noteField,
        h("div", { class: "field" }, h("label", { for: "c-waste-photo" }, "Photo (optional)"), file, h("p", { class: "field-help" }, "On a phone this opens the camera."), preview),
        value, err, h("div", { class: "row" }, btn))),
      h("div", null, h("div", { id: "c-waste-list" }, ui.skeleton(5)))));
    paintProduct();
    pick.focus();
    function submit() {
      err.hidden = true;
      if (!product) { err.textContent = "Scan or search the product first."; err.hidden = false; pick.focus(); return; }
      var m = fmt.toMilli(q.value), pr = qtyProblem(m, product);
      if (pr) { setErr(q, pr); q.focus(); return; }
      var r = reason();
      if (r === "other" && !note.value.trim()) { setErr(note, "Say what happened."); note.focus(); return; }
      setErr(note, null);
      var body = { store_id: storeId, product_id: product.id, qty_milli: m, reason: r, note: note.value.trim() || null, key: key };
      if (photo) { body.photo_base64 = photo.split(",")[1]; body.photo_type = "image/jpeg"; }
      DQ.confirm({ title: "Write off " + qty(m, product.unit) + " " + product.name + "?", message: "It leaves the stock of " + storeName(storeId) + " as " + words(r) + (value.textContent ? ". " + value.textContent : "") + ". This cannot be undone; a count corrects any mistake.", confirmLabel: "Write off", danger: true }).then(function (c) {
        if (!c.ok) return;
        btn.disabled = true;
        var go = navigator.onLine ? DQ.post("/api/wastage", body).then(function (d) { return { sent: true, data: d }; }, function (e) {
          if (e.status === 0) { DQ.queue.add("Write off " + product.name, "POST", "/api/wastage", body); return { queued: true }; }
          throw e;
        }) : send("Write off " + product.name, "POST", "/api/wastage", body);
        go.then(function (res) {
          btn.disabled = false;
          DQ.toast(res.queued ? "Saved on this device. The write off is sent once the internet is back." : (res.data.duplicate ? "Already written off. Nothing changed." : "Written off: " + qty(m, product.unit) + " " + product.name + ", " + money(res.data.value_paise) + " at cost."));
          product = null; photo = null; file.value = ""; q.value = ""; note.value = ""; key = DQ.key("waste");
          DQ.clear(preview); paintProduct(); paintValue(); pick.focus();
          loadList();
        }, function (e) { btn.disabled = false; err.textContent = e.message; err.hidden = false; });
      });
    }
    function loadList() {
      return DQ.get("/api/wastage" + DQ.qs({ store_id: storeId, days: 30 })).then(function (rows) {
        var box = document.getElementById("c-waste-list");
        if (!box || !alive()) return;
        DQ.clear(box);
        var total = rows.reduce(function (a, x) { return a + x.value_paise; }, 0);
        box.appendChild(card("Written off in the last 30 days", h("div", null,
          rows.length ? h("p", { class: "text-2 c-note" }, rows.length + " write off" + (rows.length > 1 ? "s" : "") + ", " + money(total) + " at cost.") : null,
          rows.length ? h("ul", { class: "c-lines" }, rows.map(function (x) {
            return h("li", { class: "c-line" },
              h("div", { class: "c-line-main" }, h("strong", null, x.name), h("span", { class: "mono c-line-qty" }, qty(x.qty_milli, x.unit))),
              h("div", { class: "c-line-sub" }, ui.badge(cap((x.reason || x.type).split(":")[0]), x.type === "damage" ? "attention" : "neutral"),
                h("span", { class: "text-2" }, T.dt(x.at) + (x.person ? " · " + x.person : "") + " · " + money(x.value_paise)),
                (x.reason || "").indexOf(":") > 0 ? h("span", { class: "text-2" }, x.reason.slice(x.reason.indexOf(":") + 1).trim()) : null),
              x.photo_id ? h("a", { class: "c-thumb", href: "/api/attachments/" + x.photo_id, target: "_blank", rel: "noopener", "aria-label": "Open the photo of " + x.name },
                h("img", { src: "/api/attachments/" + x.photo_id, alt: "Photo of " + x.name, loading: "lazy" })) : null);
          })) : ui.empty({ title: "Nothing written off recently", text: "Expired, damaged or spoiled stock you write off appears here with its photo." }))));
      }, function (e) { var box = document.getElementById("c-waste-list"); if (box) { DQ.clear(box); box.appendChild(ui.error(e, loadList)); } });
    }
    return loadList();
  } });

  /* ================================================================ TILL: the device side
     The bill number is made here before payment: store code + device id + a counter
     kept on the device. It is the bill's safety key. Paid bills made offline wait in
     a list on the device and go in order through /api/checkout/sync. */
  var till = {
    k: function (name, sid) { return ck("till." + name + "." + sid); },
    get: function (name, sid, def) { return lsGet(till.k(name, sid), def); },
    set: function (name, sid, v) { if (!lsSet(till.k(name, sid), v)) DQ.toast("This device's storage is full. Send the waiting bills before selling more.", { kind: "risk" }); },
    pending: function (sid) { return till.get("pending", sid, []); },
    number: function (store, prefix) {
      var n = (lsGet(ck("till.counter"), 0) || 0) + 1;
      lsSet(ck("till.counter"), n);
      return (prefix || "") + store.code + "-" + DQ.deviceId().replace(/^web-/, "") + "-" + ("0000" + n).slice(-5);
    },
    use: function (sid) { var l = lsGet(ck("till.stores"), []); if (l.indexOf(sid) < 0) { l.push(sid); lsSet(ck("till.stores"), l); } }
  };
  function tillChanged() { window.dispatchEvent(new Event("dq-till")); }
  var syncing = {};
  function syncTill(sid) {
    if (syncing[sid] || !navigator.onLine || !DQ.me) return Promise.resolve();
    var list = till.pending(sid);
    if (!list.length) return Promise.resolve();
    syncing[sid] = true;
    var batch = list.slice(0, 50);
    return DQ.post("/api/checkout/sync", { store_id: sid, device_id: DQ.deviceId(), bills: batch, pending_after: list.length - batch.length }, { quiet: true }).then(function (r) {
      syncing[sid] = false;
      var by = {};
      r.results.forEach(function (x) { by[x.bill_number] = x; });
      var refused = batch.filter(function (b) { return by[b.bill_number] && by[b.bill_number].status === "refused"; });
      till.set("pending", sid, till.pending(sid).filter(function (b) { return !by[b.bill_number]; }));
      if (refused.length) {
        var rl = till.get("refused", sid, []);
        refused.forEach(function (b) { b.error = by[b.bill_number].error; rl.push(b); });
        till.set("refused", sid, rl);
        DQ.toast(refused.length + " bill" + (refused.length > 1 ? "s were" : " was") + " not accepted. Open Checkout to see why.", { kind: "risk" });
      }
      tillChanged();
      if (till.pending(sid).length && r.results.length) return syncTill(sid);
    }, function (e) {
      syncing[sid] = false;
      if (e.status && e.status < 500 && e.status !== 401 && e.status !== 429) DQ.toast("Waiting bills could not be sent: " + e.message, { kind: "risk" });
      tillChanged();
    });
  }
  function heartbeat() {
    if (!DQ.me || !navigator.onLine) return;
    lsGet(ck("till.stores"), []).forEach(function (sid) {
      var s = storeById(sid);
      if (!s || s.mode !== "connected" || !DQ.can("sales.bill", sid)) return;
      DQ.post("/api/checkout/heartbeat", { store_id: sid, device_id: DQ.deviceId(), pending: till.pending(sid).length }, { quiet: true }).catch(function () {});
      syncTill(sid);
    });
  }
  setInterval(heartbeat, 60000);
  setTimeout(heartbeat, 4000);
  window.addEventListener("online", function () { setTimeout(heartbeat, 300); });

  function lineTotal(l) { return Math.floor(l.qty_milli * (l.price_paise || 0) / 1000) - (l.discount_paise || 0); }
  function cartTotal(c) { return c.lines.reduce(function (a, l) { return a + (l.loading ? 0 : lineTotal(l)); }, 0); }
  function paidSoFar(c) { return c.payments.reduce(function (a, p) { return a + p.amount_paise; }, 0); }
  function priceToday(prices, sid, fallback) {
    var today = fmt.today();
    var act = (prices || []).filter(function (r) { return r.start_date <= today && (!r.end_date || r.end_date >= today) && (!r.store_id || r.store_id === sid); });
    act.sort(function (a, b) { return (a.store_id ? 0 : 1) - (b.store_id ? 0 : 1) || b.start_date.localeCompare(a.start_date); });
    return act.length ? act[0].price_paise : fallback;
  }
  function payloadLine(l) {
    if (l.misc) return { description: l.description, barcode: l.barcode || null, qty_milli: l.qty_milli, unit_price_paise: l.price_paise, discount_paise: 0 };
    return { product_id: l.product_id, qty_milli: l.qty_milli, unit_price_paise: l.price_paise, discount_paise: l.discount_paise || 0, override_reason: l.override ? l.reason : null };
  }
  function pad2(s, w) { s = String(s); return s.length >= w ? s : new Array(w - s.length + 1).join(" ") + s; }
  function localReceipt(store, bill, cart, width) {
    width = width || 32;
    var out = [(DQ.me.company.name || "").slice(0, width), store.name.slice(0, width), ("Bill " + bill.bill_number).slice(0, width), new Array(width + 1).join("-")];
    cart.lines.forEach(function (l) {
      out.push((l.name || l.description || "Item").slice(0, width));
      var left = "  " + (l.qty_milli / 1000) + " x " + (l.price_paise / 100).toFixed(2);
      out.push(left + pad2((lineTotal(l) / 100).toFixed(2), width - left.length));
    });
    out.push(new Array(width + 1).join("-"));
    var t = (cartTotal(cart) / 100).toFixed(2);
    out.push("Total" + pad2(t, width - 5));
    cart.payments.forEach(function (p) { out.push(p.method.toUpperCase() + pad2((p.amount_paise / 100).toFixed(2), width - p.method.length)); });
    var ch = cart.payments.reduce(function (a, p) { return a + (p.tendered_paise ? p.tendered_paise - p.amount_paise : 0); }, 0);
    if (ch) out.push("Change" + pad2((ch / 100).toFixed(2), width - 6));
    out.push("Thank you");
    return out.join("\n");
  }
  function printText(text) {
    var old = document.getElementById("c-print");
    if (old) old.remove();
    document.body.appendChild(h("pre", { id: "c-print", class: "c-print-only", "aria-hidden": "true" }, text));
    window.print();
  }
  function receiptActions(getId) {
    var phone = ui.input({ type: "tel", inputmode: "tel", autocomplete: "off", placeholder: "10-digit mobile", maxlength: 15, id: "c-wa-" + Math.random().toString(36).slice(2, 7) });
    var err = h("p", { class: "field-error", role: "alert", hidden: true });
    var wa = ui.button("Send on WhatsApp", function () {
      err.hidden = true;
      var digits = phone.value.replace(/\D/g, "");
      if (digits.length < 10) { err.textContent = "Enter the customer's 10-digit mobile number."; err.hidden = false; phone.focus(); return; }
      if (!navigator.onLine) { err.textContent = "WhatsApp needs the internet. Print the receipt instead, or try again later."; err.hidden = false; return; }
      var win = window.open("", "_blank");
      getId().then(function (id) {
        if (!id) throw new Error("This bill has not reached the server yet. Try again in a minute.");
        return DQ.get("/api/checkout/bills/" + id + DQ.qs({ phone: digits }));
      }).then(function (b) {
        if (!b.whatsapp_link) throw new Error("That phone number does not look right. Check it and try again.");
        if (win) { win.opener = null; win.location.href = b.whatsapp_link; } else window.location.href = b.whatsapp_link;
      }, function (e) { if (win) win.close(); err.textContent = e.message; err.hidden = false; });
    }, "ghost");
    return h("div", { class: "c-wa" }, ui.field("Customer's mobile for WhatsApp", phone), err, wa);
  }

  /* ================================================================ CHECKOUT */
  DQ.route("#/checkout", { title: "Checkout", nav: "Checkout", can: "sales.bill", render: function (main, params, query, alive) {
    var store = DQ.storeObj();
    if (!store) return noStore(main);
    if (store.mode !== "connected") {
      main.appendChild(ui.page("Checkout"));
      main.appendChild(ui.empty({ title: store.name + " runs standalone", text: "Its sales come from the daily sales file or totals typed on the Sales screen, so there is no till here. Switch the store to connected in Settings to sell with Checkout.", action: { label: "Go to Settings", href: "#/settings" } }));
      return;
    }
    var sid = store.id;
    till.use(sid);
    var priceSeen = {};                       // product id -> price today (this session)
    var mayOverride = DQ.can("sales.refund", sid);
    var cart = till.get("cart", sid, null);
    if (!cart || !cart.number || cart.store_id !== sid) cart = newCart();
    var done = null;                          // the receipt being shown after payment
    var method = "cash";

    function newCart() { return { store_id: sid, number: till.number(store), lines: [], payments: [], pin: null, started_at: new Date().toISOString() }; }
    function save() { till.set("cart", sid, cart); }

    var numberEl = h("span", { class: "mono" });
    var heldBtn = ui.button("Held bills", recall, "ghost");
    var miscBtn = ui.button("Misc item", function () { offerMisc(null); }, "ghost");
    var holdBtn = ui.button("Hold", holdBill, "ghost");
    var voidBtn = ui.button("Void", voidBill, "ghost");
    main.appendChild(ui.page("Checkout", { sub: h("span", null, store.name + " · bill ", numberEl), actions: [heldBtn, miscBtn, holdBtn, voidBtn] }));
    var banner = h("div", { class: "c-till-banner", "aria-live": "polite" });
    main.appendChild(banner);

    var pick = picker({ label: "Scan a barcode, or search", placeholder: "Scan, or type a name, SKU or barcode", onPick: function (p, q, meta) { addProduct(p, q, meta); },
      onCode: function (code) { if (/^\d{6,}$/.test(code)) { unknownCode(code); return true; } return false; } });
    var cartBox = h("ul", { class: "c-cart", "aria-label": "Items on the bill" });
    var cartEmpty = h("p", { class: "text-2 c-cart-empty" }, "Scan the first item. A scanner types into the box above and presses Enter by itself.");
    var payBox = h("div", { class: "c-pay" });
    var barTotal = h("strong", { class: "mono" });
    var payBar = h("div", { class: "c-paybar", hidden: true }, h("span", null, "Total ", barTotal),
      h("button", { type: "button", class: "btn btn-primary", onclick: function () { payBox.scrollIntoView({ behavior: "smooth", block: "start" }); } }, "Pay"));
    main.classList.add("c-till-page");
    main.appendChild(h("div", { class: "c-till" },
      h("div", { class: "c-till-left" }, card(null, pick.el, { cls: "c-scan-card" }), card(null, h("div", null, cartEmpty, cartBox), { cls: "c-cart-card" })),
      h("div", { class: "c-till-right" }, payBox)));
    main.appendChild(payBar);

    function focusScan() { if (alive() && !document.querySelector("dialog[open]")) pick.focus(); }
    function onKey(e) {
      if (!alive()) { document.removeEventListener("keydown", onKey, true); return; }
      if (document.querySelector("dialog[open]")) return;
      var a = document.activeElement, tag = a && a.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      if (e.ctrlKey || e.metaKey || e.altKey || e.key.length !== 1) return;
      pick.focus();                           // a scanner or a typed key goes to the scan box
    }
    document.addEventListener("keydown", onKey, true);
    function onTill() { if (!alive()) { window.removeEventListener("dq-till", onTill); return; } paintBanner(); }
    window.addEventListener("dq-till", onTill);
    window.addEventListener("online", onTill);
    window.addEventListener("offline", onTill);

    /* ------------------------------------------------ cart */
    function addProduct(p, q, meta) {
      meta = meta || {};
      if (done) nextCustomer();
      q = q || 1000;
      var price = priceSeen[p.id] !== undefined ? priceSeen[p.id] : p.price_paise;
      var line = !meta.weighed && cart.lines.filter(function (l) { return l.product_id === p.id && !l.override && !l.weighed; })[0];
      if (line) { line.qty_milli += q; }
      else {
        line = { id: DQ.key("ln"), product_id: p.id, sku: p.sku, name: p.name, unit: p.unit, qty_milli: q, price_paise: price, listed_paise: price, discount_paise: 0, weighed: !!meta.weighed, code: meta.code || null };
        cart.lines.push(line);
      }
      save(); paintCart(line.id); paintPay();
      if (price === null || price === undefined) askPrice(line);
      refinePrice(p);
      focusScan();
    }
    /* The price today for this store can differ from the catalogue price (a promotion
       or a store price), so fetch it once per product and correct the line. */
    function refinePrice(p) {
      if (priceSeen[p.id] !== undefined || !navigator.onLine) return;
      priceSeen[p.id] = p.price_paise;
      DQ.get("/api/products/" + p.id + "/overview" + DQ.qs({ store_id: sid, days: 7 })).then(function (o) {
        var full = cat.put(o.product);
        var today = priceToday(o.prices, sid, full.price_paise);
        priceSeen[p.id] = today;
        var changed = false;
        cart.lines.forEach(function (l) { if (l.product_id === p.id && !l.override && l.listed_paise !== today) { l.listed_paise = today; l.price_paise = today; changed = true; } });
        if (changed && alive()) { save(); paintCart(); paintPay(); }
      }, function () { delete priceSeen[p.id]; });
    }
    function unknownCode(code) {
      if (done) nextCustomer();
      if (!navigator.onLine) { offerMisc(code, "Barcode " + code + " is not in the catalogue saved on this till. Sell it as a misc item, or ask a manager to add it."); return; }
      var line = { id: DQ.key("ln"), loading: true, name: "Looking up " + code + "…", qty_milli: 1000, price_paise: 0, code: code };
      cart.lines.push(line); paintCart(line.id);
      DQ.get("/api/checkout/scan" + DQ.qs({ code: code, store_id: sid })).then(function (r) {
        cart.lines = cart.lines.filter(function (l) { return l.id !== line.id; });
        if (!r.found) { paintCart(); offerMisc(code, r.message); return; }
        var p = cat.byId(r.product_id);
        return (p ? Promise.resolve(p) : DQ.get("/api/products/" + r.product_id).then(cat.put)).then(function (prod) {
          if (r.price_paise !== undefined && r.price_paise !== null) priceSeen[prod.id] = r.price_paise;
          addProduct(prod, r.qty_milli, { weighed: r.weighed, code: code, price_paise: r.price_paise });
        });
      }).catch(function (e) {
        cart.lines = cart.lines.filter(function (l) { return l.id !== line.id; });
        paintCart();
        if (e.status === 0) offerMisc(code, "The server could not be reached to look up " + code + ". Sell it as a misc item, or try again.");
        else DQ.toast(e.message, { kind: "risk" });
      });
    }
    function offerMisc(code, message) {
      var desc = ui.input({ value: "Misc item", maxlength: 300 });
      var price = ui.input({ inputmode: "decimal", autocomplete: "off", class: "input c-big-input", placeholder: "0.00" });
      var err = h("p", { class: "field-error", role: "alert", hidden: true });
      modal(code ? "Barcode not found" : "Sell a misc item", function (close) {
        return h("form", { class: "c-form", onsubmit: function (e) {
          e.preventDefault();
          var pp = fmt.toPaise(price.value);
          if (!desc.value.trim()) { err.textContent = "Describe the item."; err.hidden = false; desc.focus(); return; }
          if (pp === null || pp <= 0) { err.textContent = "Type the price in rupees, like 45 or 45.50."; err.hidden = false; price.focus(); return; }
          close({ description: desc.value.trim(), price: pp });
        } },
          message ? h("p", { class: "text-2" }, message) : h("p", { class: "text-2" }, "For an item without a barcode in the catalogue. No product's stock changes until a manager maps it."),
          ui.field("Price (₹)", price), numpad(price, {}), ui.field("Description", desc), err,
          h("div", { class: "dialog-actions" }, h("button", { type: "button", class: "btn btn-ghost", onclick: function () { close(null); } }, "Cancel"),
            h("button", { type: "submit", class: "btn btn-primary" }, "Add misc item")));
      }).then(function (r) {
        if (r) {
          var line = { id: DQ.key("ln"), misc: true, description: r.description, name: r.description, barcode: code || null, qty_milli: 1000, price_paise: r.price, listed_paise: null, discount_paise: 0, unit: "each" };
          cart.lines.push(line); save(); paintCart(line.id); paintPay();
        }
        setTimeout(focusScan, 50);
      });
      setTimeout(function () { price.focus(); }, 30);
    }
    function askPrice(line) {
      var price = ui.input({ inputmode: "decimal", class: "input c-big-input", autocomplete: "off" });
      modal("Type the price of " + line.name, function (close) {
        return h("form", { class: "c-form", onsubmit: function (e) { e.preventDefault(); var pp = fmt.toPaise(price.value); if (pp === null || pp <= 0) { price.setAttribute("aria-invalid", "true"); return; } close(pp); } },
          h("p", { class: "text-2" }, line.name + " has no selling price in the catalogue. Type the price on the pack."),
          ui.field("Price (₹)", price), numpad(price, {}),
          h("div", { class: "dialog-actions" }, h("button", { type: "button", class: "btn btn-ghost", onclick: function () { close(null); } }, "Remove the item"), h("button", { type: "submit", class: "btn btn-primary" }, "Use this price")));
      }).then(function (pp) {
        if (pp) { line.price_paise = pp; line.listed_paise = null; }
        else cart.lines = cart.lines.filter(function (l) { return l !== line; });
        save(); paintCart(); paintPay(); setTimeout(focusScan, 50);
      });
    }
    function setQty(line, m) {
      if (m <= 0) { removeLine(line); return; }
      line.qty_milli = m; save(); paintCart(line.id); paintPay();
    }
    function editQty(line) {
      var i = ui.input({ inputmode: "decimal", class: "input c-big-input", value: String(line.qty_milli / 1000), autocomplete: "off" });
      var err = h("p", { class: "field-error", role: "alert", hidden: true });
      modal("How many " + line.name + "?", function (close) {
        return h("form", { class: "c-form", onsubmit: function (e) {
          e.preventDefault();
          var m = fmt.toMilli(i.value), pr = qtyProblem(m, { name: line.name, unit: line.unit || "each" }, true);
          if (pr) { err.textContent = pr; err.hidden = false; return; }
          close(m);
        } }, ui.field("Quantity (" + unitWord(line.unit) + ")", i), numpad(i, { decimal: line.unit && line.unit !== "each" }), err,
          h("div", { class: "dialog-actions" }, h("button", { type: "button", class: "btn btn-ghost", onclick: function () { close(null); } }, "Cancel"), h("button", { type: "submit", class: "btn btn-primary" }, "Set the quantity")));
      }).then(function (m) { if (m !== null && m !== undefined) setQty(line, m); setTimeout(focusScan, 50); });
      setTimeout(function () { i.select(); }, 30);
    }
    function removeLine(line) {
      var at = cart.lines.indexOf(line);
      if (at < 0) return;
      cart.lines.splice(at, 1); save(); paintCart(); paintPay();
      DQ.toast("Removed " + line.name + ".", { undo: function () { cart.lines.splice(Math.min(at, cart.lines.length), 0, line); save(); paintCart(line.id); paintPay(); } });
      focusScan();
    }
    function editPrice(line) {
      var listed = line.listed_paise;
      var price = ui.input({ inputmode: "decimal", class: "input", autocomplete: "off", value: (line.price_paise / 100).toFixed(2) });
      var disc = ui.input({ inputmode: "decimal", class: "input", autocomplete: "off", value: line.discount_paise ? (line.discount_paise / 100).toFixed(2) : "" , placeholder: "0" });
      var reason = ui.input({ maxlength: 200, value: line.reason || "", placeholder: "For example: damaged pack, staff discount" });
      var pin = mayOverride ? null : ui.input({ type: "password", inputmode: "numeric", autocomplete: "off", maxlength: 8 });
      var err = h("p", { class: "field-error", role: "alert", hidden: true });
      modal("Change the price of " + line.name, function (close) {
        return h("form", { class: "c-form", onsubmit: function (e) {
          e.preventDefault();
          var pp = fmt.toPaise(price.value), dd = disc.value.trim() ? fmt.toPaise(disc.value) : 0;
          if (pp === null || pp < 0) { err.textContent = "Type the price in rupees."; err.hidden = false; return; }
          if (dd === null || dd < 0) { err.textContent = "Type the discount in rupees, or leave it empty."; err.hidden = false; return; }
          if (dd > Math.floor(line.qty_milli * pp / 1000)) { err.textContent = "The discount is more than the line."; err.hidden = false; return; }
          var lower = (listed !== null && listed !== undefined && pp < listed) || dd > 0;
          if (lower && reason.value.trim().length < 3) { err.textContent = "Say why it is cheaper, in a few words."; err.hidden = false; reason.focus(); return; }
          if (lower && pin && !/^\d{4,8}$/.test(pin.value)) { err.textContent = "A manager types their PIN to approve a lower price."; err.hidden = false; pin.focus(); return; }
          close({ price: pp, discount: dd, reason: reason.value.trim(), pin: pin ? pin.value : null, lower: lower });
        } },
          h("p", { class: "text-2" }, listed !== null && listed !== undefined ? "The price today is " + money(listed) + " per " + (line.unit && line.unit !== "each" ? line.unit : "unit") + ". A lower price or a discount is logged with your name, the original price and the reason." : "A misc item's price can be changed freely."),
          h("div", { class: "grid-2" }, ui.field("Price per " + (line.unit && line.unit !== "each" ? line.unit : "unit") + " (₹)", price), ui.field("Discount on this line (₹)", disc)),
          ui.field("Reason", reason), pin ? ui.field("Manager PIN", pin, "A manager types it here. It is checked when the bill is paid.") : null, err,
          h("div", { class: "dialog-actions" },
            line.override ? h("button", { type: "button", class: "btn btn-ghost", onclick: function () { close({ reset: true }); } }, "Back to the normal price") : null,
            h("button", { type: "button", class: "btn btn-ghost", onclick: function () { close(null); } }, "Cancel"),
            h("button", { type: "submit", class: "btn btn-primary" }, "Apply")));
      }).then(function (r) {
        if (r && r.reset) { line.price_paise = listed; line.discount_paise = 0; line.reason = null; line.override = false; }
        else if (r) {
          line.price_paise = r.price; line.discount_paise = r.discount; line.reason = r.lower ? r.reason : null; line.override = r.lower;
          if (r.pin) cart.pin = r.pin;
        }
        save(); paintCart(line.id); paintPay(); setTimeout(focusScan, 50);
      });
    }

    function paintCart(flashId) {
      numberEl.textContent = cart.number;
      DQ.clear(cartBox);
      cartEmpty.hidden = cart.lines.length > 0;
      cart.lines.slice().reverse().forEach(function (l) {
        var li = h("li", { class: "c-cart-line" + (l.id === flashId ? " c-flash" : "") + (l.loading ? " c-loading" : "") },
          h("div", { class: "c-cl-name" }, h("strong", null, l.name),
            h("span", { class: "text-3 mono" }, l.misc ? "Misc item" + (l.barcode ? " · " + l.barcode : "") : (l.sku || "") + (l.price_paise !== null && l.price_paise !== undefined ? " · " + money(l.price_paise) + (l.unit && l.unit !== "each" ? " per " + l.unit : " each") : "")),
            l.override ? h("span", { class: "c-override" }, ui.badge("Price changed", "attention"), " ", l.reason || "") : null),
          l.loading ? h("span", { class: "text-2" }, "Looking up…") : h("div", { class: "c-cl-ctl" },
            h("div", { class: "c-stepper", role: "group", "aria-label": "Quantity of " + l.name },
              h("button", { type: "button", class: "c-step", "aria-label": "One less " + l.name, onclick: function () { setQty(l, l.unit && l.unit !== "each" ? l.qty_milli - 1000 : l.qty_milli - 1000); focusScan(); } }, "−"),
              h("button", { type: "button", class: "c-step-qty mono", "aria-label": "Quantity " + qty(l.qty_milli, l.unit) + ". Change it", onclick: function () { editQty(l); } }, qty(l.qty_milli, l.unit)),
              h("button", { type: "button", class: "c-step", "aria-label": "One more " + l.name, onclick: function () { setQty(l, l.qty_milli + 1000); focusScan(); } }, "+")),
            h("span", { class: "c-cl-total mono" }, money(lineTotal(l)), l.discount_paise ? h("span", { class: "text-3 c-cl-disc" }, "incl. −" + money(l.discount_paise)) : null),
            h("div", { class: "c-cl-more" },
              h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: function () { editPrice(l); } }, "Price"),
              h("button", { type: "button", class: "btn btn-ghost btn-sm", "aria-label": "Remove " + l.name, onclick: function () { removeLine(l); } }, "Remove"))));
        cartBox.appendChild(li);
      });
      var heldN = till.get("held", sid, []).length;
      heldBtn.textContent = "Held bills" + (heldN ? " (" + heldN + ")" : "");
      holdBtn.disabled = !cart.lines.length || !!done;
      voidBtn.disabled = !cart.lines.length || !!done;
    }

    /* ------------------------------------------------ payment */
    function paintPay() {
      DQ.clear(payBox);
      payBar.hidden = !!done || !cart.lines.length;
      barTotal.textContent = money(cartTotal(cart));
      if (done) { paintReceipt(); return; }
      var total = cartTotal(cart), paid = paidSoFar(cart), left = total - paid;
      var count = cart.lines.reduce(function (a, l) { return a + (l.unit && l.unit !== "each" ? 1 : l.qty_milli / 1000); }, 0);
      var discounts = cart.lines.reduce(function (a, l) { return a + (l.discount_paise || 0) + (l.override && l.listed_paise ? Math.max(l.listed_paise - l.price_paise, 0) * l.qty_milli / 1000 : 0); }, 0);
      payBox.appendChild(card(null, h("div", { class: "c-total" },
        h("p", { class: "c-total-label" }, "Total"),
        h("p", { class: "c-total-value mono", "aria-live": "polite" }, money(total)),
        h("p", { class: "text-2" }, fmt.num(Math.round(count)) + " item" + (count === 1 ? "" : "s") + (discounts ? " · savings " + money(Math.round(discounts)) : "")))));
      if (!cart.lines.length) return;
      var body = h("div", { class: "c-form" });
      if (cart.payments.length) {
        body.appendChild(h("ul", { class: "c-pays" }, cart.payments.map(function (p, i) {
          return h("li", null, h("span", null, cap(p.method === "upi" ? "UPI" : p.method) + (p.tendered_paise ? " (received " + money(p.tendered_paise) + ")" : "")), h("span", { class: "mono" }, money(p.amount_paise)),
            h("button", { type: "button", class: "btn btn-ghost btn-sm", "aria-label": "Remove this payment", onclick: function () { cart.payments.splice(i, 1); save(); paintPay(); } }, "Remove"));
        })));
        body.appendChild(h("p", { class: "c-left" }, "Still to pay ", h("strong", { class: "mono" }, money(left))));
      }
      body.appendChild(ui.tabs([{ value: "cash", label: "Cash" }, { value: "upi", label: "UPI" }, { value: "card", label: "Card" }], method, function (v) { method = v; paintPay(); }));
      var err = h("p", { class: "form-error", role: "alert", hidden: true, id: "c-pay-err" });
      if (method === "cash") {
        var got = ui.input({ inputmode: "decimal", autocomplete: "off", class: "input c-big-input", id: "c-cash-in", placeholder: (left / 100).toFixed(2) });
        var change = h("p", { class: "c-change", "aria-live": "polite" });
        var paintChange = function () {
          var g = got.value.trim() ? fmt.toPaise(got.value) : left;
          if (g === null) { change.textContent = ""; return; }
          change.textContent = g > left ? "Change to give " + money(g - left) : g < left ? "Still " + money(left - g) + " short; take the rest another way" : "Exact amount";
          change.setAttribute("data-kind", g > left ? "change" : g < left ? "short" : "exact");
        };
        got.addEventListener("input", paintChange);
        got.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); takeCash(); } });
        var notes = [left];
        [1000, 5000, 10000, 20000, 50000, 200000].forEach(function (n) { var v = Math.ceil(left / n) * n; if (v > left && notes.indexOf(v) < 0 && notes.length < 5) notes.push(v); });
        var takeCash = function () {
          var g = got.value.trim() ? fmt.toPaise(got.value) : left;
          if (g === null || g <= 0) { err.textContent = "Type the cash received in rupees."; err.hidden = false; got.focus(); return; }
          cart.payments.push({ method: "cash", amount_paise: Math.min(g, left), tendered_paise: g });
          save(); afterPayment();
        };
        body.appendChild(ui.field("Cash received (₹)", got));
        body.appendChild(h("div", { class: "c-notes", role: "group", "aria-label": "Quick amounts" }, notes.map(function (v, i) {
          return h("button", { type: "button", class: "btn btn-ghost", onclick: function () { got.value = (v / 100).toFixed(2); paintChange(); } }, i === 0 ? "Exact " + money(v, { whole: v % 100 === 0 }) : money(v, { whole: true }));
        })));
        body.appendChild(change);
        body.appendChild(err);
        body.appendChild(ui.button("Take the cash", takeCash, "primary", { class: "btn btn-primary btn-block c-pay-btn" }));
        paintChange();
      } else {
        var amt = ui.input({ inputmode: "decimal", autocomplete: "off", class: "input", value: (left / 100).toFixed(2) });
        var ref = ui.input({ maxlength: 64, autocomplete: "off", placeholder: method === "upi" ? "UPI reference, optional" : "Last 4 digits or approval code, optional" });
        var label = method === "upi" ? "UPI" : "Card";
        body.appendChild(h("p", { class: "text-2" }, method === "upi" ? "Ask the customer to pay by UPI. Press the button only after the payment shows as successful." : "Take the card on the terminal. Press the button only after the terminal says approved."));
        body.appendChild(h("div", { class: "grid-2" }, ui.field("Amount (₹)", amt), ui.field("Reference", ref)));
        body.appendChild(err);
        body.appendChild(ui.button(label + " payment succeeded", function () {
          var a = fmt.toPaise(amt.value);
          if (a === null || a <= 0) { err.textContent = "Type the amount in rupees."; err.hidden = false; return; }
          if (a > left) { err.textContent = "That is more than the " + money(left) + " still to pay."; err.hidden = false; return; }
          cart.payments.push({ method: method, amount_paise: a, reference: ref.value.trim() || null });
          save(); afterPayment();
        }, "primary", { class: "btn btn-primary btn-block c-pay-btn" }));
      }
      payBox.appendChild(card("Payment", body));
    }
    function afterPayment() {
      var left = cartTotal(cart) - paidSoFar(cart);
      if (left <= 0) complete(); else { method = method === "cash" ? "upi" : method; paintPay(); }
    }
    function complete() {
      if (cart.lines.some(function (l) { return l.loading; })) { DQ.toast("Wait a moment: an item is still being looked up.", { kind: "attention" }); return; }
      var bill = { store_id: sid, bill_number: cart.number, device_id: DQ.deviceId(), offline: false, paid_at: new Date().toISOString(),
        lines: cart.lines.map(payloadLine), manager_pin: cart.pin || null,
        payments: cart.payments.map(function (p) { return { method: p.method, amount_paise: p.amount_paise, tendered_paise: p.method === "cash" ? p.tendered_paise : null, reference: p.reference || null }; }) };
      var pend = till.pending(sid);
      var keepOffline = function () { bill.offline = true; pend = till.pending(sid); pend.push(bill); till.set("pending", sid, pend); finish(bill, null); syncTill(sid); };
      if (!navigator.onLine || pend.length) { keepOffline(); return; }
      DQ.clear(payBox); payBox.appendChild(card(null, h("p", { class: "c-total-value" }, "Completing the bill…")));
      DQ.post("/api/checkout/bills", bill).then(function (res) { finish(bill, res); }, function (e) {
        if (e.status === 0 || e.status >= 500) { keepOffline(); return; }
        paintPay();
        var err = document.getElementById("c-pay-err");
        if (err) { err.textContent = "Not paid yet: " + e.message + (e.status === 403 ? " Change the price back, or ask a manager to type their PIN under Price." : ""); err.hidden = false; }
        if (e.status === 422 || e.status === 403) { cart.payments = []; save(); paintPay(); err = document.getElementById("c-pay-err"); if (err) { err.textContent = "Not paid yet: " + e.message + " The payments were cleared; take them again after fixing this."; err.hidden = false; } }
      });
    }
    function finish(bill, res) {
      var change = cart.payments.reduce(function (a, p) { return a + (p.tendered_paise ? p.tendered_paise - p.amount_paise : 0); }, 0);
      done = { bill: bill, id: res ? res.id : null, duplicate: res && res.duplicate, offline: !res, change: change, text: localReceipt(store, bill, cart) };
      lsSet(till.k("last", sid), done);
      cart = newCart(); save();
      paintCart(); paintPay(); paintBanner();
      if (res) DQ.get("/api/checkout/bills/" + res.id).then(function (b) { if (done && done.id === res.id) { done.text = b.receipt; lsSet(till.k("last", sid), done); if (alive()) paintPay(); } }, function () {});
    }
    function billId(d) {
      if (d.id) return Promise.resolve(d.id);
      return DQ.get("/api/checkout/bills" + DQ.qs({ store_id: sid, number: d.bill.bill_number, limit: 1 })).then(function (r) { var b = r.filter(function (x) { return x.bill_number === d.bill.bill_number; })[0]; if (b) d.id = b.id; return b ? b.id : null; });
    }
    function paintReceipt() {
      var d = done;
      var next = ui.button("Next customer", nextCustomer, "primary", { class: "btn btn-primary btn-block c-pay-btn" });
      payBox.appendChild(card(null, h("div", { class: "c-done" },
        h("p", { class: "c-done-title" }, d.duplicate ? "Already paid. Nothing changed." : "Paid"),
        d.change ? h("div", { class: "c-change-big" }, h("span", null, "Change to give"), h("strong", { class: "mono" }, money(d.change))) : null,
        d.offline ? msgBox("attention", "Saved on this till. The bill is sent once the internet is back.") : null,
        h("pre", { class: "c-receipt", "aria-label": "Receipt" }, d.text),
        h("div", { class: "row" }, ui.button("Print", function () { printText(d.text); }, "ghost")),
        receiptActions(function () { return billId(d); }),
        next)));
      setTimeout(function () { if (alive() && done) next.focus(); }, 30);
    }
    function nextCustomer() { done = null; paintCart(); paintPay(); focusScan(); }

    /* ------------------------------------------------ hold, recall, void */
    function payloadHold(c, held) { return { store_id: sid, bill_number: c.number, device_id: DQ.deviceId(), held: held, lines: c.lines.filter(function (l) { return !l.loading; }).map(payloadLine) }; }
    function holdBill(silent) {
      if (!cart.lines.length) return;
      var c = cart;
      c.payments = [];
      var held = till.get("held", sid, []); held.push(c); till.set("held", sid, held);
      send("Hold bill " + c.number, "POST", "/api/checkout/hold", payloadHold(c, true)).catch(function (e) { DQ.toast("Bill " + c.number + " is held on this till only: " + e.message, { kind: "attention" }); });
      cart = newCart(); save(); paintCart(); paintPay();
      if (silent !== true) DQ.toast("Bill " + c.number + " held. Recall it from Held bills.");
      focusScan();
    }
    function recall() {
      var held = till.get("held", sid, []);
      if (!held.length) { DQ.toast("No bills are held on this till."); return; }
      modal("Held bills", function (close) {
        return h("div", { class: "c-form" }, h("ul", { class: "c-list" }, held.map(function (c) {
          return h("li", null, h("button", { type: "button", class: "c-list-item c-list-btn", onclick: function () { close(c); } },
            h("span", { class: "c-li-main" }, h("strong", { class: "mono" }, c.number), h("span", { class: "text-2" }, " · " + c.lines.length + " line" + (c.lines.length > 1 ? "s" : "") + " · started " + T.time(c.started_at))),
            h("span", { class: "c-li-side mono" }, money(cartTotal(c)))));
        })), cart.lines.length ? h("p", { class: "text-2" }, "The bill on screen is held first, so nothing is lost.") : null,
          h("div", { class: "dialog-actions" }, h("button", { type: "button", class: "btn btn-ghost", onclick: function () { close(null); } }, "Close")));
      }).then(function (c) {
        if (!c) { focusScan(); return; }
        if (done) done = null;
        if (cart.lines.length) holdBill(true);
        till.set("held", sid, till.get("held", sid, []).filter(function (x) { return x.number !== c.number; }));
        cart = c; save(); paintCart(); paintPay(); focusScan();
        DQ.toast("Bill " + c.number + " is back on screen.");
      });
    }
    function voidBill() {
      if (!cart.lines.length) return;
      DQ.confirm({ title: "Void bill " + cart.number + "?", message: "Nothing is sold and no stock changes. The void is recorded with your name and the reason.", reason: true, reasonLabel: "Why is it voided? For example: customer left", confirmLabel: "Void the bill", danger: true }).then(function (r) {
        if (!r.ok) { focusScan(); return; }
        var c = cart;
        send("Void bill " + c.number, "POST", "/api/checkout/void", { store_id: sid, bill_number: c.number, reason: r.reason, lines: c.lines.filter(function (l) { return !l.loading; }).map(payloadLine) }).then(function () {
          DQ.toast("Bill " + c.number + " voided.");
        }, function (e) { DQ.toast("The void was not recorded: " + e.message, { kind: "risk" }); });
        cart = newCart(); save(); paintCart(); paintPay(); focusScan();
      });
    }

    /* ------------------------------------------------ waiting bills */
    function paintBanner() {
      DQ.clear(banner);
      var n = till.pending(sid).length, refused = till.get("refused", sid, []);
      if (!navigator.onLine) banner.appendChild(msgBox("attention", "Offline. Keep selling: bills are kept on this till and sent in order when the internet is back."));
      if (n) banner.appendChild(h("div", { class: "c-msg c-msg-row", "data-kind": "attention", role: "status" },
        h("span", null, n + " bill" + (n > 1 ? "s" : "") + " waiting to send from this till."),
        navigator.onLine ? h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: function () { syncTill(sid); } }, "Send now") : null));
      if (refused.length) banner.appendChild(h("div", { class: "c-msg", "data-kind": "risk", role: "alert" },
        h("p", null, h("strong", null, refused.length + " bill" + (refused.length > 1 ? "s were" : " was") + " not accepted by the server. "), "Fix the cause, then send again, or show a manager."),
        h("ul", { class: "c-refused" }, refused.map(function (b) {
          var total = b.payments.reduce(function (a, p) { return a + p.amount_paise; }, 0);
          return h("li", null, h("span", { class: "mono" }, b.bill_number + " · " + money(total)), h("span", null, " " + (b.error || "")),
            h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: function () {
              till.set("refused", sid, till.get("refused", sid, []).filter(function (x) { return x.bill_number !== b.bill_number; }));
              delete b.error; var p = till.pending(sid); p.push(b); till.set("pending", sid, p); syncTill(sid); paintBanner();
            } }, "Send again"),
            h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: function () {
              DQ.confirm({ title: "Remove bill " + b.bill_number + " from this till?", message: "The sale will not be recorded anywhere. Do this only after a manager has recorded it another way.", reason: true, confirmLabel: "Remove", danger: true }).then(function (r) {
                if (!r.ok) return;
                till.set("refused", sid, till.get("refused", sid, []).filter(function (x) { return x.bill_number !== b.bill_number; }));
                paintBanner();
              });
            } }, "Remove"));
        }))));
    }

    paintCart(); paintPay(); paintBanner();
    cat.load().catch(function () {});
    syncTill(sid);
    focusScan();
  } });

  /* ================================================================ BILLS AND RETURNS */
  var BILL_STATE = { paid: ["Paid", "ok"], refund: ["Refund", "attention"], voided: ["Voided", "risk"], held: ["Held", "data"], open: ["Open", "data"] };
  function methodName(m) { return m === "upi" ? "UPI" : cap(m); }
  DQ.route("#/bills", { title: "Bills and returns", nav: "Bills and returns", render: function (main, params, query, alive) {
    var store = DQ.storeObj();
    if (!store) return noStore(main);
    main.appendChild(ui.page("Bills and returns", { sub: "Find a bill in " + store.name + " by its number, day or amount. Returns are refunded from the bill." }));
    var number = ui.input({ value: query.number || "", autocomplete: "off", placeholder: "Part of the number" });
    var day = ui.input({ type: "date", value: query.day !== undefined ? query.day : fmt.today(), max: fmt.today() });
    var amount = ui.input({ inputmode: "decimal", value: query.amount || "", autocomplete: "off", placeholder: "Exact total, like 250" });
    var form = h("form", { class: "c-search", onsubmit: function (e) {
      e.preventDefault();
      DQ.go("#/bills" + DQ.qs({ number: number.value.trim(), day: day.value || "", amount: amount.value.trim() }));
    } }, ui.field("Bill number", number), ui.field("Day", day), ui.field("Amount (₹)", amount), h("div", { class: "c-search-btn" }, h("button", { class: "btn btn-primary", type: "submit" }, "Find")));
    main.appendChild(card(null, form));
    var body = h("div", null, ui.skeleton(5));
    main.appendChild(body);
    var amt = query.amount ? fmt.toPaise(query.amount) : null;
    return DQ.get("/api/checkout/bills" + DQ.qs({ store_id: store.id, number: query.number || null, day: query.day !== undefined ? query.day : fmt.today(), amount_paise: amt, limit: 100 })).then(function (rows) {
      if (!alive()) return;
      DQ.clear(body);
      body.appendChild(ui.table([
        { key: "bill_number", label: "Bill", render: function (b) { return h("span", { class: "mono" }, b.bill_number); } },
        { key: "paid_at", label: "Time", render: function (b) { return b.paid_at ? T.dt(b.paid_at) : "Not paid"; } },
        { key: "state", label: "State", render: function (b) { var s = BILL_STATE[b.state] || [b.state, "neutral"]; return h("span", null, ui.badge(s[0], s[1]), b.made_offline ? " " : null, b.made_offline ? ui.badge("Made offline", "neutral") : null); } },
        { key: "lines", label: "Items", kind: "num", render: function (b) { return fmt.num(b.lines.length); } },
        { key: "payments", label: "Paid by", render: function (b) { return b.payments.map(function (p) { return methodName(p.method); }).join(", ") || "—"; } },
        { key: "total_paise", label: "Total", kind: "money" }
      ], rows, { onRow: function (b) { DQ.go("#/bills/" + b.id); }, caption: "Bills found",
        empty: { title: "No bill matches", text: store.mode === "connected" ? "Try another day, or clear the number and amount. Bills are made on Checkout." : store.name + " runs standalone, so it has no till bills. Its sales come from the daily file.", action: store.mode === "connected" ? { label: "Go to Checkout", href: "#/checkout" } : { label: "See sales", href: "#/sales" } } }));
    });
  } });

  DQ.route("#/bills/:id", { title: "Bill", render: function (main, params, query, alive) {
    main.appendChild(ui.skeleton(6));
    return DQ.get("/api/checkout/bills/" + params.id).then(function (b) {
      if (!alive()) return;
      DQ.clear(main);
      var store = storeById(b.store_id) || DQ.storeObj();
      var st = BILL_STATE[b.state] || [b.state, "neutral"];
      main.appendChild(ui.page((b.state === "refund" ? "Refund " : "Bill ") + b.bill_number, {
        sub: (b.paid_at ? T.dt(b.paid_at) : "Not paid") + (b.cashier ? " · " + b.cashier : "") + (b.approved_by ? " · approved by " + b.approved_by : ""),
        actions: h("a", { class: "btn btn-ghost", href: "#/bills" }, "Find another bill") }));
      main.appendChild(h("div", { class: "c-banner-row" }, ui.badge(st[0], st[1]), b.made_offline ? ui.badge("Made offline", "neutral") : null,
        b.refund_of ? h("a", { href: "#/bills/" + b.refund_of }, "See the original bill") : null,
        b.void_reason ? h("span", { class: "text-2" }, "Voided: " + b.void_reason) : null, b.refund_reason ? h("span", { class: "text-2" }, "Reason: " + b.refund_reason) : null));
      var left = card("Receipt", h("div", { class: "c-form" }, h("pre", { class: "c-receipt" }, b.receipt),
        h("div", { class: "row" }, ui.button("Print", function () { printText(b.receipt); }, "ghost")),
        b.state === "paid" || b.state === "refund" ? receiptActions(function () { return Promise.resolve(b.id); }) : null));
      var right = null;
      if (b.state === "paid" && store && store.mode === "connected" && DQ.can("sales.bill", b.store_id)) right = refundForm(b, store);
      main.appendChild(h("div", { class: right ? "c-two" : "" }, left, right));
    });
    function refundForm(b, store) {
      var rows = [], refNo = till.number(store, "R-");
      var lines = b.lines.filter(function (l) { return l.product_id; });
      if (!lines.length) return card("Return items", h("p", { class: "text-2" }, "This bill has only misc items, which cannot be returned to stock here."));
      var list = h("div", { class: "c-ref-lines" });
      lines.forEach(function (l, i) {
        var p = cat.byId(l.product_id), name = p ? p.name : (l.description || "Product"), unit = p ? p.unit : "each";
        var r = { line: l, unit: unit, name: name };
        r.on = h("input", { type: "checkbox", class: "c-check", id: "c-ref-on-" + i });
        r.q = ui.input({ inputmode: "decimal", class: "input c-num-input", value: String(l.qty_milli / 1000), id: "c-ref-q-" + i });
        r.cond = ui.select([{ value: "1", label: "Resaleable: back to stock" }, { value: "0", label: "Damaged: written off" }], "1", { id: "c-ref-c-" + i });
        r.q.addEventListener("input", paintTotal); r.on.addEventListener("change", paintTotal);
        rows.push(r);
        list.appendChild(h("div", { class: "c-ref-line" },
          h("label", { class: "c-checkline", for: r.on.id }, r.on, h("span", null, h("strong", null, name), h("br"), h("span", { class: "text-2 mono" }, qty(l.qty_milli, unit) + " × " + money(l.unit_price_paise) + (l.discount_paise ? " − " + money(l.discount_paise) : "")))),
          h("div", { class: "grid-2" }, ui.field("Quantity returned", r.q), ui.field("Condition", r.cond))));
      });
      if (lines.some(function (l) { return !cat.byId(l.product_id); }) && navigator.onLine) cat.load(true).then(function () {
        if (alive() && lines.every(function (l) { return cat.byId(l.product_id); })) DQ.render();
      }, function () {});
      var reason = ui.input({ maxlength: 200, placeholder: "For example: wrong size, leaking pack" });
      var methodSel = ui.select((b.payments.length ? b.payments.map(function (p) { return p.method; }) : ["cash"]).concat(["cash", "upi", "card"]).filter(function (m, i, a) { return a.indexOf(m) === i; }).map(function (m) { return { value: m, label: "Refund by " + methodName(m) }; }), b.payments[0] ? b.payments[0].method : "cash");
      var pin = ui.input({ type: "password", inputmode: "numeric", autocomplete: "off", maxlength: 8 });
      var total = h("p", { class: "c-value", "aria-live": "polite" });
      var err = h("div", { class: "form-error", role: "alert", hidden: true });
      var btn = ui.button("Refund", go, "primary");
      function chosen() {
        return rows.filter(function (r) { return r.on.checked; }).map(function (r) {
          var m = fmt.toMilli(r.q.value);
          var gross = Math.floor(r.line.qty_milli * r.line.unit_price_paise / 1000) - (r.line.discount_paise || 0);
          return { r: r, m: m, value: m ? Math.floor(gross * m / r.line.qty_milli) : 0 };
        });
      }
      function paintTotal() { var c = chosen(), v = c.reduce(function (a, x) { return a + x.value; }, 0); total.textContent = c.length ? "Refund about " + money(v) : "Tick what is being returned."; btn.textContent = c.length ? "Refund " + money(v) : "Refund"; }
      function go() {
        err.hidden = true;
        var c = chosen();
        if (!c.length) { err.textContent = "Tick at least one item being returned."; err.hidden = false; return; }
        var bad = c.filter(function (x) { return x.m === null || x.m <= 0 || x.m > x.r.line.qty_milli || (x.r.unit === "each" && x.m % 1000); })[0];
        if (bad) { err.textContent = bad.r.name + ": return between 1 and " + qty(bad.r.line.qty_milli, bad.r.unit) + ", in whole units where it is sold that way."; err.hidden = false; return; }
        if (reason.value.trim().length < 3) { err.textContent = "Give the reason for the refund."; err.hidden = false; reason.focus(); return; }
        if (!/^\d{4,8}$/.test(pin.value)) { err.textContent = "A manager types their PIN to approve the refund."; err.hidden = false; pin.focus(); return; }
        var v = c.reduce(function (a, x) { return a + x.value; }, 0);
        DQ.confirm({ title: "Refund " + money(v) + " by " + methodName(methodSel.value) + "?", message: "A refund cannot be undone. Resaleable items go back to stock; damaged ones are written off.", confirmLabel: "Refund", danger: true }).then(function (ok) {
          if (!ok.ok) return;
          btn.disabled = true;
          DQ.post("/api/checkout/refunds", { store_id: b.store_id, refund_number: refNo, original_bill_number: b.bill_number, reason: reason.value.trim(), method: methodSel.value, manager_pin: pin.value, device_id: DQ.deviceId(),
            lines: c.map(function (x) { return { product_id: x.r.line.product_id, qty_milli: x.m, resaleable: x.r.cond.value === "1" }; }) }).then(function (res) {
            DQ.toast((res.duplicate ? "This refund was already made. " : "Refunded ") + money(-res.total_paise) + ". Give it back by " + methodName(methodSel.value) + ".");
            DQ.go("#/bills/" + res.id);
          }, function (e) { btn.disabled = false; pin.value = ""; err.textContent = e.message; err.hidden = false; });
        });
      }
      paintTotal();
      return card("Return items", h("div", { class: "c-form" }, h("p", { class: "text-2" }, "Tick the items coming back. A paid bill is never edited: a refund is recorded against it."), list,
        ui.field("Reason", reason), h("div", { class: "grid-2" }, ui.field("Refund method", methodSel), ui.field("Manager PIN", pin)), total, err, h("div", { class: "row" }, btn)));
    }
  } });

  /* ================================================================ DAY CLOSE */
  DQ.route("#/day-close", { title: "Day close", nav: "Day close", render: function (main, params, query, alive) {
    var store = DQ.storeObj();
    if (!store) return noStore(main);
    var day = query.day || fmt.today(), lastHist = [];
    var dayIn = ui.input({ type: "date", value: day, max: fmt.today(), "aria-label": "Day", class: "input c-day-in" });
    dayIn.addEventListener("change", function () { if (dayIn.value) DQ.go("#/day-close" + DQ.qs({ day: dayIn.value })); });
    main.appendChild(ui.page("Day close", { sub: store.name + ". Check the day, count the cash, and close it so tonight's forecast uses complete sales.", actions: dayIn }));
    var body = h("div", { class: "c-stack" }, ui.skeleton(6));
    main.appendChild(body);
    return Promise.all([DQ.get("/api/day-close" + DQ.qs({ store_id: store.id, day: day })), DQ.get("/api/day-close/history" + DQ.qs({ store_id: store.id, days: 30 }))]).then(function (r) {
      if (!alive()) return;
      var s = r[0], hist = r[1], z = s.z_report;
      lastHist = hist;
      DQ.clear(body);
      var canClose = DQ.can("day.close", store.id);
      if (s.closed) body.appendChild(msgBox("ok", T.date(s.day) + " is closed" + (s.closed_automatically ? " automatically" : "") + (s.closed_at ? " (" + T.dt(s.closed_at) + ")" : "") + "." + (s.late_data ? " Late data arrived after closing; tonight's run uses it." : "")));
      body.appendChild(card("Checks for " + T.date(s.day), h("ul", { class: "c-checks" }, s.checks.map(function (c) {
        return h("li", { "data-ok": c.ok ? "yes" : "no" }, h("span", { class: "c-check-mark", "aria-hidden": "true" }, c.ok ? "✓" : "!"), h("span", null, h("span", { class: "sr-only" }, c.ok ? "Done: " : "Needs attention: "), c.text));
      }))));
      if (s.open_bills.length) body.appendChild(card("Open bills (" + s.open_bills.length + ")", h("div", null,
        h("p", { class: "text-2" }, "These bills were started or held and never paid. Closing the day voids them."),
        h("ul", { class: "c-lines" }, s.open_bills.map(function (b) { return h("li", { class: "c-line" }, h("div", { class: "c-line-main" }, h("a", { href: "#/bills/" + b.id, class: "mono" }, b.bill_number), h("span", { class: "mono" }, money(b.total_paise)))); })))));
      var cashIn = null, diffEl = null;
      if (s.mode === "connected" && z) {
        var bm = z.by_method_paise || {};
        body.appendChild(card("Z-report", h("div", { class: "tiles c-tiles" },
          tile("Bills", fmt.num(z.bills), z.made_offline ? z.made_offline + " made offline" : null),
          tile("Sales", money(z.sales_paise), "GST inside " + money(z.tax_paise)),
          tile("Discounts", money(z.discounts_paise)),
          tile("Refunds", money(z.refunds_paise), fmt.num(z.refunds) + " refund" + (z.refunds === 1 ? "" : "s")),
          tile("Voids", fmt.num(z.voids)),
          tile("Cash", money(bm.cash || 0)), tile("UPI", money(bm.upi || 0)), tile("Card", money(bm.card || 0)))));
        cashIn = ui.input({ inputmode: "decimal", autocomplete: "off", class: "input c-big-input", disabled: s.closed || !canClose });
        diffEl = h("p", { class: "c-value", "aria-live": "polite" });
        var paintDiff = function () {
          var c = fmt.toPaise(cashIn.value);
          if (!cashIn.value.trim() || c === null) { diffEl.textContent = "Count the cash in the drawer and type it."; diffEl.removeAttribute("data-kind"); return; }
          var d = c - s.cash_expected_paise;
          diffEl.textContent = d === 0 ? "The cash matches." : (d < 0 ? "Short by " : "Over by ") + money(Math.abs(d)) + ".";
          diffEl.setAttribute("data-kind", d === 0 ? "ok" : "risk");
        };
        cashIn.addEventListener("input", paintDiff);
        body.appendChild(card("Cash", h("div", { class: "c-form" },
          h("div", { class: "c-cash" }, h("div", null, h("p", { class: "tile-label" }, "Cash expected"), h("p", { class: "c-total-value mono" }, money(s.cash_expected_paise))),
            s.closed ? closedCash() : h("div", null, ui.field("Cash counted (₹)", cashIn))), s.closed ? null : diffEl)));
        paintDiff();
      } else {
        body.appendChild(card("Sales for the day", h("div", { class: "c-form" },
          h("div", { class: "tiles c-tiles" }, tile("Units sold", qty(s.sales_units_milli)), tile("Sales value", money(s.sales_revenue_paise))),
          h("p", { class: "text-2" }, store.name + " runs standalone: its sales come from the day's file or typed totals."),
          h("div", { class: "row" }, DQ.can("imports.upload") ? h("a", { class: "btn btn-ghost", href: "#/imports/new/daily_sales" }, "Upload the day's sales file") : null,
            DQ.can("sales.enter", store.id) ? h("a", { class: "btn btn-ghost", href: "#/sales" + DQ.qs({ tab: "entry", day: s.day }) }, "Type the totals") : null))));
      }
      if (!s.closed) {
        if (!canClose) body.appendChild(msgBox("neutral", "Only a store manager, Admin or Owner closes the day. Anyone can check it here."));
        else {
          var closedShop = h("input", { type: "checkbox", class: "c-check", id: "c-dc-closed" });
          var noSales = h("input", { type: "checkbox", class: "c-check", id: "c-dc-nosales" });
          var note = ui.input({ maxlength: 300, placeholder: "Optional" });
          var err = h("div", { class: "form-error", role: "alert", hidden: true });
          var btn = ui.button("Close " + T.date(s.day), function () {
            err.hidden = true;
            var counted = cashIn && cashIn.value.trim() ? fmt.toPaise(cashIn.value) : null;
            if (cashIn && cashIn.value.trim() && (counted === null || counted < 0)) { err.textContent = "Type the cash counted in rupees."; err.hidden = false; cashIn.focus(); return; }
            var msg = [];
            if (s.open_bills.length) msg.push(s.open_bills.length + " open bill" + (s.open_bills.length > 1 ? "s are" : " is") + " voided.");
            if (cashIn && counted === null) msg.push("No cash count is recorded.");
            msg.push("Closing cannot be undone. Data that arrives later is still accepted and marks the day for a re-run.");
            DQ.confirm({ title: "Close " + T.date(s.day) + " for " + store.name + "?", message: msg.join(" "), confirmLabel: "Close the day" }).then(function (c) {
              if (!c.ok) return;
              btn.disabled = true;
              DQ.post("/api/day-close", { store_id: store.id, day: s.day, cash_counted_paise: counted, void_open_bills: s.open_bills.length > 0, no_sales: noSales.checked, store_was_closed: closedShop.checked, note: note.value.trim() || null }).then(function () {
                DQ.toast(T.date(s.day) + " is closed."); DQ.render();
              }, function (e) { btn.disabled = false; err.textContent = e.message; err.hidden = false; });
            });
          });
          body.appendChild(card("Close the day", h("div", { class: "c-form" },
            h("label", { class: "c-checkline", for: "c-dc-closed" }, closedShop, h("span", null, h("strong", null, "The store was closed today. "), "The forecast learns that this day had no trading.")),
            h("label", { class: "c-checkline", for: "c-dc-nosales" }, noSales, h("span", null, h("strong", null, "Nothing was sold. "), "The store was open but made no sales.")),
            ui.field("Note", note), err, h("div", { class: "row" }, btn))));
        }
      }
      body.appendChild(card("Recent closes", ui.table([
        { key: "day", label: "Day", kind: "date" },
        { key: "closed_by", label: "Closed by", render: function (x) { return x.automatic ? ui.badge("Closed automatically", "attention") : (x.closed_by || "—"); } },
        { key: "cash_expected_paise", label: "Cash expected", kind: "money" },
        { key: "cash_counted_paise", label: "Cash counted", kind: "money" },
        { key: "d", label: "Difference", kind: "num", render: function (x) { if (x.cash_counted_paise === null || x.cash_expected_paise === null) return "—"; var d = x.cash_counted_paise - x.cash_expected_paise; return d === 0 ? "None" : (d < 0 ? "Short " : "Over ") + money(Math.abs(d)); } },
        { key: "notes", label: "Notes", render: function (x) { return h("span", null, x.store_was_closed ? ui.badge("Store closed", "neutral") : null, x.late_data ? ui.badge("Late data", "attention") : null, x.note ? " " + x.note : null); } }
      ], hist, { onRow: function (x) { DQ.go("#/day-close" + DQ.qs({ day: x.day })); }, caption: "Recent day closes", empty: { title: "No day has been closed yet", text: "Close each day after the last sale. A day nobody closes is closed automatically at night and flagged." } })));
    });
    function closedCash() {
      var hc = (lastHist || []).filter(function (x) { return x.day === day; })[0];
      if (!hc || hc.cash_counted_paise === null) return h("div", null, h("p", { class: "tile-label" }, "Cash counted"), h("p", { class: "text-2" }, "Not recorded"));
      var d = hc.cash_counted_paise - (hc.cash_expected_paise || 0);
      return h("div", null, h("p", { class: "tile-label" }, "Cash counted"), h("p", { class: "c-total-value mono" }, money(hc.cash_counted_paise)),
        h("p", { class: "text-2" }, d === 0 ? "Matched" : (d < 0 ? "Short by " : "Over by ") + money(Math.abs(d))));
    }
    function tile(label, value, note) { return h("div", { class: "tile" }, h("p", { class: "tile-label" }, label), h("p", { class: "tile-value" }, value), note ? h("p", { class: "tile-note" }, note) : null); }
  } });

  /* ================================================================ SALES */
  DQ.route("#/sales", { title: "Sales", nav: "Sales", render: function (main, params, query, alive) {
    var store = DQ.storeObj();
    if (!store) return noStore(main);
    var canEnter = store.mode === "standalone" && DQ.can("sales.enter", store.id);
    var tab = query.tab === "days" ? "days" : query.tab === "entry" && canEnter ? "entry" : "grid";
    main.appendChild(ui.page("Sales", { sub: store.name + (store.mode === "standalone" ? " runs standalone: sales come from the day's file or typed totals." : " is connected: sales come from the tills."),
      actions: store.mode === "standalone" && DQ.can("imports.upload") ? h("a", { class: "btn btn-ghost", href: "#/imports/new/daily_sales" }, "Upload the day's sales file") : null }));
    var tabs = [{ value: "grid", label: "By product and day" }, { value: "days", label: "Daily totals" }];
    if (canEnter) tabs.push({ value: "entry", label: "Type a day's totals" });
    main.appendChild(ui.tabs(tabs, tab, function (v) { DQ.go("#/sales" + (v === "grid" ? "" : DQ.qs({ tab: v }))); }));
    var body = h("div");
    main.appendChild(body);
    if (tab === "days") return days();
    if (tab === "entry") return entry();
    return grid();

    function grid() {
      var to = query.to || fmt.today();
      var from = query.from || new Date(new Date(to + "T00:00:00Z").getTime() - 13 * 86400000).toISOString().slice(0, 10);
      var fromIn = ui.input({ type: "date", value: from, max: fmt.today() }), toIn = ui.input({ type: "date", value: to, max: fmt.today() });
      var qIn = ui.input({ type: "search", value: query.q || "", placeholder: "Name or SKU" });
      body.appendChild(card(null, h("form", { class: "c-search", onsubmit: function (e) { e.preventDefault(); DQ.go("#/sales" + DQ.qs({ from: fromIn.value, to: toIn.value, q: qIn.value.trim() })); } },
        ui.field("From", fromIn), ui.field("To", toIn), ui.field("Product", qIn), h("div", { class: "c-search-btn" }, h("button", { class: "btn btn-primary", type: "submit" }, "Show")))));
      var out = h("div", null, ui.skeleton(8));
      body.appendChild(out);
      return DQ.get("/api/sales" + DQ.qs({ store_id: store.id, from: from, to: to, q: query.q || null })).then(function (g) {
        if (!alive()) return;
        DQ.clear(out);
        if (!g.rows.length) { out.appendChild(ui.empty({ title: "No sales in these days", text: store.mode === "standalone" ? "Upload the day's sales file or type the totals, and they appear here." : "Bills paid on Checkout appear here once each day is closed.", action: store.mode === "standalone" && canEnter ? { label: "Type a day's totals", href: "#/sales?tab=entry" } : null })); return; }
        var thead = h("tr", null, h("th", { scope: "col", class: "c-sticky" }, "Product"), g.days.map(function (d) { return h("th", { scope: "col", class: "num" }, fmt.dayShort(d)); }), h("th", { scope: "col", class: "num" }, "Total"));
        var totRow = h("tr", { class: "c-tot-row" }, h("th", { scope: "row", class: "c-sticky" }, "Sales value"), g.days.map(function (d) { return h("td", { class: "num" }, money(g.totals[d].revenue_paise, { whole: true })); }),
          h("td", { class: "num" }, money(g.rows.reduce(function (a, r) { return a + r.revenue_paise; }, 0), { whole: true })));
        var rows = g.rows.slice(0, 300).map(function (r) {
          return h("tr", null, h("th", { scope: "row", class: "c-sticky" }, h("span", null, r.name), h("br"), h("span", { class: "text-3 mono" }, r.sku + (r.unit && r.unit !== "each" ? " · " + r.unit : ""))),
            g.days.map(function (d) {
              var c = r.days[d];
              if (!c) return h("td", { class: "num text-3" }, "—");
              return h("td", { class: "num" + (c.out_of_stock ? " c-oos" : ""), title: c.out_of_stock ? "Out of stock this day" + (c.oos_source ? " (" + words(c.oos_source) + ")" : "") : null },
                bare(c.units_milli, r.unit), c.out_of_stock ? h("span", { class: "c-oos-mark" }, " OOS") : null);
            }), h("td", { class: "num" }, h("strong", null, qty(r.units_milli, r.unit)), h("br"), h("span", { class: "text-3" }, money(r.revenue_paise, { whole: true }))));
        });
        out.appendChild(h("p", { class: "text-2 c-note" }, "Units sold per day. ", h("span", { class: "c-oos-mark" }, "OOS"), " marks a day the product was out of stock, so its sales were held back, not low demand."));
        out.appendChild(h("div", { class: "table-wrap c-grid-wrap" }, h("table", { class: "table c-grid" }, h("caption", { class: "sr-only" }, "Units sold by product and day"), h("thead", null, thead), h("tbody", null, totRow, rows))));
        if (g.rows.length > 300) out.appendChild(h("p", { class: "text-3 c-note" }, "Showing the 300 best sellers of " + fmt.num(g.rows.length) + ". Search to find others."));
      });
    }
    function days() {
      body.appendChild(ui.skeleton(8));
      return DQ.get("/api/sales/days" + DQ.qs({ store_id: store.id, days: 30 })).then(function (rows) {
        if (!alive()) return;
        DQ.clear(body);
        body.appendChild(ui.table([
          { key: "day", label: "Day", kind: "date" },
          { key: "units_milli", label: "Units", kind: "qty" },
          { key: "revenue_paise", label: "Sales value", kind: "money" },
          { key: "products", label: "Products", kind: "num" },
          { key: "closed", label: "Day close", render: function (x) { return h("span", null, x.closed ? ui.badge(x.closed_automatically ? "Closed automatically" : "Closed", x.closed_automatically ? "attention" : "ok") : ui.badge("Open", "neutral"), x.late_data ? " " : null, x.late_data ? ui.badge("Late data", "attention") : null); } }
        ], rows, { onRow: function (x) { DQ.go("#/day-close" + DQ.qs({ day: x.day })); }, caption: "Daily sales totals" }));
      });
    }
    function entry() {
      var day = query.day || fmt.today();
      var dayIn = ui.input({ type: "date", value: day, max: fmt.today() });
      dayIn.addEventListener("change", function () { if (dayIn.value) DQ.go("#/sales" + DQ.qs({ tab: "entry", day: dayIn.value })); });
      var filter = ui.input({ type: "search", placeholder: "Filter by name or SKU" });
      body.appendChild(card(null, h("div", { class: "c-search" }, ui.field("Day", dayIn), ui.field("Find a product", filter),
        h("p", { class: "text-2 c-search-help" }, "Type the units sold for each product. Leave the value empty to use the shelf price. Only products you change are saved."))));
      var out = h("div", null, ui.skeleton(8));
      body.appendChild(out);
      return Promise.all([cat.load(true), DQ.get("/api/sales" + DQ.qs({ store_id: store.id, from: day, to: day }))]).then(function (r) {
        if (!alive()) return;
        DQ.clear(out);
        var have = {}; r[1].rows.forEach(function (x) { var c = x.days[day]; if (c) have[x.product_id] = c; });
        var prods = r[0].filter(function (p) { return p.status !== "discontinued"; }).sort(function (a, b) { return a.name.localeCompare(b.name); });
        if (!prods.length) { out.appendChild(ui.empty({ title: "No products yet", text: "Add your products first, then type their sales here.", action: { label: "Import products", href: "#/imports/new/products" } })); return; }
        var rows = prods.map(function (p) {
          var c = have[p.id];
          var u = ui.input({ inputmode: "decimal", class: "input c-num-input", autocomplete: "off", value: c ? String(c.units_milli / 1000) : "", "aria-label": "Units of " + p.name, id: "c-se-u-" + p.id });
          var v = ui.input({ inputmode: "decimal", class: "input c-num-input", autocomplete: "off", value: c ? (c.revenue_paise / 100).toFixed(2) : "", placeholder: p.price_paise ? (p.price_paise / 100).toFixed(2) + " each" : "", "aria-label": "Sales value of " + p.name + " in rupees" });
          var tr = h("tr", { "data-q": (p.name + " " + p.sku).toLowerCase() }, h("th", { scope: "row" }, p.name, h("br"), h("span", { class: "text-3 mono" }, p.sku + " · " + unitWord(p.unit))), h("td", { class: "num" }, u), h("td", { class: "num" }, v));
          return { p: p, u: u, v: v, tr: tr, u0: u.value, v0: v.value };
        });
        filter.addEventListener("input", function () { var t = filter.value.trim().toLowerCase(); rows.forEach(function (x) { x.tr.hidden = t && x.tr.getAttribute("data-q").indexOf(t) < 0; }); });
        var err = h("div", { class: "form-error", role: "alert", hidden: true });
        var result = h("div", { "aria-live": "polite" });
        var btn = ui.button("Save the totals for " + T.date(day), function () {
          err.hidden = true; DQ.clear(result);
          var lines = [], bad = null;
          rows.forEach(function (x) {
            if (x.u.value === x.u0 && x.v.value === x.v0) return;
            if (!x.u.value.trim() && !x.v.value.trim()) return;
            var m = x.u.value.trim() ? fmt.toMilli(x.u.value) : 0, pr = qtyProblem(m, x.p, true);
            var rv = x.v.value.trim() ? fmt.toPaise(x.v.value) : null;
            if (pr) { bad = bad || x.p.name + ": " + pr; setErr(x.u, pr); return; }
            if (x.v.value.trim() && (rv === null || rv < 0)) { bad = bad || x.p.name + ": the value is not a number."; return; }
            lines.push({ product_id: x.p.id, units_milli: m, revenue_paise: rv });
          });
          if (bad) { err.textContent = bad; err.hidden = false; return; }
          if (!lines.length) { err.textContent = "Nothing changed. Type the units sold for at least one product."; err.hidden = false; return; }
          btn.disabled = true;
          send("Sales totals for " + T.date(day), "PUT", "/api/sales/entry", { store_id: store.id, day: day, lines: lines }).then(function (res) {
            btn.disabled = false;
            if (res.queued) { result.appendChild(msgBox("attention", "Saved on this device. The totals are sent once the internet is back.")); return; }
            var d = res.data;
            rows.forEach(function (x) { x.u0 = x.u.value; x.v0 = x.v.value; });
            result.appendChild(msgBox("ok", "Saved " + lines.length + " product" + (lines.length > 1 ? "s" : "") + " for " + T.date(day) + ": " + d.new + " new, " + d.updated + " changed" + (d.stock_moves ? ", stock moved for " + d.stock_moves : "") + "."));
          }, function (e) { btn.disabled = false; err.textContent = e.message; err.hidden = false; });
        });
        out.appendChild(h("div", { class: "table-wrap c-entry-wrap" }, h("table", { class: "table" }, h("caption", { class: "sr-only" }, "Sales entry for " + T.date(day)),
          h("thead", null, h("tr", null, h("th", { scope: "col" }, "Product"), h("th", { scope: "col", class: "num" }, "Units sold"), h("th", { scope: "col", class: "num" }, "Sales value (₹)"))),
          h("tbody", null, rows.map(function (x) { return x.tr; })))));
        out.appendChild(h("div", { class: "c-sticky-foot" }, err, result, h("div", { class: "row" }, btn, h("a", { class: "btn btn-ghost", href: "#/day-close" + DQ.qs({ day: day }) }, "Go to Day close"))));
      });
    }
  } });
})();
