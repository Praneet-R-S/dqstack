"""A burst of tills queues instead of failing: requests in progress never exceed the pool."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from dqstack.app import db as dbmod
from dqstack.app.main import Admission


def test_request_threads_follow_the_pool(monkeypatch):
    pg = replace(dbmod.settings, database_url="postgresql+psycopg://u:p@h/db")
    monkeypatch.setattr(dbmod, "settings", pg)
    for k in ("DQ_DB_POOL_SIZE", "DQ_DB_MAX_OVERFLOW", "DQ_REQUEST_THREADS"):
        monkeypatch.delenv(k, raising=False)
    assert dbmod.pool_limits() == (10, 20)
    assert dbmod.request_threads() == 26                 # 30 connections, 4 spare
    monkeypatch.setenv("DQ_DB_POOL_SIZE", "5")
    monkeypatch.setenv("DQ_DB_MAX_OVERFLOW", "10")
    assert dbmod.request_threads() == 11
    monkeypatch.setenv("DQ_REQUEST_THREADS", "3")
    assert dbmod.request_threads() == 3
    monkeypatch.setattr(dbmod, "settings", replace(pg, database_url="sqlite:///x.db"))
    monkeypatch.delenv("DQ_REQUEST_THREADS")
    assert dbmod.request_threads() is None               # SQLite: no limit


def test_admission_holds_api_requests_to_the_limit():
    live, peak = 0, 0

    async def app(scope, receive, send):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.01)
        live -= 1

    gate = Admission(app, n=3)

    async def burst(path, k):
        await asyncio.gather(*[gate({"type": "http", "path": path}, None, None)
                               for _ in range(k)])

    asyncio.run(burst("/api/pos/v1/messages", 40))
    assert peak == 3, "API requests in progress must not exceed the pool's share"
    peak = 0
    asyncio.run(burst("/site-assets/css/dive.css", 10))
    assert peak == 10, "files and pages are not held back"
