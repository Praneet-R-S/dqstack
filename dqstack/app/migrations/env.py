"""Alembic environment.

The database address comes from, in order: a connection handed in by code (tests and
`manage.py`), `-x url=...` on the command line, then the application settings
(DATABASE_URL). SQLite runs in batch mode so later column changes work there too.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from dqstack.app import models  # noqa: F401  -- registers every table
from dqstack.app.config import settings
from dqstack.app.db import Base

config = context.config
if config.config_file_name and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _url() -> str:
    x = context.get_x_argument(as_dictionary=True)
    return x.get("url") or config.get_main_option("sqlalchemy.url") or settings.database_url


def _configure(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata,
                      render_as_batch=connection.dialect.name == "sqlite",
                      compare_type=True)


def run_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True,
                      compare_type=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    conn = config.attributes.get("connection")
    if conn is not None:
        _configure(conn)
        with context.begin_transaction():
            context.run_migrations()
        return
    eng = create_engine(_url(), poolclass=pool.NullPool, future=True)
    with eng.connect() as connection:
        _configure(connection)
        with context.begin_transaction():
            context.run_migrations()
    eng.dispose()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
