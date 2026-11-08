"""Build step 1 -- the database itself.

* The Alembic migrations build exactly the schema the models describe, so a deployed
  database and a test database never differ.
* The append-only rule holds inside the database: raw SQL cannot rewrite a stock
  movement or an activity-log entry, on SQLite (development) or PostgreSQL
  (production). Only a departing company's purge, holding its own permit, may delete,
  and nothing may ever update.

The PostgreSQL half runs when DQ_TEST_POSTGRES_URL points at an empty database (CI
starts one; locally: see deploy/README.md). Without it those tests are skipped and
say so, rather than pretending to pass.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, delete, func, select, text
from sqlalchemy.exc import DBAPIError

from dqstack.app import db as dbmod
from dqstack.app.db import Base
from dqstack.app.models import ActivityLog, Company, Product, PurgePermit, StockMovement, Store

ROOT = Path(__file__).resolve().parents[2]
PG_URL = os.environ.get("DQ_TEST_POSTGRES_URL")


def _alembic(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logger"] = False
    return cfg


def _migrated_engine(url: str):
    command.upgrade(_alembic(url), "head")
    return create_engine(url, future=True)


def _seed(eng) -> tuple[str, str]:
    """Two companies, each with one movement and one log entry, written with Core
    inserts so no ORM safety net is involved."""
    ids = []
    with eng.begin() as conn:
        for name in ("Alpha", "Beta"):
            cid = dbmod.new_id()
            sid, pid = dbmod.new_id(), dbmod.new_id()
            conn.execute(Company.__table__.insert().values(id=cid, name=name, rules={}, setup={}))
            conn.execute(Store.__table__.insert().values(id=sid, company_id=cid, code="S1",
                                                         name="One"))
            conn.execute(Product.__table__.insert().values(id=pid, company_id=cid, sku="P1",
                                                           name="Rice 1kg"))
            conn.execute(StockMovement.__table__.insert().values(
                id=dbmod.new_id(), company_id=cid, store_id=sid, product_id=pid,
                qty_milli=5000, type="opening"))
            conn.execute(ActivityLog.__table__.insert().values(
                id=dbmod.new_id(), company_id=cid, action="store.created"))
            ids.append(cid)
    return ids[0], ids[1]


def _refused(eng, sql: str, **params) -> None:
    with pytest.raises(DBAPIError) as e:
        with eng.begin() as conn:
            conn.execute(text(sql), params)
    assert "append-only" in str(e.value)


def _assert_guards(eng) -> None:
    a, b = _seed(eng)
    for table in ("activity_log", "stock_movements"):
        _refused(eng, f"UPDATE {table} SET company_id = company_id")
        _refused(eng, f"DELETE FROM {table} WHERE company_id = :c", c=a)
    # A purge permit for A lets A's rows go -- and only A's, and never an update.
    with eng.begin() as conn:
        conn.execute(PurgePermit.__table__.insert().values(company_id=a, reason="test"))
    _refused(eng, "UPDATE activity_log SET action = 'rewritten' WHERE company_id = :c", c=a)
    _refused(eng, "DELETE FROM activity_log WHERE company_id = :c", c=b)
    with eng.begin() as conn:
        for table in ("activity_log", "stock_movements"):
            conn.execute(text(f"DELETE FROM {table} WHERE company_id = :c"), {"c": a})
        conn.execute(delete(PurgePermit.__table__).where(PurgePermit.company_id == a))
    with eng.connect() as conn:
        left = conn.execute(select(StockMovement.company_id, func.count())
                            .group_by(StockMovement.company_id)).all()
        assert left == [(b, 1)]
        assert conn.scalar(select(func.count()).select_from(ActivityLog)) == 1


def _assert_no_drift(eng) -> None:
    with eng.connect() as conn:
        diffs = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}),
                                 Base.metadata)
    assert diffs == [], f"migrations and models disagree: {diffs}"


# ------------------------------------------------------------------- SQLite

def test_create_all_path_has_the_guards():
    """Development and the test suite build tables with create_all(); they must be
    protected the same way as a migrated database."""
    _assert_guards(dbmod.engine())


def test_migrations_match_the_models_on_sqlite(tmp_path):
    eng = _migrated_engine(f"sqlite:///{tmp_path / 'm.db'}")
    _assert_no_drift(eng)
    eng.dispose()


def test_migrated_sqlite_has_the_guards(tmp_path):
    eng = _migrated_engine(f"sqlite:///{tmp_path / 'g.db'}")
    _assert_guards(eng)
    eng.dispose()


def test_downgrade_and_upgrade_again(tmp_path):
    url = f"sqlite:///{tmp_path / 'd.db'}"
    cfg = _alembic(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    eng = create_engine(url, future=True)
    with eng.connect() as conn:
        names = {r[0] for r in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type IN ('table','trigger')")}
    assert names <= {"alembic_version"}, names
    command.upgrade(cfg, "head")
    _assert_no_drift(eng)
    eng.dispose()


# --------------------------------------------------------------- PostgreSQL

pg = pytest.mark.skipif(not PG_URL, reason="set DQ_TEST_POSTGRES_URL to test PostgreSQL")


@pg
def test_postgres_migrates_matches_models_and_guards():
    from .conftest import reset_postgres
    dbmod.engine().dispose()               # the per-test fixture may hold this database
    reset_postgres(PG_URL)                 # start from empty even if a run was aborted
    cfg = _alembic(PG_URL)
    eng = _migrated_engine(PG_URL)
    try:
        _assert_no_drift(eng)
        _assert_guards(eng)
        with pytest.raises(DBAPIError) as e:
            with eng.begin() as conn:
                conn.exec_driver_sql("TRUNCATE activity_log")
        assert "append-only" in str(e.value)
        command.downgrade(cfg, "base")
        with eng.connect() as conn:
            left = conn.exec_driver_sql(
                "SELECT count(*) FROM pg_tables WHERE schemaname = 'public' "
                "AND tablename <> 'alembic_version'").scalar()
        assert left == 0
    finally:
        eng.dispose()
