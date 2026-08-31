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
  let onHand = new Map();          // id -> { n: units, at: ms }, planner-entered
  let cfg = { leadTime: 2, cover: 9, horizonDays: 28 };

  /* ---- persistence ------------------------------------------------------

     Entries are `{ n, at }`: the count and when it was taken. The timestamp is what
     makes cycle counting possible at all -- without it a count from this morning and
     one from three months ago are indistinguishable, and a stock figure with no age
     is a stock figure you cannot decide whether to trust.

     Counts written before the timestamp existed are plain numbers. They are read as
     counts of unknown age rather than discarded, and treated as due for recount,
     which is the safe reading of "we do not know when this was taken". */
  function load() {
    try {
      const raw = JSON.parse(localStorage.getItem(KEY) || "{}");
      onHand = new Map();
      for (const [k, v] of Object.entries(raw)) {
        if (v && typeof v === "object") {
          if (Number.isFinite(+v.n) && +v.n >= 0) {
            onHand.set(k, { n: +v.n, at: Number.isFinite(+v.at) ? +v.at : null });
          }
        } else if (Number.isFinite(+v) && +v >= 0) {
          onHand.set(k, { n: +v, at: null });          // pre-timestamp entry
        }
      }
    } catch (_) { onHand = new Map(); }
  }

  function save() {
    try { localStorage.setItem(KEY, JSON.stringify(Object.fromEntries(onHand))); }
    catch (_) { /* private windows and full quotas are not worth failing over */ }
  }

  const get = (id) => (onHand.has(id) ? onHand.get(id).n : null);
  const known = (id) => onHand.has(id);
  const countedTotal = () => onHand.size;
  const countedAt = (id) => (onHand.has(id) ? onHand.get(id).at : null);

  function set(id, v) {
    if (v === null || v === "" || !Number.isFinite(+v) || +v < 0) onHand.delete(id);
    else onHand.set(id, { n: Math.round(+v), at: Date.now() });
    save();
  }

  const clearAll = () => { onHand.clear(); save(); };

  /* ---- cycle counting ---------------------------------------------------

     Nobody counts a whole shop at once. Inventory teams cycle count: the lines that
     carry the money get counted often, the long tail rarely, and the schedule is
     driven by the same ABC class the pipeline already assigns. Without this the
     stock figures the order list depends on decay silently -- they were true once
     and nothing on screen says how long ago.

     The intervals are the textbook ones and deliberately round. They are a starting
     policy, not a claim: what matters is that a count has an age at all and that the
     valuable lines come round faster than the tail. */
  const INTERVAL = { A: 7, B: 28, C: 91 };
  const DAY = 864e5;

  /** Days since this product was counted, or null if it never was. `Infinity` for a
      count of unknown age, which sorts it with the never-counted where it belongs. */
  function countAge(id) {
    if (!onHand.has(id)) return null;
    const at = onHand.get(id).at;
    if (!at) return Infinity;
    return (Date.now() - at) / DAY;
  }

  /** Is this product due a count, and why. One of:
        never    no count has ever been taken
        stale    the count is older than its class allows
        ok       counted recently enough
      The interval comes from the product's own ABC class, so an A line falls due in
      a week and a C line in a quarter. */
  function countDue(s) {
    const cls = (s.abc || "C").toUpperCase();
    const every = INTERVAL[cls] || INTERVAL.C;
    const age = countAge(s.id);
    if (age === null) return { state: "never", every, age: null, over: null, cls };
    if (age >= every) {
      return { state: "stale", every, age, over: age === Infinity ? null : age - every, cls };
    }
    return { state: "ok", every, age, over: null, cls };
  }

  /** The count worklist: what is due, ordered so the first hour of counting resolves
      the most value. Never-counted and stale are both due; among them the ranking is
      what a full shelf is worth, for the same reason the order list uses it. */
  function countPlan(list) {
    const due = [];
    const byClass = { A: { due: 0, total: 0 }, B: { due: 0, total: 0 }, C: { due: 0, total: 0 } };
    let dueValue = 0;
    for (const s of list) {
      const d = countDue(s);
      const b = byClass[d.cls] || (byClass[d.cls] = { due: 0, total: 0 });
      b.total++;
      if (d.state === "ok") continue;
      b.due++;
      const value = (s.orderUpTo || 0) * (s.cost || 0);
      dueValue += value;
      due.push({ s, ...d, value });
    }
    due.sort((a, b) => b.value - a.value);
    return { due, byClass, dueValue, n: due.length, intervals: INTERVAL };
  }

  /** "counted today" / "3 days ago" / "never counted". */
  function countWord(s) {
    const d = countDue(s);
    if (d.state === "never") return "never counted";
    if (d.age === Infinity) return "counted, date unknown";
    if (d.age < 1) return "counted today";
    const n = Math.floor(d.age);
    return `counted ${n} day${n === 1 ? "" : "s"} ago`;
  }

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
           classAdvice, countedAt, countAge, countDue, countPlan, countWord,
           INTERVAL, get cfg() { return cfg; },
           get size() { return onHand.size; } };
})();
