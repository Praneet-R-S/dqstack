/* DQ-Stack landing page: the scroll mechanics, the festival chart, the stock target
   decision, and the shop clock.

   One rule for all of it: native scroll stays native. A passive listener only marks the
   page dirty and asks for a frame; each scene reads its own progress and eases toward it.
   Everything animates with transform, opacity, clip-path or a canvas redraw. Reduced
   motion gets every scene in its finished state. */
(function () {
  "use strict";
  window.dqLanding = true;

  var root = document.documentElement;
  var reduced = root.classList.contains("rm");
  var coarse = window.matchMedia && matchMedia("(pointer: coarse)").matches;
  var narrowQ = window.matchMedia("(max-width: 900px)");
  function $(s, el) { return (el || document).querySelector(s); }
  function $$(s, el) { return Array.prototype.slice.call((el || document).querySelectorAll(s)); }
  function clamp(v, a, b) { a = a === undefined ? 0 : a; b = b === undefined ? 1 : b; return v < a ? a : v > b ? b : v; }
  function lerp(a, b, t) { return a + (b - a) * t; }
  function easeInOut(t) { return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2; }
  function easeOut(t) { return 1 - Math.pow(1 - t, 3); }
  var inr = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });
  function rupees(v) { return "₹" + inr.format(Math.round(v)); }

  /* ================================================================ the frame loop */
  var scenes = [];
  var dirty = true, queued = false;
  function vh() { return window.innerHeight; }
  /* A pinned track: 0 when its top reaches the top of the screen, 1 when its end does.
     A section in normal flow: 0 as it enters at the bottom, 1 as it leaves at the top. */
  function progressOf(el, pinned) {
    var r = el.getBoundingClientRect();
    if (pinned) {
      var span = r.height - vh();
      if (span > 1) return clamp(-r.top / span);
    }
    return clamp((vh() - r.top) / (vh() + r.height));
  }
  function scene(el, opts) {
    var s = { el: el, pinned: !!opts.pinned, update: opts.update, p: reduced ? 1 : 0, target: 0, k: opts.k || (coarse ? 0.24 : 0.14), fixed: opts.fixed };
    scenes.push(s);
    return s;
  }
  function frame() {
    queued = false;
    var again = false;
    for (var i = 0; i < scenes.length; i++) {
      var s = scenes[i];
      if (reduced) { if (dirty || s.force) { s.update(1, 1); s.force = false; } continue; }
      if (dirty) s.target = s.fixed ? s.fixed() : progressOf(s.el, s.pinned && s.el.offsetHeight > vh() + 2);
      var prev = s.p;
      s.p = Math.abs(s.target - s.p) < 0.0004 ? s.target : lerp(s.p, s.target, s.k);
      if (s.p !== prev || s.force) { s.update(s.p, s.target); s.force = false; }
      if (s.p !== s.target) again = true;
    }
    dirty = false;
    clock.tick();
    if (again || pointer.moving) request();
  }
  function request() { if (!queued) { queued = true; requestAnimationFrame(frame); } }
  window.addEventListener("scroll", function () { dirty = true; request(); }, { passive: true });
  window.addEventListener("resize", function () { dirty = true; scenes.forEach(function (s) { s.force = true; }); fest.resize(); request(); });

  /* ================================================================ the menu */
  (function menu() {
    var btn = $(".menu-toggle"), panel = $("#menu");
    if (!btn || !panel) return;
    function set(open) {
      btn.setAttribute("aria-expanded", String(open));
      btn.setAttribute("aria-label", open ? "Close the menu" : "Open the menu");
      panel.hidden = !open;
      document.body.style.overflow = open ? "hidden" : "";
    }
    btn.addEventListener("click", function () { set(btn.getAttribute("aria-expanded") !== "true"); });
    panel.addEventListener("click", function (e) { if (e.target.closest("a")) set(false); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape" && !panel.hidden) { set(false); btn.focus(); } });
  })();

  /* ================================================================ pointer (hero depth) */
  var pointer = { x: 0, y: 0, tx: 0, ty: 0, moving: false };
  if (!reduced && !coarse) {
    window.addEventListener("pointermove", function (e) {
      pointer.tx = (e.clientX / window.innerWidth) * 2 - 1;
      pointer.ty = (e.clientY / window.innerHeight) * 2 - 1;
      pointer.moving = true;
      request();
    }, { passive: true });
  }

  /* ================================================================ 1. closing time */
  (function hero() {
    var el = $("#top");
    if (!el) return;
    var rails = $$(".hero-shelf .rail", el), light = $(".hero-light", el), device = $(".hero-device", el),
      display = $(".hero-device .display", el), receipt = $(".receipt", el), copy = $(".hero-copy", el);
    scene(el, {
      fixed: function () { return clamp(window.scrollY / Math.max(el.offsetHeight, 1)); },
      k: 0.2,
      update: function (s) {
        if (reduced) return;
        pointer.x = lerp(pointer.x, pointer.tx, 0.08);
        pointer.y = lerp(pointer.y, pointer.ty, 0.08);
        if (Math.abs(pointer.x - pointer.tx) < 0.001 && Math.abs(pointer.y - pointer.ty) < 0.001) pointer.moving = false;
        var mx = pointer.x, my = pointer.y;
        rails.forEach(function (r) {
          var d = parseFloat(r.getAttribute("data-depth")) || 0.08;
          r.style.transform = "translate3d(" + (mx * d * 160).toFixed(2) + "px," + (-s * d * 1100 + my * d * 70).toFixed(2) + "px,0)";
        });
        if (light) light.style.transform = "translate3d(" + (mx * 36).toFixed(2) + "px," + (-s * 140 + my * 24).toFixed(2) + "px,0)";
        if (device) device.style.transform = "translate3d(" + (mx * -16).toFixed(2) + "px," + (-s * 300 + my * -10).toFixed(2) + "px,0)";
        if (display) { display.style.setProperty("--ry", (mx * 4).toFixed(2) + "deg"); display.style.setProperty("--rx", (-my * 3).toFixed(2) + "deg"); }
        if (receipt) receipt.style.transform = "translate3d(" + (mx * -28).toFixed(2) + "px," + (-s * 620).toFixed(2) + "px,0) rotate(" + (s * 8).toFixed(2) + "deg)";
        if (copy) { copy.style.transform = "translate3d(0," + (-s * 90).toFixed(2) + "px,0)"; copy.style.opacity = String(1 - clamp((s - 0.35) / 0.5) * 0.8); }
      }
    });
  })();

  /* ================================================================ 2. the guess (pin + kinetic lines) */
  (function guess() {
    var el = $("#guess");
    if (!el) return;
    var lines = $$(".kline", el), arts = $$("[data-at]", $(".guess-art", el));
    scene(el, {
      pinned: true,
      update: function (p) {
        var now = -1;
        lines.forEach(function (l, i) { var on = p >= parseFloat(l.getAttribute("data-at")); l.classList.toggle("on", on); if (on) now = i; });
        lines.forEach(function (l, i) { l.classList.toggle("now", i === now); });
        arts.forEach(function (a) { a.classList.toggle("on", p >= parseFloat(a.getAttribute("data-at"))); });
      }
    });
  })();

  /* ================================================================ 3. the ledger (wipe + lines adding up) */
  (function ledger() {
    var el = $("#ledger");
    if (!el) return;
    var card = $(".ledger-card", el), rows = $$(".ledger-table tbody tr", el), total = $("#ledger-total"), note = $(".note", el);
    scene(el, {
      pinned: true,
      update: function (p) {
        var pinned = el.offsetHeight > vh() + 2;
        var w = pinned ? clamp((p - 0.04) / 0.3) : clamp((p - 0.08) / 0.28);
        card.style.setProperty("--wipe", ((1 - easeOut(w)) * 100).toFixed(2) + "%");
        if (note) note.style.opacity = String(1 - w * 0.85);
        var rp = pinned ? clamp((p - 0.22) / 0.56) : clamp((p - 0.2) / 0.45);
        var count = Math.floor(rp * rows.length + 0.0001);
        rows.forEach(function (r, i) { r.classList.toggle("on", i < count); });
        total.textContent = count ? rows[count - 1].getAttribute("data-bal") : "0";
      }
    });
  })();

  /* ================================================================ 4. the night board (split flap) */
  (function board() {
    var rows = $$(".board li[data-flip]");
    if (!rows.length) return;
    var letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ";
    function flip(li) {
      if (li.classList.contains("done") || li.classList.contains("ready")) return;
      var st = $(".st", li), to = st.getAttribute("data-to");
      var finish = function () { st.textContent = to; li.classList.add(to === "READY" ? "ready" : "done"); };
      if (reduced) { finish(); return; }
      var n = 0;
      var timer = setInterval(function () {
        n++;
        var s = "";
        for (var i = 0; i < to.length; i++) s += i < n - 4 ? to[i] : letters[(Math.random() * 26) | 0];
        st.textContent = s;
        if (n >= to.length + 4) { clearInterval(timer); finish(); }
      }, 45);
    }
    if (reduced || !("IntersectionObserver" in window)) { rows.forEach(flip); return; }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (!e.isIntersecting) return;
        io.unobserve(e.target);
        var li = e.target, delay = li.classList.contains("last") ? 700 : 120;
        setTimeout(function () { flip(li); }, delay);
      });
    }, { rootMargin: "0px 0px -28% 0px", threshold: 1 });
    rows.forEach(function (r) { io.observe(r); });
  })();

  /* ================================================================ 5. the festival (the peak) */
  /* Sample figures, labelled on the page: clay diyas in one store, two weeks of ordinary
     sales, then the forecast for sixteen days with Dhanteras on day 7 and Diwali on
     day 9. Deliveries arrive in whole cases of 12 ahead of the rush. */
  var FEST = (function () {
    var DAY = 86400000, today = Date.UTC(2026, 9, 30), diwali = Date.UTC(2026, 10, 8);
    var H = 14, F = 16;
    var lift = { "-9": 1.25, "-8": 1.4, "-7": 1.6, "-6": 1.9, "-5": 2.4, "-4": 3.0, "-3": 3.9, "-2": 5.6, "-1": 7.2, "0": 8.4, "1": 2.2, "2": 1.2, "3": 0.8, "4": 0.9 };
    var wd = [1.4, 0.85, 0.85, 0.9, 0.95, 1.1, 1.35];
    var noise = [0.7, 1.3, 0.9, 1.15, 0.6, 1.25, 1.05, 0.8, 1.4, 0.95, 0.75, 1.2, 1.1, 0.85];
    var deliveries = { 2: 36, 5: 48, 7: 48, 12: 12, 15: 12 };
    var days = [], stock = 18;
    for (var i = -H; i < F; i++) {
      var t = today + i * DAY, d = new Date(t), k = Math.round((t - diwali) / DAY);
      var f = lift[String(k)] || 1, m = 3.2 * wd[d.getUTCDay()] * f, r = f > 1.5 ? 4 : 8;
      var sd = Math.sqrt(m + (m * m) / r);
      var row = { i: i, date: d, k: k, m: m, lo: Math.max(0, m - 1.645 * sd * 0.82), q25: Math.max(0, m - 0.674 * sd * 0.9),
        q75: m + 0.674 * sd * 1.1, hi: m + 1.645 * sd * 1.15, plan: m + 0.71 * sd,
        sold: i < 0 ? Math.max(0, Math.round(m * noise[(i + H) % noise.length])) : null,
        fest: k === -2 ? "Dhanteras" : k === 0 ? "Diwali" : null, deliv: 0, stock: null };
      if (i >= 0) { row.deliv = deliveries[i] || 0; stock += row.deliv; row.stock = stock; stock = Math.max(0, stock - m); row.after = stock; }
      days.push(row);
    }
    return { days: days, H: H, F: F };
  })();

  var fest = (function () {
    var el = $("#festival"), canvas = $("#fest-canvas");
    if (!el || !canvas) return { resize: function () {} };
    var ctx = canvas.getContext("2d"), warm = $(".fest-warm", el), copies = $$(".fc", el);
    var W = 0, Hh = 0, dpr = 1, lastP = reduced ? 1 : 0, visible = false, flickerOn = false;
    var C = { grid: "rgba(255,255,255,0.06)", ink: "#F5F3FA", ink2: "#BDB8CB", ink3: "#8F8A9C", sold: "rgba(143,138,156,0.6)",
      band: "rgba(143,134,255,0.16)", band2: "rgba(143,134,255,0.32)", plan: "#19B3A3", amber: "#D4A63A", lamp: "#F2B45A",
      cell: "#181818", cellLine: "#272727", today: "#8F86FF" };
    var MONO = "500 11px 'Geist Mono', ui-monospace, monospace", MONO_L = "500 13px 'Geist Mono', ui-monospace, monospace", UI = "500 12px 'Geist', system-ui, sans-serif";
    var WD = ["S", "M", "T", "W", "T", "F", "S"];
    var MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

    function resize() {
      var r = canvas.getBoundingClientRect();
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      W = Math.max(1, r.width); Hh = Math.max(1, r.height);
      canvas.width = Math.round(W * dpr); canvas.height = Math.round(Hh * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      draw(lastP);
    }
    function roundRect(x, y, w, h, r) {
      ctx.beginPath();
      ctx.moveTo(x + r, y); ctx.lineTo(x + w - r, y); ctx.quadraticCurveTo(x + w, y, x + w, y + r);
      ctx.lineTo(x + w, y + h - r); ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
      ctx.lineTo(x + r, y + h); ctx.quadraticCurveTo(x, y + h, x, y + h - r);
      ctx.lineTo(x, y + r); ctx.quadraticCurveTo(x, y, x + r, y); ctx.closePath();
    }
    function lampAt(x, y, glow, t) {
      if (glow <= 0) return;
      var fl = 1 + (reduced ? 0 : Math.sin(t / 140 + x) * 0.06 + Math.sin(t / 61 + y) * 0.04);
      ctx.save();
      ctx.globalAlpha = glow;
      ctx.fillStyle = "#8F6408";
      ctx.beginPath(); ctx.ellipse(x, y + 4, 11, 6, 0, 0, Math.PI); ctx.fill();
      ctx.fillStyle = "#B07A1A"; ctx.fillRect(x - 11, y + 2, 22, 2);
      ctx.fillStyle = C.lamp;
      ctx.shadowColor = "rgba(242,180,90,0.9)"; ctx.shadowBlur = 28 * fl;
      ctx.beginPath(); ctx.moveTo(x, y - 16 * fl); ctx.quadraticCurveTo(x + 6, y - 4, x, y); ctx.quadraticCurveTo(x - 6, y - 4, x, y - 16 * fl); ctx.fill();
      ctx.shadowBlur = 0;
      ctx.fillStyle = "#FFE9B0";
      ctx.beginPath(); ctx.moveTo(x, y - 9 * fl); ctx.quadraticCurveTo(x + 3, y - 3, x, y - 1); ctx.quadraticCurveTo(x - 3, y - 3, x, y - 9 * fl); ctx.fill();
      ctx.restore();
    }
    function draw(p, time) {
      lastP = p;
      if (!W) return;
      var t = time || performance.now();
      var narrow = W < 700;
      var Hs = narrow ? 6 : FEST.H, F = FEST.F, n = Hs + F;
      var days = FEST.days.slice(FEST.H - Hs);
      var x0 = narrow ? 36 : Math.max(64, (W - 1200) / 2 + 24), x1 = W - (narrow ? 16 : Math.max(40, (W - 1200) / 2 + 8));
      var col = (x1 - x0) / n;
      var yT = Hh * (narrow ? 0.4 : 0.36), yB = Hh * 0.64, s0 = Hh * 0.69, s1 = Hh * 0.77, c0 = Hh * 0.81, c1 = Hh * (narrow ? 0.87 : 0.9);
      var yMax = 80, sMax = 110;
      function X(i) { return x0 + (i + Hs + 0.5) * col; }
      function Y(v) { return yB - (v / yMax) * (yB - yT); }
      function SY(v) { return s1 - (v / sMax) * (s1 - s0); }
      var cur = reduced ? F : easeInOut(clamp((p - 0.06) / 0.62)) * F;   // forecast days shown
      var warmth = reduced ? 1 : clamp((p - 0.38) / 0.34);
      if (warm) warm.style.opacity = warmth.toFixed(3);

      ctx.clearRect(0, 0, W, Hh);
      var intro = reduced ? 1 : clamp(p / 0.06);
      ctx.globalAlpha = 0.25 + 0.75 * intro;

      // grid and scale
      ctx.font = MONO; ctx.textBaseline = "middle";
      for (var g = 0; g <= yMax; g += 20) {
        ctx.strokeStyle = C.grid; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(x0, Y(g) + 0.5); ctx.lineTo(x1, Y(g) + 0.5); ctx.stroke();
        ctx.fillStyle = C.ink3; ctx.textAlign = "right"; ctx.fillText(String(g), x0 - 8, Y(g));
      }
      ctx.textAlign = "left"; ctx.fillStyle = C.ink3; ctx.fillText("packs a day", x0, yT - 18);

      // festival columns (calendar facts, brighter once the forecast reaches them)
      days.forEach(function (d) {
        if (!d.fest) return;
        var reached = clamp(cur - d.i);
        ctx.fillStyle = "rgba(212,166,58," + (0.06 + 0.1 * reached).toFixed(3) + ")";
        ctx.fillRect(X(d.i) - col / 2, yT - 4, col, c1 - yT + 4);
      });

      // today divider
      var xd = x0 + Hs * col;
      ctx.strokeStyle = "rgba(255,255,255,0.22)"; ctx.setLineDash([3, 4]);
      ctx.beginPath(); ctx.moveTo(xd + 0.5, yT - 28); ctx.lineTo(xd + 0.5, c1); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle = C.ink2; ctx.font = MONO; ctx.textAlign = "left";
      ctx.fillText(narrow ? "Today" : "Today, 30 Oct", xd + 8, yT - 18);
      ctx.fillStyle = C.ink3; ctx.textAlign = "right"; if (!narrow) ctx.fillText("Sold", xd - 8, yT - 18);

      // what sold (history bars)
      days.forEach(function (d) {
        if (d.sold === null) return;
        var h = (d.sold / yMax) * (yB - yT), bw = Math.max(3, col * 0.42);
        ctx.fillStyle = C.sold; roundRect(X(d.i) - bw / 2, yB - h, bw, Math.max(h, 1), Math.min(3, bw / 2)); ctx.fill();
      });

      // forecast fan, revealed up to the cursor
      var fd = days.filter(function (d) { return d.i >= 0; });
      function upTo(key) {
        var pts = [];
        for (var j = 0; j < fd.length; j++) {
          var d = fd[j];
          if (d.i <= cur) pts.push([X(d.i), Y(d[key])]);
          else {
            var a = fd[j - 1];
            if (a && cur > a.i) { var fr = cur - a.i; pts.push([lerp(X(a.i), X(d.i), fr), Y(lerp(a[key], d[key], fr))]); }
            break;
          }
        }
        return pts;
      }
      function band(lo, hi, fill) {
        var top = upTo(hi), bot = upTo(lo);
        if (top.length < 2) return;
        ctx.fillStyle = fill; ctx.beginPath();
        top.forEach(function (q, k) { if (k) ctx.lineTo(q[0], q[1]); else ctx.moveTo(q[0], q[1]); });
        for (var k = bot.length - 1; k >= 0; k--) ctx.lineTo(bot[k][0], bot[k][1]);
        ctx.closePath(); ctx.fill();
      }
      function line(key, stroke, width, dash) {
        var pts = upTo(key);
        if (pts.length < 2) return;
        ctx.strokeStyle = stroke; ctx.lineWidth = width; ctx.lineJoin = "round"; ctx.setLineDash(dash || []);
        ctx.beginPath(); pts.forEach(function (q, k) { if (k) ctx.lineTo(q[0], q[1]); else ctx.moveTo(q[0], q[1]); }); ctx.stroke();
        ctx.setLineDash([]);
      }
      band("lo", "hi", C.band);
      band("q25", "q75", C.band2);
      var planFocus = reduced ? 0 : clamp((p - 0.52) / 0.05) * (1 - clamp((p - 0.73) / 0.05));
      line("plan", C.plan, 2 + planFocus * 1.5, [6, 5]);
      line("m", C.ink, 2.5);
      fd.forEach(function (d) {
        if (d.i > cur) return;
        ctx.fillStyle = C.ink; ctx.beginPath(); ctx.arc(X(d.i), Y(d.m), narrow ? 2 : 2.5, 0, Math.PI * 2); ctx.fill();
      });
      if (planFocus > 0.02) {
        var pd = fd[Math.min(fd.length - 1, 9)];
        ctx.globalAlpha = planFocus; ctx.font = MONO; ctx.fillStyle = C.plan; ctx.textAlign = "right";
        ctx.fillText("Planned for", X(pd.i) - 10, Y(pd.plan) - 12);
        ctx.globalAlpha = 1;
      }
      // the cursor
      if (!reduced && cur < F - 0.01) {
        var xc = lerp(xd, X(F - 1) + col / 2, cur / F);
        ctx.strokeStyle = "rgba(255,255,255,0.16)"; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(xc + 0.5, yT - 4); ctx.lineTo(xc + 0.5, s1); ctx.stroke();
      }

      // on the shelf: stock at the start of each day, with deliveries
      ctx.fillStyle = C.ink3; ctx.font = MONO; ctx.textAlign = "left";
      ctx.fillText("On the shelf", x0, s0 - 14);
      ctx.strokeStyle = C.grid; ctx.beginPath(); ctx.moveTo(x0, s1 + 0.5); ctx.lineTo(x1, s1 + 0.5); ctx.stroke();
      fd.forEach(function (d) {
        var show = clamp(cur - d.i + 1);
        if (show <= 0) return;
        var h = (d.stock / sMax) * (s1 - s0) * show, bw = col * 0.78;
        ctx.fillStyle = "rgba(25,179,163," + (0.16 + 0.22 * show).toFixed(3) + ")";
        roundRect(X(d.i) - bw / 2, s1 - h, bw, Math.max(h, 1), 2); ctx.fill();
        if (d.deliv) {
          var pop = easeOut(clamp((cur - d.i + 1) / 0.6));
          if (pop > 0) {
            ctx.save(); ctx.globalAlpha = pop;
            var lbl = "+" + d.deliv, tw = ctx.measureText(lbl).width + 10, by = SY(d.stock) - 22 - (1 - pop) * 8;
            ctx.fillStyle = C.amber; roundRect(X(d.i) - tw / 2, by, tw, 16, 4); ctx.fill();
            ctx.fillStyle = "#1E1B16"; ctx.textAlign = "center"; ctx.fillText(lbl, X(d.i), by + 8);
            ctx.restore();
          }
        }
      });
      var end = reduced ? 1 : clamp((p - 0.74) / 0.06);
      if (end > 0) {
        var dw = fd[9];
        ctx.save(); ctx.globalAlpha = end; ctx.fillStyle = C.ink; ctx.font = MONO; ctx.textAlign = "left";
        ctx.fillText(Math.round(dw.after) + " left after Diwali", X(dw.i) + col * 0.6, SY(dw.stock) - 4);
        ctx.restore();
      }

      // the calendar strip
      days.forEach(function (d) {
        var x = X(d.i) - col / 2 + 2, w = col - 4, h = c1 - c0;
        var past = d.i < 0, isToday = d.i === 0;
        ctx.fillStyle = C.cell; roundRect(x, c0, w, h, Math.min(8, w / 3)); ctx.fill();
        ctx.lineWidth = isToday || d.fest ? 1.5 : 1;
        ctx.strokeStyle = isToday ? C.today : d.fest ? C.amber : C.cellLine; ctx.stroke();
        var label = d.date.getUTCDate();
        var showNum = !narrow || isToday || d.fest || d.i % 3 === 0;
        if (showNum) {
          ctx.fillStyle = past ? C.ink3 : C.ink; ctx.font = narrow ? MONO : MONO_L; ctx.textAlign = "center";
          ctx.fillText(String(label), X(d.i), c0 + h * 0.4);
          ctx.fillStyle = C.ink3; ctx.font = MONO;
          ctx.fillText(WD[d.date.getUTCDay()], X(d.i), c0 + h * 0.74);
        }
      });
      ctx.fillStyle = C.ink3; ctx.font = MONO; ctx.textAlign = "left";
      ctx.fillText(MON[days[0].date.getUTCMonth()] + " to " + MON[days[days.length - 1].date.getUTCMonth()] + " 2026", x0, c1 + 16);

      // the lamps: lit as the forecast reaches each festival
      days.forEach(function (d) {
        if (!d.fest) return;
        var glow = reduced ? 1 : clamp((cur - d.i + 0.5) / 0.8);
        ctx.font = UI; ctx.fillStyle = glow > 0.05 ? C.lamp : C.ink3;
        ctx.globalAlpha = 0.45 + 0.55 * Math.max(glow, intro * 0.4);
        // On a narrow chart the two festivals are a few pixels apart: their names lean away from each other.
        if (narrow) { ctx.textAlign = d.k < 0 ? "right" : "left"; ctx.fillText(d.fest, X(d.i) + (d.k < 0 ? 4 : -4), yT - 52); }
        else { ctx.textAlign = "center"; ctx.fillText(d.fest, X(d.i), yT - 52); }
        ctx.globalAlpha = 1;
        lampAt(X(d.i), yT - 30, glow, t);
      });
      ctx.globalAlpha = 1;
      flickerOn = !reduced && visible && cur >= 7.3;
    }
    function updateCopies(p) {
      copies.forEach(function (c) {
        var a = parseFloat(c.getAttribute("data-in")), b = parseFloat(c.getAttribute("data-out"));
        c.classList.toggle("on", p >= a && p < b);
      });
    }
    scene(el, { pinned: true, k: coarse ? 0.22 : 0.12, update: function (p) { draw(p); updateCopies(p); } });
    // gentle flicker while the lamps are lit and the scene is on screen
    var last = 0;
    function flick(t) {
      if (flickerOn && t - last > 33) { last = t; draw(lastP, t); }
      if (visible) requestAnimationFrame(flick);
    }
    if ("IntersectionObserver" in window) {
      new IntersectionObserver(function (es) {
        es.forEach(function (e) { var was = visible; visible = e.isIntersecting; if (visible && !was && !reduced) requestAnimationFrame(flick); });
      }).observe(el);
    }
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(resize);
    resize();
    return { resize: resize };
  })();

  /* ================================================================ 6. the tagline (B11) */
  (function tagline() {
    var p = $("[data-words]");
    if (!p) return;
    var words = p.textContent.trim().split(/\s+/);
    p.textContent = "";
    var spans = words.map(function (w, i) {
      var s = document.createElement("span");
      s.className = "w"; s.textContent = w;
      p.appendChild(s);
      if (i < words.length - 1) p.appendChild(document.createTextNode(" "));
      return s;
    });
    scene(p, {
      fixed: function () {
        var r = p.getBoundingClientRect(), line = vh() * 0.72;
        return clamp((line - r.top) / (r.height + vh() * 0.3));
      },
      k: 0.3,
      update: function (t) {
        var on = Math.floor(t * (spans.length + 1));
        spans.forEach(function (s, i) { s.classList.toggle("on", i < on); });
      }
    });
  })();

  /* ================================================================ 7. the decision (signature) */
  /* DQ-Stack's own rule (dqstack/decide.py): a unit short costs its margin; a unit left
     over costs holding for the risk period, a markdown risk, and spoilage once cover
     outruns shelf life. The target is the demand quantile at short / (short + over),
     kept within the company's service floor and ceiling. */
  (function decide() {
    var box = $("#decide");
    if (!box) return;
    var RISK = 9, HOLD = 0.0022, MARKDOWN = 0.16, FLOOR = 0.5, CEIL = 0.995;
    var PRODUCTS = {
      diya: { price: 110, cost: 70, shelf: 365, mu: 28.8, r: 6, max: 80, unit: "packs", facts: "Sells for ₹110, costs ₹70, keeps a year." },
      katli: { price: 460, cost: 360, shelf: 5, mu: 12, r: 5, max: 32, unit: "boxes", facts: "Sells for ₹460, costs ₹360, keeps five days." }
    };
    var svg = $("#decide-chart"), input = $("#decide-target"), valueEl = $("#decide-value"), facts = $("#decide-facts"),
      oShort = $("#o-short"), oShortSub = $("#o-short-sub"), oOver = $("#o-over"), oOverSub = $("#o-over-sub"),
      oCost = $("#o-cost"), oCostSub = $("#o-cost-sub"), note = $("#decide-note"), best = $("#decide-best");
    var NS = "http://www.w3.org/2000/svg";
    var X0 = 44, X1 = 620, YT = 28, YB = 250;
    var model = null, S = 30, animating = null;

    function build(key) {
      var pr = PRODUCTS[key];
      var under = pr.price - pr.cost;
      var holding = pr.cost * HOLD * RISK, markdown = pr.cost * MARKDOWN;
      var excess = clamp((RISK - pr.shelf) / Math.max(pr.shelf, 1));
      var spoil = pr.shelf < 60 ? pr.cost * excess * 0.5 : 0;
      var over = holding + markdown + spoil;
      var q = under / (under + over), qUsed = clamp(q, FLOOR, CEIL);
      var N = pr.max * 4, pmf = [], pr0 = pr.r / (pr.r + pr.mu);
      pmf[0] = Math.pow(pr0, pr.r);
      for (var d = 1; d <= N; d++) pmf[d] = pmf[d - 1] * (d + pr.r - 1) / d * (1 - pr0);
      var short = [], left = [], cost = [], cdf = [], acc = 0;
      for (var s = 0; s <= pr.max; s++) {
        var sh = 0, lo = 0;
        for (d = 0; d <= N; d++) { if (d > s) sh += (d - s) * pmf[d]; else lo += (s - d) * pmf[d]; }
        short[s] = sh; left[s] = lo; cost[s] = under * sh + over * lo;
        acc += pmf[s]; cdf[s] = acc;
      }
      var sOpt = 0; for (s = 1; s <= pr.max; s++) if (cost[s] < cost[sOpt]) sOpt = s;
      var sDq = 0; while (sDq < pr.max && cdf[sDq] < qUsed) sDq++;
      return { key: key, pr: pr, under: under, over: over, q: q, qUsed: qUsed, pmf: pmf, short: short, left: left, cost: cost, sOpt: sOpt, sDq: sDq };
    }
    function el(name, attrs, parent) {
      var e = document.createElementNS(NS, name);
      for (var k in attrs) e.setAttribute(k, attrs[k]);
      (parent || svg).appendChild(e);
      return e;
    }
    function xOf(s) { return X0 + (s / model.pr.max) * (X1 - X0); }
    var g = {};
    function drawStatic() {
      while (svg.lastChild && svg.lastChild.nodeName !== "desc") svg.removeChild(svg.lastChild);
      var m = model, pr = m.pr;
      var pmax = 0; for (var d = 0; d <= pr.max; d++) pmax = Math.max(pmax, m.pmf[d]);
      var bw = (X1 - X0) / (pr.max + 1);
      g.bars = [];
      for (d = 0; d <= pr.max; d++) {
        var h = (m.pmf[d] / pmax) * (YB - YT) * 0.62;
        g.bars.push(el("rect", { x: (X0 + d * bw + 0.5).toFixed(2), y: (YB - h).toFixed(2), width: Math.max(1, bw - 1.5).toFixed(2), height: h.toFixed(2), rx: 1.5 }));
      }
      el("line", { x1: X0, y1: YB + 0.5, x2: X1, y2: YB + 0.5, stroke: "#313131" });
      var step = pr.max > 40 ? 10 : 4;
      for (var s = 0; s <= pr.max; s += step) {
        var t = el("text", { x: xOf(s), y: YB + 18, "text-anchor": "middle", fill: "#8F8A9C", "font-size": 11, "font-family": "Geist Mono, monospace" });
        t.textContent = String(s);
      }
      var cap = el("text", { x: X1, y: YB + 38, "text-anchor": "end", fill: "#8F8A9C", "font-size": 11, "font-family": "Geist, sans-serif" });
      cap.textContent = "Bars: how likely each level of demand is until the next delivery (" + pr.unit + ")";
      var cmax = 0; for (s = 0; s <= pr.max; s++) cmax = Math.max(cmax, m.cost[s]);
      g.cmax = cmax;
      var dpath = "";
      for (s = 0; s <= pr.max; s++) dpath += (s ? "L" : "M") + xOf(s).toFixed(2) + " " + yCost(m.cost[s]).toFixed(2);
      el("path", { d: dpath, fill: "none", stroke: "#F5F3FA", "stroke-width": 2, "stroke-linejoin": "round" });
      var lbl = el("text", { x: X0 + 4, y: YT - 8, fill: "#F5F3FA", "font-size": 12, "font-family": "Geist, sans-serif", "font-weight": 500 });
      lbl.textContent = "Cost of being wrong";
      // DQ-Stack's target
      var xd = xOf(m.sDq), yd = yCost(m.cost[m.sDq]);
      el("line", { x1: xd, y1: yd, x2: xd, y2: YB, stroke: "#19B3A3", "stroke-width": 1.5, "stroke-dasharray": "4 4" });
      el("circle", { cx: xd, cy: yd, r: 6, fill: "#19B3A3" });
      var dl = el("text", { x: xd + 10, y: yd - 16, fill: "#19B3A3", "font-size": 12, "font-family": "Geist, sans-serif", "font-weight": 500 });
      dl.textContent = "DQ-Stack: " + m.sDq;
      try {
        var bb = dl.getBBox();
        var back = el("rect", { x: bb.x - 4, y: bb.y - 2, width: bb.width + 8, height: bb.height + 4, rx: 4, fill: "#000000", opacity: 0.78 });
        svg.insertBefore(back, dl);
      } catch (e) { /* not rendered yet: the label still reads */ }
      if (m.sOpt !== m.sDq) {
        var xo = xOf(m.sOpt), yo = yCost(m.cost[m.sOpt]);
        el("circle", { cx: xo, cy: yo, r: 4, fill: "none", stroke: "#BDB8CB", "stroke-width": 1.5 });
        var ol = el("text", { x: xo - 8, y: yo + 20, "text-anchor": "end", fill: "#BDB8CB", "font-size": 11, "font-family": "Geist, sans-serif" });
        ol.textContent = "Lowest cost: " + m.sOpt;
      }
      // your target
      g.handle = el("line", { x1: 0, y1: YT, x2: 0, y2: YB, stroke: "#C17BFF", "stroke-width": 2 });
      g.knob = el("circle", { cx: 0, cy: 0, r: 7, fill: "#F5F3FA", stroke: "#9A4BE0", "stroke-width": 3 });
      g.you = el("text", { x: 0, y: YT + 4, fill: "#C17BFF", "font-size": 12, "font-family": "Geist, sans-serif", "font-weight": 500 });
    }
    function yCost(c) { return YB - 6 - (c / g.cmax) * (YB - YT - 20); }
    function render() {
      var m = model, pr = m.pr, s = Math.round(S);
      g.bars.forEach(function (b, d) { b.setAttribute("fill", d > S ? "rgba(240,98,77,0.5)" : "rgba(143,134,255,0.45)"); });
      var x = xOf(S), y = yCost(lerp(m.cost[Math.floor(S)], m.cost[Math.min(pr.max, Math.ceil(S))], S - Math.floor(S)));
      g.handle.setAttribute("x1", x); g.handle.setAttribute("x2", x);
      g.knob.setAttribute("cx", x); g.knob.setAttribute("cy", y);
      g.you.setAttribute("x", Math.min(x + 8, X1 - 60)); g.you.textContent = "You: " + s;
      valueEl.textContent = s + " " + pr.unit;
      input.value = String(s);
      input.setAttribute("aria-valuetext", s + " " + pr.unit);
      oShort.textContent = rupees(m.under * m.short[s]);
      oShortSub.textContent = "about " + m.short[s].toFixed(1) + " " + pr.unit + " missed";
      oOver.textContent = rupees(m.over * m.left[s]);
      oOverSub.textContent = "about " + m.left[s].toFixed(1) + " " + pr.unit + " left over";
      oCost.textContent = rupees(m.cost[s]);
      var diff = m.cost[s] - m.cost[m.sDq];
      oCostSub.textContent = s === m.sDq ? "DQ-Stack's target" : diff > 0 ? rupees(diff) + " more than DQ-Stack's target" : rupees(-diff) + " less, below your service floor";
    }
    function setNote() {
      var m = model, pct = Math.round(m.qUsed * 100);
      if (m.sOpt === m.sDq) note.textContent = "DQ-Stack would plan for " + m.sDq + ": enough until the next delivery about " + pct + " times in 100.";
      else note.textContent = "The lowest cost is at " + m.sOpt + ", but your business rules never plan below an even chance of having it, so DQ-Stack would plan for " + m.sDq + ".";
    }
    function choose(key) {
      model = build(key);
      facts.textContent = model.pr.facts;
      input.max = String(model.pr.max);
      S = Math.round(model.pr.mu);
      $$(".seg button", box).forEach(function (b) { b.setAttribute("aria-checked", String(b.getAttribute("data-product") === key)); });
      drawStatic(); setNote(); render();
    }
    function fromPointer(e) {
      var r = svg.getBoundingClientRect();
      var vx = ((e.clientX - r.left) / r.width) * 640;
      S = clamp(Math.round(((vx - X0) / (X1 - X0)) * model.pr.max), 0, model.pr.max);
      render();
    }
    var dragging = false;
    svg.addEventListener("pointerdown", function (e) { dragging = true; svg.setPointerCapture(e.pointerId); cancelAnimationFrame(animating); fromPointer(e); });
    svg.addEventListener("pointermove", function (e) { if (dragging) fromPointer(e); });
    svg.addEventListener("pointerup", function () { dragging = false; });
    svg.addEventListener("pointercancel", function () { dragging = false; });
    input.addEventListener("input", function () { cancelAnimationFrame(animating); S = parseInt(input.value, 10) || 0; render(); });
    $$(".seg button", box).forEach(function (b) {
      b.addEventListener("click", function () { choose(b.getAttribute("data-product")); });
      b.addEventListener("keydown", function (e) {
        if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
        var all = $$(".seg button", box), i = all.indexOf(b), nx = all[(i + (e.key === "ArrowRight" ? 1 : all.length - 1)) % all.length];
        nx.focus(); nx.click(); e.preventDefault();
      });
    });
    best.addEventListener("click", function () {
      var from = S, to = model.sDq, t0 = performance.now();
      if (reduced) { S = to; render(); return; }
      cancelAnimationFrame(animating);
      (function step(t) {
        var k = clamp((t - t0) / 500);
        S = lerp(from, to, 1 - Math.pow(1 - k, 3));
        render();
        if (k < 1) animating = requestAnimationFrame(step); else { S = to; render(); }
      })(t0);
    });
    choose("diya");
  })();

  /* ================================================================ 8. the team's day (pan) */
  var day = (function () {
    var el = $("#day"), strip = $("#day-strip");
    if (!el || !strip) return { panel: function () { return null; } };
    var panels = $$(".panel", strip), active = 0;
    function vertical() { return reduced || narrowQ.matches; }
    scene(el, {
      pinned: true,
      update: function (p) {
        if (vertical()) { strip.style.transform = ""; return; }
        var pe = easeInOut(clamp((p - 0.06) / 0.86));
        var dist = Math.max(0, strip.scrollWidth - window.innerWidth);
        strip.style.transform = "translate3d(" + (-pe * dist).toFixed(1) + "px,0,0)";
        active = Math.round(pe * (panels.length - 1));
      }
    });
    return { panel: function () { return vertical() ? null : panels[active]; } };
  })();

  /* ================================================================ 9 and 10. reveals, the deck, the queue */
  (function reveals() {
    var items = $$(".reveal, .deal");
    var queue = $("#queue"), msg = $("#queue-msg");
    function runQueue() {
      if (!queue || queue.dataset.done) return;
      queue.dataset.done = "1";
      var bills = $$("span", queue);
      if (reduced) { bills.forEach(function (b) { b.classList.add("sent"); }); msg.textContent = "Back online · all 5 sent, counted once"; return; }
      setTimeout(function () {
        msg.textContent = "Back online · sending";
        bills.forEach(function (b, i) { setTimeout(function () { b.classList.add("sent"); if (i === bills.length - 1) msg.textContent = "All 5 sent, each counted once"; }, 260 * (i + 1)); });
      }, 900);
    }
    if (!("IntersectionObserver" in window)) { items.forEach(function (i) { i.classList.add("in"); }); runQueue(); return; }
    var io = new IntersectionObserver(function (es) {
      es.forEach(function (e) {
        if (!e.isIntersecting) return;
        e.target.classList.add("in");
        io.unobserve(e.target);
        if (queue && e.target.contains(queue)) runQueue();
      });
    }, { threshold: 0.15 });
    items.forEach(function (i) { io.observe(i); });
  })();

  /* ================================================================ the shop clock and the bar */
  var clock = (function () {
    var box = $("#clock"), timeEl = $("#clock-time"), labelEl = $("#clock-label");
    var acts = $$("main > section[data-clock]");
    var navLinks = $$(".bar-links a[data-nav]");
    var NAV = { guess: "night", ledger: "night", night: "night", festival: "night", morning: "day", day: "day", questions: "questions" };
    var shown = timeEl ? timeEl.textContent + "|" + labelEl.textContent : "";
    var sections = $$("main > section");
    function short(el) { return narrowQ.matches && el.getAttribute("data-clock-short") ? el.getAttribute("data-clock-short") : el.getAttribute("data-clock-label"); }
    return {
      tick: function () {
        if (!box) return;
        var mid = vh() * 0.45, cur = null, sec = null;
        acts.forEach(function (a) { var r = a.getBoundingClientRect(); if (r.top <= mid) cur = a; });
        sections.forEach(function (a) { var r = a.getBoundingClientRect(); if (r.top <= mid && r.bottom > mid) sec = a; });
        if (!cur) cur = acts[0];
        var src = cur;
        if (cur && cur.id === "day") { var pnl = day.panel(); if (pnl) src = pnl; }
        var key = src.getAttribute("data-clock") + "|" + short(src);
        if (key !== shown) {
          shown = key;
          timeEl.textContent = src.getAttribute("data-clock");
          labelEl.textContent = short(src);
          box.classList.remove("tick"); void box.offsetWidth; box.classList.add("tick");
        }
        var navKey = sec ? NAV[sec.id] : null;
        navLinks.forEach(function (a) { if (a.getAttribute("data-nav") === navKey) a.setAttribute("aria-current", "true"); else a.removeAttribute("aria-current"); });
      }
    };
  })();

  narrowQ.addEventListener && narrowQ.addEventListener("change", function () { dirty = true; scenes.forEach(function (s) { s.force = true; }); request(); });
  dirty = true;
  scenes.forEach(function (s) { s.force = true; });
  request();
})();
