/* ============================================================================
   The order list — what to actually buy.

   Everything upstream of this screen produces a recommendation. This is the screen
   that produces an order, and until it existed the application stopped one step
   short of the thing a store actually needs: a list of products, quantities, and
   what it will cost.

   The arithmetic is deliberately boring, because the value is in it being right
   rather than in it being clever:

       order quantity = stock up to  -  what is on the shelf

   The interesting part is what happens when the second number is missing. A
   product that has never been counted is not a product that needs nothing, and
   showing it as "order 0" would quietly drop it out of the buy. So counted and
   uncounted are different states with different rows, and the header says how many
   of each there are before the planner trusts a total.

   Rows are ordered by how soon the shelf empties rather than by name or by value.
   A planner working down this list from the top is working in the order the store
   will actually run out, which is the only ordering that makes the list safe to
   abandon halfway through.
   ============================================================================ */

window.Orders = (() => {
  const $ = (id) => document.getElementById(id);

  let D = null;
  let hooks = {};
  let all = [];

  const state = {
    store: "",
    show: "attention",        // attention | all | uncounted | over
    query: "",
    limit: 60,
    sort: "urgency",
  };

  const name = (s) => s._name || Review.prettyItem(s.item);

  /* ---- which rows, in which order --------------------------------------- */

  const RANK = { critical: 0, low: 1, unknown: 2, ok: 3, over: 4 };

  /** What a full shelf of this product is worth. The only value figure available for
      a product nobody has counted, and therefore the one that decides which of a
      thousand uncounted products is worth counting first. */
  const atStake = (l) => (l.target || 0) * (l.unitCost || 0);

  function lines() {
    const q = state.query.trim().toLowerCase();
    let out = all
      .filter((s) => !state.store || s.store === state.store)
      .filter((s) => !q || name(s).toLowerCase().includes(q) ||
                     s.id.toLowerCase().includes(q))
      .map((s) => Stock.line(s));

    if (state.show === "attention") {
      // Anything that needs a decision: short of cover, or never counted.
      out = out.filter((l) => l.coverStatus === "critical" || l.coverStatus === "low" ||
                              !l.counted);
    } else if (state.show === "uncounted") {
      out = out.filter((l) => !l.counted);
    } else if (state.show === "due") {
      // Never counted, or counted longer ago than this product's class allows.
      out = out.filter((l) => Stock.countDue(l.s).state !== "ok");
    } else if (state.show === "over") {
      out = out.filter((l) => l.coverStatus === "over");
    }

    if (state.sort === "value") {
      out.sort((a, b) => (b.lineCost || 0) - (a.lineCost || 0) ||
                         (b.target || 0) - (a.target || 0));
    } else {
      /* Urgency: how soon the shelf empties. Uncounted products sit between "low"
         and "fine" -- they might be either, and that uncertainty is itself a
         reason to look at them before the ones known to be comfortable.

         Among themselves, uncounted products are ranked by what is at stake on the
         shelf -- target times unit cost -- because they have no cover figure to be
         ranked by and arbitrary order is what makes a thousand blank fields feel
         impossible. Counting the twenty most valuable is a morning's work that
         resolves most of the money; counting twenty arbitrary ones resolves nothing
         in particular. */
      out.sort((a, b) =>
        RANK[a.coverStatus] - RANK[b.coverStatus] ||
        (a.cover ?? 99) - (b.cover ?? 99) ||
        (b.lineCost ?? atStake(b)) - (a.lineCost ?? atStake(a)));
    }
    return out;
  }

  /* ---- render ------------------------------------------------------------ */

  function render() {
    if (!D) return;
    const list = lines();
    const scoped = all.filter((s) => !state.store || s.store === state.store);
    const sum = Stock.summary(scoped);

    paintTotals(sum, scoped.length);

    $("orCount").textContent = list.length === scoped.length
      ? `${list.length.toLocaleString()} products`
      : `${list.length.toLocaleString()} of ${scoped.length.toLocaleString()}`;

    const shown = list.slice(0, state.limit);
    $("orBody").innerHTML = shown.length
      ? shown.map(row).join("")
      : `<p class="or-empty">${U.esc(emptyWord())}</p>`;

    $("orMore").hidden = list.length <= state.limit;
    $("orMore").textContent =
      `Show ${Math.min(60, list.length - state.limit)} more ` +
      `(${(list.length - state.limit).toLocaleString()} not shown)`;

    $("orExport").disabled = sum.toOrderLines === 0;
  }

  function emptyWord() {
    if (state.query) return `Nothing matches “${state.query}”.`;
    switch (state.show) {
      case "attention": return "Nothing needs ordering and nothing is uncounted. The shelves are covered.";
      case "uncounted": return "Every product in scope has a stock count.";
      case "due": return "Nothing is due a count. Every product has been counted inside the interval its class asks for.";
      case "over": return "Nothing is carrying more than the forecast period needs.";
      default: return "No products in scope.";
    }
  }

  /* The header figures. Two totals are reported separately and never added: what
     the order costs, and how much of the range has not been counted. A single
     confident total over a half-counted assortment is the number most likely to be
     believed and most likely to be wrong. */
  function paintTotals(sum, scoped) {
    $("orLines").textContent = sum.toOrderLines.toLocaleString();
    $("orUnits").textContent = Plain.units(sum.toOrderUnits);
    $("orCost").textContent = Plain.money(sum.toOrderCost);

    const pct = scoped ? Math.round((sum.counted / scoped) * 100) : 0;
    $("orCountedBar").style.width = pct + "%";
    $("orCountedText").textContent = sum.uncounted
      ? `${sum.counted.toLocaleString()} of ${scoped.toLocaleString()} counted — ` +
        `the totals above cover only those`
      : `All ${scoped.toLocaleString()} counted`;

    const warn = $("orWarn");
    warn.hidden = sum.uncounted === 0;
    if (sum.uncounted) {
      /* Quantify the gap rather than just naming it. "996 uncounted" is a chore;
         "996 uncounted, $712k of shelf value" is a reason, and the list behind the
         button is ordered so the first twenty resolve most of it. */
      const un = all
        .filter((s) => (!state.store || s.store === state.store) && !Stock.known(s.id))
        .reduce((a, s) => a + (s.orderUpTo || 0) * (s.cost || 0), 0);
      $("orWarnText").textContent =
        `${sum.uncounted.toLocaleString()} product${sum.uncounted === 1 ? " has" : "s have"} ` +
        `no stock count — ${Plain.money(un)} of shelf value that cannot become an ` +
        `order yet. They are listed most valuable first, so the top of that list is ` +
        `where counting pays.`;
    }
  }

  function row(l) {
    const e = U.esc;
    const s = l.s;
    const cw = Stock.COVER_WORD[l.coverStatus];
    const due = Stock.countDue(s);
    return `<div class="or-row" data-id="${e(l.id)}" data-cover="${l.coverStatus}">
      <div class="or-cell or-name">
        <button class="or-link" data-open="${e(l.id)}"
                title="Open this product">${e(name(s))}</button>
        <span class="or-sub">${e(Review.prettyStore(s.store))} ·
          ${e(Plain.catWord(s.cat))} · ${e(s.abc || "C")}${e(s.xyz || "Z")}</span>
      </div>

      <div class="or-cell or-onhand">
        <label class="sr-only" for="oh-${e(l.id)}">Units on hand for ${e(name(s))}</label>
        <input id="oh-${e(l.id)}" class="or-input" type="number" min="0" step="1"
               inputmode="numeric" data-onhand="${e(l.id)}"
               value="${l.counted ? l.onHand : ""}" placeholder="count">
        <span class="or-age" data-due="${e(due.state)}"
              title="${e(s.abc || "C")}-class, so it wants counting every ${
                due.every} days. ${e(Stock.countWord(s))}.">${
          due.state === "never" ? "never counted"
            : due.state === "stale" ? "due a recount"
            : Stock.countWord(s)}</span>
      </div>

      <div class="or-cell or-cover">
        <span class="or-cover-word">${e(cw.word)}</span>
        <span class="or-sub">${l.cover == null ? "—"
          : l.cover >= 99 ? "99+ days left"
          : `${l.cover.toFixed(l.cover < 10 ? 1 : 0)} days left`}</span>
      </div>

      <div class="or-cell or-num">
        <span class="or-target">${Plain.units(l.target || 0)}</span>
        <span class="or-sub">target</span>
      </div>

      <div class="or-cell or-num or-qty">
        ${l.qty == null
          ? '<span class="or-dash" title="Enter a stock count to get an order quantity">—</span>'
          : `<span class="or-qty-n${l.qty > 0 ? " is-order" : ""}">${Plain.units(l.qty)}</span>`}
        <span class="or-sub">to order</span>
      </div>

      <div class="or-cell or-num">
        <span class="or-line-cost">${l.lineCost == null ? "—" : Plain.money(l.lineCost)}</span>
        <span class="or-sub">at $${(l.unitCost || 0).toFixed(2)}</span>
      </div>
    </div>`;
  }

  /* ---- the purchase order, as a file ------------------------------------- */

  function orderRows() {
    return lines()
      .filter((l) => l.qty > 0)
      .map((l) => ({
        product: name(l.s), store: Review.prettyStore(l.s.store),
        category: Plain.catWord(l.s.cat),
        classification: `${l.s.abc || "C"}${l.s.xyz || "Z"}`,
        onHand: l.onHand, target: l.target, order: l.qty,
        unitCost: (l.unitCost || 0).toFixed(2),
        lineCost: (l.lineCost || 0).toFixed(2),
        daysCover: l.cover == null ? "" : l.cover.toFixed(1),
        status: Stock.COVER_WORD[l.coverStatus].word,
      }));
  }

  /* ---- wiring ------------------------------------------------------------ */

  function init(bundle, h = {}) {
    D = bundle; hooks = h;
    all = D.series.slice();

    const stores = [...new Set(D.series.map((s) => s.store))].sort();
    $("orStore").innerHTML = '<option value="">All stores</option>' +
      stores.map((x) => `<option value="${U.esc(x)}">${U.esc(Review.prettyStore(x))}</option>`).join("");

    $("orStore").addEventListener("change", (e) => {
      state.store = e.target.value; state.limit = 60; render();
    });
    $("orSearch").addEventListener("input", (e) => {
      state.query = e.target.value; state.limit = 60; render();
    });
    $("orSort").addEventListener("change", (e) => { state.sort = e.target.value; render(); });
    $("orMore").addEventListener("click", () => { state.limit += 60; render(); });

    $("orFilters").addEventListener("click", (e) => {
      const b = e.target.closest("[data-show]");
      if (!b) return;
      state.show = b.dataset.show; state.limit = 60;
      $("orFilters").querySelectorAll("[data-show]").forEach((x) =>
        x.setAttribute("aria-pressed", String(x === b)));
      render();
    });

    /* Stock counts are saved as they are typed, without a Save button.

       A count is a fact about the world, not a proposal, and there is nothing to
       confirm about it -- unlike a forecast override, which is a judgement and
       therefore has a reason attached. Re-rendering on every keystroke would take
       the caret with it, so the row is updated in place and the totals are
       recomputed around it. */
    $("orBody").addEventListener("input", (e) => {
      const inp = e.target.closest("[data-onhand]");
      if (!inp) return;
      Stock.set(inp.dataset.onhand, inp.value === "" ? null : inp.value);
      updateRow(inp.closest(".or-row"));
      const scoped = all.filter((s) => !state.store || s.store === state.store);
      paintTotals(Stock.summary(scoped), scoped.length);
      hooks.onStockChange?.();
    });

    // Enter moves to the next count, which is how anyone types a stock take.
    $("orBody").addEventListener("keydown", (e) => {
      if (e.key !== "Enter" || !e.target.closest("[data-onhand]")) return;
      e.preventDefault();
      const inputs = [...$("orBody").querySelectorAll("[data-onhand]")];
      const next = inputs[inputs.indexOf(e.target) + 1];
      if (next) { next.focus(); next.select(); }
    });

    $("orBody").addEventListener("click", (e) => {
      const b = e.target.closest("[data-open]");
      if (b) hooks.onOpenProduct?.(b.dataset.open);
    });

    $("orExport").addEventListener("click", () => hooks.onExport?.(orderRows()));

    render();
  }

  /** Repaint one row in place, so typing a count does not move the caret. */
  function updateRow(el) {
    if (!el) return;
    const s = all.find((x) => x.id === el.dataset.id);
    if (!s) return;
    const l = Stock.line(s);
    const cw = Stock.COVER_WORD[l.coverStatus];
    el.dataset.cover = l.coverStatus;
    el.querySelector(".or-cover-word").textContent = cw.word;
    el.querySelector(".or-cover .or-sub").textContent =
      l.cover == null ? "—" : l.cover >= 99 ? "99+ days left"
        : `${l.cover.toFixed(l.cover < 10 ? 1 : 0)} days left`;
    const qtyCell = el.querySelector(".or-qty");
    qtyCell.innerHTML = (l.qty == null
      ? '<span class="or-dash" title="Enter a stock count to get an order quantity">—</span>'
      : `<span class="or-qty-n${l.qty > 0 ? " is-order" : ""}">${Plain.units(l.qty)}</span>`) +
      '<span class="or-sub">to order</span>';
    el.querySelector(".or-line-cost").textContent =
      l.lineCost == null ? "—" : Plain.money(l.lineCost);
    // The count was just taken, so its age resets with it.
    const age = el.querySelector(".or-age");
    if (age) {
      const due = Stock.countDue(s);
      age.dataset.due = due.state;
      age.textContent = due.state === "never" ? "never counted"
        : due.state === "stale" ? "due a recount" : Stock.countWord(s);
      age.title = `${s.abc || "C"}-class, so it wants counting every ${due.every} days. ` +
                  `${Stock.countWord(s)}.`;
    }
  }

  return { init, render, orderRows, get state() { return state; },
           setStore(v) { state.store = v; $("orStore").value = v; state.limit = 60; render(); },
           focus(show) {
             state.show = show; state.limit = 60;
             const b = document.querySelector(`#orFilters [data-show="${show}"]`);
             if (b) document.querySelectorAll("#orFilters [data-show]").forEach((x) =>
               x.setAttribute("aria-pressed", String(x === b)));
             render();
           } };
})();
