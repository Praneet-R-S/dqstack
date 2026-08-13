/* ============================================================================
   Plain language.

   Every word a non-specialist reads comes from this file. That is the point of
   it existing: the workspace previously spoke the vocabulary of the model —
   "intermittent", "relWidth", "targetQ", "orderUpTo", "WSPL", "conformal" — and
   a planner who has not read the thesis cannot act on any of that. Scattering
   friendlier strings through the render code would have worked for a week and
   then drifted, with the grid calling something "swing" while a card called the
   same thing "changed a lot".

   Two rules held here.

   Nothing is dumbed down to the point of being wrong. "Hard to predict" is a
   fair reading of a wide predictive interval; "we don't know" would not be. Where
   a number is genuinely uncertain the wording says so, because the entire
   argument of this system is that hiding uncertainty is what makes forecasts
   dangerous.

   Every plain phrase keeps its technical name reachable. The expert view still
   says `relWidth`, and the tooltip on a plain phrase gives the real term. A
   planner who wants to learn the vocabulary can, and an analyst reading over
   their shoulder is not lied to.
   ============================================================================ */

window.Plain = (() => {

  /* ---- what kind of thing is this product? ------------------------------
     The Syntetos-Boylan-Croston quadrants, in words a buyer would use. */
  const CLASS = {
    smooth:       { label: "Sells steadily",     hint: "Regular, predictable sales most days.",
                    term: "smooth demand" },
    erratic:      { label: "Unpredictable size", hint: "Sells most days, but the amount jumps around a lot.",
                    term: "erratic demand" },
    intermittent: { label: "Sells occasionally", hint: "Long quiet stretches with occasional sales.",
                    term: "intermittent demand" },
    lumpy:        { label: "Sells in bursts",    hint: "Quiet for a while, then a big order all at once. The hardest kind to forecast.",
                    term: "lumpy demand" },
    cold_start:   { label: "New product",        hint: "Too little history to learn a pattern from yet.",
                    term: "cold start" },
    terminal:     { label: "Being discontinued", hint: "Sales are winding down towards zero.",
                    term: "terminal decline" },
  };

  /* Category names. The raw values are M5's shouty identifiers and were leaking
     into the product page as "HOBBIES". One mapping, used everywhere. */
  const CAT = { FOODS: "Food", HOUSEHOLD: "Household", HOBBIES: "Hobbies" };
  const catWord = (c) => CAT[c] || c;

  /* ---- why is this item in front of me? --------------------------------
     Ordered by how much a planner should care. The first matching reason is the
     headline; the rest become secondary notes. */
  const REASONS = [
    { key: "risk", label: "Likely to run out", short: "Running out",
      detail: "At the stock level currently planned, demand is expected to exceed supply before the next delivery.",
      action: "Raising the order protects sales. Lowering it saves cash but risks empty shelves.",
      term: "stock-out risk", tone: "urgent" },

    { key: "censored", label: "May have been out of stock", short: "Shelf looked empty",
      detail: "Some days recorded zero sales while the product was probably not on the shelf. Those zeros mean 'could not buy', not 'did not want' — so real demand is higher than the sales history suggests.",
      action: "The forecast has already been corrected for this. Worth a sanity check against what actually happened in store.",
      term: "censored demand", tone: "warn" },

    { key: "stale", label: "Sales pattern changed", short: "Pattern changed",
      detail: "This product's behaviour shifted recently, so older history is a poorer guide than usual.",
      action: "If you know what changed — a price move, a new competitor, a range change — that context is worth more than the history.",
      term: "regime change", tone: "warn" },

    { key: "swing", label: "Big change since last week", short: "Moved a lot",
      detail: "The forecast has moved substantially from the version you saw last week.",
      action: "Check the movement is explainable before acting on it.",
      term: "forecast revision", tone: "warn" },

    { key: "wide", label: "Hard to predict", short: "Hard to predict",
      detail: "The range of plausible outcomes is unusually wide, so any single number here is a weak guide.",
      action: "Your own knowledge is worth more than usual on this one.",
      term: "wide prediction interval", tone: "info" },

    { key: "cold-start", label: "Too new to judge", short: "Too new",
      detail: "Not enough sales history to learn this product's own pattern, so the forecast borrows from similar products.",
      action: "Expect to correct this one by hand for a few weeks.",
      term: "cold start", tone: "info" },

    { key: "terminal", label: "Winding down", short: "Winding down",
      detail: "Sales are trending towards zero, most likely a planned exit from the range.",
      action: "Order conservatively. Leftover stock on a discontinued line usually gets written off.",
      term: "terminal decline", tone: "info" },
  ];

  const REASON_BY_KEY = Object.fromEntries(REASONS.map((r) => [r.key, r]));

  /** The headline reason, chosen by the priority order above rather than by
      whatever order the pipeline happened to emit. */
  function headline(reasons) {
    for (const r of REASONS) if (reasons.includes(r.key)) return r;
    return { key: "ok", label: "Looks fine", detail: "Nothing unusual about this one.",
             action: "No action needed.", term: "no exception", tone: "ok" };
  }

  /** The short form, for the sidebar's 288px rows. Falls back to the full label so a
      reason added later is never blank. */
  const short = (key) => (REASON_BY_KEY[key]?.short) || REASON_BY_KEY[key]?.label || key;

  const others = (reasons) =>
    REASONS.filter((r) => reasons.includes(r.key)).slice(1);

  /* ---- thresholds, calibrated from this assortment ---------------------
     Hardcoded cutoffs were the first attempt and they were useless: guessing that
     a relative interval width above 1.1 means "low confidence" put 24 of 24 cards
     on the review screen at "Low", which tells a planner nothing and trains them
     to ignore the field. The real distribution here runs 0.30 to 1214 with
     terciles at 1.22 and 2.61.

     So the words are terciles of the actual assortment, computed once at boot.
     That makes them explicitly comparative — "wider than two thirds of your
     range" — which is the judgement a planner is making anyway when they decide
     where to spend attention. The absolute number stays on hover for anyone who
     wants it. Fallbacks cover the case where calibrate() was never called. */
  let CUT = { conf: [1.2, 2.6], qual: [0.94, 0.85, 0.70] };

  function calibrate(series) {
    const pct = (arr, q) => {
      const a = arr.filter(Number.isFinite).sort((x, y) => x - y);
      return a.length ? a[Math.min(a.length - 1, Math.floor(a.length * q))] : 0;
    };
    const rw = series.map((s) => s.relWidth);
    const h = series.map((s) => s.health);
    CUT = {
      conf: [pct(rw, 1 / 3), pct(rw, 2 / 3)],
      // Health is a score with an absolute meaning, so only the middle band is
      // calibrated; "Poor" stays an absolute statement about the history.
      qual: [pct(h, 2 / 3), pct(h, 1 / 3), 0.70],
    };
    return CUT;
  }

  /* ---- data quality, as a sentence rather than a score ---------------- */
  function quality(health) {
    const [good, fair, patchy] = CUT.qual;
    if (health >= good) return { word: "Good", note: "The sales history behind this looks clean — better than most of your range.", tone: "ok" };
    if (health >= fair) return { word: "Fair", note: "A few gaps or oddities in the history, but usable.", tone: "info" };
    if (health >= patchy) return { word: "Patchy", note: "Enough missing or suspect days that the forecast is on shakier ground than most.", tone: "warn" };
    return { word: "Poor", note: "The history has serious gaps. Treat this forecast as a rough guide only.", tone: "urgent" };
  }

  /** For a product with no history at all. "Poor" is a judgement about the data;
      this is the absence of any data to judge, which is a different message and
      calls for a different action from the planner. */
  const qualityUnknown = () => ({
    word: "Not yet known",
    note: "There is no sales history for this product, so there is nothing to assess. Expect to steer it by hand for the first few weeks.",
    tone: "info",
  });

  /* ---- how confident are we? -------------------------------------------
     relWidth is the interval width relative to the median, so it is comparable
     across products of wildly different size. */
  function confidence(relWidth) {
    const [tight, wide] = CUT.conf;
    if (relWidth <= tight) return { word: "High", note: "Outcomes are more tightly clustered than for two thirds of your products.", tone: "ok" };
    if (relWidth <= wide) return { word: "Moderate", note: "About as predictable as the middle of your range.", tone: "info" };
    return { word: "Low", note: "A wider spread than two thirds of your products. Treat the single number with caution.", tone: "warn" };
  }

  /* ---- the service level, explained by what it costs -------------------
     targetQ is the critical fractile. A planner does not need the formula; they
     need to know why this product is planned tighter than that one. */
  function service(s) {
    const pct = Math.round(s.targetQ * 100);
    const why = s.marginRate >= 0.35
      ? "high profit per unit, so a lost sale costs more than extra stock"
      : s.shelfLife && s.shelfLife <= 7
        ? "short shelf life, so leftover stock is usually wasted"
        : s.marginRate <= 0.15
          ? "thin margin, so overstocking eats the profit quickly"
          : "a balance of margin against holding and waste costs";
    return {
      pct,
      label: `Aim to meet demand ${pct}% of the time`,
      why: `Set from this product's own economics: ${why}.`,
      term: "critical fractile = underage / (underage + overage)",
    };
  }

  /* ---- model feature names, in English --------------------------------
     Used by the "why this number" explanation. Patterns rather than a lookup
     table, because the feature set is generated and a missing key would render a
     raw column name to a non-technical user. */
  const FEATURE_PATTERNS = [
    [/^rmean_(\d+)$/, (m) => `Average sales over the last ${m[1]} days`],
    [/^rmed_(\d+)$/, (m) => `Typical (middle) sales over the last ${m[1]} days`],
    [/^rstd_(\d+)$/, (m) => `How much sales bounced around over the last ${m[1]} days`],
    [/^rmax_(\d+)$/, (m) => `Best day in the last ${m[1]} days`],
    [/^rmin_(\d+)$/, (m) => `Worst day in the last ${m[1]} days`],
    [/^rsum_(\d+)$/, (m) => `Total sales over the last ${m[1]} days`],
    [/^rzero_(\d+)$/, (m) => `Share of days with no sales in the last ${m[1]}`],
    [/^ewma_?(\d+)?$/, (m) => `Recent sales trend${m[1] ? ` (${m[1]}-day)` : ""}`],
    [/^lag_(\d+)$/, (m) => `Sales ${m[1]} day${m[1] === "1" ? "" : "s"} ago`],
    [/^dow(_.*)?$/, () => "Which day of the week it is"],
    [/^wom|^woy|^week/, () => "Time of year"],
    [/^month|^moy/, () => "Which month it is"],
    [/^trend/, () => "Long-run trend"],
    [/^price_rel|^rel_price/, () => "Price compared with its recent norm"],
    [/^price/, () => "Current price"],
    [/^promo|^discount|^markdown/, () => "Promotion or discount running"],
    [/^snap/, () => "Benefit-payment days, which lift grocery demand"],
    [/^event|^holiday/, () => "Nearby holiday or event"],
    [/^adi/, () => "How often this product sells at all"],
    [/^cv2/, () => "How erratic the sale sizes are"],
    [/^health/, () => "Quality of this product's sales history"],
    [/^age|^weeks_since/, () => "How long this product has been selling"],
    [/^zero_run|^days_since/, () => "How long since the last sale"],
    [/^regime/, () => "Time since the sales pattern last changed"],
    [/^cat_|^dept_|^store_|^state_/, () => "Where and what category it sells in"],
    [/^shelf/, () => "How long the product keeps"],
  ];

  function feature(name) {
    for (const [re, fn] of FEATURE_PATTERNS) {
      const m = re.exec(name);
      if (m) return fn(m);
    }
    // Last resort: make the raw name readable rather than showing snake_case.
    return name.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  }

  /* ---- money, at the scale a person reads ------------------------------ */
  function money(v, { sign = false } = {}) {
    const a = Math.abs(v);
    const s = sign && v > 0 ? "+" : v < 0 ? "-" : "";
    if (a >= 1e6) return `${s}$${(a / 1e6).toFixed(a >= 1e7 ? 0 : 1)}M`;
    if (a >= 1e4) return `${s}$${Math.round(a / 1e3)}k`;
    if (a >= 1e3) return `${s}$${(a / 1e3).toFixed(1)}k`;
    return `${s}$${a.toFixed(a < 10 && a % 1 !== 0 ? 2 : 0)}`;
  }

  const units = (v) =>
    Math.abs(v) >= 1e4 ? `${Math.round(v / 1e3)}k` : Math.round(v).toLocaleString();

  /** "3 units more" / "12% fewer" — a difference stated the way people say it.

      Percentages off a near-zero base are worse than useless: a product that went
      from 1 unit to 42 rendered as "3131% up", which reads as a system error and
      buries the only fact that matters, which is that it was previously almost
      nothing. Below a base of 10 units the ratio is dropped and the change is
      described instead. */
  function change(now, before) {
    if (before == null) return null;
    const d = now - before;
    if (Math.abs(d) < 0.5) return { word: "about the same", pct: null, dir: "flat" };
    const dir = d > 0 ? "up" : "down";
    const word = `${units(Math.abs(d))} ${d > 0 ? "more" : "fewer"}`;
    if (before < 10) {
      return { word, pct: null, dir,
               note: before < 1 ? "was almost nothing last week"
                                : `up from just ${units(before)}` };
    }
    return { word, pct: Math.abs(Math.round((d / before) * 100)), dir,
             note: null };
  }

  /* ---- the jargon glossary, for the one place it belongs -------------- */
  const GLOSSARY = [
    ["Forecast range",
     "We never give one number alone. The range shows what is plausible, not just what is likely. A wide range is a warning, not a defect."],
    ["Service level",
     "How often you want to have enough stock. 95% means running short one week in twenty. Higher is not automatically better — it costs cash and waste."],
    ["Out of stock vs no demand",
     "A day with zero sales might mean nobody wanted it, or that the shelf was empty. These look the same in the data and mean opposite things, so the system separates them before forecasting."],
    ["Data quality",
     "How trustworthy this product's sales history is: how many days are missing, suspect, or affected by the product not being stocked."],
    ["Money at risk",
     "The profit expected to be lost if nothing changes — mostly sales you cannot make because stock ran out."],
    ["Suggested stock level",
     "Not an order quantity — a target. It is how much you want on the shelf and in transit combined, to cover demand until the next delivery arrives. It covers a longer window than the weekly forecast, so the two numbers are not directly comparable."],
    ["Why this number",
     "The forecast is not a black box. Each product shows which patterns in its own history pushed the number up or down, and by how much."],
  ];

  return { CLASS, CAT, catWord, REASONS, REASON_BY_KEY, headline, others, short, quality, confidence,
           service, feature, money, units, change, calibrate, qualityUnknown, GLOSSARY,
           get CUT() { return CUT; } };
})();
