/* DQ-Stack landing page: "The Dive".

   Every pinned stage is a stack of photographs ("levels"). Level i+1 sits inside level
   i at an anchor point; going from one to the next is a single homothety (a scale about
   one fixed point) applied to both, so the incoming photograph grows out of exactly the
   place the outgoing one is zooming into. Matched framing does the rest.

   Scroll positions are measured in the section's own vh units (data-length), smoothed
   with a light lerp so the camera glides. Without this script, or with reduced motion,
   the page is a plain sequence of photographs and words. */
(function () {
  "use strict";
  var doc = document.documentElement;
  window.dqDive = true;
  if (!doc.classList.contains("js") || doc.classList.contains("rm")) return;

  /* ------------------------------------------------------------ helpers */
  function clamp(v, a, b) { return v < a ? a : v > b ? b : v; }
  function seg(p, a, b) { return clamp((p - a) / (b - a), 0, 1); }
  function smooth(t) { return t * t * (3 - 2 * t); }
  function lerp(a, b, t) { return a + (b - a) * t; }
  var EASE = {
    io: smooth,
    i: function (t) { return t * t; },
    o: function (t) { return 1 - (1 - t) * (1 - t); },
    l: function (t) { return t; }
  };
  /* Keyframes: [[pos, value, easing of the segment that starts here], ...] */
  function kf(table, p) {
    if (p <= table[0][0]) return table[0][1];
    for (var i = 0; i < table.length - 1; i++) {
      var a = table[i], b = table[i + 1];
      if (p <= b[0]) return lerp(a[1], b[1], (EASE[a[2] || "io"])((p - a[0]) / (b[0] - a[0])));
    }
    return table[table.length - 1][1];
  }
  /* The position at which a keyframed value first reaches v (for the ruler). */
  function kfFind(table, v) {
    for (var i = 0; i < table.length - 1; i++) {
      var a = table[i], b = table[i + 1];
      var lo = Math.min(a[1], b[1]), hi = Math.max(a[1], b[1]);
      if (v >= lo - 1e-6 && v <= hi + 1e-6 && a[1] !== b[1]) {
        var f = EASE[a[2] || "io"], t0 = 0, t1 = 1;
        for (var k = 0; k < 30; k++) {
          var m = (t0 + t1) / 2, val = lerp(a[1], b[1], f(m));
          if ((val < v) === (b[1] > a[1])) t0 = m; else t1 = m;
        }
        return lerp(a[0], b[0], (t0 + t1) / 2);
      }
    }
    return v <= table[0][1] ? table[0][0] : table[table.length - 1][0];
  }
  function nums(s) { return s ? s.split(",").map(Number) : null; }

  var W = 0, H = 0, wide = true;
  var touch = matchMedia("(pointer: coarse)").matches;
  var FRAC = 0.36;               // the next photograph's size inside this one

  /* ------------------------------------------------------------ levels */
  function Level(el) {
    this.el = el;
    this.w = +el.dataset.w; this.h = +el.dataset.h;
    var f = nums(el.dataset.focus) || [this.w / 2, this.h / 2];
    this.fx = f[0]; this.fy = f[1];
    this.anchor = nums(el.dataset.anchor);
    this.fit = nums(el.dataset.fit);
    this.boxes = [].slice.call(el.children).filter(function (c) { return c.classList.contains("box"); });
    this.vis = null; this.tf = ""; this.op = -1; this.fe = -1;
  }
  Level.prototype.layout = function () {
    var c = Math.max(W / this.w, H / this.h);
    var fx = this.fx, fy = this.fy;
    if (this.fit) {
      var fw = this.fit[2] - this.fit[0];
      if (fw * c > W * 0.94) c = (W * 0.94) / fw;
      fx = (this.fit[0] + this.fit[2]) / 2;
    }
    var bw = this.w * c, bh = this.h * c;
    var left = W / 2 - fx * c, top = H / 2 - fy * c;
    left = bw >= W ? clamp(left, W - bw, 0) : (W - bw) / 2;
    if (bh >= H) top = clamp(top, H - bh, 0);
    this.c = c; this.left = left; this.top = top;
    for (var i = 0; i < this.boxes.length; i++) {
      var s = this.boxes[i].style;
      s.left = left + "px"; s.top = top + "px"; s.width = bw + "px"; s.height = bh + "px";
    }
  };
  Level.prototype.map = function (x, y) { return [this.left + x * this.c, this.top + y * this.c]; };
  Level.prototype.set = function (on, s, ox, oy, op, fe, blur) {
    var st = this.el.style;
    if (!on) { if (this.vis !== false) { st.visibility = "hidden"; this.vis = false; } return; }
    if (this.vis !== true) { st.visibility = "visible"; this.vis = true; }
    var tf = s === 1 ? "none" : "translate3d(" + ox.toFixed(1) + "px," + oy.toFixed(1) + "px,0) scale(" + s.toFixed(5) + ") translate3d(" + (-ox).toFixed(1) + "px," + (-oy).toFixed(1) + "px,0)";
    if (tf !== this.tf) { st.transform = tf; this.tf = tf; }
    op = Math.round(op * 1000) / 1000;
    if (op !== this.op) { st.opacity = op; this.op = op; }
    fe = Math.round(fe * 10) / 10;
    if (fe !== this.fe) {
      if (fe > 0) {
        var g = "linear-gradient(to right, transparent, #000 " + fe + "%, #000 " + (100 - fe) + "%, transparent)";
        var v = "linear-gradient(to bottom, transparent, #000 " + fe + "%, #000 " + (100 - fe) + "%, transparent)";
        st.webkitMaskImage = g + "," + v; st.maskImage = g + "," + v;
        st.webkitMaskComposite = "source-in"; st.maskComposite = "intersect";
      } else { st.webkitMaskImage = ""; st.maskImage = ""; }
      this.fe = fe;
    }
    var b = blur > 0.05 ? "blur(" + blur.toFixed(1) + "px)" : "";
    if (b !== this.blur) { st.filter = b; this.blur = b; }
  };

  function Camera(stage) {
    this.levels = [].slice.call(stage.querySelectorAll(":scope > .lvl")).map(function (el) { return new Level(el); });
    this.links = [];
  }
  Camera.prototype.layout = function () {
    var L = this.levels;
    L.forEach(function (l) { l.layout(); });
    this.links = [];
    for (var i = 0; i < L.length - 1; i++) {
      var a = L[i + 1].anchor, P = L[i].map(a[0], a[1]);
      var x0 = P[0] - FRAC * W / 2, y0 = P[1] - FRAC * H / 2;
      this.links.push({ K: 1 / FRAC, ox: x0 / (1 - FRAC), oy: y0 / (1 - FRAC) });
    }
  };
  Camera.prototype.render = function (cam) {
    var L = this.levels, last = L.length - 1;
    cam = clamp(cam, 0, last);
    var n = Math.min(Math.floor(cam), last), t = cam - n;
    if (n === last) t = 0;
    var k = this.links[n];
    for (var i = 0; i <= last; i++) {
      if (i === n) {
        if (t > 0) L[i].set(true, Math.pow(k.K, t), k.ox, k.oy, 1, 0, wide ? 5 * seg(t, 0.45, 1) : 0);
        else L[i].set(true, 1, 0, 0, 1, 0, 0);
      } else if (i === n + 1 && t > 0) {
        L[i].set(true, Math.pow(k.K, t - 1), k.ox, k.oy, smooth(seg(t, 0.25, 0.75)), 16 * (1 - seg(t, 0.55, 0.98)), 0);
      } else L[i].set(false);
    }
    this.cam = cam;
  };

  /* ------------------------------------------------------------ the city at night */
  var IMG = {};
  function img(src) {
    if (!IMG[src]) { IMG[src] = new Image(); IMG[src].decoding = "async"; IMG[src].src = src; }
    return IMG[src];
  }
  function City(canvas) {
    this.cv = canvas; this.ctx = canvas.getContext("2d");
    this.off = img("/site-assets/img/dive/city-off.webp");
    this.lit = img("/site-assets/img/dive/city-lit.webp");
    this.mask = document.createElement("canvas"); this.mask.width = 60; this.mask.height = 40;
    this.mctx = this.mask.getContext("2d");
    this.work = document.createElement("canvas"); this.work.width = 1200; this.work.height = 800;
    this.wctx = this.work.getContext("2d");
    this.level = -1;
    /* Each block of the city gets the moment its lights come on: a few anywhere from the
       start (shops still open at dusk), the rest in a wave out from the shop we left. */
    var thr = this.thr = new Float32Array(60 * 40), seed = 7;
    function rnd() { seed = (seed * 16807) % 2147483647; return (seed - 1) / 2147483646; }
    var ox = 690 / 20, oy = 560 / 20, maxd = Math.hypot(60, 40 * 1.25) * 0.75;
    for (var j = 0; j < 40; j++) for (var i = 0; i < 60; i++) {
      var r = rnd(), d = Math.hypot(i + 0.5 - ox, (j + 0.5 - oy) * 1.25) / maxd;
      thr[j * 60 + i] = r < 0.16 ? r * 0.9 : 0.2 + 0.72 * clamp(0.78 * d + 0.22 * rnd(), 0, 1);
    }
    var self = this;
    [this.off, this.lit].forEach(function (im) {
      if (!im.complete) im.addEventListener("load", function () { self.level = -1; self.draw(self.want || 0.18); });
    });
  }
  City.prototype.draw = function (level) {
    this.want = level;
    if (Math.abs(level - this.level) < 0.002) return;
    if (!this.off.complete || !this.lit.complete || !this.off.naturalWidth) return;
    this.level = level;
    var m = this.mctx, thr = this.thr;
    m.clearRect(0, 0, 60, 40);
    m.fillStyle = "#000";
    for (var j = 0; j < 40; j++) for (var i = 0; i < 60; i++) {
      var a = clamp((level - thr[j * 60 + i]) / 0.06, 0, 1);
      if (a > 0) { m.globalAlpha = a; m.fillRect(i, j, 1, 1); }
    }
    m.globalAlpha = 1;
    var w = this.wctx;
    w.globalCompositeOperation = "copy";
    w.drawImage(this.lit, 0, 0, 1200, 800);
    w.globalCompositeOperation = "destination-in";
    w.imageSmoothingEnabled = true;
    w.drawImage(this.mask, 0, 0, 1200, 800);
    var c = this.ctx;
    c.clearRect(0, 0, 1200, 800);
    c.drawImage(this.off, 0, 0, 1200, 800);
    c.drawImage(this.work, 0, 0);
  };

  /* ------------------------------------------------------------ scenes */
  function Scene(el) {
    this.el = el; this.len = +el.dataset.length || 100;
    el.style.setProperty("--len", this.len);
    this.cues = [].slice.call(el.querySelectorAll(".cue")).map(function (c) {
      var at = +c.dataset.at, span = +c.dataset.span;
      var fi = c.dataset.fadein != null ? +c.dataset.fadein : Math.min(30, span * 0.25);
      var fo = c.dataset.fadeout != null ? +c.dataset.fadeout : Math.min(30, span * 0.25);
      return { el: c, at: at, span: span, fi: fi, fo: fo, op: -1, y: 0 };
    });
  }
  Scene.prototype.measure = function () {
    var r = this.el.getBoundingClientRect();
    this.top = r.top + window.scrollY;
    this.height = this.el.offsetHeight;
    this.unit = this.height / this.len;
  };
  Scene.prototype.pos = function (y) { return (y - this.top) / this.unit; };
  Scene.prototype.active = function (y) { return y > this.top - H * 1.1 && y < this.top + this.height + H * 0.1; };
  Scene.prototype.renderCues = function (p) {
    for (var i = 0; i < this.cues.length; i++) {
      var c = this.cues[i];
      var inT = c.fi > 0 ? seg(p, c.at, c.at + c.fi) : (p >= c.at - 1 || c.at === 0 ? 1 : 0);
      var outT = c.fo > 0 ? seg(p, c.at + c.span - c.fo, c.at + c.span) : 0;
      if (c.at === 0 && c.fi === 0) inT = 1;
      var op = Math.min(smooth(inT), 1 - smooth(outT));
      var y = (1 - smooth(inT)) * 18 - smooth(outT) * 18;
      op = Math.round(op * 1000) / 1000;
      if (op !== c.op || y !== c.y) {
        c.el.style.opacity = op;
        c.el.style.translate = "0 " + y.toFixed(1) + "px";
        c.el.style.visibility = op > 0 ? "visible" : "hidden";
        c.op = op; c.y = y;
      }
    }
  };

  /* ------------------------------------------------------------ the dive */
  var diveEl = document.getElementById("dive");
  var dive = new Scene(diveEl);
  var diveCam = new Camera(document.getElementById("dive-stage"));
  var heroCity = new City(diveEl.querySelector("canvas.city"));
  var heroWord = diveEl.querySelector(".hero-word");
  var heroBoxes = diveCam.levels[0].boxes;
  var roll = diveEl.querySelector(".roll"), spill = diveEl.querySelector(".spill");
  var wipe = diveEl.querySelector(".wipe"), wipeLine = diveEl.querySelector(".wipe-line");
  var ring = diveEl.querySelector(".ring"), slot = diveEl.querySelector(".slot"), dim = diveEl.querySelector(".dim");
  var labelLevel = diveCam.levels[5];

  var DIVE_CAM = [[0, 0, "l"], [45, 0, "i"], [190, 1, "o"], [300, 2, "l"], [410, 2, "i"], [475, 3, "o"], [540, 4, "l"], [705, 4, "io"], [790, 5, "l"], [1040, 5]];
  var WIPE = [[0, 0, "l"], [548, 0, "io"], [602, 0.5, "l"], [640, 0.5, "io"], [698, 1, "l"], [1040, 1]];

  /* The signature: the paper label lifts off and becomes the forecast card. */
  var card = document.getElementById("card"), face = document.getElementById("card-face");
  var cardY = card.querySelector(".card-y"), rows = [].slice.call(card.querySelector(".card-body").children);
  var cardN = document.getElementById("card-n");
  var fin = { x: 0, y: 0, w: 380, h: 420 }, cardState = { op: -1, n: -1 };
  function layoutCard() {
    card.style.transform = "none";
    if (W >= 900) {
      fin.w = 380;
      card.style.width = fin.w + "px";
      fin.h = card.offsetHeight;
      fin.x = W - fin.w - Math.max(110, W * 0.1);
      fin.y = (H - fin.h) / 2 + 20;
    } else {
      fin.w = Math.min(340, W - 32);
      card.style.width = fin.w + "px";
      fin.h = card.offsetHeight;
      fin.x = (W - fin.w) / 2;
      fin.y = Math.max(76, H * 0.1);
    }
  }
  function renderCard(m) {
    if (m <= 0) {
      if (cardState.op !== 0) { card.style.opacity = 0; face.style.opacity = 0; cardState.op = 0; }
      ring.style.opacity = 0; slot.style.opacity = 0; dim.style.opacity = (0.3 * seg(m, -0.12, 0)).toFixed(3);
      return;
    }
    var a = labelLevel.map(430, 852), b = labelLevel.map(1058, 1040);
    var k = smooth(seg(m, 0.12, 0.5));
    var x = lerp(a[0], fin.x, k), y = lerp(a[1], fin.y, k);
    var w = lerp(b[0] - a[0], fin.w, k), h = lerp(b[1] - a[1], fin.h, k);
    card.style.transform = "translate3d(" + x.toFixed(1) + "px," + y.toFixed(1) + "px,0) scale(" + (w / fin.w).toFixed(4) + "," + (h / fin.h).toFixed(4) + ")";
    var op = seg(m, 0.05, 0.1);
    card.style.opacity = op; cardState.op = op;
    cardY.style.opacity = 1 - seg(m, 0.16, 0.42);
    face.style.transform = "translate3d(" + x.toFixed(1) + "px," + y.toFixed(1) + "px,0) scale(" + (w / 300).toFixed(4) + ")";
    face.style.opacity = op * (1 - seg(m, 0.14, 0.28));
    for (var i = 0; i < rows.length; i++) {
      var t = smooth(seg(m, 0.48 + i * 0.05, 0.58 + i * 0.05));
      rows[i].style.opacity = t;
      rows[i].style.transform = "translateY(" + ((1 - t) * 8).toFixed(1) + "px)";
    }
    var n = Math.round(24 * smooth(seg(m, 0.72, 0.9)));
    if (n !== cardState.n) { cardN.textContent = n; cardState.n = n; }
    ring.style.opacity = smooth(seg(m, 0, 0.04)) * (1 - seg(m, 0.08, 0.12));
    dim.style.opacity = (0.3 + 0.3 * k).toFixed(3);
    slot.style.opacity = seg(m, 0.1, 0.16).toFixed(3);
  }

  var pointer = { x: 0, y: 0, tx: 0, ty: 0 };
  function renderDive(p) {
    diveCam.render(kf(DIVE_CAM, p));
    heroCity.draw(0.18);

    /* hero depth: the sky drifts slower than the city; the pointer tilts the planes */
    if (p < 120) {
      var px = pointer.x, py = pointer.y, sp = Math.max(p, 0);
      heroBoxes[0].style.transform = "translate3d(" + (-px * 6).toFixed(1) + "px," + (sp * 0.5 - py * 4).toFixed(1) + "px,0)";
      heroBoxes[1].style.transform = "translate3d(" + (px * 12).toFixed(1) + "px," + (py * 8).toFixed(1) + "px,0)";
      heroWord.style.transform = "translate3d(" + (px * 3).toFixed(1) + "px," + (sp * 0.9).toFixed(1) + "px,0)";
      heroWord.style.opacity = 1 - smooth(seg(p, 40, 95));
    }

    /* the shutter rolls up */
    var u = smooth(seg(p, 318, 402));
    roll.style.transform = "translate3d(0," + (-u * 101).toFixed(2) + "%,0)";
    spill.style.transform = roll.style.transform;
    spill.style.opacity = Math.sin(u * Math.PI).toFixed(3);

    /* two ways to get it wrong */
    var w = kf(WIPE, p);
    var cut = ((1 - w) * 100).toFixed(2);
    wipe.style.clipPath = "inset(0 0 0 " + cut + "%)";
    wipeLine.style.left = cut + "%";
    wipeLine.style.opacity = Math.min(1, w * 12, (1 - w) * 12).toFixed(3);

    renderCard(seg(p, 795, 900));
    dive.renderCues(p);
  }

  /* ------------------------------------------------------------ the pull back (peak) */
  var peakEl = document.getElementById("peak");
  var peak = new Scene(peakEl);
  var peakCam = new Camera(document.getElementById("peak-stage"));
  var peakCity = new City(peakEl.querySelector("canvas.city"));
  var glow = peakEl.querySelector(".glow"), stamp = peakEl.querySelector(".stamp");
  var PEAK_CAM = [[0, 3, "l"], [30, 3, "io"], [100, 2, "l"], [135, 2, "i"], [195, 1, "o"], [250, 0, "l"], [460, 0]];
  var LIGHTS = [[0, 0.18, "l"], [246, 0.18, "io"], [335, 1.02, "l"], [460, 1.02]];
  function renderPeak(p) {
    peakCam.render(kf(PEAK_CAM, p));
    peakCity.draw(kf(LIGHTS, p));
    glow.style.opacity = (smooth(seg(p, 270, 345)) * 0.9).toFixed(3);
    var s = seg(p, -50, 10);
    stamp.style.opacity = smooth(s).toFixed(3);
    stamp.style.transform = "scale(" + (1.6 - 0.6 * smooth(s)).toFixed(3) + ") rotate(-5deg)";
    peak.renderCues(p);
  }

  /* ------------------------------------------------------------ how: the sideways track */
  var howEl = document.getElementById("how"), how = new Scene(howEl);
  var track = document.getElementById("how-track"), howItems = [].slice.call(track.children);
  var trackSpan = 0, hereIdx = -1;
  function layoutHow() {
    track.style.transform = "";
    trackSpan = Math.max(0, track.scrollWidth - W);
  }
  function renderHow(p) {
    if (!wide) return;
    var t = smooth(seg(p, 30, how.len - 130));
    track.style.transform = "translate3d(" + (-t * trackSpan).toFixed(1) + "px,0,0)";
    var idx = Math.round(t * (howItems.length - 1));
    if (idx !== hereIdx) {
      howItems.forEach(function (li, i) { li.classList.toggle("here", i === idx); });
      hereIdx = idx;
    }
  }

  /* ------------------------------------------------------------ team: the deck */
  var teamEl = document.getElementById("team"), team = new Scene(teamEl);
  var deck = [].slice.call(document.querySelectorAll(".deck-card"));
  var tilt = [-2.2, 1.6, -1.2, 2];
  deck.forEach(function (c, i) { c.style.setProperty("--r", tilt[i % 4] + "deg"); });
  function renderTeam(p) {
    if (!wide) { deck.forEach(function (c) { c.style.opacity = ""; c.style.transform = ""; }); return; }
    deck.forEach(function (c, i) {
      var t = smooth(seg(p, 10 + i * 26, 40 + i * 26));
      c.style.opacity = t.toFixed(3);
      c.style.transform = "translate3d(0," + ((1 - t) * 40).toFixed(1) + "px,0) rotate(" + ((1 - t) * tilt[i % 4]).toFixed(2) + "deg)";
    });
  }

  /* ------------------------------------------------------------ close */
  var readyEl = document.getElementById("ready"), readyBg = document.getElementById("ready-bg");
  function renderReady() {
    var r = readyEl.getBoundingClientRect();
    if (r.top > H || r.bottom < 0) return;
    var t = clamp(1 - r.top / H, 0, 1);
    readyBg.style.transform = "scale(" + (1.1 - 0.1 * smooth(t)).toFixed(4) + ")";
  }

  /* ------------------------------------------------------------ the altitude ruler */
  var alt = document.getElementById("alt"), altDot = document.getElementById("alt-dot"), altRead = document.getElementById("alt-read");
  var altLinks = [].slice.call(alt.querySelectorAll("a[data-cam]"));
  var ALT_M = [2000, 200, 12, 4, 1, 0.06];
  var tickY = [];
  function layoutRuler() {
    var base = alt.querySelector(".alt-list").getBoundingClientRect().top;
    tickY = altLinks.map(function (a) { var r = a.getBoundingClientRect(); return r.top - base + 6; });
  }
  function fmtAlt(m) {
    if (m >= 1000) return (m / 1000).toFixed(1).replace(/\.0$/, "") + " km";
    if (m >= 1) return Math.round(m) + " m";
    return Math.max(1, Math.round(m * 100)) + " cm";
  }
  var rulerOn = false, rulerHere = -1;
  function renderRuler(cam, on) {
    if (on !== rulerOn) { alt.classList.toggle("on", on); rulerOn = on; }
    if (!on || !tickY.length) return;
    var n = Math.min(Math.floor(cam), ALT_M.length - 1), t = cam - n;
    if (n >= ALT_M.length - 1) { n = ALT_M.length - 1; t = 0; }
    var y = t > 0 ? lerp(tickY[n], tickY[n + 1], t) : tickY[n];
    var m = t > 0 ? ALT_M[n] * Math.pow(ALT_M[n + 1] / ALT_M[n], t) : ALT_M[n];
    altDot.style.setProperty("--y", y.toFixed(1) + "px");
    altRead.style.setProperty("--y", (y - 8).toFixed(1) + "px");
    altRead.textContent = fmtAlt(m);
    var here = Math.round(cam);
    if (here !== rulerHere) { altLinks.forEach(function (a, i) { a.classList.toggle("here", i === here); }); rulerHere = here; }
  }
  /* Going to a height: the camera glides there through the lerp. */
  altLinks.forEach(function (a) {
    a.addEventListener("click", function (e) {
      e.preventDefault();
      var p = kfFind(DIVE_CAM, +a.dataset.cam);
      if (+a.dataset.cam === 0) p = 0;
      if (+a.dataset.cam === 4) p = 560;
      if (+a.dataset.cam === 5) p = 920;
      window.scrollTo(0, dive.top + p * dive.unit);
    });
  });
  /* Peak levels on the ruler: city, street, shop, label. */
  var PEAK_TO_RULER = [0, 1, 2, 5];
  function peakRulerCam(c) {
    var n = Math.min(Math.floor(c), 2), t = c - n;
    return lerp(PEAK_TO_RULER[n], PEAK_TO_RULER[n + 1], t);
  }

  /* The headline lives between the sky and the city: it must end above the tower tops,
     so only the spire passes in front of it. */
  function fitHero() {
    heroWord.style.fontSize = "";
    var limit = diveCam.levels[0].map(565, 200)[1] - 6;
    var fs = parseFloat(getComputedStyle(heroWord).fontSize), guard = 0;
    while (heroWord.offsetTop + heroWord.offsetHeight > limit && fs > 26 && guard++ < 40) {
      fs *= 0.95; heroWord.style.fontSize = fs.toFixed(1) + "px";
    }
  }

  /* ------------------------------------------------------------ loop */
  var target = window.scrollY, cur = target, raf = 0;
  function layout() {
    W = window.innerWidth;
    H = document.getElementById("dive-stage").clientHeight || window.innerHeight;
    wide = W >= 900;
    [dive, peak, how, team].forEach(function (s) { s.measure(); });
    diveCam.layout(); peakCam.layout();
    fitHero(); layoutCard(); layoutHow(); layoutRuler();
    render(cur, true);
  }
  function render(y, force) {
    var pd = dive.pos(y), pp = peak.pos(y);
    var inDive = dive.active(y), inPeak = peak.active(y);
    if (inDive || force) renderDive(clamp(pd, -5, dive.len));
    if (inPeak || force) renderPeak(clamp(pp, -60, peak.len));
    if (how.active(y) || force) renderHow(how.pos(y));
    if (team.active(y) || force) renderTeam(team.pos(y));
    renderReady();
    var onDive = pd > -20 && pd < dive.len - 95, onPeak = pp > -20 && pp < peak.len - 95;
    renderRuler(onDive ? diveCam.cam : onPeak ? peakRulerCam(peakCam.cam) : 0, (onDive || onPeak) && W >= 520);
  }
  function frame() {
    raf = 0;
    var k = touch ? 0.28 : 0.15;
    pointer.x += (pointer.tx - pointer.x) * 0.08; pointer.y += (pointer.ty - pointer.y) * 0.08;
    cur += (target - cur) * k;
    if (Math.abs(target - cur) < 0.4) cur = target;
    render(cur);
    if (cur !== target || Math.abs(pointer.tx - pointer.x) > 0.002 || Math.abs(pointer.ty - pointer.y) > 0.002) raf = requestAnimationFrame(frame);
  }
  function kick() { if (!raf) raf = requestAnimationFrame(frame); }

  window.addEventListener("scroll", function () { target = window.scrollY; kick(); }, { passive: true });
  if (!touch) {
    window.addEventListener("pointermove", function (e) {
      pointer.tx = (e.clientX / W) * 2 - 1; pointer.ty = (e.clientY / H) * 2 - 1; kick();
    }, { passive: true });
  }
  var rz = 0, lastW = 0;
  window.addEventListener("resize", function () {
    clearTimeout(rz);
    rz = setTimeout(function () {
      if (touch && window.innerWidth === lastW) return;  // a phone's address bar, not a real resize
      lastW = window.innerWidth; layout();
    }, 120);
  });

  /* The rest of the photographs load once the first screen is in. */
  function warm() {
    /* Decoded ahead, so a photograph's first appearance mid-dive costs no frame. */
    [].forEach.call(document.querySelectorAll(".stage img"), function (im) {
      im.loading = "eager";
      if (im.decode) im.decode().catch(function () {});
    });
  }
  if (document.readyState === "complete") setTimeout(warm, 300);
  else window.addEventListener("load", function () { setTimeout(warm, 300); });

  lastW = window.innerWidth;
  layout();
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(function () { layoutCard(); layoutRuler(); render(cur, true); });
})();
