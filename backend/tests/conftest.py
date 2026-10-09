"""Pytest fixtures shared across tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient


@pytest.fixture()
def data_dir(tmp_path, monkeypatch) -> Path:
    """Per-test isolated persistent root (equivalent to /var/lib/shmax)."""
    d = tmp_path / "shmax-data"
    d.mkdir()
    monkeypatch.setenv("SHMAX_DATA_DIR", str(d))
    return d


@pytest_asyncio.fixture()
async def db(data_dir: Path):
    """Create a fresh SQLite-backed database for one test."""
    from shmax.database import Database, DatabaseSettings, create_all

    settings = DatabaseSettings(data_dir=data_dir)
    database = Database(settings)
    try:
        await create_all(database.engine)
        yield database
    finally:
        await database.dispose()


@pytest_asyncio.fixture()
async def db_session(db) -> AsyncIterator:
    async with db.session() as s:
        yield s


@pytest.fixture()
def client(data_dir: Path):
    """FastAPI TestClient with an isolated per-test persistent root."""
    from shmax.api.app import create_app
    from shmax.config import RuntimeMode, Settings

    settings = Settings(
        host="127.0.0.1",
        port=0,
        data_dir=data_dir,
        log_level="WARNING",
        runtime_mode=RuntimeMode.TEST)
    app = create_app(settings)
    with TestClient(app) as c:
        yield c
