"""Migration runner tests — verify Alembic upgrade head actually runs."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from shmax.database import Database, DatabaseSettings, run_migrations_sync
from shmax.config import RuntimeMode


def _tables_present(db_path: Path) -> set[str]:
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'alembic_%'"
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        con.close()


def test_migration_creates_all_tables(tmp_path, monkeypatch):
    monkeypatch.setenv("SHMAX_DATA_DIR", str(tmp_path))
    settings = DatabaseSettings(data_dir=tmp_path)
    db = Database(settings)
    try:
        # Empty database, run migration.
        rev = run_migrations_sync(settings.url)
        assert rev != "head"
        # All expected tables exist.
        expected = {
            "settings",
            "admin_users",
            "setup_tokens",
            "max_accounts",
            "max_chats",
            "telegram_accounts",
            "telegram_targets",
            "telegram_users",
            "routes",
            "acl_rules",
            "message_map",
            "delivery_queue",
            "audit_log",
            "schema_meta",
        }
        present = _tables_present(tmp_path / "db" / "shmax.db")
        missing = expected - present
        assert not missing, f"missing tables: {missing}"
    finally:
        asyncio.run(db.dispose())


def test_migration_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("SHMAX_DATA_DIR", str(tmp_path))
    settings = DatabaseSettings(data_dir=tmp_path)
    db = Database(settings)
    try:
        r1 = run_migrations_sync(settings.url)
        r2 = run_migrations_sync(settings.url)
        assert r1 == r2
    finally:
        asyncio.run(db.dispose())


def test_migration_records_revision(tmp_path, monkeypatch):
    monkeypatch.setenv("SHMAX_DATA_DIR", str(tmp_path))
    settings = DatabaseSettings(data_dir=tmp_path)
    db = Database(settings)
    try:
        run_migrations_sync(settings.url)
        con = sqlite3.connect(str(tmp_path / "db" / "shmax.db"))
        try:
            row = con.execute("SELECT version_num FROM alembic_version LIMIT 1").fetchone()
            assert row is not None
            assert row[0] == "0012_multi_group"
        finally:
            con.close()
    finally:
        asyncio.run(db.dispose())
