"""Hosting platforms' addresses are read the way this server needs them (Render)."""

from __future__ import annotations

import pytest

from dqstack.app.config import load


@pytest.mark.parametrize("given, expected", [
    ("postgres://u:p@db:5432/dq", "postgresql+psycopg://u:p@db:5432/dq"),
    ("postgresql://u:p@db/dq", "postgresql+psycopg://u:p@db/dq"),
    ("postgresql+psycopg://u:p@db/dq", "postgresql+psycopg://u:p@db/dq"),
    ("sqlite:///x.db", "sqlite:///x.db"),
])
def test_database_url_uses_psycopg3(monkeypatch, given, expected):
    monkeypatch.setenv("DQ_ENV", "development")
    monkeypatch.setenv("DATABASE_URL", given)
    assert load().database_url == expected


def test_public_url_falls_back_to_render_address(monkeypatch):
    monkeypatch.setenv("DQ_ENV", "development")
    monkeypatch.delenv("DQ_PUBLIC_URL", raising=False)
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://dqstack.onrender.com/")
    assert load().public_url == "https://dqstack.onrender.com"
    monkeypatch.setenv("DQ_PUBLIC_URL", "https://app.example.in")
    assert load().public_url == "https://app.example.in"


def test_production_still_refuses_without_https_address(monkeypatch):
    monkeypatch.setenv("DQ_ENV", "production")
    monkeypatch.setenv("DQ_SECRET_KEY", "k" * 44)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db/dq")
    monkeypatch.delenv("DQ_PUBLIC_URL", raising=False)
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    with pytest.raises(RuntimeError, match="DQ_PUBLIC_URL"):
        load()
