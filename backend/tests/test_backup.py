"""Backup / restore tests."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from shmax.backup.manager import (
    BACKUP_FORMAT_ID,
    create_backup,
    inspect_backup,
    restore_backup,
    validate_backup,
)


def _seed_data_root(root: Path) -> None:
    (root / "db").mkdir(parents=True)
    (root / "config").mkdir()
    (root / "secrets").mkdir()
    (root / "state").mkdir()
    (root / "backups").mkdir()
    (root / "sessions" / "max").mkdir(parents=True)
    (root / "sessions" / "telegram").mkdir(parents=True)
    (root / "cache").mkdir()
    (root / "config" / "app.json").write_text('{"hello":"world"}')
    (root / "state" / "marker.txt").write_text("state-marker")


def test_backup_creates_archive(data_dir):
    _seed_data_root(data_dir)
    out = data_dir / "backups" / "test.shmax"
    manifest = create_backup(data_dir=data_dir, out_path=out)
    assert manifest.format == BACKUP_FORMAT_ID
    assert out.is_file()
    assert len(manifest.entries) >= 1


def test_backup_excludes_cache(data_dir):
    _seed_data_root(data_dir)
    (data_dir / "cache" / "throwaway.bin").write_bytes(b"x" * 1024)
    out = data_dir / "backups" / "test.shmax"
    create_backup(data_dir=data_dir, out_path=out)
    with tarfile.open(out, "r:gz") as tar:
        names = [m.name for m in tar.getmembers()]
    assert not any("cache" in n for n in names)


def test_inspect_and_validate(data_dir):
    _seed_data_root(data_dir)
    out = data_dir / "backups" / "test.shmax"
    create_backup(data_dir=data_dir, out_path=out)
    manifest = inspect_backup(out)
    assert manifest.format == BACKUP_FORMAT_ID
    ok, errors = validate_backup(out)
    assert ok, errors


def test_restore_round_trip(data_dir, tmp_path):
    _seed_data_root(data_dir)
    out = data_dir / "backups" / "test.shmax"
    create_backup(data_dir=data_dir, out_path=out)
    target = tmp_path / "restore"
    manifest = restore_backup(out, target)
    assert manifest.format == BACKUP_FORMAT_ID
    assert (target / "state" / "marker.txt").is_file()
    assert (target / "state" / "marker.txt").read_text() == "state-marker"
