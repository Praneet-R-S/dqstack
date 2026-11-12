/* Screens, group d: Connectors (Tally and Zoho Inventory), for Owners and Admins.
   The server is dqstack/app/routes/connectors.py. Everything a connector brings in
   becomes an ordinary import job, so this screen links to the Imports screen for the
   check report, the problem list and Undo. Uses the helpers in js/core.js. */
(function () {
  "use strict";
  var DQ = window.DQ, h = DQ.h, fmt = DQ.fmt, ui = DQ.ui;
  /* DQ.t fills a template ({name}) and translates it when js/i18n.js is loaded. */
  var t = DQ.t || function (s, v) {
    return String(s).replace(/\{(\w+)\}/g, function (m, k) { return v && v[k] !== undefined ? v[k] : m; });
  };

  /* ================================================================ helpers */
  function card(title, children, opts) {
    opts = opts || {};
    var id = "d-h-" + Math.random().toString(36).slice(2, 8);
    return h("section", { class: "card", "aria-labelledby": id },
      h("div", { class: "a-card-head" }, h("h2", { id: id, class: "title-3" }, title), opts.aside || null),
      opts.sub ? h("p", { class: "text-2 a-card-sub" }, opts.sub) : null,
      children);
  }
  function notice(kind, children) {
    return h("div", { class: "a-notice", "data-kind": kind, role: kind === "risk" ? "alert" : "status" }, children);
  }
  function when(iso) { return iso ? fmt.dateTime(iso) : "—"; }
  function busy(btn, label, promise) {
    var old = btn.textContent;
    btn.disabled = true; btn.textContent = label;
    return promise.then(function (v) { btn.disabled = false; btn.textContent = old; return v; },
      function (e) { btn.disabled = false; btn.textContent = old; DQ.toast(e.message, { kind: "risk" }); throw e; });
  }
  function swallow() { /* the toast has said it */ }
  /* A form whose button shows progress and whose errors are the server's own words. */
  function form(children, label, onSubmit, opts) {
    opts = opts || {};
    var err = h("div", { class: "form-error", role: "alert", hidden: true });
    var btn = h("button", { class: "btn btn-primary", type: "submit" }, label);
    var f = h("form", { class: "stack a-form", novalidate: true }, children, err, h("div", { class: "row" }, btn, opts.extra || null));
    f.addEventListener("submit", function (e) {
      e.preventDefault();
      var bad = Array.prototype.filter.call(f.querySelectorAll("[required]"), function (i) { return !i.disabled && !String(i.value).trim(); });
      if (bad.length) { err.textContent = "Fill in every box marked as needed."; err.hidden = false; bad[0].focus(); return; }
      var old = btn.textContent;
      btn.disabled = true; btn.textContent = opts.busy || "Saving…"; err.hidden = true;
      Promise.resolve().then(onSubmit).then(function () { btn.disabled = false; btn.textContent = old; },
        function (e2) { btn.disabled = false; btn.textContent = old; err.textContent = e2 && e2.message ? e2.message : String(e2); err.hidden = false; });
    });
    return f;
  }
  function copyField(label, text, help) {
    var input = ui.input({ readonly: true, value: text, spellcheck: "false" });
    var btn = ui.button("Copy", function () {
      function done() { btn.textContent = "Copied"; setTimeout(function () { btn.textContent = "Copy"; }, 2000); }
      if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, fallback); else fallback();
      function fallback() { input.select(); try { document.execCommand("copy"); done(); } catch (e) { DQ.toast("Select the text and copy it by hand.", { kind: "attention" }); } }
    }, "ghost");
    input.addEventListener("focus", function () { input.select(); });
    var f = ui.field(label, input, help);
    f.appendChild(h("div", { class: "row" }, btn));
    return f;
  }
  function check(label, on, help) {
    var cb = h("input", { type: "checkbox" });
    cb.checked = !!on;
    return { input: cb, el: h("div", { class: "field" }, h("label", { class: "a-check" }, cb, h("span", null, label)), help ? h("p", { class: "field-help" }, help) : null) };
  }
  function storeOptions(stores, first) {
    return (first ? [first] : []).concat(stores.map(function (s) { return { value: s.id, label: s.name + " (" + s.code + ")" }; }));
  }
  function runStatus(s) {
    var m = { succeeded: ["Done", "ok"], warning: ["Done, with notes", "attention"], failed: ["Failed", "risk"], running: ["Running", "data"] }[s] || [s, "neutral"];
    return ui.badge(m[0], m[1]);
  }
  var TRIGGERS = { schedule: "Nightly", manual: "Sync now", export: "Export", "import": "File read" };
  function runsTable(runs, empty) {
    return ui.table([
      { key: "started_at", label: "When", render: function (r) { return when(r.started_at); } },
      { key: "trigger", label: "How", render: function (r) { return (TRIGGERS[r.trigger] || r.trigger) + (r.person ? ", " + r.person : ""); } },
      { key: "status", label: "Result", render: function (r) { return runStatus(r.status); } },
      { key: "message", label: "What happened", render: function (r) {
        var links = (r.import_job_ids || []).map(function (id, i) { return h("a", { href: "#/imports/" + id }, t("Import {n}", { n: i + 1 })); });
        return h("div", null, h("span", null, r.message || "—"), links.length ? h("div", { class: "row" }, links) : null);
      } }
    ], runs, { caption: "Recent runs", empty: empty });
  }
  function isoDay(d) { return d.toISOString().slice(0, 10); }

  /* ================================================================ the screen */
  var TABS = [{ value: "tally", label: "Tally" }, { value: "zoho", label: "Zoho Inventory" }];
  var CALLBACK = {
    connected: ["ok", "Zoho Inventory is connected. Match its warehouses to your stores, then sync."],
    denied: ["attention", "Zoho's consent page was cancelled, so nothing was connected."],
    expired: ["attention", "That took too long. Press Connect again."],
    failed: ["risk", "Zoho did not give DQ-Stack access. The reason is shown below; check the client id, secret, data centre and redirect address."],
    "not-allowed": ["risk", "Only an Owner or Admin can connect Zoho Inventory."]
  };

  DQ.route("#/connectors", { title: "Connectors", nav: "Connectors", can: "integrations.manage", render: function (main, params, query, alive) {
    var tab = query.tab || (query.zoho ? "zoho" : "tally");
    main.appendChild(ui.page("Connectors", { sub: "Bring products and sales in from the software you already use, and send sales and purchases to your accounts in Tally." }));
    main.appendChild(ui.tabs(TABS, tab, function (v) { DQ.go("#/connectors?tab=" + v); }));
    var box = h("div", { class: "stack", role: "tabpanel" });
    main.appendChild(box);
    if (query.zoho && CALLBACK[query.zoho]) {
      box.appendChild(notice(CALLBACK[query.zoho][0], h("p", null, CALLBACK[query.zoho][1])));
      try { history.replaceState(null, "", "#/connectors?tab=zoho"); } catch (e) { /* harmless */ }
    }
    var body = h("div", { class: "stack" }, ui.skeleton(6));
    box.appendChild(body);
    function load() {
      return Promise.all([DQ.get("/api/connectors"), DQ.get("/api/stores")]).then(function (res) {
        if (!alive()) return;
        DQ.clear(body);
        if (tab === "zoho") zoho(body, res[0], res[1], load);
        else tally(body, res[0].tally, res[1], load);
      }, function (err) { if (!alive()) return; DQ.clear(body); body.appendChild(ui.error(err, load)); });
    }
    return load();
  } });

  /* ================================================================ Tally */
  function tally(body, data, stores, reload) {
    var s = data.settings;
    /* ---- export */
    var today = new Date(Date.now() + 330 * 60000);
    var first = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), 1));
    var from = ui.input({ type: "date", required: true, value: isoDay(first) });
    var to = ui.input({ type: "date", required: true, value: isoDay(today) });
    var boxes = stores.map(function (st) {
      var cb = h("input", { type: "checkbox", value: st.id });
      cb.checked = true;
      return { cb: cb, el: h("label", { class: "a-check" }, cb, h("span", null, st.name + " (" + st.code + ")")) };
    });
    var dl = ui.button("Download the Tally file", function () {
      var ids = boxes.filter(function (b) { return b.cb.checked; }).map(function (b) { return b.cb.value; });
      if (!ids.length) { DQ.toast("Choose at least one store.", { kind: "attention" }); return; }
      if (!from.value || !to.value) { DQ.toast("Choose the first and last day.", { kind: "attention" }); return; }
      DQ.download("/api/connectors/tally/export" + DQ.qs({ start: from.value, end: to.value, stores: ids.join(",") }), dl).then(function (name) {
        if (name) reload();
      });
    });
    body.appendChild(card("Send to Tally", [
      h("p", { class: "text-2" }, s.voucher_per === "bill" ?
        "One sales voucher per bill and one credit note per refund, each store's uploaded or typed sales as one voucher a day, and one purchase voucher per delivery received, with GST split by rate." :
        "One sales voucher per store per day (one line per product), one credit note per store per day for refunds, and one purchase voucher per delivery received, with GST split by rate. Uploaded or typed sales join their day."),
      h("div", { class: "grid-2" }, ui.field("First day", from), ui.field("Last day", to)),
      h("fieldset", { class: "a-fieldset" }, h("legend", null, "Stores"), h("div", { class: "row" }, boxes.map(function (b) { return b.el; }))),
      h("div", { class: "row" }, dl),
      h("p", { class: "field-help" }, "You get a zip with two files. In TallyPrime, import 1-masters.xml first (Import, Masters), then 2-vouchers.xml (Import, Transactions). The masters create any ledger, stock item, unit and godown the vouchers need, so an empty company works too.")
    ]));

    /* ---- ledger names */
    var fields = {};
    function text(key, label, help) {
      fields[key] = ui.input({ value: s[key] || "", maxlength: 100, required: key !== "tally_company", spellcheck: "false" });
      return ui.field(label, fields[key], help);
    }
    var per = ui.select([{ value: "day", label: "One per store per day (recommended)" }, { value: "bill", label: "One per bill" }], s.voucher_per);
    var naming = ui.select([{ value: "name", label: "Product name" }, { value: "sku", label: "SKU" }], s.item_name);
    var zero = ui.select([{ value: "Exempt", label: "Exempt" }, { value: "Nil Rated", label: "Nil Rated" }], s.zero_rate_taxability);
    var godowns = check("A godown for each store", s.godowns, "Turn off if this Tally company does not keep godowns (locations).");
    var entered = check("Include sales uploaded or typed in DQ-Stack", s.include_entered_sales, "For stores without tills. Their GST is worked out from each product's rate.");
    body.appendChild(card("Your Tally company's names", form([
      h("p", { class: "text-2" }, "Tally companies name their ledgers differently. Use the names in yours, so the vouchers post to them. In a name, {rate} becomes the GST rate: Sales GST {rate}% gives Sales GST 5%."),
      h("div", { class: "grid-2" },
        text("tally_company", "Tally company name", "As shown in Tally. Leave empty to import into whichever company is open."),
        ui.field("Sales vouchers", per),
        text("counter_party", "Counter sales are made to", "Usually Cash."),
        text("sales_ledger", "Sales ledger"),
        text("purchase_ledger", "Purchase ledger"),
        text("round_off", "Round off ledger"),
        text("output_cgst", "Output CGST"), text("output_sgst", "Output SGST"),
        text("input_cgst", "Input CGST"), text("input_sgst", "Input SGST"),
        text("input_igst", "Input IGST", "Purchases from a supplier in another state."),
        text("misc_sales", "Misc items ledger", "Items sold at the till without a product.")),
      h("details", { class: "card a-details" }, h("summary", null, "Voucher types, units and stock items"),
        h("div", { class: "grid-2" },
          text("sales_voucher_type", "Sales voucher type"), text("credit_note_voucher_type", "Credit note voucher type"),
          text("purchase_voucher_type", "Purchase voucher type"), text("output_igst", "Output IGST"),
          text("counter_party_group", "Group for the counter sales ledger", "Used only if that ledger is new."),
          text("unit_each", "Unit for items sold one by one"), text("unit_kg", "Unit for kilograms"), text("unit_litre", "Unit for litres"),
          ui.field("Name stock items by", naming), ui.field("0% items are", zero)),
        godowns.el, entered.el)
    ], "Save the names", function () {
      var out = { voucher_per: per.value, item_name: naming.value, zero_rate_taxability: zero.value,
        godowns: godowns.input.checked, include_entered_sales: entered.input.checked };
      Object.keys(fields).forEach(function (k) { out[k] = fields[k].value.trim(); });
      return DQ.api("PUT", "/api/connectors/tally/settings", out).then(function () { DQ.toast("Saved. The next Tally file uses these names."); reload(); });
    })));

    /* ---- import */
    var kind = ui.select([
      { value: "products", label: "Stock items, as products" },
      { value: "sales_history", label: "Day book or sales register, as sales history (forecast only)" },
      { value: "daily_sales", label: "Day book or sales register, as daily sales (moves stock)" }], "products");
    var store = ui.select(storeOptions(stores, { value: "", label: "No default: every line names its godown" }), "");
    var file = h("input", { type: "file", class: "input", accept: ".xml,application/xml,text/xml", required: true });
    var gmap = s.godown_map || {};
    var rows = h("div", { class: "stack" });
    function gRow(name, code) {
      var n = ui.input({ value: name || "", placeholder: "Godown name in Tally", "aria-label": "Godown name in Tally", maxlength: 100 });
      var c = ui.select([{ value: "", label: "Choose a store" }].concat(stores.map(function (st) { return { value: st.code, label: st.name + " (" + st.code + ")" }; })), code || "", { "aria-label": "Store" });
      var r = h("div", { class: "grid-2" }, n, c);
      r._get = function () { return [n.value.trim(), c.value]; };
      rows.appendChild(r);
    }
    Object.keys(gmap).forEach(function (k) { gRow(k, gmap[k]); });
    if (!Object.keys(gmap).length) gRow("", "");
    body.appendChild(card("Bring in from Tally", [
      h("p", { class: "text-2" }, "In Tally, open the Stock Items list or the Day Book and export it as XML (Data Interchange). The file is checked like any spreadsheet: you see what would change before anything does, and Undo is on the import's page."),
      form([
        h("div", { class: "grid-2" }, ui.field("What the file holds", kind), ui.field("Store for lines without a godown", store)),
        ui.field("Tally XML file", file, "Up to 50 MB. UTF-8 or UTF-16, as Tally writes it.")
      ], "Read the file", function () {
        if (!file.files || !file.files[0]) throw new Error("Choose the file exported from Tally.");
        var fd = new FormData();
        fd.append("kind", kind.value);
        fd.append("file", file.files[0]);
        if (store.value) fd.append("store_id", store.value);
        return DQ.upload("/api/connectors/tally/import", fd).then(function (job) { DQ.go("#/imports/" + job.id); });
      }, { busy: "Reading…" }),
      h("details", { class: "card a-details" }, h("summary", null, "Godowns and stores"),
        h("p", { class: "text-2" }, "A godown named like a store's code or name is matched by itself. List the others here."),
        rows,
        h("div", { class: "row" },
          ui.button("Add a godown", function () { gRow("", ""); }, "ghost"),
          ui.button("Save the godowns", function (e) {
            var m = {};
            Array.prototype.forEach.call(rows.children, function (r) { var v = r._get(); if (v[0] && v[1]) m[v[0]] = v[1]; });
            busy(e.currentTarget, "Saving…", DQ.api("PUT", "/api/connectors/tally/settings", { godown_map: m })).then(function () { DQ.toast("Godowns saved."); }, swallow);
          })))
    ]));
    body.appendChild(card("Recent Tally files", runsTable(data.runs, { title: "Nothing sent or read yet", text: "Downloads and files read appear here." })));
  }

  /* ================================================================ Zoho */
  function zoho(body, all, stores, reload) {
    var z = all.zoho, s = z.settings;
    if (z.last_error && z.status !== "not_connected") {
      body.appendChild(notice(z.status === "needs_reconnect" ? "risk" : "attention", h("p", null, z.last_error)));
    }
    if (z.status === "connected") return connected(body, z, s, stores, reload, all.inline);
    var dc = ui.select(z.data_centres, s.data_centre || "in");
    var cid = ui.input({ value: z.client_id || "", required: true, autocomplete: "off", spellcheck: "false", placeholder: "1000.XXXXXXXX" });
    var secret = ui.input({ type: "password", autocomplete: "new-password", required: !z.has_secret, placeholder: z.has_secret ? "Stored. Leave empty to keep it." : "" });
    body.appendChild(card(z.status === "needs_reconnect" ? "Connect Zoho Inventory again" : "Connect Zoho Inventory", [
      h("p", { class: "text-2" }, "DQ-Stack reads your Zoho items and invoices (or sales orders) every night, and when you press Sync now. It only asks Zoho for permission to read, never to change anything."),
      h("ol", { class: "a-howto" },
        h("li", null, "In the Zoho API Console for your data centre (for India, api-console.zoho.in), add a Server-based Application."),
        h("li", null, "Give it your DQ-Stack address as its homepage, and this as its Authorized Redirect URI:",
          copyField("Redirect address", z.redirect_uri)),
        h("li", null, "Copy its Client ID and Client Secret below, and press Connect. Zoho asks you to allow read access, then sends you back here.")),
      form([
        ui.field("Zoho data centre", dc, "Where your Zoho account lives: zoho.in for India."),
        h("div", { class: "grid-2" }, ui.field("Client ID", cid), ui.field("Client Secret", secret, "Stored encrypted. It is never shown again."))
      ], "Connect", function () {
        return DQ.post("/api/connectors/zoho/connect", { client_id: cid.value.trim(), client_secret: secret.value.trim() || null, data_centre: dc.value })
          .then(function (r) { location.href = r.authorize_url; });
      }, { busy: "Opening Zoho…" }),
      selfClient(z, dc, cid, secret, reload)
    ]));
    body.appendChild(card("Permissions asked of Zoho", [
      h("p", { class: "text-2" }, "Read only. DQ-Stack never creates, changes or deletes anything in Zoho."),
      h("ul", { class: "a-howto" }, z.scopes.map(function (sc) { return h("li", null, h("code", null, sc)); }))]));
    if (z.runs.length) body.appendChild(card("Last syncs", runsTable(z.runs)));
  }

  function selfClient(z, dc, cid, secret, reload) {
    var code = ui.input({ autocomplete: "off", spellcheck: "false", placeholder: "1000.xxxxxxxx.xxxxxxxx" });
    return h("details", { class: "card a-details" }, h("summary", null, "Using a Self Client instead"),
      h("p", { class: "text-2" }, "A Self Client has no redirect. In the API Console, open its Generate Code tab, paste these scopes, choose 10 minutes, and paste the code it makes here, with its Client ID and Secret above."),
      copyField("Scopes", z.scopes.join(",")),
      form([ui.field("Code from the Zoho API Console", code)], "Connect with this code", function () {
        if (!code.value.trim()) throw new Error("Paste the code from the Zoho API Console.");
        return DQ.post("/api/connectors/zoho/connect", { client_id: cid.value.trim(), client_secret: secret.value.trim() || null, data_centre: dc.value })
          .then(function () { return DQ.post("/api/connectors/zoho/code", { code: code.value.trim() }); })
          .then(function () { DQ.toast("Zoho Inventory is connected."); reload(); });
      }, { busy: "Connecting…" }));
  }

  function connected(body, z, s, stores, reload, inline) {
    var orgs = s.organizations || [];
    var syncBtn = ui.button("Sync now", function () { sync(false, syncBtn); });
    var fullBtn = ui.button("Sync everything again", function () {
      DQ.confirm({ title: "Read everything from Zoho again?", message: "Every item, and every invoice since the start date, is read again and each day's sales are rebuilt. Use it after changing the warehouse list, or if invoices were deleted in Zoho. At Zoho's limit of 100 requests a minute this can take a while.", confirmLabel: "Sync everything" })
        .then(function (c) { if (c.ok) sync(true, fullBtn); });
    }, "ghost");
    function sync(full, btn) {
      busy(btn, inline ? "Syncing…" : "Asking…", DQ.post("/api/connectors/zoho/sync", { full: full })).then(function (r) {
        if (r.queued) DQ.toast(r.message);
        else DQ.toast(r.run.message || "Sync finished.", { kind: r.run.status === "failed" ? "risk" : r.run.status === "warning" ? "attention" : "ok" });
        reload();
      }, swallow);
    }
    var disc = ui.button("Disconnect", function (e) {
      var b = e.currentTarget;
      DQ.confirm({ title: "Disconnect Zoho Inventory?", message: "DQ-Stack stops reading Zoho and forgets its access. Products and sales already brought in stay. You can connect again at any time.", confirmLabel: "Disconnect", danger: true })
        .then(function (c) { if (c.ok) busy(b, "Disconnecting…", DQ.post("/api/connectors/zoho/disconnect")).then(function () { DQ.toast("Zoho Inventory is disconnected."); reload(); }, swallow); });
    }, "ghost");
    var orgSel = orgs.length > 1 ? ui.select(orgs.map(function (o) { return { value: o.id, label: o.name }; }), s.organization_id) : null;
    body.appendChild(card("Zoho Inventory", [
      h("div", { class: "row" }, ui.badge("Connected", "ok"),
        h("span", null, s.organization_name || s.organization_id || "No organisation chosen"),
        h("span", { class: "text-2" }, z.last_sync_at ? t("Last synced {when}", { when: when(z.last_sync_at) }) : "Not synced yet")),
      z.sync_requested ? notice("data", h("p", null, "A sync is waiting to start. This page shows it when it is done.")) : null,
      orgSel ? h("div", { class: "row" }, ui.field("Organisation", orgSel), ui.button("Use this organisation", function (e) {
        busy(e.currentTarget, "Saving…", DQ.api("PUT", "/api/connectors/zoho/settings", { organization_id: orgSel.value })).then(function () { DQ.toast("Organisation changed. The next sync reads it from the start."); reload(); }, swallow);
      }, "ghost")) : null,
      h("p", { class: "text-2" }, t("Syncs every night at {time}, company time. Only changes since the last sync are read.", { time: s.sync_time || "00:30" })),
      h("div", { class: "row" }, syncBtn, fullBtn, disc)
    ]));

    /* ---- warehouses */
    var wh = s.warehouses || [];
    var wmap = s.warehouse_map || {};
    var selects = {};
    var opts = storeOptions(stores, { value: "", label: "Not counted" });
    var def = ui.select(storeOptions(stores, { value: "", label: "Not counted" }), s.default_store_id || "");
    var table = wh.length ? ui.table([
      { key: "name", label: "Zoho warehouse", render: function (w) { return h("span", null, w.name, w.active ? null : h("span", { class: "text-2" }, " (inactive)")); } },
      { key: "id", label: "DQ-Stack store", render: function (w) {
        selects[w.id] = ui.select(opts, wmap[w.id] || "", { "aria-label": t("Store for {name}", { name: w.name }) });
        return selects[w.id];
      } }
    ], wh, { caption: "Warehouses and stores" }) : notice("attention", h("p", null, "No warehouses were read from Zoho. Press Read the list again; if the organisation has none, choose the store below."));
    body.appendChild(card("Warehouses and stores", h("div", { class: "stack" },
      h("p", { class: "text-2" }, "Each Zoho warehouse's sales go to the store you choose. A warehouse marked Not counted is left out, and the sync says how many lines that was."),
      table,
      ui.field("Store for sales with no warehouse", def),
      h("div", { class: "row" },
        ui.button("Save the stores", function (e) {
          var m = {};
          Object.keys(selects).forEach(function (k) { if (selects[k].value) m[k] = selects[k].value; });
          busy(e.currentTarget, "Saving…", DQ.api("PUT", "/api/connectors/zoho/settings", { warehouse_map: m, default_store_id: def.value }))
            .then(function () { DQ.toast("Saved. The next sync rebuilds each day's sales with these stores."); reload(); }, swallow);
        }),
        ui.button("Read the list again", function (e) {
          busy(e.currentTarget, "Reading…", DQ.post("/api/connectors/zoho/warehouses")).then(function () { reload(); }, swallow);
        }, "ghost"))
    )));

    /* ---- what to bring in */
    var source = ui.select([{ value: "invoices", label: "Invoices" }, { value: "salesorders", label: "Sales orders" }], s.sales_source);
    var into = ui.select([{ value: "daily_sales", label: "Daily sales: also moves stock (stores without tills)" }, { value: "sales_history", label: "Sales history: for the forecast only" }], s.sales_into);
    var start = ui.input({ type: "date", value: s.start_date || "" });
    var time = ui.input({ type: "time", value: s.sync_time || "00:30", required: true });
    body.appendChild(card("What to bring in", form([
      h("div", { class: "grid-2" },
        ui.field("Sales come from", source, "Drafts and void documents never count."),
        ui.field("Sales go in as", into, "Daily sales are refused for a store whose sales come from its tills: the tills are the record."),
        ui.field("Read sales from", start, "Empty means the last 90 days, on the first sync."),
        ui.field("Nightly sync at", time, "Company time. The forecast runs after it."))
    ], "Save", function () {
      return DQ.api("PUT", "/api/connectors/zoho/settings", { sales_source: source.value, sales_into: into.value, start_date: start.value, sync_time: time.value })
        .then(function () { DQ.toast("Saved."); reload(); });
    })));
    body.appendChild(card("Last syncs", runsTable(z.runs, { title: "No syncs yet", text: "Press Sync now, or wait for tonight's." })));
  }
})();
