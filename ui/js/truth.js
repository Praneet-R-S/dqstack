/* ============================================================================
   What the history actually says.

   This module exists because the charts were quietly lying, and lying in the one
   way this whole system was built to prevent.

   A day with zero recorded sales means one of two opposite things. Either nobody
   wanted the product, or the shelf was empty and nobody could buy it. The first is
   demand information. The second is the absence of demand information, and feeding
   it to a forecaster as a zero teaches the model that demand is lower than it is.
   Separating the two is the argument the pipeline makes.

   The exported history encodes that separation as `null`: a day the pipeline
   excluded is `hist[i] === null`, with `histRaw[i]` keeping whatever the till
   recorded. Both chart surfaces then did `y(v)` on that null, and `y(null)` is
   arithmetically identical to `y(0)` — so an excluded day was drawn as a day of
   zero sales. The chart showed exactly the confusion the pipeline had just spent
   eight stages resolving, on 16,120 days across 579 of 1,000 products.

   So this file owns three things, and both surfaces read them rather than
   deriving their own:

     - which days are excluded, and which of the two reasons applies
     - a path builder that breaks the line at excluded days instead of dropping it
       to the floor
     - where the calendar events fall, so a spike has a caption

   The two reasons are worth distinguishing on screen because they call for
   different responses from a planner. `gap` means the product was not carried at
   that store at all — there is nothing to investigate, the day is simply not
   evidence. `suspect` means it was carried, the till recorded zero, and the
   pipeline judged that it was probably unavailable — which is worth a sanity
   check against what actually happened in store.
   ============================================================================ */

window.Truth = (() => {

  const GAP = "gap";           // not carried here at all
  const SUSPECT = "suspect";   // carried, recorded zero, judged probably unavailable

  /** Per-day verdict for the whole exported history: `null` where the day is real
      evidence, otherwise which of the two exclusions applies.

      `carried` is the discriminator. Every excluded-but-carried day in the bundle
      recorded exactly zero, which is what makes the judgement possible at all: a
      stocked product that sold nothing is the signature being looked for. */
  function classify(s) {
    const h = s.hist || [];
    const c = s.carried || [];
    return h.map((v, i) => (v == null ? (c[i] === false ? GAP : SUSPECT) : null));
  }

  /** Contiguous runs of the same exclusion, in window coordinates.

      Runs rather than per-day rectangles because a fortnight out of range is one
      fact, not fourteen, and fourteen abutting rectangles at 8px each paint a
      seam every 8px that reads as texture instead of as a span. */
  function runs(s, from = 0, n = Infinity) {
    const kinds = classify(s);
    const end = Math.min(kinds.length, from + n);
    const out = [];
    let cur = null;
    for (let i = from; i < end; i++) {
      const k = kinds[i];
      if (k && cur && cur.kind === k && cur.i1 === i - 1 - from) {
        cur.i1 = i - from;
        continue;
      }
      if (cur) { out.push(cur); cur = null; }
      if (k) cur = { kind: k, i0: i - from, i1: i - from };
    }
    if (cur) out.push(cur);
    return out;
  }

  /** How much of this product's history is usable, as counts.

      Reported rather than inferred from `censoredDays`, which counts over the
      pipeline's own window and disagrees with the exported 126 days for about half
      the assortment. A figure on screen should be countable from the data beside
      it. */
  function audit(s, from = 0, n = Infinity) {
    const kinds = classify(s);
    const end = Math.min(kinds.length, from + n);
    let gaps = 0, suspects = 0, known = 0;
    for (let i = from; i < end; i++) {
      if (kinds[i] === GAP) gaps++;
      else if (kinds[i] === SUSPECT) suspects++;
      else known++;
    }
    return { gaps, suspects, known, total: gaps + suspects + known };
  }

  /** The last day that is actual evidence, which is where a forecast legitimately
      hinges. Anchoring the fan on `hist[last]` put the hinge on the floor whenever
      the most recent day happened to be excluded. */
  function lastKnown(vals) {
    for (let i = vals.length - 1; i >= 0; i--) {
      if (vals[i] != null) return { i, v: vals[i] };
    }
    return null;
  }

  /** A polyline that breaks at excluded days.

      Returns the path plus any isolated points, because a single known day between
      two excluded ones is a one-point subpath and SVG draws nothing for it — the
      day would vanish, which is a quieter version of the same dishonesty. */
  function line(vals, x, y) {
    let d = "";
    const dots = [];
    let open = false;
    for (let i = 0; i < vals.length; i++) {
      const v = vals[i];
      if (v == null) { open = false; continue; }
      const px = x(i), py = y(v);
      if (!open) {
        const nextKnown = vals[i + 1] != null;
        const prevKnown = i > 0 && vals[i - 1] != null;
        if (!nextKnown && !prevKnown) { dots.push([px, py]); continue; }
        d += `M${px.toFixed(1)},${py.toFixed(1)}`;
        open = true;
      } else {
        d += `L${px.toFixed(1)},${py.toFixed(1)}`;
      }
    }
    return { d, dots };
  }

  /** Calendar events falling inside a history window.

      The pipeline already models these; the interface never showed them, so a
      planner looking at a spike had no way to learn it was a public holiday
      without knowing the calendar by heart. Only events inside the drawn window
      are returned, so a surface showing 35 days does not claim a marker for a date
      it is not displaying. */
  function events(dates, evs, from = 0, n = Infinity) {
    const hist = (dates && dates.history) || [];
    const end = Math.min(hist.length, from + n);
    const out = [];
    for (const ev of evs || []) {
      const i = hist.indexOf(ev.date);
      if (i < from || i >= end) continue;
      out.push({ i: i - from, date: ev.date, name: ev.name, type: ev.type });
    }
    return out;
  }

  /** Event names arrive as run-together identifiers — `MothersDay`,
      `IndependenceDay` — and were never meant to be read by anyone. */
  const eventLabel = (name) =>
    String(name || "").replace(/([a-z])([A-Z])/g, "$1 $2").replace(/_/g, " ");

  return { GAP, SUSPECT, classify, runs, audit, lastKnown, line, events, eventLabel };
})();
