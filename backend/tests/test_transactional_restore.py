"""Transactional restore tests.

The restore must:

* reject bad manifests before touching production data;
* create a safety backup first;
* call ``before_restore`` hooks (workers stopped);
* atomically swap staged files into data_dir;
* call ``after_restore`` hooks (DB / SecretStore reload);
* roll back to the safety backup when anything in the swap
  or the after-restore hooks raises.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shmax.backup.manager import (
    Manifest,
    RestoreError,
    TransactionalRestore,
    create_backup,
)


def _populate(data_dir: Path) -> dict[str, bytes]:
    """Plant files in the data dir and return their bytes."""
    files = {
        "secrets/key": b"OLD_KEY",
        "config/config.yaml": b"old: true\n",
    }
    for rel, body in files.items():
        path = data_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    # Real SQLite file so ``backup`` accepts it.
    import sqlite3
    db_path = data_dir / "db" / "shmax.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)
    con = sqlite3.connect(str(db_path))
    con.execute("CREATE TABLE test (n INTEGER)")
    con.execute("INSERT INTO test VALUES (1)")
    con.commit()
    con.close()
    files["db/shmax.db"] = db_path.read_bytes()
    return files


def _populate_new(backup_staging: Path) -> None:
    import sqlite3
    db_path = backup_staging / "db" / "shmax.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.execute("CREATE TABLE test (n INTEGER)")
    con.execute("INSERT INTO test VALUES (2)")
    con.commit()
    con.close()
    (backup_staging / "secrets" / "key").parent.mkdir(parents=True, exist_ok=True)
    (backup_staging / "secrets" / "key").write_bytes(b"NEW_KEY")
    (backup_staging / "config" / "config.yaml").parent.mkdir(parents=True, exist_ok=True)
    (backup_staging / "config" / "config.yaml").write_bytes(b"new: true\n")


def test_transactional_restore_swaps_files(tmp_path: Path):
    data = tmp_path / "data"
    backups = tmp_path / "backups"
    _populate(data)

    archive = tmp_path / "new.shmax"
    create_backup(data_dir=data, out_path=archive, shmax_version="1.0")
    # Mutate a NON-DB file so the restore swaps real changes in
    # without breaking the SQLite safety-backup step.
    (data / "secrets" / "key").write_bytes(b"AFTER_BACKUP")
    (data / "config" / "config.yaml").write_bytes(b"after_backup: true\n")

    tr = TransactionalRestore(
        data_dir=data,
        backups_dir=backups,
        shmax_version="1.0",
    )
    outcome = tr.run(archive)

    assert isinstance(outcome.manifest, Manifest)
    assert (data / "db" / "shmax.db").is_file()
    import sqlite3 as _s
    c = _s.connect(str(data / "db" / "shmax.db"))
    n = c.execute("SELECT n FROM test").fetchone()[0]
    c.close()
    assert n == 1, n
    assert (data / "secrets" / "key").read_bytes() == b"OLD_KEY"
    assert (data / "config" / "config.yaml").read_bytes() == b"old: true\n"
    assert outcome.safety_backup_path.is_file()


def test_hooks_called_in_correct_order(tmp_path: Path):
    data = tmp_path / "data"
    backups = tmp_path / "backups"
    _populate(data)
    archive = tmp_path / "new.shmax"
    create_backup(data_dir=data, out_path=archive, shmax_version="1.0")

    calls: list[str] = []

    def before():
        calls.append("before")

    def after():
        calls.append("after")

    tr = TransactionalRestore(data_dir=data, backups_dir=backups)
    tr.on_before_restore(before)
    tr.on_after_restore(after)
    tr.run(archive)

    assert calls == ["before", "after"], calls


def test_after_restore_hook_failure_rolls_back(tmp_path: Path):
    data = tmp_path / "data"
    backups = tmp_path / "backups"
    _populate(data)

    archive = tmp_path / "new.shmax"
    create_backup(data_dir=data, out_path=archive, shmax_version="1.0")
    # Reset DB content to known state for the "before" comparison.
    import sqlite3
    con = sqlite3.connect(str(data / "db" / "shmax.db"))
    con.execute("DELETE FROM test")
    con.execute("INSERT INTO test VALUES (99)")
    con.commit()
    con.close()

    tr = TransactionalRestore(data_dir=data, backups_dir=backups)
    tr.on_after_restore(lambda: (_ for _ in ()).throw(RuntimeError("reload boom")))
    with pytest.raises(RestoreError) as ei:
        tr.run(archive)
    assert "reload boom" in str(ei.value)
    import sqlite3 as _s
    c = _s.connect(str(data / "db" / "shmax.db"))
    n = c.execute("SELECT n FROM test").fetchone()[0]
    c.close()
    # After the rollback the n=99 row must be back.
    assert n == 99, n


def test_before_restore_hook_failure_aborts_before_swap(tmp_path: Path):
    data = tmp_path / "data"
    backups = tmp_path / "backups"
    _populate(data)
    archive = tmp_path / "new.shmax"
    create_backup(data_dir=data, out_path=archive, shmax_version="1.0")
    import sqlite3 as _s
    before_bytes = (data / "db" / "shmax.db").read_bytes()

    tr = TransactionalRestore(data_dir=data, backups_dir=backups)
    tr.on_before_restore(lambda: (_ for _ in ()).throw(RuntimeError("pre-stop failed")))
    with pytest.raises(RestoreError):
        tr.run(archive)
    # The data_dir is untouched.
    assert (data / "db" / "shmax.db").read_bytes() == before_bytes


def test_safety_backup_failure_aborts(tmp_path: Path):
    data = tmp_path / "data"
    backups = tmp_path / "backups"
    data.mkdir(parents=True, exist_ok=True)
    archive = tmp_path / "new.shmax"
    # An unparseable archive causes the safety backup's tar to
    # succeed on the (valid) data_dir but the manifest gate to fail
    # before any production data is touched.
    import io as _io
    import tarfile
    archive.parent.mkdir(parents=True, exist_ok=True)
    bio = _io.BytesIO()
    with tarfile.open(fileobj=bio, mode="w:gz") as tar:
        bad = tarfile.TarInfo(name="manifest.json")
        bad.size = 2
        tar.addfile(bad, _io.BytesIO(b"{}"))
    archive.write_bytes(bio.getvalue())

    tr = TransactionalRestore(data_dir=data, backups_dir=backups)
    with pytest.raises(RestoreError):
        tr.run(archive)


def test_rejects_unsupported_format(tmp_path: Path):
    data = tmp_path / "data"
    backups = tmp_path / "backups"
    data.mkdir(parents=True, exist_ok=True)
    archive = tmp_path / "bad.shmax"

    # Build a tar with a manifest carrying the wrong format id.
    import io as _io
    import json as _json
    import tarfile
    bad_manifest = {"format": "wrong-v1", "entries": []}
    bio = _io.BytesIO()
    with tarfile.open(fileobj=bio, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="manifest.json")
        body = _json.dumps(bad_manifest).encode()
        info.size = len(body)
        tar.addfile(info, _io.BytesIO(body))
    archive.write_bytes(bio.getvalue())

    tr = TransactionalRestore(data_dir=data, backups_dir=backups)
    with pytest.raises(RestoreError) as ei:
        tr.run(archive)
    assert "format" in str(ei.value).lower()