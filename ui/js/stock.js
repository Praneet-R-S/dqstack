/* ============================================================================
   What you already have.

   The application could forecast demand and compute a target — "stock up to 156
   units" — and then stopped. It never asked the one question that turns a target
   into an order: how many are on the shelf right now. Without that, every screen
   was a recommendation nobody could act on, and the system was a forecaster wearing
   the clothes of an inventory tool.

   On-hand is entered by the planner and kept in this browser. It is not derived,
   estimated, or back-filled from the simulation, and there is deliberately no
   "fill these in for me" button: a fabricated stock count would flow straight into
   an order quantity and out of the building as a purchase, which is the one place
   in this system where a plausible-looking invention would cost real money.

   So a product is in one of two states and the interface says which:

     counted     on-hand known -> order quantity, days of cover, line cost
     uncounted   on-hand unknown -> the target, and an invitation to count it

   Everything else here is derived from that one input plus what the pipeline
   already exports.

   ---- days of cover

   Cover is on-hand divided by expected demand per day, which is the figure a store
   manager actually thinks in: "four days left". It is compared against the two
   windows the ordering logic already uses — the lead time, and the review period
   plus lead time — so the thresholds are the system's own rather than invented
   here. Below lead time means the shelf empties before a delivery can arrive.
   ============================================================================ */

window.Stock = (() => {

  const KEY = "dq.onhand";
  let onHand = new Map();          // id -> units, planner-entered
  let cfg = { leadTime: 2, cover: 9, horizonDays: 28 };

  /* ---- persistence ------------------------------------------------------ */

  function load() {
    try {
      const raw = JSON.parse(localStorage.getItem(KEY) || "{}");
      onHand = new Map(Object.entries(raw)
        .filter(([, v]) => Number.isFinite(+v) && +v >= 0)
        .map(([k, v]) => [k, +v]));
    } catch (_) { onHand = new Map(); }
  }

  function save() {
    try { localStorage.setItem(KEY, JSON.stringify(Object.fromEntries(onHand))); }
    catch (_) { /* private windows and full quotas are not worth failing over */ }
  }

  const get = (id) => (onHand.has(id) ? onHand.get(id) : null);
  const known = (id) => onHand.has(id);
  const countedTotal = () => onHand.size;

  function set(id, v) {
    if (v === null || v === "" || !Number.isFinite(+v) || +v < 0) onHand.delete(id);
    else onHand.set(id, Math.round(+v));
    save();
  }

  const clearAll = () => { onHand.clear(); save(); };

  /* ---- the arithmetic that turns a target into an order ----------------- */

  /** Expected demand per day over the window the order has to cover. Taken from
      the forecast mean rather than from history, because the order is about what
      is coming, not what happened. */
  function dailyDemand(s) {
    const m = s.mean || [];
    if (!m.length) return 0;
    const n = Math.min(cfg.cover, m.length);
    let sum = 0;
    for (let i = 0; i < n; i++) sum += m[i] || 0;
    return n ? sum / n : 0;
  }

  /** Everything the order list needs for one product.

      `qty` is null when the count is unknown -- not zero. Zero is a real answer
      meaning "you have enough", and conflating the two would hide every product
      that has never been counted inside a list of products that need nothing. */
  function line(s) {
    const id = s.id;
    const target = s.orderUpTo;
    const have = get(id);
    const per = dailyDemand(s);
    const cover = have != null && per > 0.01 ? have / per : null;

    const qty = have == null || target == null
      ? null
      : Math.max(0, Math.round(target - have));

    return {
      id, s, target, onHand: have, counted: have != null,
      perDay: per,
      cover,
      coverStatus: coverStatus(cover),
      qty,
      unitCost: s.cost || 0,
      lineCost: qty == null ? null : qty * (s.cost || 0),
      // What the shortfall is worth if it is not ordered: the margin on the units
      // demand is expected to want and the shelf will not have.
      shortfall: have == null || target == null ? null : Math.max(0, target - have),
    };
  }

  /** Cover thresholds taken from the ordering policy's own windows, so they cannot
      drift from the logic that produced the target. */
  function coverStatus(cover) {
    if (cover == null) return "unknown";
    if (cover < cfg.leadTime) return "critical";   // empties before a delivery lands
    if (cover < cfg.cover) return "low";           // inside the window this order covers
    if (cover > cfg.horizonDays) return "over";    // more than the whole forecast horizon
    return "ok";
  }

  const COVER_WORD = {
    critical: { word: "Runs out first", note: "Less stock than the delivery takes to arrive." },
    low: { word: "Needs topping up", note: "Will not last the window this order has to cover." },
    ok: { word: "Covered", note: "Enough to see it through to the next delivery." },
    over: { word: "Overstocked", note: "More than the whole forecast period needs." },
    unknown: { word: "Not counted", note: "Enter what is on the shelf to get an order quantity." },
  };

  /* ---- portfolio figures the rail reports ------------------------------- */

  /** One pass over a set of products, producing every number the sidebar shows.

      Kept as a single traversal because the rail asks for all of it on every render
      and the assortment is a thousand products; six separate reducers over the same
      array is the kind of thing that is invisible until the list is ten times
      longer. */
  function summary(list) {
    const out = {
      total: list.length, counted: 0, uncounted: 0,
      toOrderLines: 0, toOrderUnits: 0, toOrderCost: 0,
      critical: 0, low: 0, ok: 0, over: 0,
      overstockUnits: 0, overstockCost: 0,
      perishRisk: 0, perishCost: 0,
      targetUnits: 0,
    };
    for (const s of list) {
      const l = line(s);
      out.targetUnits += l.target || 0;
      if (!l.counted) { out.uncounted++; out[l.coverStatus]++; continue; }
      out.counted++;
      out[l.coverStatus]++;
      if (l.qty > 0) {
        out.toOrderLines++;
        out.toOrderUnits += l.qty;
        out.toOrderCost += l.lineCost;
      }
      if (l.coverStatus === "over") {
        const excess = Math.max(0, l.onHand - (l.target || 0));
        out.overstockUnits += excess;
        out.overstockCost += excess * (s.cost || 0);
        /* Overstock on something that keeps for three weeks is a different problem
           from overstock on something that keeps for two years. Only the first is
           money about to be thrown away. */
        if ((s.shelfLife || 999) <= 30) {
          out.perishRisk++;
          out.perishCost += excess * (s.cost || 0);
        }
      }
    }
    return out;
  }

  /* ---- ABC and XYZ, which the pipeline exports and nothing displayed ----

     The two classifications every inventory course teaches and every planner uses:
     ABC ranks products by what they are worth to you, XYZ by how predictable they
     are. The pipeline has assigned both to all thousand products since the first
     run and no planner-facing screen has ever shown them.

     The pairing is the useful part. An AX product -- valuable and predictable --
     should be run tight and automatically. A CZ product -- cheap and erratic -- is
     where blanket policies waste the most attention. */
  function abcMix(list) {
    const cell = {};
    for (const a of "ABC") for (const x of "XYZ") cell[a + x] = { n: 0, value: 0 };
    let value = 0;
    for (const s of list) {
      const k = (s.abc || "C") + (s.xyz || "Z");
      if (!cell[k]) cell[k] = { n: 0, value: 0 };
      const v = (s.orderUpTo || 0) * (s.cost || 0);
      cell[k].n++; cell[k].value += v; value += v;
    }
    return { cell, value, n: list.length };
  }

  const ABC_WORD = {
    A: "Your biggest earners", B: "Middle of the range", C: "The long tail",
  };
  const XYZ_WORD = {
    X: "steady and predictable", Y: "some variation", Z: "hard to predict",
  };

  /** The plain reading of one ABC/XYZ pairing, which is where the advice lives. */
  function classAdvice(abc, xyz) {
    if (abc === "A" && xyz === "X") {
      return "Valuable and predictable. Run these tight — a small safety margin protects a lot of profit.";
    }
    if (abc === "A" && xyz === "Z") {
      return "Valuable but erratic. These deserve the most of your attention, because neither a formula nor a blanket rule handles them well.";
    }
    if (abc === "A") return "Valuable and reasonably predictable. Worth checking regularly.";
    if (abc === "C" && xyz === "Z") {
      return "Cheap and erratic. The worst place to spend attention — set a rule and let it run.";
    }
    if (abc === "C") return "Low value. Review in bulk rather than one at a time.";
    return "Middle of the range. Handle by exception.";
  }

  function init(run) {
    cfg = {
      leadTime: run.leadTime || 2,
      cover: (run.reviewPeriod || 7) + (run.leadTime || 2),
      horizonDays: run.horizon || 28,
    };
    load();
  }

  return { init, get, set, known, clearAll, countedTotal, line, summary,
           dailyDemand, coverStatus, COVER_WORD, abcMix, ABC_WORD, XYZ_WORD,
           classAdvice, get cfg() { return cfg; },
           get size() { return onHand.size; } };
})();
