/* DQ-Stack public site.

   Progressive enhancement only. Without this file every page is complete and
   readable. With it: sections reveal once as they scroll into view, the menu
   opens on small screens, the navigation marks the section being read, the
   tagline comes up word by word, and the order calculator works.

   No scroll work runs unthrottled: reveals and the section marker use
   IntersectionObserver, and the tagline's scroll listener is attached only
   while the tagline is on screen and is batched through requestAnimationFrame. */

(function () {
  "use strict";

  window.dqReady = true;

  var root = document.documentElement;
  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  var hasIO = "IntersectionObserver" in window;

  /* ------------------------------------------------------------ reveals */

  function setupReveals() {
    var items = document.querySelectorAll("[data-reveal]");
    if (!items.length) return;
    if (!hasIO || reduceMotion.matches) {
      items.forEach(function (el) { el.classList.add("is-in"); });
      return;
    }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        entry.target.classList.add("is-in");
        io.unobserve(entry.target);
      });
    }, { rootMargin: "0px 0px -8% 0px", threshold: 0 });
    items.forEach(function (el) { io.observe(el); });
  }

  /* --------------------------------------------------------------- menu */

  function setupMenu() {
    var toggle = document.querySelector(".menu-toggle");
    var menu = document.getElementById("menu");
    if (!toggle || !menu) return;
    var outside = document.querySelectorAll("main, footer");
    var wide = window.matchMedia("(min-width: 1024px)");

    function setOpen(open, returnFocus) {
      toggle.setAttribute("aria-expanded", String(open));
      toggle.setAttribute("aria-label", open ? "Close menu" : "Open menu");
      menu.classList.toggle("is-open", open);
      root.classList.toggle("menu-open", open);
      outside.forEach(function (el) {
        if (open) el.setAttribute("inert", "");
        else el.removeAttribute("inert");
      });
      if (open) {
        var first = menu.querySelector("a");
        if (first) first.focus({ preventScroll: true });
      } else if (returnFocus) {
        toggle.focus();
      }
    }

    toggle.addEventListener("click", function () {
      setOpen(toggle.getAttribute("aria-expanded") !== "true", true);
    });
    menu.addEventListener("click", function (event) {
      if (event.target.closest("a")) setOpen(false, false);
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && menu.classList.contains("is-open")) setOpen(false, true);
    });
    var onWide = function (event) { if (event.matches) setOpen(false, false); };
    if (wide.addEventListener) wide.addEventListener("change", onWide);
    else if (wide.addListener) wide.addListener(onWide);
  }

  /* -------------------------------------------- where you are on the page */

  function setupSpy() {
    var links = document.querySelectorAll("a[data-spy], a.menu-link[href^='#']");
    if (!links.length || !hasIO) return;
    var byId = {};
    links.forEach(function (a) {
      var id = a.getAttribute("href").slice(1);
      (byId[id] = byId[id] || []).push(a);
    });
    var sections = [];
    Object.keys(byId).forEach(function (id) {
      var el = document.getElementById(id);
      if (el) sections.push(el);
    });
    document.querySelectorAll("[data-spy-group]").forEach(function (el) { sections.push(el); });
    sections.sort(function (a, b) {
      return a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1;
    });

    var visible = new Map();
    var current = null;
    function groupOf(el) { return el.getAttribute("data-spy-group") || el.id; }
    function mark(id) {
      if (id === current) return;
      current = id;
      links.forEach(function (a) { a.removeAttribute("aria-current"); });
      if (id && byId[id]) byId[id].forEach(function (a) { a.setAttribute("aria-current", "location"); });
    }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) { visible.set(entry.target, entry.isIntersecting); });
      var active = null;
      sections.forEach(function (el) { if (visible.get(el)) active = groupOf(el); });
      mark(active);
    }, { rootMargin: "-45% 0px -50% 0px" });
    sections.forEach(function (el) { io.observe(el); });
  }

  /* ------------------------------------------------- tagline, word by word
     Each word gets a position along the reading path (line, then distance
     along the line). As the block moves from 85% to 35% of the viewport
     height, words whose position has been passed come up, one at a time,
     in reading order. Once up, a word stays up. */

  function setupTagline() {
    var block = document.querySelector("[data-tagline]");
    if (!block) return;

    var words = [];
    block.querySelectorAll(".tl").forEach(function (line) {
      var parts = line.textContent.trim().split(/\s+/);
      line.textContent = "";
      parts.forEach(function (text, i) {
        var span = document.createElement("span");
        span.className = "w";
        span.textContent = text;
        line.appendChild(span);
        if (i < parts.length - 1) line.appendChild(document.createTextNode(" "));
        words.push(span);
      });
    });

    if (reduceMotion.matches || !hasIO) {
      words.forEach(function (w) { w.classList.add("on"); });
      return;
    }

    var positions = [];
    var next = 0;
    var ticking = false;
    var listening = false;

    function measure() {
      var lines = [];
      words.forEach(function (w) {
        var top = w.offsetTop;
        var line = lines.length && Math.abs(lines[lines.length - 1].top - top) < 4 ? lines[lines.length - 1] : null;
        if (!line) { line = { top: top, words: [] }; lines.push(line); }
        line.words.push(w);
      });
      positions = [];
      lines.forEach(function (line, li) {
        var first = line.words[0];
        var last = line.words[line.words.length - 1];
        var left = first.offsetLeft;
        var width = Math.max(1, last.offsetLeft + last.offsetWidth - left);
        line.words.forEach(function (w) {
          positions.push((li + (w.offsetLeft - left) / width) / lines.length);
        });
      });
    }

    function update() {
      ticking = false;
      var vh = window.innerHeight || root.clientHeight;
      var top = block.getBoundingClientRect().top;
      var progress = (vh * 0.85 - top) / (vh * 0.5);
      while (next < words.length && positions[next] < progress) {
        words[next].classList.add("on");
        next += 1;
      }
      if (next >= words.length) stop();
    }

    function onScroll() {
      if (ticking) return;
      ticking = true;
      window.requestAnimationFrame(update);
    }
    function start() {
      if (listening || next >= words.length) return;
      listening = true;
      window.addEventListener("scroll", onScroll, { passive: true });
      onScroll();
    }
    function stop() {
      if (!listening) return;
      listening = false;
      window.removeEventListener("scroll", onScroll);
    }

    measure();
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) start();
        else stop();
      });
      if (next >= words.length) io.disconnect();
    });
    io.observe(block);

    var resizing = false;
    window.addEventListener("resize", function () {
      if (resizing) return;
      resizing = true;
      window.requestAnimationFrame(function () { resizing = false; measure(); onScroll(); });
    });
  }

  /* ------------------------------------------------------ order calculator
     Suggested order = stock target - on hand - already on order, rounded up
     to the case pack, raised to the supplier minimum. Nothing is suggested
     when stock already covers the target. */

  function setupCalculator() {
    var form = document.getElementById("order-calc");
    if (!form) return;

    var MINUS = "−";
    var fields = {
      target: { empty: "Enter the stock target." },
      onhand: { empty: "Enter the units on hand." },
      onorder: { empty: "Enter the units already on order. Use 0 if there are none." },
      pack: { empty: "Enter the case pack.", least: 1, leastMsg: "A case holds at least 1 unit." },
      cost: { empty: "Enter the cost of one unit in rupees.", least: 1, leastMsg: "A unit costs at least ₹1." },
      min: { empty: "Enter the supplier's minimum order value in rupees. Use 0 if there is none." }
    };
    var keys = Object.keys(fields);
    keys.forEach(function (k) {
      fields[k].input = form.elements[k];
      fields[k].error = document.getElementById(fields[k].input.id + "-err");
      fields[k].input.removeAttribute("readonly");
    });

    var out = {};
    form.querySelectorAll("[data-out]").forEach(function (n) { out[n.getAttribute("data-out")] = n; });
    var steps = form.querySelector("[data-calc-steps]");
    var result = form.querySelector(".calc-result");
    var emptyNote = form.querySelector("[data-calc-empty]");
    var blurred = {};

    function fmt(n) { return n.toLocaleString("en-IN"); }

    function read(k) {
      var f = fields[k];
      var raw = f.input.value.trim().replace(/,/g, "");
      if (raw === "") return { error: f.empty, isEmpty: true };
      if (!/^\d+$/.test(raw)) return { error: "Use a whole number, like 12." };
      if (raw.length > 6) return { error: "Use a number below 10,00,000." };
      var value = parseInt(raw, 10);
      if (f.least && value < f.least) return { error: f.leastMsg };
      return { value: value };
    }

    function showError(k, message) {
      var f = fields[k];
      if (message) {
        f.error.innerHTML = '<svg class="icon icon-sm" aria-hidden="true" focusable="false"><use href="#i-warning-circle"/></svg>';
        f.error.appendChild(document.createTextNode(message));
        f.error.hidden = false;
        f.input.setAttribute("aria-invalid", "true");
      } else {
        f.error.hidden = true;
        f.error.textContent = "";
        f.input.removeAttribute("aria-invalid");
      }
    }

    function compute() {
      var v = {};
      var complete = true;
      keys.forEach(function (k) {
        var r = read(k);
        if (r.error) {
          complete = false;
          showError(k, r.isEmpty && !blurred[k] ? null : r.error);
        } else {
          v[k] = r.value;
          showError(k, null);
        }
      });

      steps.hidden = !complete;
      result.hidden = !complete;
      emptyNote.hidden = complete;
      if (!complete) return;

      var need = v.target - v.onhand - v.onorder;
      out.need.textContent = fmt(v.target) + " " + MINUS + " " + fmt(v.onhand) + " " + MINUS + " " +
        fmt(v.onorder) + " = " + (need < 0 ? MINUS + fmt(-need) : fmt(need));

      var cased = 0;
      var order = 0;
      if (need > 0) {
        // The supplier minimum is an order value in rupees. Below it, whole cases are
        // added until the order reaches it.
        cased = Math.ceil(need / v.pack) * v.pack;
        var caseValue = v.pack * v.cost;
        var casesForMin = Math.ceil(v.min / caseValue);
        order = Math.max(cased, casesForMin * v.pack);
        out["pack-label"].textContent = "Rounded up to whole cases of " + fmt(v.pack);
        out["min-label"].textContent = order === cased
          ? "Order value ₹" + fmt(cased * v.cost) + " meets the ₹" + fmt(v.min) + " minimum"
          : "Raised from " + fmt(cased) + " so the order reaches the ₹" + fmt(v.min) + " minimum";
      } else {
        out["pack-label"].textContent = "Nothing needed, so nothing to round up";
        out["min-label"].textContent = "No order, so no supplier minimum applies";
      }
      out.cased.textContent = fmt(cased);
      out.final.textContent = fmt(order);
      out.result.textContent = fmt(order);

      if (order === 0) {
        out.unit.textContent = "units. Stock already covers the target.";
      } else if (order % v.pack === 0) {
        var cases = order / v.pack;
        out.unit.textContent = (order === 1 ? "unit, " : "units, ") + fmt(cases) + (cases === 1 ? " case" : " cases");
      } else {
        out.unit.textContent = order === 1 ? "unit" : "units";
      }
    }

    form.addEventListener("input", compute);
    form.addEventListener("focusout", function (event) {
      var name = event.target && event.target.name;
      if (name && fields[name]) { blurred[name] = true; compute(); }
    });
    form.addEventListener("submit", function (event) { event.preventDefault(); compute(); });
    var reset = form.querySelector("[data-calc-reset]");
    if (reset) {
      reset.addEventListener("click", function () {
        keys.forEach(function (k) {
          fields[k].input.value = fields[k].input.getAttribute("data-default");
          blurred[k] = false;
        });
        compute();
      });
    }
    compute();
  }

  /* --------------------------------------------------------------- start */

  function init() {
    try { setupReveals(); } catch (e) { root.classList.remove("js"); }
    [setupMenu, setupSpy, setupTagline, setupCalculator].forEach(function (fn) {
      try { fn(); } catch (e) { if (window.console) console.error(e); }
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
