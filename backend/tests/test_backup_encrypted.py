"""Tests for encrypted backup round-trip."""

from __future__ import annotations

import pytest

from shmax.backup.manager import (
    BACKUP_FORMAT_ID,
    create_backup,
    create_encrypted_backup,
    is_encrypted,
    list_backups,
    restore_backup,
)


def _seed(root: Path) -> None:
    # local import to avoid circular at module load
    from pathlib import Path

    r: Path = root
    for sub in ("db", "config", "secrets", "state", "backups"):
        (r / sub).mkdir(parents=True, exist_ok=True)
    (r / "sessions" / "max").mkdir(parents=True, exist_ok=True)
    (r / "sessions" / "telegram").mkdir(parents=True, exist_ok=True)
    (r / "config" / "app.json").write_text('{"k":1}')
    (r / "state" / "marker.txt").write_text("state-marker")


from pathlib import Path


def test_plaintext_backup_still_works(data_dir):
    _seed(data_dir)
    out = data_dir / "backups" / "plain.shmax"
    manifest = create_backup(data_dir=data_dir, out_path=out)
    assert manifest.format == BACKUP_FORMAT_ID
    assert not is_encrypted(out)


def test_encrypted_backup_round_trip(data_dir):
    _seed(data_dir)
    out = data_dir / "backups" / "enc.shmax.enc"
    manifest = create_encrypted_backup(
        data_dir=data_dir, out_path=out, passphrase="correct horse battery staple"
    )
    assert is_encrypted(out)
    assert manifest.format == BACKUP_FORMAT_ID
    assert manifest.notes[0].startswith("encrypted")

    restored = restore_backup(archive_path=out, data_dir=data_dir / "restore", passphrase="correct horse battery staple")
    assert restored.format == BACKUP_FORMAT_ID
    assert (data_dir / "restore" / "config" / "app.json").read_text() == '{"k":1}'
    assert (data_dir / "restore" / "state" / "marker.txt").read_text() == "state-marker"


def test_wrong_passphrase_raises(data_dir):
    _seed(data_dir)
    out = data_dir / "backups" / "enc.shmax.enc"
    create_encrypted_backup(
        data_dir=data_dir, out_path=out, passphrase="right passphrase here"
    )
    with pytest.raises(ValueError):
        restore_backup(
            archive_path=out, data_dir=data_dir / "restore", passphrase="wrong passphrase"
        )


def test_list_backups_includes_encryption_flag(data_dir):
    _seed(data_dir)
    create_backup(data_dir=data_dir, out_path=data_dir / "backups" / "a.shmax")
    create_encrypted_backup(
        data_dir=data_dir,
        out_path=data_dir / "backups" / "b.shmax.enc",
        passphrase="eightpluschars",
    )
    rows = list_backups(data_dir)
    by_name = {r["name"]: r for r in rows}
    assert by_name["a.shmax"]["encrypted"] is False
    assert by_name["b.shmax.enc"]["encrypted"] is True


def test_short_passphrase_rejected(data_dir):
    _seed(data_dir)
    with pytest.raises(ValueError):
        create_encrypted_backup(
            data_dir=data_dir,
            out_path=data_dir / "backups" / "bad.shmax.enc",
            passphrase="short",
        )
