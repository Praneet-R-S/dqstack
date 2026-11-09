/* The frame every screen sits in, the router, and the sign-in screens.

   Same frame everywhere (section 12 rule 1): left menu, a top bar with the store
   switcher, search, save status, the alerts bell and the account menu. The store being
   viewed is always named. Screens register with DQ.route() in screens-*.js; this file
   loads last and starts the app. */
(function () {
  "use strict";
  var DQ = window.DQ, h = DQ.h;

  var GROUPS = [
    { label: null, items: ["#/home", "#/setup", "#/today"] },
    { label: "Stock", items: ["#/stock", "#/count", "#/receive", "#/transfers", "#/wastage"] },
    { label: "Buying", items: ["#/orders", "#/suppliers"] },
    { label: "Selling", items: ["#/checkout", "#/bills", "#/day-close", "#/sales"] },
    { label: "Insight", items: ["#/reports", "#/model", "/planner/"] },
    { label: "Company", items: ["#/imports", "#/alerts", "#/settings", "#/activity"] }
  ];
  var PUBLIC = ["#/signin", "#/signup", "#/forgot", "#/reset/:token", "#/invite/:token"];

  /* ---------------------------------------------------------------- matching */
  function match(hash) {
    var path = hash.split("?")[0];
    var query = {};
    (hash.split("?")[1] || "").split("&").forEach(function (kv) {
      if (!kv) return;
      var p = kv.split("=");
      query[decodeURIComponent(p[0])] = decodeURIComponent((p[1] || "").replace(/\+/g, " "));
    });
    for (var i = 0; i < DQ.routes.length; i++) {
      var r = DQ.routes[i], a = r.pattern.split("/"), b = path.split("/");
      if (a.length !== b.length) continue;
      var params = {}, ok = true;
      for (var j = 0; j < a.length; j++) {
        if (a[j].charAt(0) === ":") params[a[j].slice(1)] = decodeURIComponent(b[j]);
        else if (a[j] !== b[j]) { ok = false; break; }
      }
      if (ok) return { route: r, params: params, query: query };
    }
    return null;
  }
  function routeFor(pattern) { return DQ.routes.filter(function (r) { return r.pattern === pattern; })[0]; }
  DQ.go = function (hash) { if (location.hash === hash) render(); else location.hash = hash; };

  /* ---------------------------------------------------------------- frame */
  function navItem(pattern, current) {
    if (pattern === "/planner/") {
      return h("a", { class: "nav-item", href: "/planner/", title: "The forecasting workspace for analysts" }, h("span", { class: "nav-dot", "aria-hidden": "true" }), "Planner workspace");
    }
    var r = routeFor(pattern);
    if (!r || !r.nav) return null;
    if (r.can && !DQ.can(r.can)) return null;
    if (r.show && !r.show()) return null;
    var active = current === pattern;
    return h("a", { class: "nav-item", href: pattern, "aria-current": active ? "page" : null },
      h("span", { class: "nav-dot", "aria-hidden": "true" }), r.nav,
      r.navBadge ? h("span", { class: "nav-badge", "data-nav-badge": pattern }) : null);
  }
  function frame(current) {
    /* A sub-page (#/settings/stores, #/orders/:id) lights the menu item it sits under:
       the longest menu route that is the page's route or a prefix of it. */
    var active = null, cur = current && current.pattern;
    GROUPS.forEach(function (g) {
      g.items.forEach(function (p) {
        if (cur && (cur === p || cur.indexOf(p + "/") === 0) && (!active || p.length > active.length)) active = p;
      });
    });
    var nav = h("nav", { class: "sidebar", "aria-label": "Main" },
      h("a", { class: "brand", href: "#/home" }, h("span", { class: "brand-mark", "aria-hidden": "true" }), "DQ-Stack"),
      GROUPS.map(function (g) {
        var items = g.items.map(function (p) { return navItem(p, active); }).filter(Boolean);
        if (!items.length) return null;
        return h("div", { class: "nav-group" }, g.label ? h("p", { class: "nav-label" }, g.label) : null, items);
      }));
    var store = DQ.storeObj();
    var storePick = DQ.stores().length > 1 ? DQ.ui.storePicker(function (id) { DQ.setStore(id); render(); }) :
      h("span", { class: "store-name" }, store ? store.name : "No store yet");
    if (storePick.tagName === "SELECT") storePick.classList.add("store-switch");
    var search = h("input", { class: "input search", type: "search", placeholder: "Search name, SKU or barcode", "aria-label": "Search products" });
    search.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && search.value.trim()) DQ.go("#/stock?q=" + encodeURIComponent(search.value.trim()));
    });
    var bell = h("a", { class: "bell", href: "#/alerts", "aria-label": "Alerts" }, h("span", { class: "bell-icon", "aria-hidden": "true" }, "●"), h("span", { class: "bell-count", id: "bell-count", hidden: true }));
    var me = DQ.me.person;
    var menuBtn = h("button", { class: "account", type: "button", "aria-haspopup": "true", "aria-expanded": "false" }, initials(me.name));
    var menu = h("div", { class: "account-menu", hidden: true },
      h("p", { class: "account-who" }, h("strong", null, me.name), h("br"), roleName(me.role) + " · " + DQ.me.company.name),
      h("a", { href: "#/alerts/settings" }, "My alert settings"),
      h("a", { href: "/privacy.html" }, "Privacy"),
      h("button", { type: "button", onclick: signOut }, "Sign out"));
    menuBtn.addEventListener("click", function () {
      var open = menu.hidden; menu.hidden = !open; menuBtn.setAttribute("aria-expanded", String(open));
    });
    var top = h("header", { class: "topbar" },
      h("button", { class: "menu-toggle", type: "button", "aria-label": "Menu", onclick: function () { document.body.classList.toggle("nav-open"); } }, "☰"),
      h("div", { class: "top-store" }, h("span", { class: "sr-only" }, "Store: "), storePick),
      search,
      h("span", { id: "save-status", class: "save-status", "aria-live": "polite" }),
      bell, h("div", { class: "account-wrap" }, menuBtn, menu));
    return { nav: nav, top: top };
  }
  function initials(name) { return name.split(/\s+/).map(function (w) { return w[0]; }).slice(0, 2).join("").toUpperCase(); }
  function roleName(r) { return { owner: "Owner", admin: "Admin", store_manager: "Store manager", purchaser: "Purchaser", stock_clerk: "Stock clerk", cashier: "Cashier", viewer: "Viewer" }[r] || r; }
  DQ.roleName = roleName;

  function refreshBell() {
    if (!DQ.me) return;
    DQ.get("/api/alerts/count", { allow401: true }).then(function (c) {
      var el = document.getElementById("bell-count");
      if (!el) return;
      el.hidden = !c.open;
      el.textContent = c.open > 99 ? "99+" : String(c.open);
      el.setAttribute("data-kind", c.risk ? "risk" : "attention");
    }, function () { /* the bell is best effort */ });
  }

  /* ---------------------------------------------------------------- render */
  var app = document.getElementById("app");
  var token = 0;
  function render() {
    var hash = location.hash || "#/home";
    var m = match(hash);
    var isPublic = m && PUBLIC.indexOf(m.route.pattern) >= 0;
    document.body.classList.remove("nav-open");
    if (!DQ.me && !isPublic) { DQ.go("#/signin"); return; }
    if (!m) { m = match("#/not-found"); }
    if (m.route.can && !DQ.can(m.route.can)) { m = match("#/no-access"); }
    var my = ++token;
    DQ.clear(app);
    var main = h("main", { id: "main", class: "main", tabindex: "-1" });
    if (isPublic) {
      app.className = "auth-layout";
      app.appendChild(main);
    } else {
      app.className = "app-layout";
      var f = frame(m.route);
      app.appendChild(f.nav);
      app.appendChild(h("div", { class: "content" }, f.top, main));
      DQ.paintSave();
      refreshBell();
    }
    document.title = (m.route.title || "DQ-Stack") + " · DQ-Stack";
    try {
      var out = m.route.render(main, m.params, m.query, function () { return my === token; });
      if (out && out.catch) out.catch(function (err) { if (my === token) { DQ.clear(main); main.appendChild(DQ.ui.error(err, render)); } });
    } catch (err) {
      DQ.clear(main); main.appendChild(DQ.ui.error(err, render));
      if (window.console) console.error(err);
    }
    if (!isPublic) main.focus({ preventScroll: true });
  }
  DQ.render = render;
  DQ.signedOut = function (message) {
    var was = !!DQ.me;
    DQ.me = null;
    if (was) DQ.toast(message || "Please sign in again.", { kind: "attention" });
    if (location.hash.indexOf("#/signin") !== 0) location.hash = "#/signin";
  };
  function signOut() {
    DQ.post("/api/auth/logout").then(function () { DQ.me = null; location.hash = "#/signin"; }, function () { DQ.me = null; location.hash = "#/signin"; });
  }

  /* ---------------------------------------------------------------- fallback screens */
  DQ.route("#/not-found", { title: "Not found", render: function (main) {
    main.appendChild(DQ.ui.empty({ title: "That page does not exist", text: "The address may be mistyped, or the page has moved.", action: { label: "Go to Home", href: "#/home" } }));
  } });
  DQ.route("#/no-access", { title: "Not available", render: function (main) {
    main.appendChild(DQ.ui.empty({ title: "Your role does not include this screen", text: "Ask an Owner or Admin if you need it.", action: { label: "Go to Home", href: "#/home" } }));
  } });

  /* ---------------------------------------------------------------- sign in and friends */
  function authCard(title, sub, body) {
    return h("section", { class: "auth-card" },
      h("a", { class: "brand", href: "/" }, h("span", { class: "brand-mark", "aria-hidden": "true" }), "DQ-Stack"),
      h("h1", { class: "title-1" }, title), sub ? h("p", { class: "text-2" }, sub) : null, body);
  }
  function form(fields, submitLabel, onSubmit, extra) {
    var err = h("div", { class: "form-error", role: "alert", hidden: true });
    var btn = h("button", { class: "btn btn-primary btn-block", type: "submit" }, submitLabel);
    var f = h("form", { class: "stack", novalidate: true }, fields, err, btn, extra || null);
    f.addEventListener("submit", function (e) {
      e.preventDefault();
      var bad = Array.prototype.filter.call(f.querySelectorAll("[required]"), function (i) { return !i.value.trim(); });
      if (bad.length) { err.textContent = "Fill in every box."; err.hidden = false; bad[0].focus(); return; }
      btn.disabled = true; btn.textContent = "Please wait…"; err.hidden = true;
      onSubmit().then(function () { btn.disabled = false; btn.textContent = submitLabel; }, function (e2) {
        btn.disabled = false; btn.textContent = submitLabel;
        err.textContent = e2.message + (e2.problems && e2.problems.length ? " " + e2.problems.join("; ") : "");
        err.hidden = false;
      });
    });
    return f;
  }
  function afterSignIn(me) { DQ.me = me; location.hash = "#/home"; }

  DQ.route("#/signin", { title: "Sign in", render: function (main) {
    var login = DQ.ui.input({ type: "text", autocomplete: "username", required: true, inputmode: "email" });
    var pw = DQ.ui.input({ type: "password", autocomplete: "current-password", required: true });
    main.appendChild(authCard("Sign in", null, form([
      DQ.ui.field("Email or phone", login), DQ.ui.field("Password", pw)
    ], "Sign in", function () {
      return DQ.post("/api/auth/login", { login: login.value.trim(), password: pw.value }, { quiet: true, allow401: true }).then(afterSignIn);
    }, h("p", { class: "auth-links" }, h("a", { href: "#/forgot" }, "Forgot your password?"), " · ", h("a", { href: "#/signup" }, "Start a new company")))));
    login.focus();
  } });

  DQ.route("#/signup", { title: "Start a company", render: function (main) {
    var co = DQ.ui.input({ required: true, autocomplete: "organization" });
    var name = DQ.ui.input({ required: true, autocomplete: "name" });
    var email = DQ.ui.input({ type: "email", required: true, autocomplete: "email" });
    var pw = DQ.ui.input({ type: "password", required: true, autocomplete: "new-password", minlength: 10 });
    main.appendChild(authCard("Start your company", "You become its Owner. The setup checklist then guides you to your first forecast.", form([
      DQ.ui.field("Company name", co), DQ.ui.field("Your name", name), DQ.ui.field("Email", email),
      DQ.ui.field("Password", pw, "At least 10 characters.")
    ], "Create the company", function () {
      return DQ.post("/api/auth/signup", { company_name: co.value.trim(), name: name.value.trim(), email: email.value.trim(), password: pw.value }, { quiet: true }).then(function (me) { DQ.me = me; location.hash = "#/setup"; });
    }, h("p", { class: "auth-links" }, "By creating a company you accept the ", h("a", { href: "/terms.html" }, "terms"), " and ", h("a", { href: "/privacy.html" }, "privacy notice"), ". ", h("a", { href: "#/signin" }, "Sign in instead")))));
    co.focus();
  } });

  DQ.route("#/forgot", { title: "Forgot password", render: function (main) {
    var login = DQ.ui.input({ required: true, autocomplete: "username" });
    var done = h("p", { class: "notice", hidden: true });
    main.appendChild(authCard("Forgot your password?", "We email a link to choose a new one. An Admin can also send you one.", form([DQ.ui.field("Email or phone", login), done], "Send the link", function () {
      return DQ.post("/api/auth/password/forgot", { login: login.value.trim() }, { quiet: true }).then(function (r) { done.textContent = r.message; done.hidden = false; });
    }, h("p", { class: "auth-links" }, h("a", { href: "#/signin" }, "Back to sign in")))));
  } });

  DQ.route("#/reset/:token", { title: "Choose a new password", render: function (main, p) {
    var pw = DQ.ui.input({ type: "password", required: true, autocomplete: "new-password", minlength: 10 });
    main.appendChild(authCard("Choose a new password", null, form([DQ.ui.field("New password", pw, "At least 10 characters.")], "Save the password", function () {
      return DQ.post("/api/auth/password/reset", { token: p.token, password: pw.value }, { quiet: true }).then(function (r) { DQ.toast(r.message); location.hash = "#/signin"; });
    })));
  } });

  DQ.route("#/invite/:token", { title: "Join your team", render: function (main, p) {
    main.appendChild(DQ.ui.skeleton(3));
    return DQ.get("/api/auth/invitation/" + encodeURIComponent(p.token), { allow401: true }).then(function (inv) {
      DQ.clear(main);
      var pw = DQ.ui.input({ type: "password", required: true, autocomplete: "new-password", minlength: 10 });
      main.appendChild(authCard("Join " + inv.company, "Hello " + inv.name + ". You are invited as " + roleName(inv.role) + ". Choose a password to start.", form([DQ.ui.field("Password", pw, "At least 10 characters.")], "Join", function () {
        return DQ.post("/api/auth/invitation/accept", { token: p.token, password: pw.value }, { quiet: true }).then(afterSignIn);
      })));
    }, function (err) {
      DQ.clear(main);
      main.appendChild(authCard("This invitation cannot be used", err.message, h("a", { class: "btn btn-primary", href: "#/signin" }, "Go to sign in")));
    });
  } });

  /* ---------------------------------------------------------------- start */
  window.addEventListener("hashchange", render);
  document.addEventListener("click", function (e) {
    var menu = document.querySelector(".account-menu");
    if (menu && !menu.hidden && !e.target.closest(".account-wrap")) menu.hidden = true;
  });
  setInterval(refreshBell, 60000);
  DQ.get("/api/auth/me", { allow401: true }).then(function (me) { DQ.me = me; render(); DQ.queue.flush(); },
    function () { DQ.me = null; render(); });
})();
