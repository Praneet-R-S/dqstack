"""Database-level protection for the append-only tables.

The ORM listeners in models.py stop application code from updating or deleting a
stock movement or an activity-log entry. These triggers stop everything else: a
hand-written SQL statement, a script run against production, a future bug that uses
a Core `update()` and so skips the ORM. On-hand stock is only trustworthy if the
movements under it cannot be quietly rewritten, and the log is only evidence if it
cannot be edited after the fact.

UPDATE is always refused. DELETE is refused unless the row's company holds a purge
permit, which exists only inside the transaction that erases a departing company
(see `PurgePermit`). TRUNCATE is refused on PostgreSQL. A database superuser can
still drop a trigger; the application's database role must therefore not be one
(deploy/README.md).

The Alembic baseline migration carries a frozen copy of this SQL, as migrations
should. `tests/app/test_step1_migrations.py` checks both paths refuse the same way.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

APPEND_ONLY_TABLES = ("stock_movements", "activity_log")


def sqlite_statements() -> list[str]:
    out: list[str] = []
    for t in APPEND_ONLY_TABLES:
        out.append(f"""
CREATE TRIGGER IF NOT EXISTS {t}_append_only_update BEFORE UPDATE ON {t}
BEGIN
  SELECT RAISE(ABORT, '{t} is append-only: correct a mistake with a new record');
END""")
        out.append(f"""
CREATE TRIGGER IF NOT EXISTS {t}_append_only_delete BEFORE DELETE ON {t}
WHEN NOT EXISTS (SELECT 1 FROM purge_permits WHERE company_id = OLD.company_id)
BEGIN
  SELECT RAISE(ABORT, '{t} is append-only: correct a mistake with a new record');
END""")
    return out


def postgres_statements() -> list[str]:
    out = ["""
CREATE OR REPLACE FUNCTION dq_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' AND EXISTS (
       SELECT 1 FROM purge_permits WHERE company_id = OLD.company_id) THEN
    RETURN OLD;
  END IF;
  RAISE EXCEPTION '% is append-only: correct a mistake with a new record', TG_TABLE_NAME
    USING ERRCODE = 'restrict_violation';
END
$$"""]
    for t in APPEND_ONLY_TABLES:
        out.append(f"DROP TRIGGER IF EXISTS {t}_append_only ON {t}")
        out.append(f"""
CREATE TRIGGER {t}_append_only BEFORE UPDATE OR DELETE ON {t}
  FOR EACH ROW EXECUTE FUNCTION dq_append_only()""")
        out.append(f"DROP TRIGGER IF EXISTS {t}_no_truncate ON {t}")
        out.append(f"""
CREATE TRIGGER {t}_no_truncate BEFORE TRUNCATE ON {t}
  FOR EACH STATEMENT EXECUTE FUNCTION dq_append_only()""")
    return out


def statements(dialect: str) -> list[str]:
    if dialect == "sqlite":
        return sqlite_statements()
    if dialect == "postgresql":
        return postgres_statements()
    raise RuntimeError(f"No append-only guard written for the {dialect} database.")


def install(conn: Connection) -> None:
    # text() rather than exec_driver_sql: psycopg reads a bare % (PL/pgSQL's format
    # marker) as a parameter placeholder, and text() escapes it for each driver.
    for sql in statements(conn.dialect.name):
        conn.execute(text(sql))
