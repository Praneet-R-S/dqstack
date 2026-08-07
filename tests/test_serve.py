"""The read path: the Arrow query table, the routes, and the traversal guard.

The browser's DuckDB-WASM table is built from `series_table_arrow()`, and the
JavaScript fallback is built from the same bundle's nested objects. The two are
required to answer identically — `Query.selftest()` in the browser checks that at
runtime, and these tests check the Python half of the contract: that the table
carries the columns the client queries, that the types survive the round trip
through Arrow, and that the reason encoding the client filters on means what the
client thinks it means.

A column silently missing from this table is not a crash. It is a WHERE clause
that matches nothing, a worklist that quietly comes back empty, and a planner who
believes there are no exceptions today.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pyarrow as pa
import pytest

from dqstack.config import UI_DATA
from dqstack.serve import (
    SERIES_COLUMNS,
    Store,
    make_server,
    series_table_arrow,
    to_ipc,
)

pytestmark = pytest.mark.skipif(
    not (UI_DATA / "bundle.json").exists(),
    reason="no exported bundle; run `python -m dqstack.pipeline --export` first",
)


@pytest.fixture(scope="module")
def table() -> pa.Table:
    return series_table_arrow()


@pytest.fixture(scope="module")
def bundle() -> dict:
    return json.loads((UI_DATA / "bundle.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------------ the table

def test_every_declared_column_is_present(table):
    missing = set(SERIES_COLUMNS) - set(table.column_names)
    assert not missing, f"declared but not emitted: {sorted(missing)}"


def test_columns_the_client_queries_by_name_exist(table):
    """Named explicitly rather than derived from SERIES_COLUMNS, so that removing
    one from the dict is a test failure instead of a silently narrower table.
    Every name here appears in a WHERE, ORDER BY or GROUP BY in ui/js/duck.js."""
    required = {
        "id", "item", "store", "dept", "cat", "state",     # identity, grouping
        "cls", "health", "abc", "xyz",                     # Stage 1
        "censoredDays", "priceCompleteness",               # data health
        "revenue", "marginRate", "targetQ", "underage", "overage",
        "orderUpTo", "priority", "swing", "relWidth",      # triage and decision
        "reasons", "demandH", "demandW1",                  # derived
    }
    assert required <= set(table.column_names)


def test_row_count_matches_the_bundle(table, bundle):
    assert table.num_rows == len(bundle["series"]) == bundle["run"]["nSeries"]


def test_no_list_columns(table):
    """List columns would multiply the transfer for no query benefit, and the
    client's `narrow()` does not expect them. The per-horizon vectors are summed
    to scalars instead."""
    bad = [f.name for f in table.schema if pa.types.is_list(f.type)]
    assert not bad, f"list columns leaked into the query table: {bad}"


def test_reason_encoding_is_delimiter_wrapped(table, bundle):
    """The client filters with `contains(reasons, '|censored|')` and detects queue
    membership with `length(reasons) > 2`. Both depend on the encoding being a
    pipe on each end, including for the empty case, which must be exactly '||'."""
    got = table.column("reasons").to_pylist()
    for enc, src in zip(got, bundle["series"], strict=True):
        assert enc.startswith("|") and enc.endswith("|")
        assert enc == "|" + "|".join(src["reasons"] or []) + "|"
        # The queue predicate: empty is '||' (length 2), anything else is longer.
        assert (len(enc) > 2) == bool(src["reasons"])


def test_reason_membership_matches_the_source(table, bundle):
    """A substring test can match across a boundary if the delimiters are wrong —
    'cold-start' contains 'cold', so an unwrapped encoding would over-match."""
    encoded = table.column("reasons").to_pylist()
    codes = {c for s in bundle["series"] for c in s["reasons"]}
    for code in sorted(codes):
        by_substring = {i for i, e in enumerate(encoded) if f"|{code}|" in e}
        by_source = {i for i, s in enumerate(bundle["series"]) if code in s["reasons"]}
        assert by_substring == by_source, f"reason {code!r} mis-encoded"


def test_horizon_sums_match_the_source(table, bundle):
    """`demandH` and `demandW1` replace the per-horizon `mean` vector. Float32
    storage is deliberate (it halves the transfer), so the tolerance is a relative
    one at float32 precision rather than exact equality."""
    h = table.column("demandH").to_pylist()
    w1 = table.column("demandW1").to_pylist()
    for got_h, got_w1, s in zip(h, w1, bundle["series"], strict=True):
        want_h, want_w1 = sum(s["mean"]), sum(s["mean"][:7])
        assert got_h == pytest.approx(want_h, rel=1e-5, abs=1e-3)
        assert got_w1 == pytest.approx(want_w1, rel=1e-5, abs=1e-3)


def test_no_nulls_in_columns_the_client_sorts_on(table):
    """DuckDB sorts NULL last by default while the JavaScript comparator treats a
    null as NaN and produces an unstable order, so the two backends would disagree
    on any column carrying nulls. Keeping them null-free is what makes the parity
    guarantee cheap."""
    for col in ("priority", "health", "swing", "relWidth", "revenue", "id"):
        assert table.column(col).null_count == 0, f"{col} carries nulls"


def test_ipc_round_trip_preserves_values(table):
    """What the browser receives is the IPC stream, not the table, so the stream is
    what has to be checked."""
    import io

    import pyarrow.ipc as ipc

    back = ipc.open_stream(io.BytesIO(to_ipc(table))).read_all()
    assert back.num_rows == table.num_rows
    assert back.column_names == table.column_names
    assert back.column("id").to_pylist() == table.column("id").to_pylist()
    assert back.column("reasons").to_pylist() == table.column("reasons").to_pylist()


def test_transfer_is_far_smaller_than_the_bundle(table):
    """The point of shipping a scalar table rather than the whole bundle. The
    bundle is ~3.7 MB because of the fan, the history and the weight vectors, none
    of which any query touches."""
    arrow_kb = len(to_ipc(table)) / 1024
    bundle_kb = (UI_DATA / "bundle.json").stat().st_size / 1024
    assert arrow_kb < 400
    assert arrow_kb < bundle_kb / 5


def test_the_same_queries_the_client_runs_execute_in_duckdb(table):
    """The client's SQL is not exercised by the Python suite otherwise, and a
    function name that exists in DuckDB-WASM but not in DuckDB (or vice versa) is
    exactly the kind of drift that only shows up in a browser."""
    import duckdb

    con = duckdb.connect()
    con.register("series", table)

    ids = con.execute(
        "SELECT id FROM series WHERE length(reasons) > 2 "
        "AND (contains(reasons, '|wide|') OR contains(reasons, '|risk|')) "
        "ORDER BY priority DESC, id"
    ).fetchall()
    assert ids

    rollup = con.execute(
        """SELECT cat AS key, count(*) AS series, round(avg(health), 4) AS health,
                  round(sum(demandH), 1) AS demand, round(sum(revenue), 2) AS revenue,
                  round(avg(targetQ), 4) AS targetQ,
                  sum(CASE WHEN length(reasons) > 2 THEN 1 ELSE 0 END) AS flagged
           FROM series GROUP BY 1 ORDER BY revenue DESC"""
    ).fetch_df()
    assert len(rollup) > 1
    assert rollup["series"].sum() == table.num_rows
    assert (rollup["flagged"] <= rollup["series"]).all()


# ------------------------------------------------------------------ the server

@pytest.fixture(scope="module")
def base_url():
    srv = make_server(port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _get(url):
    with urllib.request.urlopen(url, timeout=20) as r:
        return r.status, r.headers, r.read()


def test_series_arrow_route_serves_the_table(base_url):
    import io

    import pyarrow.ipc as ipc

    status, headers, body = _get(f"{base_url}/series.arrow")
    assert status == 200
    assert "arrow" in headers["Content-Type"]
    assert ipc.open_stream(io.BytesIO(body)).read_all().num_rows > 0


def test_workspace_is_served_over_http(base_url):
    """DuckDB-WASM is an ES module whose worker is cross-origin, and a page with a
    null origin — which is what `file://` gives you — can load neither. The static
    half of the server is what gives the engine an origin to exist in."""
    status, headers, body = _get(base_url + "/")
    assert status == 200
    assert "text/html" in headers["Content-Type"]
    assert b"duck.js" in body, "index.html must load the query-engine module"

    status, headers, _ = _get(f"{base_url}/js/duck.js")
    assert status == 200
    assert "javascript" in headers["Content-Type"]


def test_path_traversal_is_refused(base_url):
    for attack in ("/../dqstack/config.py", "/../../Windows/win.ini",
                   "/css/../../dqstack/serve.py", "/%2e%2e/dqstack/config.py"):
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(base_url + attack)
        assert e.value.code == 404, f"{attack} was not refused"


def test_store_rollup_runs_in_the_store(base_url):
    """CON-5: aggregation is expressed as SQL and executed in the analytical store.
    Exercised through the route so the whole path is covered, not just the method."""
    status, _, body = _get(f"{base_url}/health-by-class")
    assert status == 200
    rows = json.loads(body)
    assert rows and {"planning_class", "series", "mean_health"} <= set(rows[0])
    assert sum(r["series"] for r in rows) == Store().sql(
        "SELECT count(*) AS n FROM dim_series"
    )["n"][0]
