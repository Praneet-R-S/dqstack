/* ============================================================================
   Shared helpers: formatting, colour, and the theme-aware token reader.

   Colours are read from CSS custom properties rather than duplicated here, so
   the canvas and the DOM cannot drift apart when the theme changes. There is
   exactly one source of truth for every hue, and it is tokens.css.
   ============================================================================ */

const U = (() => {
  const css = (name) =>
    getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  let cache = {};
  const TOKENS = [
    "--bg-base", "--bg-surface", "--bg-raised", "--bg-inset", "--bg-overlay",
    "--border-subtle", "--border-strong",
    "--text-primary", "--text-secondary", "--text-tertiary",
    "--series-forecast", "--series-actual", "--series-override", "--series-prior",
    "--state-risk", "--state-excess", "--state-attention", "--state-ok",
    // Registered so a canvas renderer can ask for the action colour. tk() returns a
    // grey fallback for any unregistered name, which is silent -- the chart simply
    // paints grey -- so the list is the contract, not a convenience.
    "--ui-accent",
  ];
  function refresh() {
    cache = {};
    TOKENS.forEach((t) => (cache[t] = css(t)));
    cache.fontNum = css("--font-num") || "monospace";
    cache.fontUI = css("--font-ui") || "sans-serif";
  }
  const tk = (name) => cache[name] || "#888";

  /* ---- numeric formatting. Every numeral in the product goes through here so
         alignment and rounding are consistent across grid, chart and tables. */
  const nf = (v, d = 0) =>
    v === null || v === undefined || Number.isNaN(v)
      ? "—"
      : Number(v).toLocaleString("en-US", {
          minimumFractionDigits: d,
          maximumFractionDigits: d,
        });

  const compact = (v) => {
    if (v === null || v === undefined || Number.isNaN(v)) return "—";
    const a = Math.abs(v);
    if (a >= 1e9) return (v / 1e9).toFixed(a >= 1e10 ? 0 : 1) + "B";
    if (a >= 1e6) return (v / 1e6).toFixed(a >= 1e7 ? 0 : 1) + "M";
    if (a >= 1e4) return (v / 1e3).toFixed(0) + "k";
    if (a >= 1e3) return (v / 1e3).toFixed(1) + "k";
    return nf(v, a < 10 && a % 1 !== 0 ? 1 : 0);
  };

  // Sign outside the currency symbol: "-$223", not "$-223".
  const money = (v, d = 0) =>
    v === null || v === undefined || Number.isNaN(v)
      ? "—"
      : (v < 0 ? "-$" : "$") + compact(Math.abs(v), d);

  const pct = (v, d = 1) =>
    v === null || v === undefined || Number.isNaN(v) ? "—" : (v * 100).toFixed(d) + "%";

  const signed = (v, d = 0) =>
    v === null || v === undefined || Number.isNaN(v)
      ? "—"
      : (v > 0 ? "+" : "") + nf(v, d);

  const signedPct = (v, d = 1) =>
    v === null || v === undefined || Number.isNaN(v)
      ? "—"
      : (v > 0 ? "+" : "") + (v * 100).toFixed(d) + "%";

  /* ---- colour ---------------------------------------------------------- */
  function hexToRgb(h) {
    h = (h || "").replace("#", "");
    if (h.length === 3) h = h.split("").map((c) => c + c).join("");
    const n = parseInt(h || "888888", 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }
  const rgba = (hex, a) => {
    const [r, g, b] = hexToRgb(hex);
    return `rgba(${r},${g},${b},${a})`;
  };
  const mix = (h1, h2, t) => {
    const a = hexToRgb(h1), b = hexToRgb(h2);
    const c = a.map((v, i) => Math.round(v + (b[i] - v) * t));
    return `rgb(${c[0]},${c[1]},${c[2]})`;
  };

  /* Data-health -> colour. Never the only encoding: the numeral is always
     printed beside it, per the never-colour-alone rule. */
  function healthColor(h) {
    if (h === null || h === undefined || Number.isNaN(h)) return tk("--text-tertiary");
    if (h >= 0.9) return tk("--state-excess");
    if (h >= 0.75) return mix(tk("--state-excess"), tk("--state-attention"), 0.55);
    if (h >= 0.6) return tk("--state-attention");
    return tk("--state-risk");
  }

  /* Variance -> diverging tint. Excess through neutral to risk, never a rainbow,
     and capped at 18% opacity so the numeral on top stays readable. */
  function varianceTint(rel) {
    if (rel === null || rel === undefined || Number.isNaN(rel) || Math.abs(rel) < 0.03)
      return null;
    const m = Math.min(Math.abs(rel) / 0.45, 1);
    const hex = rel > 0 ? tk("--state-excess") : tk("--state-risk");
    return rgba(hex, 0.06 + 0.12 * m);
  }

  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  /* Short *plain* words, not short jargon. These label the densest surfaces in the
     application, where the full phrases from plain.js ("Sells occasionally", "Being
     discontinued") do not fit -- but "intermit" and "terminal" are not shorter English,
     they are abbreviated method vocabulary, which is the thing this interface is not
     supposed to put in front of a planner anywhere. */
  const CLASS_SHORT = {
    smooth: "steady", erratic: "jumpy", intermittent: "occasional",
    lumpy: "bursty", cold_start: "new", terminal: "ending",
  };
  const REASON_COLOR = {
    risk: "--state-risk", stale: "--state-attention", censored: "--series-override",
    wide: "--series-forecast", swing: "--series-prior",
    "cold-start": "--state-excess", terminal: "--text-tertiary",
  };

  /* Prepare a canvas for a CSS-pixel box on a high-DPI display. Skipping this
     is what makes canvas text look soft next to DOM text. */
  function fitCanvas(canvas, w, h) {
    const dpr = window.devicePixelRatio || 1;
    const W = Math.max(1, Math.round(w * dpr));
    const H = Math.max(1, Math.round(h * dpr));
    if (canvas.width !== W || canvas.height !== H) {
      canvas.width = W;
      canvas.height = H;
    }
    canvas.style.width = w + "px";
    canvas.style.height = h + "px";
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return ctx;
  }

  function isoWeek(dateStr) {
    const d = new Date(dateStr + "T00:00:00Z");
    const t = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
    const day = t.getUTCDay() || 7;
    t.setUTCDate(t.getUTCDate() + 4 - day);
    const y0 = new Date(Date.UTC(t.getUTCFullYear(), 0, 1));
    return Math.ceil(((t - y0) / 86400000 + 1) / 7);
  }
  const shortDate = (s) =>
    new Date(s + "T00:00:00Z").toLocaleDateString("en-US", {
      month: "short", day: "numeric", timeZone: "UTC",
    });

  function debounce(fn, ms) {
    let t;
    return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
  }

  refresh();
  /* HTML-escape. Every value that reaches innerHTML goes through this. The SQL
     console renders arbitrary column names and string values from query results,
     which makes it the one surface in the app where unescaped interpolation would
     be an injection rather than merely a rendering bug. */
  const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ESCAPES[c]);

  return {
    esc,
    refresh, tk, nf, compact, money, pct, signed, signedPct,
    rgba, mix, hexToRgb, healthColor, varianceTint, clamp,
    CLASS_SHORT, REASON_COLOR,
    fitCanvas, isoWeek, shortDate, debounce,
  };
})();
