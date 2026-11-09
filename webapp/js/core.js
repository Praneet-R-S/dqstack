/* DQ-Stack store app: the shared core every screen uses.

   window.DQ provides
     api(method, path, body?, opts?)  JSON over fetch, CSRF header, plain-language errors,
                                      and the save status in the top bar for every write
     upload(path, formData)           multipart (imports)
     me, can(action, storeId?)        who is signed in and what they may do
     store(), setStore(id)            the store being viewed (always named on screen)
     fmt.*                            Indian formats: rupees with lakh and crore, Indian
                                      digit grouping, DD MMM YYYY, IST
     h(tag, attrs, ...children)       build DOM without innerHTML
     route(pattern, def)              register a screen; see start.js for the router
     toast(text, {undo, kind})        safe actions are instant with Undo for 10 seconds
     confirm({...})                   irreversible actions ask, and can ask for a reason
     queue                            offline queue: work saved on the device, sent once
     ui.*                             table, empty, skeleton, error, badge, field, tabs
     scan.attach(input, fn)           USB/Bluetooth scanners in keyboard mode
     scan.camera(fn)                  phone camera, where the browser can read barcodes

   Interface rules (spec section 12) this file exists to make easy to follow:
   the same frame everywhere, every number says how fresh it is, save status always
   visible, Undo for safe actions, offline is normal, errors say what to do, empty
   screens teach, 44px touch targets, Indian formats, plain language. */
(function () {
  "use strict";

  var DQ = (window.DQ = window.DQ || {});
  DQ.me = null;
  DQ.routes = [];

  /* ---------------------------------------------------------------- DOM */
  function h(tag, attrs) {
    var el = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        var v = attrs[k];
        if (v === null || v === undefined || v === false) return;
        if (k === "class") el.className = v;
        else if (k === "text") el.textContent = v;
        else if (k === "html") throw new Error("DQ.h never takes html: build nodes instead");
        else if (k.slice(0, 2) === "on" && typeof v === "function") el.addEventListener(k.slice(2), v);
        else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
        else if (v === true) el.setAttribute(k, "");
        else el.setAttribute(k, String(v));
      });
    }
    for (var i = 2; i < arguments.length; i++) append(el, arguments[i]);
    return el;
  }
  function append(el, c) {
    if (c === null || c === undefined || c === false) return;
    if (Array.isArray(c)) { c.forEach(function (x) { append(el, x); }); return; }
    el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }
  DQ.h = h;
  DQ.clear = clear;

  /* ---------------------------------------------------------------- formats */
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  function group(n) {                       // 1234567 -> 12,34,567
    var neg = n < 0, s = String(Math.abs(Math.trunc(n)));
    if (s.length > 3) {
      var last = s.slice(-3), rest = s.slice(0, -3);
      rest = rest.replace(/\B(?=(\d{2})+(?!\d))/g, ",");
      s = rest + "," + last;
    }
    return (neg ? "-" : "") + s;
  }
  function istParts(iso) {
    var d = new Date(iso);
    var t = new Date(d.getTime() + (330 + d.getTimezoneOffset()) * 60000);
    return t;
  }
  DQ.fmt = {
    num: function (n) { return n === null || n === undefined ? "—" : group(n); },
    rupees: function (paise, opts) {
      if (paise === null || paise === undefined) return "—";
      opts = opts || {};
      var neg = paise < 0, p = Math.abs(paise);
      if (opts.short) {
        var r = p / 100;
        if (r >= 1e7) return (neg ? "-" : "") + "₹" + (r / 1e7).toFixed(2) + " Cr";
        if (r >= 1e5) return (neg ? "-" : "") + "₹" + (r / 1e5).toFixed(2) + " L";
      }
      var whole = Math.floor(p / 100), frac = p % 100;
      var out = "₹" + group(whole) + (opts.whole ? "" : "." + (frac < 10 ? "0" : "") + frac);
      return (neg ? "-" : "") + out;
    },
    qty: function (milli, unit) {
      if (milli === null || milli === undefined) return "—";
      var v = milli / 1000;
      var s = unit && unit !== "each" ? v.toFixed(3).replace(/\.?0+$/, "") : group(Math.round(v));
      return unit && unit !== "each" ? s + " " + unit : s;
    },
    pct: function (x, digits) { return x === null || x === undefined ? "—" : (100 * x).toFixed(digits === undefined ? 0 : digits) + "%"; },
    date: function (iso) {                  // 07 Nov 2026
      if (!iso) return "—";
      var p = iso.slice(0, 10).split("-");
      return p[2] + " " + MONTHS[+p[1] - 1] + " " + p[0];
    },
    dayShort: function (iso) { if (!iso) return "—"; var p = iso.slice(0, 10).split("-"); return +p[2] + " " + MONTHS[+p[1] - 1]; },
    time: function (iso) {                  // 14:32 (IST)
      if (!iso) return "—";
      var t = istParts(iso);
      return ("0" + t.getHours()).slice(-2) + ":" + ("0" + t.getMinutes()).slice(-2);
    },
    dateTime: function (iso) { return iso ? DQ.fmt.date(istParts(iso).toISOString()) + ", " + DQ.fmt.time(iso) + " IST" : "—"; },
    ago: function (iso) {
      if (!iso) return "never";
      var m = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
      if (m < 1) return "just now";
      if (m < 60) return m + " min ago";
      if (m < 60 * 36) return Math.round(m / 60) + " h ago";
      return Math.round(m / 1440) + " days ago";
    },
    toMilli: function (text) { var v = parseFloat(String(text).replace(/,/g, "")); return isNaN(v) ? null : Math.round(v * 1000); },
    toPaise: function (text) { var v = parseFloat(String(text).replace(/[₹,\s]|Rs\.?/gi, "")); return isNaN(v) ? null : Math.round(v * 100); },
    today: function () { return istParts(new Date().toISOString()).toISOString().slice(0, 10); }
  };

  /* ---------------------------------------------------------------- save status */
  var pendingWrites = 0, lastError = null;
  function paintSave() {
    var el = document.getElementById("save-status");
    if (!el) return;
    var waiting = DQ.queue ? DQ.queue.count() : 0;
    var text, kind;
    if (!navigator.onLine) { text = "Offline" + (waiting ? ", " + waiting + " change" + (waiting > 1 ? "s" : "") + " waiting" : ""); kind = "attention"; }
    else if (pendingWrites > 0) { text = "Saving…"; kind = "busy"; }
    else if (waiting) { text = "Sending " + waiting + " waiting change" + (waiting > 1 ? "s" : ""); kind = "busy"; }
    else if (lastError) { text = "Not saved: " + lastError; kind = "risk"; }
    else { text = "Saved"; kind = "ok"; }
    el.textContent = text;
    el.setAttribute("data-kind", kind);
  }
  DQ.paintSave = paintSave;

  /* ---------------------------------------------------------------- API */
  function ApiError(message, status, problems) {
    var e = new Error(message);
    e.status = status; e.problems = problems || [];
    return e;
  }
  DQ.api = function (method, path, body, opts) {
    opts = opts || {};
    var write = method !== "GET";
    var headers = { "Accept": "application/json", "X-DQ-Device": DQ.deviceId() };
    if (body !== undefined && body !== null && !(body instanceof FormData)) headers["Content-Type"] = "application/json";
    if (write && DQ.me && DQ.me.csrf) headers["X-CSRF-Token"] = DQ.me.csrf;
    if (write && !opts.quiet) { pendingWrites++; paintSave(); }
    return fetch(path, {
      method: method, headers: headers, credentials: "same-origin",
      body: body instanceof FormData ? body : (body === undefined || body === null ? undefined : JSON.stringify(body))
    }).then(function (r) {
      var type = r.headers.get("content-type") || "";
      var read = type.indexOf("application/json") >= 0 ? r.json() : (opts.raw ? r.blob() : r.text());
      return read.then(function (data) {
        if (!r.ok) {
          var msg = data && data.error ? data.error : "Something went wrong. Please try again.";
          if (r.status === 401 && !opts.allow401) DQ.signedOut(msg);
          throw ApiError(msg, r.status, data && data.problems);
        }
        if (write && !opts.quiet) lastError = null;
        return data;
      });
    }, function () {
      throw ApiError("The server could not be reached. Check the internet connection and try again.", 0);
    }).then(function (data) {
      if (write && !opts.quiet) { pendingWrites--; paintSave(); }
      return data;
    }, function (err) {
      if (write && !opts.quiet) { pendingWrites--; lastError = err.status ? err.message.slice(0, 80) : "no connection"; paintSave(); }
      throw err;
    });
  };
  DQ.get = function (path, opts) { return DQ.api("GET", path, null, opts); };
  DQ.post = function (path, body, opts) { return DQ.api("POST", path, body || {}, opts); };
  DQ.upload = function (path, form) { return DQ.api("POST", path, form); };
  DQ.download = function (path) { window.location.href = path; };
  DQ.qs = function (o) {
    var p = Object.keys(o).filter(function (k) { return o[k] !== null && o[k] !== undefined && o[k] !== ""; })
      .map(function (k) { return encodeURIComponent(k) + "=" + encodeURIComponent(o[k]); });
    return p.length ? "?" + p.join("&") : "";
  };

  DQ.deviceId = function () {
    try {
      var id = localStorage.getItem("dq.device");
      if (!id) { id = "web-" + Math.random().toString(36).slice(2, 10); localStorage.setItem("dq.device", id); }
      return id;
    } catch (e) { return "web"; }
  };
  DQ.key = function (prefix) { return (prefix || "k") + "-" + DQ.deviceId() + "-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8); };

  /* ---------------------------------------------------------------- who and where */
  /* The server sends one yes/no per action. A role limited to its own stores only ever
     sees its own stores, so a store-level check is "may do it, and can see the store".
     The server checks again on every request: hiding a button is a courtesy, not
     security (section 15). */
  DQ.can = function (action, storeId) {
    if (!DQ.me || !(DQ.me.can || {})[action]) return false;
    return !storeId || (DQ.me.stores || []).some(function (s) { return s.id === storeId; });
  };
  DQ.stores = function () { return (DQ.me && DQ.me.stores) || []; };
  DQ.store = function () {
    var list = DQ.stores();
    var id = null;
    try { id = localStorage.getItem("dq.store"); } catch (e) { /* private window */ }
    if (!list.some(function (s) { return s.id === id; })) id = list.length ? list[0].id : null;
    return id;
  };
  DQ.storeObj = function () { var id = DQ.store(); return DQ.stores().filter(function (s) { return s.id === id; })[0] || null; };
  DQ.setStore = function (id) { try { localStorage.setItem("dq.store", id); } catch (e) { /* ignore */ } };

  /* ---------------------------------------------------------------- routes */
  DQ.route = function (pattern, def) { def.pattern = pattern; DQ.routes.push(def); };

  /* ---------------------------------------------------------------- toast and dialogs */
  DQ.toast = function (text, opts) {
    opts = opts || {};
    var region = document.getElementById("toasts");
    if (!region) return;
    var undo = opts.undo;
    var t = h("div", { class: "toast", "data-kind": opts.kind || "ok", role: "status" },
      h("span", { class: "toast-text" }, text),
      undo ? h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: function () { undo(); remove(); } }, "Undo") : null,
      h("button", { class: "toast-x", type: "button", "aria-label": "Close", onclick: remove }, "×"));
    region.appendChild(t);
    var timer = setTimeout(remove, undo ? 10000 : 5000);
    function remove() { clearTimeout(timer); if (t.parentNode) t.parentNode.removeChild(t); }
    return remove;
  };
  DQ.confirm = function (o) {
    return new Promise(function (resolve) {
      var reason = o.reason ? h("textarea", { class: "input", rows: 3, required: true, "aria-label": o.reasonLabel || "Reason", placeholder: o.reasonLabel || "Reason" }) : null;
      var err = h("p", { class: "field-error", hidden: true });
      var dlg = h("dialog", { class: "dialog", "aria-labelledby": "dlg-title" },
        h("h2", { id: "dlg-title", class: "title-3" }, o.title || "Are you sure?"),
        o.message ? h("p", { class: "text-2" }, o.message) : null,
        reason, err,
        h("div", { class: "dialog-actions" },
          h("button", { class: "btn btn-ghost", type: "button", onclick: function () { done(false); } }, o.cancelLabel || "Cancel"),
          h("button", { class: "btn " + (o.danger ? "btn-danger" : "btn-primary"), type: "button", onclick: function () {
            if (reason && reason.value.trim().length < 3) { err.textContent = "Give a reason of a few words."; err.hidden = false; reason.focus(); return; }
            done(true);
          } }, o.confirmLabel || "Confirm")));
      document.body.appendChild(dlg);
      function done(ok) { dlg.close(); dlg.remove(); resolve({ ok: ok, reason: reason ? reason.value.trim() : null }); }
      dlg.addEventListener("cancel", function (e) { e.preventDefault(); done(false); });
      dlg.showModal();
      (reason || dlg.querySelector(".btn-primary, .btn-danger")).focus();
    });
  };

  /* ---------------------------------------------------------------- offline queue
     Work done offline (counts, deliveries, bills) is kept on the device in the order it
     happened and sent when the connection returns. Every item carries its own key, so
     sending it twice is harmless: the server recognises the key and changes nothing. */
  var QKEY = "dq.queue";
  function qload() { try { return JSON.parse(localStorage.getItem(QKEY) || "[]"); } catch (e) { return []; } }
  function qsave(items) { try { localStorage.setItem(QKEY, JSON.stringify(items)); } catch (e) { /* full */ } paintSave(); }
  var flushing = false;
  DQ.queue = {
    count: function () { return qload().length; },
    items: function () { return qload(); },
    add: function (label, method, path, body) {
      var items = qload();
      items.push({ id: DQ.key("q"), label: label, method: method, path: path, body: body, at: new Date().toISOString() });
      qsave(items);
      DQ.queue.flush();
    },
    flush: function () {
      if (flushing || !navigator.onLine) return Promise.resolve();
      var items = qload();
      if (!items.length) return Promise.resolve();
      flushing = true;
      var item = items[0];
      return DQ.api(item.method, item.path, item.body, { quiet: true }).then(function () {
        qsave(qload().filter(function (x) { return x.id !== item.id; }));
        flushing = false;
        return DQ.queue.flush();
      }, function (err) {
        flushing = false;
        if (err.status && err.status < 500 && err.status !== 401 && err.status !== 429) {
          // The server refused it for a reason a retry will not fix: keep it aside and say so.
          qsave(qload().filter(function (x) { return x.id !== item.id; }));
          DQ.toast("Not saved: " + item.label + ". " + err.message, { kind: "risk" });
          return DQ.queue.flush();
        }
        paintSave();
      });
    }
  };
  window.addEventListener("online", function () { paintSave(); DQ.queue.flush(); });
  window.addEventListener("offline", paintSave);
  setInterval(function () { DQ.queue.flush(); }, 15000);

  /* ---------------------------------------------------------------- components */
  DQ.ui = {
    page: function (title, opts) {
      opts = opts || {};
      return h("header", { class: "page-head" },
        h("div", null,
          h("h1", { class: "title-1" }, title),
          opts.sub ? h("p", { class: "page-sub text-2" }, opts.sub) : null),
        opts.actions ? h("div", { class: "page-actions" }, opts.actions) : null);
    },
    fresh: function (label, iso) { return h("span", { class: "fresh", title: iso ? DQ.fmt.dateTime(iso) : "" }, label + " " + (iso ? DQ.fmt.time(iso) : "—")); },
    badge: function (text, kind) { return h("span", { class: "badge", "data-kind": kind || "neutral" }, text); },
    statusKind: function (status) {
      return { "Out": "risk", "Count needed": "risk", "Low": "attention", "Expiring": "attention", "Overstocked": "excess", "OK": "ok" }[status] || "neutral";
    },
    skeleton: function (rows) {
      var wrap = h("div", { class: "skeleton", "aria-busy": "true", "aria-label": "Loading" });
      for (var i = 0; i < (rows || 6); i++) wrap.appendChild(h("div", { class: "skeleton-row" }));
      return wrap;
    },
    empty: function (o) {
      return h("div", { class: "empty" },
        h("h2", { class: "title-3" }, o.title),
        o.text ? h("p", { class: "text-2" }, o.text) : null,
        o.action ? h("a", { class: "btn btn-primary", href: o.action.href }, o.action.label) : null);
    },
    error: function (err, retry) {
      return h("div", { class: "error-box", role: "alert" },
        h("p", null, err && err.message ? err.message : String(err)),
        err && err.problems && err.problems.length ? h("ul", null, err.problems.map(function (p) { return h("li", null, p); })) : null,
        retry ? h("button", { class: "btn btn-ghost", type: "button", onclick: retry }, "Try again") : null);
    },
    field: function (label, input, help, errorId) {
      var id = input.id || ("f-" + Math.random().toString(36).slice(2, 8));
      input.id = id;
      return h("div", { class: "field" },
        h("label", { for: id }, label), input,
        help ? h("p", { class: "field-help" }, help) : null,
        h("p", { class: "field-error", id: errorId || id + "-err", hidden: true }));
    },
    input: function (attrs) { return h("input", Object.assign({ class: "input" }, attrs || {})); },
    select: function (options, value, attrs) {
      var s = h("select", Object.assign({ class: "input" }, attrs || {}));
      options.forEach(function (o) {
        var opt = h("option", { value: o.value }, o.label);
        if (String(o.value) === String(value)) opt.selected = true;
        s.appendChild(opt);
      });
      return s;
    },
    button: function (label, onclick, kind, attrs) { return h("button", Object.assign({ class: "btn btn-" + (kind || "primary"), type: "button", onclick: onclick }, attrs || {}), label); },
    /* columns: [{key, label, kind: text|qty|money|pct|date|num, unit?, render?(row)}] */
    table: function (columns, rows, opts) {
      opts = opts || {};
      if (!rows.length) return DQ.ui.empty(opts.empty || { title: "Nothing here yet" });
      var thead = h("thead", null, h("tr", null, columns.map(function (c) {
        return h("th", { scope: "col", class: c.kind && c.kind !== "text" ? "num" : null }, c.label);
      })));
      var tbody = h("tbody");
      rows.forEach(function (r) {
        var tr = h("tr", opts.onRow ? { tabindex: 0, class: "clickable", onclick: function () { opts.onRow(r); }, onkeydown: function (e) { if (e.key === "Enter") opts.onRow(r); } } : null);
        columns.forEach(function (c) {
          var v = c.render ? c.render(r) : DQ.ui.cell(r[c.key], c.kind, c.unit ? (typeof c.unit === "function" ? c.unit(r) : r[c.unit] || c.unit) : null);
          tr.appendChild(h("td", { class: c.kind && c.kind !== "text" ? "num" : null }, v));
        });
        tbody.appendChild(tr);
      });
      return h("div", { class: "table-wrap" }, h("table", { class: "table" }, opts.caption ? h("caption", { class: "sr-only" }, opts.caption) : null, thead, tbody));
    },
    cell: function (v, kind, unit) {
      if (v === null || v === undefined || v === "") return "—";
      if (kind === "money") return DQ.fmt.rupees(v);
      if (kind === "qty") return DQ.fmt.qty(v, unit);
      if (kind === "pct") return DQ.fmt.pct(v, 1);
      if (kind === "date") return /^\d{4}-\d{2}-\d{2}/.test(v) ? DQ.fmt.date(v) : v;
      if (kind === "num") return typeof v === "number" ? DQ.fmt.num(v) : v;
      return String(v);
    },
    tabs: function (items, active, onPick) {
      return h("div", { class: "tabs", role: "tablist" }, items.map(function (it) {
        return h("button", { class: "tab", role: "tab", type: "button", "aria-selected": it.value === active ? "true" : "false", onclick: function () { onPick(it.value); } }, it.label);
      }));
    },
    storePicker: function (onChange, opts) {
      opts = opts || {};
      var list = DQ.stores();
      return DQ.ui.select(list.map(function (s) { return { value: s.id, label: s.name }; }), opts.value || DQ.store(), {
        "aria-label": "Store", onchange: function (e) { onChange(e.target.value); } });
    }
  };

  /* ---------------------------------------------------------------- scanning */
  DQ.scan = {
    /* Keyboard-wedge scanners type the code quickly and press Enter. */
    attach: function (input, onCode) {
      input.addEventListener("keydown", function (e) {
        if (e.key === "Enter") {
          e.preventDefault();
          var code = input.value.trim();
          input.value = "";
          if (code) onCode(code);
        }
      });
    },
    canUseCamera: function () { return "BarcodeDetector" in window && navigator.mediaDevices && navigator.mediaDevices.getUserMedia; },
    camera: function (onCode) {
      if (!DQ.scan.canUseCamera()) { DQ.toast("This browser cannot read barcodes from the camera. Type the code instead.", { kind: "attention" }); return; }
      var video = h("video", { class: "scan-video", playsinline: true, muted: true, "aria-label": "Camera view" });
      var dlg = h("dialog", { class: "dialog dialog-scan", "aria-label": "Scan with the camera" }, video,
        h("div", { class: "dialog-actions" }, h("button", { class: "btn btn-ghost", type: "button", onclick: stop }, "Close")));
      document.body.appendChild(dlg);
      dlg.showModal();
      var detector = new window.BarcodeDetector({ formats: ["ean_13", "ean_8", "upc_a", "upc_e", "code_128", "code_39", "qr_code"] });
      var stream = null, running = true;
      navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } }).then(function (s) {
        stream = s; video.srcObject = s; video.play(); tick();
      }, function () { DQ.toast("The camera could not be opened. Allow camera access, or type the code.", { kind: "attention" }); stop(); });
      function tick() {
        if (!running) return;
        detector.detect(video).then(function (codes) {
          if (codes.length) { var c = codes[0].rawValue; stop(); onCode(c); } else requestAnimationFrame(tick);
        }, function () { requestAnimationFrame(tick); });
      }
      function stop() { running = false; if (stream) stream.getTracks().forEach(function (t) { t.stop(); }); if (dlg.open) dlg.close(); dlg.remove(); }
    }
  };
})();
