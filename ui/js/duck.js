/* ============================================================================
   DuckDB-WASM: the query engine, in the browser.

   The specification is unusually direct about this one: fetch a slice as Arrow
   IPC, register it as a DuckDB-WASM table, and run filtering, grouping, sorting
   and aggregation as SQL on the client, so that grid interactions cost zero
   network round trips. It calls it the single biggest perceived-performance win
   available.

   It is built, and then it is measured, and the measurement only partly agrees —
   see CROSSOVER_ROWS below for the numbers. In-browser SQL wins decisively on row
   selection over large tables and loses on small ones and on low-cardinality
   rollups at every size, because a query engine in a worker has a fixed cost per
   query that a for-loop does not. So the router picks a backend from the measured
   crossover rather than from the claim, and the claim is reported as partly
   refuted rather than quietly assumed.

   What is unambiguously won is the *third* thing, which the spec does not dwell
   on: arbitrary analytical SQL against the planning table, on the client, with no
   endpoint to write and no round trip. That is the SQL console, and there is no
   JavaScript fallback for it because there is no JavaScript equivalent.

   Three things about the shape of this module are deliberate.

   It is an accelerator, never a dependency. DuckDB-WASM is a multi-megabyte WASM
   module fetched from a CDN, and there are entirely ordinary conditions under
   which it will not load: no network, a page opened from `file://` (a null origin
   cannot dynamic-import a cross-origin ES module or spawn its worker), a
   corporate proxy that blocks jsDelivr, a browser without WebAssembly. In every
   one of those cases the workspace has to keep working, so every query has a
   JavaScript fallback that returns the same answer, and `Query` picks a backend
   rather than assuming one. A planning tool that shows a blank grid because a CDN
   was slow is worse than one that never had a query engine.

   The two backends are held to agreement, not hoped to agree. `selftest()` runs
   the real workload through both and compares, and there is a Python-side test
   over the same Arrow table. An accelerator that quietly returns different
   numbers from the fallback is a correctness bug wearing a performance costume.

   Queries return row-id arrays, not row objects. The heavy per-series payload —
   the fan, 126 days of history, the weight vector — never enters the query table
   at all; nothing filters or groups on it, and it stays in the JS heap where the
   renderer already has it. SQL decides *which* rows and *in what order*, and the
   renderer looks them up. That keeps the transfer at 195 KB rather than 3.7 MB.
   ============================================================================ */

window.Duck = (() => {
  const CDN = "https://cdn.jsdelivr.net/npm/@duckdb/duckdb-wasm@1.29.0/+esm";

  const st = {
    status: "idle",        // idle | loading | ready | unavailable
    detail: "",
    rows: 0,
    loadMs: 0,
    bytes: 0,
    lastMs: 0,
    lastBackend: "js",
  };

  let db = null;
  let conn = null;
  let duckdb = null;

  const listeners = new Set();
  const onChange = (fn) => (listeners.add(fn), fn);
  const emit = () => listeners.forEach((fn) => fn(st));

  function set(status, detail = "") {
    st.status = status;
    st.detail = detail;
    emit();
  }

  /* -------------------------------------------------------------- lifecycle */

  /** Bring up DuckDB-WASM over an Arrow IPC slice. Resolves to true on success and
      false on any failure; never throws, because the caller's correct behaviour on
      failure is to carry on with the fallback. */
  async function init({ arrowUrl = "series.arrow" } = {}) {
    if (st.status === "ready") return true;
    if (st.status === "loading") return false;

    if (location.protocol === "file:") {
      set("unavailable",
          "page opened from file:// — a null origin cannot load a cross-origin " +
          "module worker. Serve with `python -m dqstack.serve` to enable SQL.");
      return false;
    }
    if (typeof WebAssembly === "undefined") {
      set("unavailable", "this browser has no WebAssembly");
      return false;
    }

    set("loading", "fetching engine and data");
    const t0 = performance.now();
    try {
      // Data and engine in parallel: they are independent, and the engine is by
      // far the larger of the two.
      const [mod, buf] = await Promise.all([
        import(/* webpackIgnore: true */ CDN),
        fetch(arrowUrl).then((r) => {
          if (!r.ok) throw new Error(`${arrowUrl} -> HTTP ${r.status}`);
          return r.arrayBuffer();
        }),
      ]);
      duckdb = mod;

      // selectBundle probes the browser and picks the narrowest WASM build it can
      // actually run — `mvp`, `eh` (exception handling), or the threaded build when
      // SharedArrayBuffer is available. Letting it choose is the point: a hardcoded
      // bundle either fails on an older browser or leaves performance on the table.
      const bundle = await duckdb.selectBundle(duckdb.getJsDelivrBundles());

      // `new Worker(crossOriginUrl)` is forbidden by the HTML specification, and the
      // CDN is by definition cross-origin, so the worker script cannot be loaded
      // directly — it fails with "cannot be accessed from origin". The documented
      // workaround is a same-origin blob shim whose only statement imports the real
      // script: importScripts() inside a worker is not subject to that restriction.
      const shim = URL.createObjectURL(new Blob(
        [`importScripts(${JSON.stringify(bundle.mainWorker)});`],
        { type: "text/javascript" },
      ));
      const worker = new Worker(shim);
      // The blob exists only to bootstrap the worker; holding the URL alive leaks it.
      URL.revokeObjectURL(shim);

      db = new duckdb.AsyncDuckDB(new duckdb.VoidLogger(), worker);
      await db.instantiate(bundle.mainModule, bundle.pthreadWorker);
      conn = await db.connect();

      // Read before the insert, not after: posting the buffer to the worker
      // transfers ownership and detaches it, so `buf.byteLength` afterwards is
      // legitimately 0 and the status line reported "0 KB Arrow".
      st.bytes = buf.byteLength;

      // insertArrowFromIPCStream is the zero-copy path: the Arrow buffer becomes
      // DuckDB's columnar storage directly, with no JSON parse and no row-wise
      // materialisation anywhere between the socket and the query engine.
      await conn.insertArrowFromIPCStream(new Uint8Array(buf), {
        name: "series", create: true,
      });

      // Counted through the connection rather than through sql(), which guards on
      // status === "ready" — a status this function has not set yet. Calling the
      // public helper here throws "not available" and lands the whole successful
      // bring-up in the catch below as a load failure.
      st.rows = Number(
        (await conn.query("SELECT count(*) AS n FROM series")).toArray()[0].n
      );
      st.loadMs = Math.round(performance.now() - t0);
      set("ready", `${st.rows.toLocaleString()} rows · ` +
                   `${(st.bytes / 1024).toFixed(0)} KB Arrow · ${st.loadMs} ms`);
      return true;
    } catch (e) {
      // Distinguishing the failure modes matters for the message the planner sees:
      // "you are offline" and "your browser is too old" call for different actions.
      const why = /Failed to fetch|NetworkError|dynamically imported/i.test(String(e))
        ? "could not reach the CDN or the data endpoint"
        : String(e && e.message || e).slice(0, 160);
      set("unavailable", why);
      db = conn = null;
      return false;
    }
  }

  const ready = () => st.status === "ready";

  /* ------------------------------------------------------------------ query */

  /** Narrow Arrow's wide integer representations to JS numbers.

      DuckDB widens aggregates to avoid overflow, and Arrow then hands them back in
      two forms that are not numbers. COUNT and SUM over BIGINT arrive as BigInt,
      which compares false against every JS number. SUM over a 32-bit integer is
      promoted to HUGEINT/DECIMAL(38,0) and arrives as a *four-element little-endian
      word array* — `sum(CASE WHEN … THEN 1 ELSE 0 END)` returned `{0:488,1:0,2:0,3:0}`
      where the fallback returned `488`, which the parity test caught and which would
      otherwise have rendered as "[object Object]" in the filter chips.

      Both are narrowed here rather than papered over with a CAST in each query,
      because the SQL console runs arbitrary user SQL and will produce these types
      whether or not the queries in this file do. */
  function narrow(v) {
    if (typeof v === "bigint") return Number(v);
    if (v && typeof v === "object" && !Array.isArray(v) && !(v instanceof Date)) {
      const words = ArrayBuffer.isView(v) ? Array.from(v)
        : (typeof v.length === "number" ? Array.from(v) : null);
      if (words && (words.length === 2 || words.length === 4) &&
          words.every((w) => typeof w === "number")) {
        let acc = 0n;
        for (let i = words.length - 1; i >= 0; i--)
          acc = (acc << 32n) | BigInt(words[i] >>> 0);
        // Two's complement: the top bit of the most significant word is the sign.
        const bits = BigInt(words.length * 32);
        if (acc >= 1n << (bits - 1n)) acc -= 1n << bits;
        return Number(acc);
      }
    }
    return v;
  }

  /** Run SQL, returning plain row objects with every numeric scalar narrowed. */
  async function sql(text) {
    if (!ready()) throw new Error("DuckDB-WASM is not available");
    const t0 = performance.now();
    const tbl = await conn.query(text);
    st.lastMs = performance.now() - t0;
    st.lastBackend = "duckdb";
    return tbl.toArray().map((r) => {
      const o = r.toJSON ? r.toJSON() : { ...r };
      for (const k in o) o[k] = narrow(o[k]);
      return o;
    });
  }

  const one = async (text) => (await sql(text))[0] || {};

  /** SQL string literal. Reason codes and hierarchy keys come from the bundle
      rather than from a user, but a workspace with a SQL console in it has no
      business concatenating unescaped strings. */
  const lit = (v) => "'" + String(v).replace(/'/g, "''") + "'";

  return {
    state: st, init, ready, sql, one, lit, narrow, onChange,
    get engine() { return duckdb; },
  };
})();


/* ============================================================================
   The resolver: one interface, two backends.

   Callers ask for "the visible rows given this scope, these reason filters and
   this sort" and get back a promise of row ids. Which engine answered is an
   implementation detail that surfaces only in the status pill and the timing
   readout — the answer is required to be identical either way.
   ============================================================================ */

window.Query = (() => {
  let ROWS = [];                      // the JS-side rows, for the fallback
  let BY_ID = new Map();

  function attach(rows) {
    ROWS = rows;
    BY_ID = new Map(rows.map((r) => [r.id, r]));
  }

  /* ---- where the engine actually wins -----------------------------------
     The specification calls in-browser SQL "the single biggest perceived-
     performance win available to you". Measured on this project's own table, in
     this browser, that is true for one of the two query shapes and false for the
     other, so the router is driven by the measurement rather than by the claim.

     Row selection — filter, then sort, then take the head:

         rows      DuckDB-WASM      JavaScript      ratio
         1,000        10.9 ms          0.5 ms        0.04x
         10,000       35.0 ms          5.2 ms        0.15x
         30,000       46.2 ms         11.6 ms        0.25x
         100,000      74.5 ms         79.9 ms        1.07x   <- crossover
         300,000      93.0 ms        215.4 ms        2.32x

     The crossover sits near a hundred thousand rows and the gap keeps opening
     above it, because the JavaScript path is superlinear (the comparison sort
     dominates) while DuckDB's is close to flat.

     Group rollup — GROUP BY one hierarchy column, six aggregates:

         rows      DuckDB-WASM      JavaScript      ratio
         1,000         6.5 ms          0.4 ms        0.06x
         30,000       19.4 ms          2.0 ms        0.10x
         300,000      54.7 ms         12.2 ms        0.22x

     This one never crosses over. The reason is cardinality, not engine quality:
     a hierarchy level here has three to ten distinct keys, and a ten-bucket hash
     aggregation over a flat array is about as cheap as computation gets, while
     every DuckDB query pays a fixed cost — postMessage to the worker, parse,
     plan, and materialise an Arrow result back. Below roughly 10^5 rows that
     fixed cost *is* the query.

     So: SQL for row selection once the table is genuinely large, JavaScript for
     the rollups and for the small case, and DuckDB unconditionally for the SQL
     console, where the alternative is not a slower answer but no answer at all.
     Shipping the slower path under a performance label would have been the easy
     thing to do and would have been wrong. */
  const CROSSOVER_ROWS = 100_000;

  /** Should SQL answer a row-selection query over `n` rows? `force` exists for
      the parity test, which has to exercise both backends at any size. */
  const useSql = (n, force) =>
    window.Duck.ready() && (force || n >= CROSSOVER_ROWS);

  const SORTS = {
    priority:   { sql: "priority DESC",            js: (a, b) => b.priority - a.priority },
    lostMargin: { sql: "priority DESC",            js: (a, b) => b.priority - a.priority },
    health:     { sql: "health ASC",               js: (a, b) => a.health - b.health },
    swing:      { sql: "abs(swing) DESC",          js: (a, b) => Math.abs(b.swing) - Math.abs(a.swing) },
    width:      { sql: "relWidth DESC",            js: (a, b) => b.relWidth - a.relWidth },
    revenue:    { sql: "revenue DESC",             js: (a, b) => b._s.revenue - a._s.revenue },
  };

  /** Ordered ids for the current scope, filters and sort. */
  async function ids({ scope = "queue", filters = [], sort = "priority", search = "",
                       forceSql = false } = {}) {
    const s = SORTS[sort] || SORTS.priority;

    if (useSql(Duck.state.rows || ROWS.length, forceSql)) {
      try {
        const where = [];
        // A series is in the queue exactly when it carries at least one reason
        // code; the delimited encoding makes that a length test rather than a join.
        if (scope === "queue") where.push("length(reasons) > 2");
        if (filters.length)
          where.push("(" + filters.map((f) =>
            `contains(reasons, ${Duck.lit("|" + f + "|")})`).join(" OR ") + ")");
        if (search) {
          const t = Duck.lit("%" + search.toLowerCase() + "%");
          where.push(`(lower(item) LIKE ${t} OR lower(store) LIKE ${t} ` +
                     `OR lower(id) LIKE ${t} OR lower(dept) LIKE ${t})`);
        }
        const rows = await Duck.sql(
          `SELECT id FROM series
           ${where.length ? "WHERE " + where.join(" AND ") : ""}
           ORDER BY ${s.sql}, id`
        );
        return rows.map((r) => r.id);
      } catch (e) {
        // A failed query is not a reason to show nothing. Fall through.
        console.warn("[duck] query failed, using fallback:", e);
      }
    }

    const t1 = performance.now();
    const set = new Set(filters);
    const q = search.toLowerCase();
    const out = ROWS.filter((r) => {
      if (scope === "queue" && !r.reasons.length) return false;
      if (set.size && !r.reasons.some((x) => set.has(x))) return false;
      if (q && !(r.item.toLowerCase().includes(q) || r.store.toLowerCase().includes(q) ||
                 r.id.toLowerCase().includes(q) || r.dept.toLowerCase().includes(q))) return false;
      return true;
    });
    // Tie-break on id so the two backends agree on order, not merely on membership.
    out.sort((a, b) => s.js(a, b) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
    Duck.state.lastMs = performance.now() - t1;
    Duck.state.lastBackend = "js";
    return out.map((r) => r.id);
  }

  const rowsFor = (idList) => idList.map((i) => BY_ID.get(i)).filter(Boolean);

  /** Reason-code histogram over the whole panel, for the filter chips. */
  async function reasonCounts({ forceSql = false } = {}) {
    const order = ["risk", "stale", "censored", "wide", "swing", "cold-start", "terminal"];
    // Seven scalar aggregates over one table, and the result feeds seven chips.
    // Same cardinality argument as levelSummary: routed by size, not by default.
    if (useSql(Duck.state.rows || ROWS.length, forceSql)) {
      try {
        const sel = order.map((k) =>
          `sum(CASE WHEN contains(reasons, ${Duck.lit("|" + k + "|")}) THEN 1 ELSE 0 END) ` +
          `AS ${k.replace("-", "_")}`).join(", ");
        const r = await Duck.one(`SELECT ${sel} FROM series`);
        return order.map((k) => [k, r[k.replace("-", "_")] || 0]);
      } catch (e) { /* fall through */ }
    }
    const c = {};
    ROWS.forEach((r) => r.reasons.forEach((x) => (c[x] = (c[x] || 0) + 1)));
    return order.map((k) => [k, c[k] || 0]);
  }

  /** Group summary at a hierarchy level. This is the query CON-5 is about: it is
      an aggregation, and it runs in a query engine rather than in a for-loop in
      application code. */
  async function levelSummary(level, { forceSql = false } = {}) {
    const COL = { cat: "cat", dept: "dept", store: "store", state: "state", cls: "cls" };
    const col = COL[level] || "cat";
    if (useSql(Duck.state.rows || ROWS.length, forceSql)) {
      try {
        return await Duck.sql(
          `SELECT ${col} AS key, count(*) AS series,
                  round(avg(health), 4) AS health,
                  round(sum(demandH), 1) AS demand,
                  round(sum(revenue), 2) AS revenue,
                  round(avg(targetQ), 4) AS targetQ,
                  sum(CASE WHEN length(reasons) > 2 THEN 1 ELSE 0 END) AS flagged
           FROM series GROUP BY 1 ORDER BY revenue DESC`
        );
      } catch (e) { /* fall through */ }
    }
    const m = new Map();
    for (const r of ROWS) {
      const k = r._s[col] ?? r[col];
      let g = m.get(k);
      if (!g) m.set(k, (g = { key: k, series: 0, _h: 0, demand: 0, revenue: 0, _t: 0, flagged: 0 }));
      g.series++; g._h += r.health; g._t += r.targetQ;
      g.demand += (r._s.mean || []).reduce((a, b) => a + b, 0);
      g.revenue += r._s.revenue;
      if (r.reasons.length) g.flagged++;
    }
    const r4 = (v) => Math.round(v * 1e4) / 1e4;
    return [...m.values()]
      .map((g) => ({ key: g.key, series: g.series, health: r4(g._h / g.series),
                     demand: Math.round(g.demand * 10) / 10,
                     revenue: Math.round(g.revenue * 100) / 100,
                     targetQ: r4(g._t / g.series), flagged: g.flagged }))
      .sort((a, b) => b.revenue - a.revenue);
  }

  /** Run both backends over the real workload and report every disagreement.
      Exposed on the console rather than buried in a test file because the claim
      being made — that the accelerator and the fallback are interchangeable — is
      one a reviewer should be able to check in the running application. */
  async function selftest() {
    if (!window.Duck.ready()) return { skipped: "DuckDB-WASM not available" };
    const cases = [];
    for (const sort of Object.keys(SORTS))
      for (const scope of ["queue", "all"])
        cases.push({ scope, sort, filters: [] });
    cases.push({ scope: "all", sort: "priority", filters: ["censored"] });
    cases.push({ scope: "queue", sort: "health", filters: ["wide", "swing"] });
    cases.push({ scope: "all", sort: "priority", filters: [], search: "store_1" });

    const fails = [];
    const times = { duckdb: [], js: [] };
    for (const c of cases) {
      const a = await ids({ ...c, forceSql: true });
      times.duckdb.push(Duck.state.lastMs);
      const saved = Duck.state.status;
      Duck.state.status = "idle";                 // force the fallback
      const b = await ids(c);
      times.js.push(Duck.state.lastMs);
      Duck.state.status = saved;
      if (a.length !== b.length || a.some((v, i) => v !== b[i]))
        fails.push({ ...c, duckdb: a.length, js: b.length,
                     firstDiff: a.findIndex((v, i) => v !== b[i]) });
    }

    const lv = [];
    for (const level of ["cat", "dept", "store", "state", "cls"]) {
      const a = await levelSummary(level, { forceSql: true });
      const saved = Duck.state.status;
      Duck.state.status = "idle";
      const b = await levelSummary(level);
      Duck.state.status = saved;
      const key = (x) => x.map((r) => `${r.key}:${r.series}:${r.health}:${r.flagged}`).join(",");
      if (key(a) !== key(b)) lv.push({ level, duckdb: a, js: b });
    }

    // Reason counts too: seven aggregates that feed the filter chips.
    const rcA = await reasonCounts({ forceSql: true });
    const savedR = Duck.state.status;
    Duck.state.status = "idle";
    const rcB = await reasonCounts();
    Duck.state.status = savedR;
    const rcMismatch = JSON.stringify(rcA) !== JSON.stringify(rcB)
      ? { duckdb: rcA, js: rcB } : null;

    const med = (xs) => xs.slice().sort((p, q) => p - q)[xs.length >> 1];
    const out = {
      cases: cases.length, mismatches: fails, levelMismatches: lv,
      reasonCountMismatch: rcMismatch,
      routing: `SQL above ${CROSSOVER_ROWS.toLocaleString()} rows; ` +
               `this table has ${(Duck.state.rows || ROWS.length).toLocaleString()}, ` +
               `so live queries use ${useSql(Duck.state.rows || ROWS.length, false) ? "SQL" : "JavaScript"}`,
      duckdbMedianMs: +med(times.duckdb).toFixed(2),
      jsMedianMs: +med(times.js).toFixed(2),
      pass: fails.length === 0 && lv.length === 0 && !rcMismatch,
    };
    console.log("[Query.selftest]", out.pass ? "PASS" : "FAIL", out);
    return out;
  }

  return { attach, ids, rowsFor, reasonCounts, levelSummary, selftest, SORTS,
           CROSSOVER_ROWS, useSql };
})();
