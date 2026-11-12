/* DQ-Stack store app: languages. Loaded before core.js.

   English is the source language: every string in the screens is written in English
   and is its own key. A language is a dictionary from that English to the translation
   (js/i18n-hi.js for Hindi), so anything not yet translated simply shows in English.

     DQ.t("Bill {number} held.", { number: n })   a template: the dictionary holds the
                                                  same template translated, with the same
                                                  {placeholders}; values are put in after
     DQ.tn(n, "{n} bill", "{n} bills", vars)      the same, choosing the plural form;
                                                  {n} is the number in Indian grouping
     DQ.tx(text)                                  a finished English sentence (a server
                                                  message, a label): looked up whole, then
                                                  against the dictionary's templates, so
                                                  "Bill R-12 was voided." finds
                                                  "Bill {number} was voided."
     DQ.i18n.set(code) / .lang() / .langs         the language in use; sets <html lang>

   DQ.h passes every text child and the placeholder, title, aria-label and alt
   attributes through DQ.tx, so plain labels need nothing at their call site. Text built
   by joining pieces must use DQ.t with a template instead, or it cannot be found.

   Numbers, rupees and dates keep Latin digits and Indian grouping in every language;
   only month names change (fmt.date reads DQ.i18n.months()).

   Adding a language: copy js/i18n-hi.js to js/i18n-xx.js, call DQ.i18n.add("xx", {...},
   { name: "...", months: [...], font: "..." }), add its script tag after this file, and
   add "xx" to SUPPORTED_LANGUAGES in dqstack/app/routes/auth.py. */
(function () {
  "use strict";

  var DQ = (window.DQ = window.DQ || {});
  var KEY = "dq.lang";
  var EN_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  var dicts = { en: {} };
  var meta = { en: { name: "English", months: EN_MONTHS } };
  var order = ["en"];
  var compiled = {};
  var lang = "en";

  function has(o, k) { return Object.prototype.hasOwnProperty.call(o, k); }
  function fill(s, vars) {
    if (!vars) return s;
    return s.replace(/\{(\w+)\}/g, function (m, k) {
      return has(vars, k) && vars[k] !== null && vars[k] !== undefined ? String(vars[k]) : m;
    });
  }
  function exact(s) {
    var d = dicts[lang];
    return d && has(d, s) && d[s] ? d[s] : null;
  }
  function esc(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }
  var NUMERIC = { n: 1, pct: 1, done: 1, total: 1 };

  /* Templates ({placeholders}) become anchored patterns, longest first, each with its
     longest fixed piece as a cheap first test. */
  function patterns() {
    if (compiled[lang]) return compiled[lang];
    var d = dicts[lang] || {}, out = [];
    Object.keys(d).forEach(function (key) {
      if (key.indexOf("{") < 0 || !d[key]) return;
      var names = [], parts = key.split(/\{(\w+)\}/);
      var src = "^", anchor = "";
      parts.forEach(function (p, i) {
        // {n} and {pct} are numbers: matching only digits keeps "Every day" from
        // looking like "{n} day".
        if (i % 2) { names.push(p); src += NUMERIC[p] ? "(-?[\\d,.]+)" : "(.+?)"; }
        else { src += esc(p); if (p.length > anchor.length) anchor = p; }
      });
      if (anchor.replace(/[\s.,:;·()-]/g, "").length < 2) return;   // too loose to match safely
      out.push({ key: key, re: new RegExp(src + "$"), names: names, anchor: anchor, len: key.length });
    });
    out.sort(function (a, b) { return b.len - a.len; });
    compiled[lang] = out;
    return out;
  }
  function viaPattern(s) {
    var list = patterns();
    for (var i = 0; i < list.length; i++) {
      var p = list[i];
      if (s.indexOf(p.anchor) < 0) continue;
      var m = p.re.exec(s);
      if (!m) continue;
      var vars = {};
      p.names.forEach(function (n, j) { var v = m[j + 1]; vars[n] = exact(v) || v; });
      return fill(dicts[lang][p.key], vars);
    }
    return null;
  }

  DQ.t = function (key, vars) {
    if (lang !== "en") { var hit = exact(key); if (hit) return fill(hit, vars); }
    return fill(key, vars);
  };
  DQ.tn = function (n, one, many, vars) {
    var v = { n: DQ.fmt ? DQ.fmt.num(n) : String(n) };
    if (vars) Object.keys(vars).forEach(function (k) { v[k] = vars[k]; });
    return DQ.t(n === 1 ? one : many, v);
  };
  DQ.tx = function (s) {
    if (lang === "en" || typeof s !== "string" || !s) return s;
    var hit = exact(s);
    if (hit) return hit;
    if (!/[A-Za-z]/.test(s)) return s;
    var m = /^(\s*)([\s\S]*?)(\s*)$/.exec(s);
    var core = m[2];
    if (core !== s) { hit = exact(core); if (hit) return m[1] + hit + m[3]; }
    hit = viaPattern(core);
    return hit ? m[1] + hit + m[3] : s;
  };

  function loadFont(code) {
    var f = meta[code] && meta[code].font;
    if (!f || document.getElementById("dq-font-" + code)) return;
    var l = document.createElement("link");
    l.id = "dq-font-" + code; l.rel = "stylesheet";
    l.href = "https://fonts.googleapis.com/css2?family=" + f + "&display=swap";
    document.head.appendChild(l);
  }

  DQ.i18n = {
    /* dict: English -> translation. info: { name (in its own script), months, font }. */
    add: function (code, dict, info) {
      dicts[code] = Object.assign(dicts[code] || {}, dict || {});
      meta[code] = Object.assign(meta[code] || {}, info || {});
      if (order.indexOf(code) < 0) order.push(code);
      compiled[code] = null;
      if (code === saved()) DQ.i18n.set(code);
    },
    langs: function () { return order.map(function (c) { return { code: c, name: meta[c].name || c }; }); },
    lang: function () { return lang; },
    dict: function (code) { return dicts[code] || {}; },
    months: function () { return (meta[lang] && meta[lang].months) || EN_MONTHS; },
    supported: function (code) { return order.indexOf(code) >= 0; },
    /* Use a language now and remember it on this device. The person's own choice is
       also saved on the server (see start.js), so it follows them to another till. */
    set: function (code) {
      if (!DQ.i18n.supported(code)) return lang;      // not loaded: keep what is shown
      lang = code;
      document.documentElement.lang = code;
      try { localStorage.setItem(KEY, code); } catch (e) { /* private window */ }
      loadFont(code);
      return code;
    },
    saved: saved
  };
  function saved() { try { return localStorage.getItem(KEY) || "en"; } catch (e) { return "en"; } }
})();
