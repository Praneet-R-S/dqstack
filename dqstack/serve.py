"""The read path: DuckDB analytical store, Arrow IPC transfer, HTTP API.

This closes three specification constraints that a static JSON bundle leaves
unexercised.

    CON-1  bulk numeric transfer shall use Apache Arrow IPC; JSON shall not be used
           for forecast matrices
    CON-3  forecasts shall be precomputed in batch; model inference shall not occur
           on the primary read path
    CON-5  numeric aggregation shall be executed in the analytical store or the
           client query engine, not in application code

The capstone cut substitutes DuckDB over Parquet for ClickHouse and keeps the API, so
this is the substituted architecture rather than a new one. Nothing here performs
inference: every endpoint reads rows the pipeline already wrote. The spec calls a
synchronous inference API "the most common architectural error in ML-powered planning
tools" — latency and cost for no user benefit when forecasts change nightly.

    python -m dqstack.serve --selftest   # exercise store + every route
    python -m dqstack.serve --measure    # the Arrow vs JSON comparison
    python -m dqstack.serve              # serve on 127.0.0.1:8000

On the framework choice: this uses the standard library, not FastAPI. The installed
environment pairs fastapi 0.118 with starlette 1.2, which is incompatible, and
downgrading a global site-packages to demonstrate a read path is a poor trade. Nothing
architectural is lost — the constraints above are about *where computation happens*,
not about the web framework. `create_fastapi_app()` is the production shape for an
environment with a sane dependency pin.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.ipc as ipc

from .config import ARTIFACTS, PROCESSED, RAW, UI_DATA

ARROW_MIME = "application/vnd.apache.arrow.stream"


# ---------------------------------------------------------------- store

class Store:
    """DuckDB over the Parquet lake.

    Views rather than tables: the pipeline owns the files, and copying them into a
    database would create a second version of the truth that goes stale the moment a
    run finishes. Aggregation is expressed as SQL and executed here, in the store,
    which is what CON-5 asks for — the alternative is pulling a million rows into
    pandas to sum them, which is both slower and the thing the constraint forbids.
    """

    def __init__(self) -> None:
        self.con = duckdb.connect(":memory:")
        self._register()

    def _register(self) -> None:
        def view(name: str, path: Path) -> None:
            if path.exists():
                self.con.execute(
                    f"CREATE OR REPLACE VIEW {name} AS "
                    f"SELECT * FROM read_parquet('{path.as_posix()}')"
                )

        view("sales", RAW / "sales.parquet")
        view("prices", RAW / "prices.parquet")
        view("calendar", RAW / "calendar.parquet")
        view("items", RAW / "items.parquet")
        view("stores", RAW / "stores.parquet")
        view("dim_series", PROCESSED / "dim_series_diagnostics.parquet")
        view("censoring_masks", PROCESSED / "censoring_masks.parquet")
        view("fact_ledger", ARTIFACTS / "decision_ledger.parquet")

    def views(self) -> list[str]:
        """Registered view names. duckdb_views() exposes `view_name`, not
        `table_name`; the column names differ across DuckDB versions."""
        df = self.sql("SELECT view_name FROM duckdb_views() WHERE NOT internal")
        return sorted(df["view_name"].tolist())

    def sql(self, query: str, **params) -> pd.DataFrame:
        return self.con.execute(query, params).fetch_df()

    def arrow(self, query: str, **params) -> pa.Table:
        return self.con.execute(query, params).fetch_arrow_table()

    # -------------------------------------------------- aggregations

    ROLLUP_LEVELS = ("cat_id", "dept_id", "store_id", "state_id", "item_id")

    def hierarchy_rollup(self, level: str = "cat_id") -> pd.DataFrame:
        """Ledger economics rolled up to a hierarchy level, computed in the store."""
        if level not in self.ROLLUP_LEVELS:
            raise ValueError(f"level must be one of {list(self.ROLLUP_LEVELS)}")
        src = "items" if level in ("cat_id", "dept_id", "item_id") else "stores"
        key_expr = (
            "split_part(l.series_id, '|', 1)" if src == "items"
            else "split_part(l.series_id, '|', 2)"
        )
        join_col = "item_id" if src == "items" else "store_id"
        return self.sql(
            f"""
            SELECT  d.{level}                     AS key,
                    COUNT(DISTINCT l.series_id)   AS series,
                    SUM(l.demand)                 AS demand,
                    SUM(l.sales)                  AS sales,
                    SUM(l.lost_sales)             AS lost_sales,
                    SUM(l.realized_margin)        AS realized_margin,
                    SUM(l.lost_margin)            AS lost_margin,
                    SUM(l.holding_cost)           AS holding_cost,
                    SUM(l.waste_cost)             AS waste_cost,
                    SUM(l.sales) / NULLIF(SUM(l.demand), 0) AS fill_rate
            FROM fact_ledger l
            JOIN {src} d ON d.{join_col} = {key_expr}
            GROUP BY 1
            ORDER BY realized_margin DESC
            """
        )

    def health_by_class(self) -> pd.DataFrame:
        return self.sql(
            """
            SELECT planning_class,
                   COUNT(*)           AS series,
                   AVG(health_score)  AS mean_health,
                   SUM(censored_days) AS censored_days,
                   SUM(ooa_days)      AS out_of_assortment_days,
                   AVG(adi)           AS mean_adi,
                   AVG(cv2)           AS mean_cv2
            FROM dim_series
            GROUP BY 1
            ORDER BY series DESC
            """
        )

    def series_history(self, series_id: str, days: int = 126) -> pa.Table:
        """One series' recent history, as Arrow."""
        return self.arrow(
            """
            SELECT s.date, s.sales, m.suspected_censored
            FROM sales s
            LEFT JOIN censoring_masks m
              ON m.series_id = $sid AND m.date = s.date
            WHERE s.item_id || '|' || s.store_id = $sid
            ORDER BY s.date DESC
            LIMIT $days
            """,
            sid=series_id,
            days=days,
        )


# ---------------------------------------------------------------- Arrow

def to_ipc(table: pa.Table) -> bytes:
    """Serialise to Arrow IPC stream format, which is what CON-1 specifies."""
    sink = io.BytesIO()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


def forecast_matrix_arrow(bundle_path: Path | None = None) -> pa.Table:
    """The forecast fan as a columnar Arrow table.

    One row per (series, horizon), one column per quantile level — the shape the spec
    has in mind when it says a 30,000 x 28 float matrix is roughly 25 MB as JSON and
    under 4 MB as Arrow, and that the parse cost difference is worse than the transfer
    cost difference.
    """
    bundle_path = bundle_path or (UI_DATA / "bundle.json")
    D = json.loads(bundle_path.read_text(encoding="utf-8"))
    qs = D["run"]["chartQuantiles"]
    H = D["run"]["horizon"]

    n = len(D["series"]) * H
    sid = np.empty(n, dtype=object)
    hz = np.empty(n, dtype="int16")
    names = [f"p{int(round(q * 1000)):03d}" for q in qs]
    cols = {nm: np.empty(n, dtype="float32") for nm in names}

    i = 0
    for s in D["series"]:
        for h, row in enumerate(s["fan"], start=1):
            sid[i] = s["id"]
            hz[i] = h
            for k, nm in enumerate(names):
                cols[nm][i] = row[k]
            i += 1

    return pa.table(
        {"series_id": pa.array(sid, type=pa.string()), "horizon": pa.array(hz),
         **{k: pa.array(v) for k, v in cols.items()}}
    )


def measure_transfer() -> dict:
    """Measure the Arrow-versus-JSON claim on this project's own matrix."""
    import time

    import pyarrow.parquet as pq

    t = forecast_matrix_arrow()

    t0 = time.perf_counter(); arrow_bytes = to_ipc(t); arrow_write = time.perf_counter() - t0
    t0 = time.perf_counter()
    ipc.open_stream(io.BytesIO(arrow_bytes)).read_all()
    arrow_read = time.perf_counter() - t0

    df = t.to_pandas()
    payload = {
        "series_id": df["series_id"].tolist(),
        "horizon": df["horizon"].tolist(),
        **{c: df[c].round(1).tolist() for c in df.columns if c.startswith("p")},
    }
    t0 = time.perf_counter()
    json_bytes = json.dumps(payload, separators=(",", ":")).encode()
    json_write = time.perf_counter() - t0
    t0 = time.perf_counter(); json.loads(json_bytes); json_read = time.perf_counter() - t0

    buf = io.BytesIO()
    pq.write_table(t, buf, compression="zstd")

    return {
        "rows": t.num_rows,
        "columns": t.num_columns,
        "arrow_ipc_bytes": len(arrow_bytes),
        "json_bytes": len(json_bytes),
        "parquet_zstd_bytes": buf.tell(),
        "json_over_arrow_size": len(json_bytes) / max(len(arrow_bytes), 1),
        "arrow_write_s": round(arrow_write, 4),
        "arrow_read_s": round(arrow_read, 4),
        "json_write_s": round(json_write, 4),
        "json_read_s": round(json_read, 4),
        "arrow_parse_speedup": json_read / max(arrow_read, 1e-9),
    }


SERIES_COLUMNS: dict[str, str] = {
    # identity / hierarchy
    "id": "str", "item": "str", "store": "str", "dept": "str", "cat": "str",
    "state": "str",
    # Stage 1 diagnostics
    "cls": "str", "health": "f4", "adi": "f4", "cv2": "f4", "abc": "str",
    "xyz": "str", "censoredDays": "i4", "ooaDays": "i4",
    "priceCompleteness": "f4", "ageWeeks": "i4",
    # economics
    "revenue": "f4", "price": "f4", "cost": "f4", "marginRate": "f4",
    "shelfLife": "i4", "targetQ": "f4", "underage": "f4", "overage": "f4",
    # Stage 7 decision
    "orderUpTo": "f4",
    # triage
    "priority": "f4", "swing": "f4", "relWidth": "f4",
}


def series_table_arrow(bundle_path: Path | None = None) -> pa.Table:
    """The scalar series attributes as Arrow, for DuckDB-WASM in the browser.

    Deliberately derived from the same bundle the fallback path reads, so a query
    answered by the browser's query engine and the same query answered by the
    JavaScript fallback are comparing identical inputs. If the two paths disagreed
    on the numbers, a query accelerator would be a correctness liability rather than
    a performance win, and there is a test that checks they do not.

    Nested per-series arrays (the fan, the history, the weight vector) are excluded.
    They are render inputs, not query inputs — nothing filters or groups on them, and
    a list column would multiply the transfer for no query benefit.
    """
    bundle_path = bundle_path or (UI_DATA / "bundle.json")
    D = json.loads(bundle_path.read_text(encoding="utf-8"))
    rows = D["series"]

    cols: dict[str, pa.Array] = {}
    for name, kind in SERIES_COLUMNS.items():
        raw = [r.get(name) for r in rows]
        if kind == "str":
            cols[name] = pa.array([("" if v is None else str(v)) for v in raw], pa.string())
        else:
            arr = np.array([(np.nan if v is None else v) for v in raw], dtype="f8")
            cols[name] = (
                pa.array(np.nan_to_num(arr).astype("i4"), pa.int32()) if kind == "i4"
                else pa.array(arr.astype("f4"), pa.float32())
            )
    # `mean` and `sd` are per-horizon vectors. Summed to horizon and first-week
    # totals, which are the scalars a planner actually filters and sorts on. The
    # horizon sd is deliberately not a column: adding daily variances would assume
    # independence across days, which the model does not, and a wrong scalar in a
    # query table is worse than an absent one.
    m = [r.get("mean") or [] for r in rows]
    cols["demandH"] = pa.array([float(sum(v)) for v in m], pa.float32())
    cols["demandW1"] = pa.array([float(sum(v[:7])) for v in m], pa.float32())

    # Reason codes arrive as a list per series; the browser filters on membership, so
    # a delimited string plus DuckDB's `contains` beats shipping a list column.
    cols["reasons"] = pa.array(
        ["|" + "|".join(r.get("reasons") or []) + "|" for r in rows], pa.string()
    )
    return pa.table(cols)


# ---------------------------------------------------------------- routes

def _routes(store: Store) -> dict:
    def js(obj, code: int = 200):
        return code, "application/json", json.dumps(obj, default=str).encode()

    def health(_q):
        return js({"status": "ok", "views": store.views()})

    def metrics(_q):
        f = ARTIFACTS / "metrics.json"
        if not f.exists():
            return js({"detail": "no run recorded yet"}, 404)
        return 200, "application/json", f.read_bytes()

    def rollup(q):
        try:
            return js(store.hierarchy_rollup(q.get("level", ["cat_id"])[0]).to_dict("records"))
        except ValueError as e:
            return js({"detail": str(e)}, 400)

    def health_by_class(_q):
        return js(store.health_by_class().to_dict("records"))

    def forecast_arrow(_q):
        return 200, ARROW_MIME, to_ipc(forecast_matrix_arrow())

    def series_history(q):
        sid = q.get("series_id", [""])[0]
        days = max(1, min(2000, int(q.get("days", ["126"])[0])))
        tbl = store.series_history(sid, days)
        if tbl.num_rows == 0:
            return js({"detail": f"unknown series {sid}"}, 404)
        return 200, ARROW_MIME, to_ipc(tbl)

    def series_arrow(_q):
        return 200, ARROW_MIME, to_ipc(series_table_arrow())

    def benchmark(_q):
        return js(measure_transfer())

    return {
        "/health": health,
        "/metrics": metrics,
        "/rollup": rollup,
        "/health-by-class": health_by_class,
        "/forecast.arrow": forecast_arrow,
        "/series.arrow": series_arrow,
        "/series/history.arrow": series_history,
        "/transfer-benchmark": benchmark,
    }


UI_ROOT = UI_DATA.parent

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".wasm": "application/wasm",
    ".map": "application/json",
}


def make_server(host: str = "127.0.0.1", port: int = 8000):
    """A read-only HTTP server over the analytical store, plus the workspace itself.

    The static half is not a convenience. DuckDB-WASM is an ES module and its worker
    is cross-origin, and a page opened from `file://` has a null origin, so the
    browser refuses both the dynamic import and the worker instantiation. The
    client-side query engine the spec calls "the single biggest perceived-performance
    win" simply cannot exist without an origin, so the app has to be served.
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, unquote, urlparse

    store = Store()
    routes = _routes(store)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            fn = routes.get(u.path)
            if fn is None:
                served = self._try_static(unquote(u.path))
                if served:
                    return
                self._send(*_routes_404(routes))
                return
            try:
                code, mime, body = fn(parse_qs(u.query))
            except Exception as e:                        # never leak a traceback
                code, mime = 500, "application/json"
                body = json.dumps({"detail": f"{type(e).__name__}: {e}"}).encode()
            self._send(code, mime, body)

        def _try_static(self, path: str) -> bool:
            """Serve ui/ with a path-traversal guard."""
            rel = "index.html" if path in ("/", "") else path.lstrip("/")
            target = (UI_ROOT / rel).resolve()
            try:
                target.relative_to(UI_ROOT.resolve())
            except ValueError:
                return False                       # escaped the ui/ root
            if not target.is_file():
                return False
            mime = _MIME.get(target.suffix.lower(), "application/octet-stream")
            self._send(200, mime, target.read_bytes())
            return True

        def _send(self, code, mime, body):
            self.send_response(code)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            # DuckDB-WASM's threaded build needs SharedArrayBuffer, which browsers
            # gate behind cross-origin isolation. Without these two headers it
            # silently falls back to the slower single-threaded bundle.
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Cross-Origin-Embedder-Policy", "credentialless")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            pass                                          # quiet by default

    return ThreadingHTTPServer((host, port), Handler)


def _routes_404(routes: dict):
    return 404, "application/json", json.dumps(
        {"detail": "no such route", "routes": sorted(routes)}
    ).encode()


def create_fastapi_app():
    """Production shape. Requires a compatible fastapi/starlette pair."""
    from fastapi import FastAPI, HTTPException, Query
    from fastapi.responses import Response

    app = FastAPI(title="DQ-Stack read API", version="1.0")
    store = Store()

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "views": store.views()}

    @app.get("/rollup/{level}")
    def rollup(level: str) -> list[dict]:
        try:
            return store.hierarchy_rollup(level).to_dict("records")
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/forecast.arrow")
    def forecast_arrow():
        return Response(to_ipc(forecast_matrix_arrow()), media_type=ARROW_MIME)

    @app.get("/series/{series_id}/history.arrow")
    def series_history(series_id: str, days: int = Query(126, ge=1, le=2000)):
        tbl = store.series_history(series_id, days)
        if tbl.num_rows == 0:
            raise HTTPException(404, f"unknown series {series_id}")
        return Response(to_ipc(tbl), media_type=ARROW_MIME)

    return app


# ---------------------------------------------------------------- entry points

def selftest() -> None:
    """Exercise the store and every route over a real socket."""
    import threading
    import urllib.error
    import urllib.request

    store = Store()
    print("store views:", store.views())
    print("\nrollup by category (aggregated in DuckDB, not in pandas):")
    print("  " + store.hierarchy_rollup("cat_id").round(2)
          .to_string(index=False).replace("\n", "\n  "))
    print("\nrollup by state:")
    print("  " + store.hierarchy_rollup("state_id").round(2)
          .to_string(index=False).replace("\n", "\n  "))
    print("\nhealth by planning class:")
    print("  " + store.health_by_class().round(3)
          .to_string(index=False).replace("\n", "\n  "))

    srv = make_server(port=0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    def get(path):
        with urllib.request.urlopen(base + path) as r:
            return r.status, r.headers.get("Content-Type"), r.read()

    try:
        print(f"\nserving on {base}")
        for path in ("/health", "/health-by-class", "/rollup?level=dept_id", "/metrics"):
            code, mime, body = get(path)
            print(f"  GET {path:<32s} {code}  {len(body):>10,} bytes")
            assert code == 200

        code, mime, body = get("/forecast.arrow")
        tbl = ipc.open_stream(io.BytesIO(body)).read_all()
        assert code == 200 and mime == ARROW_MIME
        print(f"  GET {'/forecast.arrow':<32s} {code}  {len(body):>10,} bytes  {mime}")
        print(f"      round-trips to {tbl.num_rows:,} rows x {tbl.num_columns} columns")

        code, mime, body = get("/series.arrow")
        tbl = ipc.open_stream(io.BytesIO(body)).read_all()
        assert code == 200 and mime == ARROW_MIME
        print(f"  GET {'/series.arrow':<32s} {code}  {len(body):>10,} bytes  {mime}")
        print(f"      the browser's DuckDB-WASM table: {tbl.num_rows:,} rows x "
              f"{tbl.num_columns} columns")

        # The static half exists so DuckDB-WASM has an origin; if it regresses, the
        # client-side query engine silently disappears and only the fallback runs.
        for path, needle in (("/", b"duck.js"), ("/js/duck.js", b"insertArrowFromIPCStream")):
            code, mime, body = get(path)
            assert code == 200 and needle in body, f"{path} missing {needle!r}"
            print(f"  GET {path:<32s} {code}  {len(body):>10,} bytes  {mime}")

        for attack in ("/../dqstack/config.py", "/css/../../dqstack/serve.py"):
            try:
                get(attack)
                raise AssertionError(f"path traversal not refused: {attack}")
            except urllib.error.HTTPError as e:
                assert e.code == 404
                print(f"  GET {attack:<32s} {e.code}  (traversal refused)")

        sid = store.sql("SELECT series_id FROM dim_series LIMIT 1")["series_id"].iloc[0]
        code, mime, body = get(f"/series/history.arrow?series_id={sid}&days=60")
        tbl = ipc.open_stream(io.BytesIO(body)).read_all()
        assert code == 200
        print(f"  GET {'/series/history.arrow':<32s} {code}  {tbl.num_rows} rows for {sid}")

        for path, want in (("/rollup?level=bogus", 400), ("/nope", 404)):
            try:
                get(path)
                raise AssertionError(f"{path} should have returned {want}")
            except urllib.error.HTTPError as e:
                assert e.code == want, f"{path}: got {e.code}, wanted {want}"
                print(f"  GET {path:<32s} {e.code}")
    finally:
        srv.shutdown()

    print("\nselftest passed. No endpoint performs inference (CON-3).")


def main() -> None:
    import argparse
    import sys

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="DQ-Stack read API")
    ap.add_argument("--measure", action="store_true", help="Arrow vs JSON comparison")
    ap.add_argument("--selftest", action="store_true", help="exercise store and routes")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()

    if a.measure:
        m = measure_transfer()
        print(f"forecast matrix: {m['rows']:,} rows x {m['columns']} columns\n")
        print(f"  Arrow IPC      {m['arrow_ipc_bytes'] / 1e6:8.2f} MB"
              f"   parse {m['arrow_read_s'] * 1000:7.1f} ms")
        print(f"  JSON           {m['json_bytes'] / 1e6:8.2f} MB"
              f"   parse {m['json_read_s'] * 1000:7.1f} ms")
        print(f"  Parquet+zstd   {m['parquet_zstd_bytes'] / 1e6:8.2f} MB")
        print(f"\n  JSON is {m['json_over_arrow_size']:.1f}x larger on the wire and "
              f"{m['arrow_parse_speedup']:.0f}x slower to parse.")
        print("  CON-1 exists for this reason.")
        return

    if a.selftest:
        selftest()
        return

    srv = make_server(a.host, a.port)
    print(f"DQ-Stack workspace on http://{a.host}:{a.port}/")
    print(f"  API routes: {', '.join(sorted(_routes(Store())))}")
    print("  Serving the workspace over HTTP so DuckDB-WASM has an origin to load from.")
    print("  Ctrl-C to stop")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
