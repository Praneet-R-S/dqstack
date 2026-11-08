"""Database engine, sessions and the shared base class.

One engine per process. Tests build their own against a throwaway database with
`configure()`, so they never touch a developer's data and can run in parallel.

SQLite is the development default and PostgreSQL the production one; every model
is written for both. The differences that matter are handled here: SQLite does not
enforce foreign keys unless asked, and has no row locks, so `with_for_update()` is
a no-op there and only PostgreSQL gets real concurrency protection.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timezone

from sqlalchemy import DateTime, String, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .config import settings


def new_id() -> str:
    """Random ids rather than counting integers. A sequential id invites a person to
    try the next number; a random one gives nothing to try. Company separation does
    not depend on this -- every query is filtered by company -- but it removes the
    temptation entirely."""
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class IdMixin:
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)


class CreatedMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 nullable=False)


_engine: Engine | None = None
_Session: sessionmaker[Session] | None = None


def _on_connect_sqlite(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA journal_mode=WAL")
    cur.close()


def configure(url: str | None = None, **kw) -> Engine:
    """Build (or rebuild) the engine and session factory."""
    global _engine, _Session
    url = url or settings.database_url
    args: dict = {}
    if url.startswith("sqlite"):
        args["connect_args"] = {"check_same_thread": False}
        if url in ("sqlite://", "sqlite:///:memory:"):
            from sqlalchemy.pool import StaticPool
            args["poolclass"] = StaticPool
    else:
        args["pool_pre_ping"] = True
        args["pool_size"] = kw.pop("pool_size", 10)
        args["max_overflow"] = kw.pop("max_overflow", 20)
    args.update(kw)
    eng = create_engine(url, future=True, **args)
    if url.startswith("sqlite"):
        event.listen(eng, "connect", _on_connect_sqlite)
    _engine = eng
    _Session = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False, future=True)
    return eng


def engine() -> Engine:
    if _engine is None:
        configure()
    assert _engine is not None
    return _engine


def SessionLocal() -> Session:
    if _Session is None:
        configure()
    assert _Session is not None
    return _Session()


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request, rolled back on any error."""
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def create_all() -> None:
    """Development and tests only. Deployments run `alembic upgrade head`."""
    from . import guards, models  # noqa: F401  -- models registers every table
    Base.metadata.create_all(engine())
    with engine().begin() as conn:
        guards.install(conn)
