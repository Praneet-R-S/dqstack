"""A chain at the specification's first-year scale, made in bulk, and the checks that
nothing was lost after a load test (section 15: "100 stores, 10,000 products per
store, 200 tills at once"). CI's scale job runs it; it also works against staging.

    python scripts/scale_seed.py seed --url postgresql+psycopg://... \
        --stores 100 --products 10000 --targets scale-targets.json
    python scripts/loadtest.py --base-url ... --targets scale-targets.json ... --json run.json
    python scripts/scale_seed.py verify --url ... --summary run.json

seed: one company with the stores (each set to "connected", each with its own POS API
key limited to that store), the products, and opening stock for every product in
every store (stores x products stock rows, and one opening movement each, so the
ledger balances from the start). Bulk SQL, not the API: a million rows in seconds.
The schema must already exist (alembic upgrade head). Writes the targets file
scripts/loadtest.py reads. The API keys in it are for this throwaway data only.

verify: for each load-test summary, every bill sent is in the database exactly once,
with its lines, and with exactly one sale movement per line; and across the whole
company, stock on hand equals the sum of the movements for every store and product,
which is what a lost or doubled update would break. Exits non-zero otherwise.

Works on PostgreSQL and on SQLite (for a small trial run on a laptop).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DQ_ENV", "test")

from sqlalchemy import bindparam, create_engine, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from dqstack.app.models import (ApiKey, Company, Product, StockMovement,  # noqa: E402
                                StockOnHand, Store)

COMPANY_NAME = "Scale test chain"


def _url(args) -> str:
    url = args.url or os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("give --url or set DATABASE_URL")
    return url


def _defaults(table, given: set[str]) -> tuple[list[str], list[str], list]:
    """Columns the bulk insert does not name but the table requires, with the model's
    own defaults, so a column added to the models later does not break the seed."""
    cols, exprs, params = [], [], []
    for col in table.columns:
        if col.name in given or col.nullable:
            continue
        d = col.default
        if d is None or not (d.is_scalar or d.is_callable):
            raise SystemExit(f"{table.name}.{col.name} is required and has no default; "
                             "teach scripts/scale_seed.py about it")
        value = d.arg(None) if d.is_callable else d.arg
        name = f"d_{table.name}_{col.name}"
        cols.append(col.name)
        exprs.append(f":{name}")
        params.append(bindparam(name, value, type_=col.type))
    return cols, exprs, params


def _insert_select(conn, table, columns: dict[str, str], from_sql: str, params: dict) -> int:
    """INSERT INTO table (named columns + required defaults) SELECT ... FROM ..."""
    extra_cols, extra_exprs, binds = _defaults(table, set(columns))
    names = list(columns) + extra_cols
    exprs = list(columns.values()) + extra_exprs
    sql = text(f"INSERT INTO {table.name} ({', '.join(names)}) "
               f"SELECT {', '.join(exprs)} {from_sql}").bindparams(*binds)
    return conn.execute(sql, params).rowcount


def seed(args) -> int:
    url = _url(args)
    eng = create_engine(url, future=True)
    pg = eng.dialect.name == "postgresql"
    new_id = "replace(gen_random_uuid()::text, '-', '')" if pg else "lower(hex(randomblob(16)))"
    sku_expr = "'SKU' || lpad(g.n::text, 6, '0')" if pg else "'SKU' || printf('%06d', g.n)"
    opened = datetime.now(timezone.utc) - timedelta(days=1)
    t0 = time.perf_counter()

    with Session(eng) as db:
        if db.scalar(select(Company).where(Company.name == COMPANY_NAME)) is not None:
            raise SystemExit(f"'{COMPANY_NAME}' already exists here; seed an empty database")
        co = Company(name=COMPANY_NAME, rules={}, setup={})
        db.add(co)
        db.flush()
        stores, keys = [], []
        for i in range(1, args.stores + 1):
            s = Store(company_id=co.id, code=f"S{i:03d}", name=f"Store {i}",
                      state="Maharashtra", mode="connected")
            db.add(s)
            stores.append(s)
        db.flush()
        for s in stores:
            raw = "dqk_" + secrets.token_urlsafe(32)
            db.add(ApiKey(company_id=co.id, name=f"POS {s.code}", prefix=raw[:12],
                          key_hash=hashlib.sha256(raw.encode()).hexdigest(), store_id=s.id))
            keys.append({"store_code": s.code, "api_key": raw})
        db.commit()
        company_id = co.id

    p = {"c": company_id, "n": args.products, "opened": opened}
    with eng.begin() as conn:
        n_products = _insert_select(conn, Product.__table__, {
            "id": new_id, "company_id": ":c", "sku": sku_expr,
            "name": "'Product ' || g.n", "category": "'Category ' || (g.n % 50)",
            "unit": "'each'", "price_paise": "1000", "mrp_paise": "1200",
            "cost_paise": "700", "created_at": ":opened"},
            "FROM (WITH RECURSIVE s(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM s "
            "WHERE n < :n) SELECT n FROM s) g", p)
    t_products = time.perf_counter()
    cross = ("FROM stores st CROSS JOIN products pr "
             "WHERE st.company_id = :c AND pr.company_id = :c")
    opening = args.opening_units * 1000
    with eng.begin() as conn:
        n_soh = _insert_select(conn, StockOnHand.__table__, {
            "company_id": ":c", "store_id": "st.id", "product_id": "pr.id",
            "on_hand_milli": str(opening), "avg_cost_paise": "700", "known": ":yes",
            "known_since": ":opened", "opening_milli": str(opening),
            "baseline_at": ":opened", "last_movement_at": ":opened"}, cross,
            {**p, "yes": True})
        n_mv = _insert_select(conn, StockMovement.__table__, {
            "id": new_id, "company_id": ":c", "store_id": "st.id", "product_id": "pr.id",
            "qty_milli": str(opening), "type": "'opening'", "source_type": "'scale_seed'",
            "unit_cost_paise": "700", "at": ":opened"}, cross, p)
    with eng.begin() as conn:
        if pg:
            conn.execute(text("ANALYZE"))
        skus = list(conn.scalars(select(Product.sku).where(Product.company_id == company_id)
                                 .order_by(Product.sku)))
    took = time.perf_counter() - t0
    Path(args.targets).write_text(json.dumps({"company_id": company_id, "skus": skus,
                                              "stores": keys}), encoding="utf-8")
    print(f"seeded 1 company, {len(keys)} stores (one API key each), {n_products} products, "
          f"{n_soh} stock rows and {n_mv} opening movements in {took:.1f}s "
          f"(products {t_products - t0:.1f}s); targets in {args.targets}")
    return 0


def verify(args) -> int:
    eng = create_engine(_url(args), future=True)
    failures: list[str] = []
    report: dict = {"runs": []}
    with eng.connect() as conn:
        company_id = conn.scalar(text("SELECT id FROM companies WHERE name = :n"),
                                 {"n": COMPANY_NAME})
        if company_id is None:
            raise SystemExit(f"no '{COMPANY_NAME}' here; run seed first")
        c = {"c": company_id}
        total_lines = 0
        for path in args.summary:
            s = json.loads(Path(path).read_text(encoding="utf-8"))
            q = {**c, "bills": s["bill_prefix"] + "%", "msgs": s["message_prefix"] + "%"}
            bills = conn.scalar(text(
                "SELECT count(*) FROM bills WHERE company_id = :c AND bill_number LIKE :bills "
                "AND state = 'paid'"), q)
            distinct = conn.scalar(text(
                "SELECT count(DISTINCT store_id || ':' || bill_number) FROM bills "
                "WHERE company_id = :c AND bill_number LIKE :bills"), q)
            messages = conn.scalar(text(
                "SELECT count(*) FROM pos_messages WHERE company_id = :c "
                "AND external_id LIKE :msgs"), q)
            lines, line_qty = conn.execute(text(
                "SELECT count(*), coalesce(sum(l.qty_milli), 0) FROM bill_lines l "
                "JOIN bills b ON b.id = l.bill_id "
                "WHERE b.company_id = :c AND b.bill_number LIKE :bills"), q).one()
            moves, move_qty, move_keys = conn.execute(text(
                "SELECT count(*), coalesce(sum(m.qty_milli), 0), "
                "count(DISTINCT m.idempotency_key) FROM stock_movements m "
                "JOIN bills b ON b.id = m.source_id "
                "WHERE m.company_id = :c AND m.type = 'sale' AND m.source_type = 'bill' "
                "AND b.bill_number LIKE :bills"), q).one()
            sent, sent_lines = s["bills_sent"], s["lines_sent"]
            run = {"summary": path, "bills_sent": sent, "bills_in_db": bills,
                   "distinct_bills": distinct, "pos_messages": messages,
                   "lines_sent": sent_lines, "bill_lines": lines,
                   "sale_movements": moves, "distinct_movement_keys": move_keys,
                   "units_sold": -move_qty / 1000, "load_errors": s["errors"],
                   "latency_ms": s["latency_ms"], "bills_per_second": s["bills_per_second"]}
            report["runs"].append(run)
            checks = [
                (s["errors"] == 0, f"{s['errors']} requests failed during the load test"),
                (bills == sent, f"{sent} bills sent, {bills} paid bills in the database"),
                (distinct == bills, f"{bills - distinct} bills are in the database twice"),
                (messages == sent, f"{sent} bills sent, {messages} POS messages recorded"),
                (lines == sent_lines, f"{sent_lines} lines sent, {lines} bill lines stored"),
                (moves == sent_lines, f"{sent_lines} lines sold, {moves} sale movements"),
                (move_keys == moves, f"{moves - move_keys} sale movements are repeats"),
                (move_qty == -line_qty, f"movements take {-move_qty} milli, bills sold {line_qty}"),
            ]
            failures += [f"{path}: {msg}" for ok, msg in checks if not ok]
            total_lines += sent_lines
        all_sales = conn.scalar(text(
            "SELECT count(*) FROM stock_movements WHERE company_id = :c AND type = 'sale'"), c)
        unbalanced = conn.scalar(text(
            "SELECT count(*) FROM stock_on_hand s LEFT JOIN ("
            "  SELECT store_id, product_id, sum(qty_milli) AS q FROM stock_movements "
            "  WHERE company_id = :c GROUP BY store_id, product_id) m "
            "ON m.store_id = s.store_id AND m.product_id = s.product_id "
            "WHERE s.company_id = :c AND s.on_hand_milli <> coalesce(m.q, 0)"), c)
        stock_rows = conn.scalar(text(
            "SELECT count(*) FROM stock_on_hand WHERE company_id = :c"), c)
    report.update({"sale_movements_in_company": all_sales, "lines_sent_in_all_runs": total_lines,
                   "stock_rows": stock_rows, "stock_rows_not_matching_ledger": unbalanced})
    if all_sales != total_lines:
        failures.append(f"{all_sales} sale movements in the company, {total_lines} lines sent")
    if unbalanced:
        failures.append(f"{unbalanced} stock rows differ from the sum of their movements")
    report["failures"] = failures
    report["pass"] = not failures
    print(json.dumps(report, indent=2, default=str))
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print("PASS: no sale lost or doubled; every stock row equals its ledger" if not failures
          else "FAIL:\n  " + "\n  ".join(failures))
    return 0 if not failures else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("seed")
    a.add_argument("--url")
    a.add_argument("--stores", type=int, default=100)
    a.add_argument("--products", type=int, default=10000)
    a.add_argument("--opening-units", type=int, default=1000)
    a.add_argument("--targets", default="scale-targets.json")
    b = sub.add_parser("verify")
    b.add_argument("--url")
    b.add_argument("--summary", action="append", required=True)
    b.add_argument("--report")
    args = ap.parse_args()
    sys.exit(seed(args) if args.cmd == "seed" else verify(args))
