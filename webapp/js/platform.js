/* The vendor's platform portal: companies (metadata only), system health, making a
   company for a sale, pausing one, support sessions while a client grants them, and
   deleting a departing client after its confirmed period. */
(function () {
  "use strict";
  var DQ = window.DQ, h = DQ.h;
  var main = document.getElementById("main");
  var me = null;

  function api(method, path, body) {
    DQ.me = { csrf: me && me.csrf };
    return DQ.api(method, path, body, { allow401: true });
  }
  DQ.signedOut = function () { me = null; signIn(); };

  function signIn() {
    DQ.clear(main);
    var login = DQ.ui.input({ autocomplete: "username", required: true });
    var pw = DQ.ui.input({ type: "password", autocomplete: "current-password", required: true });
    var err = h("p", { class: "form-error", hidden: true });
    var form = h("form", { class: "stack auth-card", style: { margin: "64px auto" } },
      h("h1", { class: "title-1" }, "DQ-Stack platform"),
      h("p", { class: "text-2" }, "For the DQ-Stack team. Company staff sign in at ", h("a", { href: "/app/" }, "/app"), "."),
      DQ.ui.field("Email", login), DQ.ui.field("Password", pw), err,
      h("button", { class: "btn btn-primary btn-block", type: "submit" }, "Sign in"));
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      api("POST", "/api/platform/login", { login: login.value.trim(), password: pw.value }).then(function (r) { me = r; home(); },
        function (e2) { err.textContent = e2.message; err.hidden = false; });
    });
    main.appendChild(form);
    login.focus();
  }

  function home() {
    DQ.clear(main);
    main.appendChild(DQ.ui.page("Platform", { sub: "Signed in as " + me.name + ". You see each company's size and health, never its records, unless its Owner grants support access.", actions: [
      DQ.ui.button("New company", newCompany), DQ.ui.button("Sign out", function () { api("POST", "/api/auth/logout").then(signIn, signIn); }, "ghost")] }));
    var healthBox = h("section", { class: "card", "aria-labelledby": "h-health" }, h("h2", { id: "h-health", class: "title-2" }, "System health"), DQ.ui.skeleton(2));
    var list = h("section", { "aria-labelledby": "h-co", style: { marginTop: "24px" } }, h("h2", { id: "h-co", class: "title-2" }, "Companies"), DQ.ui.skeleton(5));
    main.appendChild(healthBox);
    main.appendChild(list);
    api("GET", "/api/platform/health").then(function (hl) {
      DQ.clear(healthBox).appendChild(h("h2", { id: "h-health", class: "title-2" }, "System health"));
      healthBox.appendChild(h("div", { class: "tiles", style: { marginTop: "12px" } },
        tile("Database", hl.database === "ok" ? "Working" : "Down", hl.database === "ok" ? "ok" : "risk"),
        tile("Scheduler", hl.worker_ok ? "Running" : (hl.worker_last_pass_minutes === null ? "Never seen" : "Last pass " + hl.worker_last_pass_minutes + " min ago"), hl.worker_ok ? "ok" : "risk"),
        tile("Forecasts queued or running", String(hl.queued_runs), "neutral"),
        tile("Failed runs, last 24 h", String(hl.failed_runs_24h.length), hl.failed_runs_24h.length ? "risk" : "ok"),
        tile("Version", hl.version, "neutral")));
      if (hl.failed_runs_24h.length) healthBox.appendChild(DQ.ui.table([
        { key: "company", label: "Company" }, { key: "kind", label: "Run" }, { key: "message", label: "What happened" },
        { key: "at", label: "When", render: function (r) { return DQ.fmt.dateTime(r.at); } }], hl.failed_runs_24h));
    }, function (e) { DQ.clear(healthBox).appendChild(DQ.ui.error(e, home)); });
    api("GET", "/api/platform/companies").then(function (rows) {
      DQ.clear(list).appendChild(h("h2", { id: "h-co", class: "title-2" }, "Companies"));
      list.appendChild(DQ.ui.table([
        { key: "name", label: "Company", render: function (r) { return h("span", null, r.name, r.is_demo ? " " : null, r.is_demo ? DQ.ui.badge("demo", "data") : null); } },
        { key: "status", label: "Status", render: function (r) { return DQ.ui.badge(r.status === "closing" ? "Leaving on " + DQ.fmt.date(r.deletion_due) : r.status, r.status === "active" ? "ok" : "attention"); } },
        { key: "plan", label: "Plan" },
        { key: "stores", label: "Stores", kind: "num" }, { key: "products", label: "Products", kind: "num" },
        { key: "sales_days", label: "Days of sales", kind: "num" },
        { key: "last_forecast", label: "Last forecast", render: function (r) { return r.last_forecast ? r.last_forecast.status + ", " + DQ.fmt.ago(r.last_forecast.at) : "none yet"; } },
        { key: "support", label: "Support", render: function (r) {
          if (!r.support_granted_until) return h("span", { class: "text-3" }, "Not granted");
          return DQ.ui.button("Open support session", function () { support(r); }, "ghost", { class: "btn btn-ghost btn-sm" });
        } },
        { key: "act", label: "", render: function (r) { return actions(r); } }
      ], rows, { empty: { title: "No companies yet" } }));
    }, function (e) { DQ.clear(list).appendChild(DQ.ui.error(e, home)); });
  }
  function tile(label, value, kind) {
    return h("div", { class: "tile" }, h("div", { class: "tile-label" }, label), h("div", { class: "tile-value", style: { fontSize: "20px" } }, DQ.ui.badge(value, kind)));
  }
  function actions(r) {
    var wrap = h("div", { class: "row" });
    if (r.status === "active") wrap.appendChild(DQ.ui.button("Pause", function () { setStatus(r, "suspended"); }, "ghost", { class: "btn btn-ghost btn-sm" }));
    if (r.status === "suspended") wrap.appendChild(DQ.ui.button("Resume", function () { setStatus(r, "active"); }, "ghost", { class: "btn btn-ghost btn-sm" }));
    if (r.status === "closing" && r.deletion_due && new Date(r.deletion_due) <= new Date()) {
      wrap.appendChild(DQ.ui.button("Delete now", function () { purge(r); }, "danger", { class: "btn btn-danger btn-sm" }));
    }
    return wrap;
  }
  function setStatus(r, status) {
    DQ.confirm({ title: (status === "suspended" ? "Pause " : "Resume ") + r.name + "?", message: status === "suspended" ? "Its people will not be able to sign in until you resume it." : null, confirmLabel: status === "suspended" ? "Pause" : "Resume", danger: status === "suspended" }).then(function (c) {
      if (!c.ok) return;
      api("PATCH", "/api/platform/companies/" + r.id, { status: status }).then(function () { DQ.toast("Done."); home(); }, function (e) { DQ.toast(e.message, { kind: "risk" }); });
    });
  }
  function purge(r) {
    DQ.confirm({ title: "Delete " + r.name + " for good?", message: "Every record goes, including the stock ledger and the activity log. This cannot be undone.", confirmLabel: "Delete everything", danger: true }).then(function (c) {
      if (!c.ok) return;
      api("POST", "/api/platform/companies/" + r.id + "/purge").then(function () { DQ.toast("Deleted."); home(); }, function (e) { DQ.toast(e.message, { kind: "risk" }); });
    });
  }
  function support(r) {
    DQ.confirm({ title: "Open a support session for " + r.name + "?", message: "Granted until " + DQ.fmt.dateTime(r.support_granted_until) + " for: " + r.support_reason + ". You will see the company read-only, and it is logged in their activity log. Sign out to end it.", confirmLabel: "Open" }).then(function (c) {
      if (!c.ok) return;
      api("POST", "/api/platform/companies/" + r.id + "/support").then(function (res) { window.location.href = res.go_to; }, function (e) { DQ.toast(e.message, { kind: "risk" }); });
    });
  }
  function newCompany() {
    var name = DQ.ui.input({ required: true }), owner = DQ.ui.input({ required: true }), email = DQ.ui.input({ type: "email", required: true });
    var plan = DQ.ui.select([{ value: "trial", label: "Trial" }, { value: "standard", label: "Standard" }], "trial");
    var err = h("p", { class: "form-error", hidden: true });
    var dlg = h("dialog", { class: "dialog", "aria-labelledby": "nc" }, h("h2", { id: "nc", class: "title-2" }, "New company"),
      DQ.ui.field("Company name", name), DQ.ui.field("Owner's name", owner), DQ.ui.field("Owner's email", email, "They get a link to choose their own password."), DQ.ui.field("Plan", plan), err,
      h("div", { class: "dialog-actions" }, DQ.ui.button("Cancel", function () { dlg.close(); dlg.remove(); }, "ghost"), DQ.ui.button("Create", function () {
        api("POST", "/api/platform/companies", { name: name.value.trim(), owner_name: owner.value.trim(), owner_email: email.value.trim(), plan: plan.value }).then(function (r) {
          dlg.close(); dlg.remove();
          DQ.toast(r.delivered_by === "email" ? "Created. The Owner's invitation was emailed." : "Created. Send the Owner this link: " + r.invite_link);
          home();
        }, function (e) { err.textContent = e.message; err.hidden = false; });
      })));
    document.body.appendChild(dlg);
    dlg.showModal();
    name.focus();
  }

  api("GET", "/api/platform/me").then(function (r) { me = r; home(); }, signIn);
})();
