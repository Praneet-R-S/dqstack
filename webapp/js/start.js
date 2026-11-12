/* The frame every screen sits in, the router, and the sign-in screens.

   Same frame everywhere (section 12 rule 1): left menu, a top bar with the store
   switcher, search, save status, the alerts bell and the account menu. The store being
   viewed is always named. Screens register with DQ.route() in screens-*.js; this file
   loads last and starts the app. */
(function () {
  "use strict";
  var DQ = window.DQ, h = DQ.h, t = DQ.t, tx = DQ.tx;

  var GROUPS = [
    { label: null, items: ["#/home", "#/setup", "#/today"] },
    { label: "Stock", items: ["#/stock", "#/count", "#/receive", "#/transfers", "#/wastage"] },
    { label: "Buying", items: ["#/orders", "#/suppliers"] },
    { label: "Selling", items: ["#/checkout", "#/bills", "#/day-close", "#/sales"] },
    { label: "Insight", items: ["#/forecasts", "#/reports", "#/model"] },
    { label: "Company", items: ["#/imports", "#/connectors", "#/alerts", "#/settings", "#/activity"] }
  ];
  var PUBLIC = ["#/signin", "#/signup", "#/forgot", "#/reset/:token", "#/invite/:token"];
  /* A cashier's app is the till (section 2: "bills customers on the Checkout screen"):
     the menu holds only what a till operator uses, and Home opens Checkout. */
  var TILL_GROUPS = [{ label: null, items: ["#/checkout", "#/shift", "#/bills", "#/alerts"] }];
  var TILL_ROUTES = ["#/checkout", "#/shift", "#/bills", "#/bills/:id", "#/alerts", "#/alerts/settings",
    "#/not-found", "#/no-access"];
  function tillOnly() { return !!(DQ.me && DQ.me.person && DQ.me.person.role === "cashier"); }
  DQ.tillOnly = tillOnly;
  function groups() { return tillOnly() ? TILL_GROUPS : GROUPS; }

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
    groups().forEach(function (g) {
      g.items.forEach(function (p) {
        if (cur && (cur === p || cur.indexOf(p + "/") === 0) && (!active || p.length > active.length)) active = p;
      });
    });
    var nav = h("nav", { class: "sidebar", "aria-label": "Main" },
      h("a", { class: "brand", href: "#/home" }, h("span", { class: "brand-mark", "aria-hidden": "true" }), "DQ-Stack"),
      groups().map(function (g) {
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
      h("p", { class: "account-who" }, h("strong", null, me.name), h("br"), tx(roleName(me.role)) + " · " + DQ.me.company.name),
      langSwitch({ compact: true }),
      h("a", { href: "#/alerts/settings" }, "My alert settings"),
      h("a", { href: "/privacy.html" }, "Privacy"),
      h("button", { type: "button", onclick: signOut }, "Sign out"));
    menuBtn.addEventListener("click", function () {
      var open = menu.hidden; menu.hidden = !open; menuBtn.setAttribute("aria-expanded", String(open));
    });
    var top = h("header", { class: "topbar" },
      h("button", { class: "menu-toggle", type: "button", "aria-label": "Menu", onclick: function () { document.body.classList.toggle("nav-open"); } }, "☰"),
      h("div", { class: "top-store" }, h("span", { class: "sr-only" }, "Store: "), storePick),
      tillOnly() ? null : search,
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
    if (!isPublic && tillOnly() && TILL_ROUTES.indexOf(m.route.pattern) < 0) {
      if (m.route.pattern === "#/home") { DQ.go("#/checkout"); return; }
      m = match("#/no-access");
    }
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
    document.title = tx(m.route.title || "DQ-Stack") + " · DQ-Stack";
    var skip = document.querySelector(".skip-link");
    if (skip) skip.textContent = t("Skip to the main content");
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
  /* The sign-in screen says why the person is there when a session ended under them
     (it expired after DQ_SESSION_HOURS, or they were signed out elsewhere). */
  var signedOutNote = null;
  DQ.signedOut = function (message) {
    var was = !!DQ.me;
    DQ.me = null;
    if (was) signedOutNote = "Your session has ended, so you were signed out. Sign in again to carry on.";
    if (location.hash.indexOf("#/signin") !== 0) location.hash = "#/signin";
    else if (was) render();
  };
  function signOut() {
    DQ.post("/api/auth/logout").then(function () { DQ.me = null; location.hash = "#/signin"; }, function () { DQ.me = null; location.hash = "#/signin"; });
  }

  /* ---------------------------------------------------------------- language */
  /* The language is the person's own choice: picked on the sign-in page (kept on this
     device, and saved to the account by the sign-in that follows within 30 minutes) or
     in the account menu and My alert settings (saved to the account at once). Signing in
     switches to the language saved on the account. Language names are written in their
     own script and never translated. */
  var PENDING = "dq.lang.pending";
  function saveLanguage(code) {
    if (!DQ.me || !DQ.me.person) {
      try { localStorage.setItem(PENDING, String(Date.now())); } catch (e) { /* private window */ }
      return Promise.resolve();
    }
    DQ.me.person.language = code;
    return DQ.api("PUT", "/api/auth/language", { language: code }, { quiet: true }).then(null, function (e) {
      DQ.toast(t("Your language is used on this device, but it was not saved to your account: {why}", { why: e.message }), { kind: "attention" });
    });
  }
  function adoptLanguage(me) {
    var mine = me && me.person && me.person.language, pending = 0;
    try { pending = +localStorage.getItem(PENDING) || 0; localStorage.removeItem(PENDING); } catch (e) { /* ignore */ }
    if (!mine) return;
    if (pending && Date.now() - pending < 30 * 60000) {
      if (mine !== DQ.i18n.lang()) saveLanguage(DQ.i18n.lang());
    } else if (mine !== DQ.i18n.lang() && DQ.i18n.supported(mine)) DQ.i18n.set(mine);
  }
  function langSwitch(opts) {
    opts = opts || {};
    var cur = DQ.i18n.lang();
    return h("div", { class: "lang-switch" + (opts.compact ? " lang-compact" : ""), role: "group", "aria-label": "Language" },
      opts.compact ? null : h("span", { class: "lang-label", "aria-hidden": "true" }, "Language"),
      DQ.i18n.langs().map(function (l) {
        return h("button", { type: "button", class: "lang-btn", lang: l.code, "aria-pressed": l.code === cur ? "true" : "false",
          onclick: function () {
            if (l.code === DQ.i18n.lang()) return;
            DQ.i18n.set(l.code);
            saveLanguage(l.code);
            if (opts.onChange) opts.onChange(l.code); else render();
          } }, document.createTextNode(l.name));
      }));
  }
  DQ.langSwitch = langSwitch;

  /* ---------------------------------------------------------------- fallback screens */
  DQ.route("#/not-found", { title: "Not found", render: function (main) {
    main.appendChild(DQ.ui.empty({ title: "That page does not exist", text: "The address may be mistyped, or the page has moved.", action: { label: "Go to Home", href: "#/home" } }));
  } });
  DQ.route("#/no-access", { title: "Not available", render: function (main) {
    main.appendChild(DQ.ui.empty({ title: "Your role does not include this screen", text: "Ask an Owner or Admin if you need it.", action: tillOnly() ? { label: "Go to Checkout", href: "#/checkout" } : { label: "Go to Home", href: "#/home" } }));
  } });

  /* ---------------------------------------------------------------- sign in and friends */
  function authCard(title, sub, body) {
    return h("section", { class: "auth-card" },
      h("div", { class: "auth-top" },
        h("a", { class: "brand", href: "/" }, h("span", { class: "brand-mark", "aria-hidden": "true" }), "DQ-Stack"),
        langSwitch({ compact: true })),
      h("h1", { class: "title-1" }, title), sub ? h("p", { class: "text-2" }, sub) : null, body);
  }
  function form(fields, submitLabel, onSubmit, extra) {
    var err = h("div", { class: "form-error", role: "alert", hidden: true });
    var btn = h("button", { class: "btn btn-primary btn-block", type: "submit" }, submitLabel);
    var f = h("form", { class: "stack", novalidate: true }, fields, err, btn, extra || null);
    f.addEventListener("submit", function (e) {
      e.preventDefault();
      var bad = Array.prototype.filter.call(f.querySelectorAll("[required]"), function (i) { return !i.value.trim(); });
      if (bad.length) { err.textContent = t("Fill in every box."); err.hidden = false; bad[0].focus(); return; }
      btn.disabled = true; btn.textContent = t("Please wait…"); err.hidden = true;
      onSubmit().then(function () { btn.disabled = false; btn.textContent = tx(submitLabel); }, function (e2) {
        btn.disabled = false; btn.textContent = tx(submitLabel);
        err.textContent = e2.message + (e2.problems && e2.problems.length ? " " + e2.problems.join("; ") : "");
        err.hidden = false;
      });
    });
    return f;
  }
  function afterSignIn(me) { DQ.me = me; adoptLanguage(me); location.hash = "#/home"; }

  /* What the server offers besides a password: Google, Microsoft, reset by text.
     Asked once per page load; a failure just means no extra buttons. */
  var providersP = null;
  function providers() {
    if (!providersP) providersP = DQ.get("/api/auth/providers", { allow401: true }).then(null, function () { providersP = null; return {}; });
    return providersP;
  }
  /* The callback sends people back to #/signin?sso=<reason> when it cannot sign them in. */
  var SSO_MESSAGES = {
    expired: "That sign-in took too long. Please try again.",
    failed: "We could not confirm that sign-in. Try again, or use your password.",
    cancelled: "Sign-in was cancelled.",
    unverified: "Your Google email is not verified yet. Verify it with Google, or use your password.",
    "no-email": "That account did not share an email address. Use your password instead.",
    "no-account": "No DQ-Stack account uses that email. Ask your administrator to invite you with it, or use your password.",
    disabled: "This account has been disabled. Ask your administrator.",
    locked: "This account is locked after too many wrong passwords. Try again later, or ask an administrator to unlock it.",
    "not-allowed": "Your company does not allow Google or Microsoft sign-in. Use your password.",
    "ms-tenant": "That Microsoft account is not from your company's Microsoft organisation. Use your work account, or your password. An administrator sets the organisation in Settings, Company.",
    paused: "This company's account is paused. Contact DQ-Stack support.",
    off: "That sign-in option is not set up here."
  };
  function codeInput() {
    return DQ.ui.input({ type: "text", class: "input code-input", inputmode: "numeric", autocomplete: "one-time-code", maxlength: 8, required: true, spellcheck: "false" });
  }
  function startAgain(label) {
    return h("a", { href: "#/signin", onclick: function (e) { e.preventDefault(); DQ.go("#/signin"); } }, label || "Start again");
  }

  /* Step two of signing in, when the company asks for a code after the password. */
  function codeStep(main, r) {
    DQ.clear(main);
    var ticket = r.ticket;
    var code = codeInput();
    var sent = h("p", { class: "notice", role: "status" }, r.message);
    var resend = h("button", { class: "link-btn", type: "button" }, "Send a new code");
    resend.addEventListener("click", function () {
      resend.disabled = true;
      DQ.post("/api/auth/login/code/resend", { ticket: ticket }, { quiet: true, allow401: true }).then(function (x) {
        sent.textContent = tx(x.message); code.value = ""; code.focus();
        setTimeout(function () { resend.disabled = false; }, 30000);
      }, function (e) { sent.textContent = e.message; resend.disabled = false; });
    });
    main.appendChild(authCard("Enter your code", null, form([sent, DQ.ui.field("6-digit code", code)], "Sign in", function () {
      return DQ.post("/api/auth/login/code", { ticket: ticket, code: code.value.trim() }, { quiet: true, allow401: true }).then(afterSignIn);
    }, h("p", { class: "auth-links" }, resend, " · ", startAgain()))));
    code.focus();
  }

  DQ.route("#/signin", { title: "Sign in", render: function (main, params, query) {
    var login = DQ.ui.input({ type: "text", autocomplete: "username", required: true, inputmode: "email" });
    var pw = DQ.ui.input({ type: "password", autocomplete: "current-password", required: true });
    var note = null;
    if (query && query.sso && SSO_MESSAGES[query.sso]) {
      note = h("p", { class: "form-error", role: "alert" }, SSO_MESSAGES[query.sso]);
      try { history.replaceState(null, "", "#/signin"); } catch (e) { /* old browser: harmless */ }
    } else if (signedOutNote) {
      note = h("p", { class: "notice", role: "status" }, signedOutNote);
    }
    signedOutNote = null;
    var sso = h("div", { class: "sso", hidden: true });
    main.appendChild(authCard("Sign in", null, h("div", null, note, form([
      DQ.ui.field("Email or phone", login), DQ.ui.field("Password", pw)
    ], "Sign in", function () {
      return DQ.post("/api/auth/login", { login: login.value.trim(), password: pw.value }, { quiet: true, allow401: true }).then(function (r) {
        if (r && r.two_step) codeStep(main, r); else afterSignIn(r);
      });
    }, h("p", { class: "auth-links" }, h("a", { href: "#/forgot" }, "Forgot your password?"), " · ", h("a", { href: "#/signup" }, "Start a new company"))), sso)));
    providers().then(function (pv) {
      var btns = [["google", "Sign in with Google"], ["microsoft", "Sign in with Microsoft"]].filter(function (x) { return pv[x[0]]; });
      if (!btns.length) return;
      sso.appendChild(h("p", { class: "sso-or" }, h("span", null, "or")));
      btns.forEach(function (x) {
        sso.appendChild(h("a", { class: "btn btn-ghost btn-block sso-btn", href: "/api/auth/oidc/" + x[0] + "/start", "data-provider": x[0] }, x[1]));
      });
      sso.hidden = false;
    });
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
    }, h("p", { class: "auth-links" }, "By creating a company you accept the terms and the privacy notice.", " ", h("a", { href: "/terms.html" }, "Terms"), " · ", h("a", { href: "/privacy.html" }, "Privacy notice"), " · ", h("a", { href: "#/signin" }, "Sign in instead")))));
    co.focus();
  } });

  DQ.route("#/forgot", { title: "Forgot password", render: function (main, params, query) {
    if (query && query.by === "phone") return forgotByPhone(main);
    var login = DQ.ui.input({ required: true, autocomplete: "username" });
    var done = h("p", { class: "notice", hidden: true });
    var byPhone = h("span", { hidden: true }, " · ", h("a", { href: "#/forgot?by=phone" }, "Get a code by text message instead"));
    main.appendChild(authCard("Forgot your password?", "We email a link to choose a new one. An Admin can also send you one.", form([DQ.ui.field("Email or phone", login), done], "Send the link", function () {
      return DQ.post("/api/auth/password/forgot", { login: login.value.trim() }, { quiet: true }).then(function (r) { done.textContent = tx(r.message); done.hidden = false; });
    }, h("p", { class: "auth-links" }, h("a", { href: "#/signin" }, "Back to sign in"), byPhone))));
    providers().then(function (pv) { if (pv.phone_reset) byPhone.hidden = false; });
  } });

  /* Reset by a code sent to the phone: number first, then the code and a new password. */
  function forgotByPhone(main) {
    var phone = DQ.ui.input({ type: "tel", required: true, autocomplete: "tel", inputmode: "tel" });
    function send() {
      return DQ.post("/api/auth/password/phone", { phone: phone.value.trim() }, { quiet: true });
    }
    main.appendChild(authCard("Reset by text message", "We send a 6-digit code to your phone. It works for 10 minutes.", form([
      DQ.ui.field("Mobile number", phone, "The number on your DQ-Stack account, for example 98765 43210.")
    ], "Send the code", function () {
      return send().then(function (r) { codeAndPassword(r.message); });
    }, h("p", { class: "auth-links" }, h("a", { href: "#/forgot" }, "Use email instead"), " · ", h("a", { href: "#/signin" }, "Back to sign in")))));
    phone.focus();

    function codeAndPassword(message) {
      DQ.clear(main);
      var code = codeInput();
      var pw = DQ.ui.input({ type: "password", required: true, autocomplete: "new-password", minlength: 10 });
      var sent = h("p", { class: "notice", role: "status" }, message);
      var resend = h("button", { class: "link-btn", type: "button" }, "Send a new code");
      resend.addEventListener("click", function () {
        resend.disabled = true;
        send().then(function (r) {
          sent.textContent = tx(r.message); code.value = ""; code.focus();
          setTimeout(function () { resend.disabled = false; }, 30000);
        }, function (e) { sent.textContent = e.message; resend.disabled = false; });
      });
      main.appendChild(authCard("Enter the code", null, form([
        sent, DQ.ui.field("6-digit code", code), DQ.ui.field("New password", pw, "At least 10 characters.")
      ], "Save the new password", function () {
        return DQ.post("/api/auth/password/phone/reset", { phone: phone.value.trim(), code: code.value.trim(), password: pw.value }, { quiet: true }).then(function (r) {
          DQ.toast(r.message); location.hash = "#/signin";
        });
      }, h("p", { class: "auth-links" }, resend, " · ", h("a", { href: "#/signin" }, "Back to sign in")))));
      code.focus();
    }
  }

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
      main.appendChild(authCard(t("Join {company}", { company: inv.company }), t("Hello {name}. You are invited as {role}. Choose a password to start.", { name: inv.name, role: tx(roleName(inv.role)) }), form([DQ.ui.field("Password", pw, "At least 10 characters.")], "Join", function () {
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
  DQ.get("/api/auth/me", { allow401: true }).then(function (me) { DQ.me = me; adoptLanguage(me); render(); DQ.queue.flush(); },
    function () { DQ.me = null; render(); });
})();
