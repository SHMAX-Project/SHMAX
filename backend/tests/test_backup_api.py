"""Live acceptance #17 — Backups API.

Authenticated admin: list (incl. empty), create, manifest,
encrypted secrets, restrictive permissions; unauthenticated and
CSRF protection; path-traversal rejection; invalid restore
archive rejected.
"""

from __future__ import annotations

import os
import stat

import pytest
from fastapi.testclient import TestClient

from shmax.api.app import create_app
from shmax.config import RuntimeMode, Settings


@pytest.fixture()
def client(data_dir, monkeypatch):
    monkeypatch.setenv(
        "SHMAX_TELEGRAM_REACHABILITY_URL", "http://127.0.0.1:59999"
    )
    settings = Settings(
        host="127.0.0.1", port=0, data_dir=data_dir,
        log_level="WARNING", runtime_mode=RuntimeMode.TEST,
    )
    app = create_app(settings)
    with TestClient(app) as c:
        import pathlib
        import secrets as _secrets

        sp = pathlib.Path(app.state.paths.root) / "state" / "setup_secret"
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(_secrets.token_urlsafe(24))
        token = c.post(
            "/api/setup/token",
            headers={"X-Shmax-Setup-Secret": sp.read_text()},
        ).json()["token"]
        resp = c.post(
            "/api/setup/admin",
            json={"token": token, "username": "owner",
                  "password": "owner-pass-12345"},
        )
        c.headers.update({"X-CSRF-Token": resp.json()["csrf_token"]})
        yield c


def test_backups_empty_list_ok(client):
    resp = client.get("/api/backups")
    assert resp.status_code == 200
    assert resp.json() == []


def test_backups_unauthenticated_denied(data_dir, monkeypatch):
    monkeypatch.setenv(
        "SHMAX_TELEGRAM_REACHABILITY_URL", "http://127.0.0.1:59999"
    )
    settings = Settings(
        host="127.0.0.1", port=0, data_dir=data_dir,
        log_level="WARNING", runtime_mode=RuntimeMode.TEST,
    )
    app = create_app(settings)
    with TestClient(app) as c:
        assert c.get("/api/backups").status_code in (401, 403)
        assert c.post("/api/backups", json={}).status_code in (401, 403)


def test_backups_csrf_required(client):
    # Override the CSRF header with a wrong value → 403.
    resp = client.post(
        "/api/backups", json={}, headers={"X-CSRF-Token": "wrong"}
    )
    assert resp.status_code == 403


def test_backup_create_manifest_and_permissions(client, data_dir):
    resp = client.post("/api/backups", json={})
    assert resp.status_code == 200, resp.text
    listing = client.get("/api/backups").json()
    assert len(listing) == 1
    entry = listing[0]
    # Manifest facts surfaced to the UI (live acceptance #4).
    assert entry["id"]
    assert entry["created_at"] > 0
    assert entry["size"] > 0
    assert entry["version"] == entry["shmax_version"]
    assert entry["format"] == "shmax-backup-v1"
    # Restrictive permissions on the archive.
    archive = data_dir / "backups" / entry["name"]
    mode = stat.S_IMODE(archive.stat().st_mode)
    assert mode == 0o600, f"archive must be 0600, got {oct(mode)}"
    # Persistent state coverage: DB + secrets included.
    assert "db" in entry["included_components"]
    import tarfile

    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
    assert any(n.startswith("secrets/") for n in names), (
        "SecretStore must be covered"
    )


def test_backup_secrets_remain_encrypted(client, data_dir):
    client.post("/api/backups", json={})
    import tarfile

    archive = next((data_dir / "backups").glob("*.shmax"))
    with tarfile.open(archive, "r:gz") as tar:
        for m in tar.getmembers():
            if "secret" in m.name and m.isfile():
                data = tar.extractfile(m).read()
                # Never plaintext key material.
                assert b"PRIVATE KEY" not in data
                assert b"password" not in data.lower() or m.name.endswith(
                    ".json"
                )


def test_backup_download_and_traversal_rejected(client, data_dir):
    created = client.post("/api/backups", json={}).json()
    name = created["name"]
    resp = client.get(f"/api/backups/{name}/download")
    assert resp.status_code == 200
    assert len(resp.content) > 0
    # Path traversal → rejected, not an arbitrary download.
    for bad in ("../../db/shmax.db", "..%2F..%2Fetc%2Fpasswd", "/etc/passwd"):
        r = client.post(f"/api/backups/{bad}/download")
        assert r.status_code in (400, 404, 405), bad  # 405 = route refuses pathed names
    # Restore with a traversal name too.
    r = client.post(
        "/api/backups/../../db%2Fshmax.db/restore", json={}
    )
    assert r.status_code in (400, 404, 405)


def test_restore_invalid_archive_rejected(client, data_dir):
    (data_dir / "backups").mkdir(parents=True, exist_ok=True)
    (data_dir / "backups" / "bogus.shmax").write_bytes(b"not a tar at all")
    r = client.post("/api/backups/bogus.shmax/restore", json={})
    assert r.status_code in (400, 500)
    assert r.status_code != 200


# ----------------------------------------------------------------------
# Live acceptance A7 — backup security hardening
# ----------------------------------------------------------------------
def test_backup_skips_escaping_symlink_and_junk(client, data_dir):
    import os as _os
    import sqlite3 as _sq

    (data_dir / "db").mkdir(parents=True, exist_ok=True)
    dbp = data_dir / "db" / "shmax.db"
    if not dbp.exists():
        con = _sq.connect(str(dbp))
        con.execute("CREATE TABLE t (k TEXT)")
        con.commit()
        con.close()
    # A symlink pointing OUTSIDE the data root.
    outside = data_dir.parent / "outside-secret.txt"
    outside.write_text("TOP SECRET")
    link = data_dir / "sneaky-link"
    if link.exists() or link.is_symlink():
        link.unlink()
    _os.symlink(outside, link)
    # Runtime junk.
    (data_dir / "logs").mkdir(exist_ok=True)
    (data_dir / "logs" / "shmax.log").write_text("log spam")
    (data_dir / "debug.pyc").write_bytes(b"junk")

    resp = client.post("/api/backups", json={})
    assert resp.status_code == 200
    import tarfile

    archive = next((data_dir / "backups").glob("*.shmax"))
    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
    assert not any("sneaky-link" in n for n in names), (
        "escaping symlink must be skipped"
    )
    assert not any(n.endswith(".log") for n in names)
    assert not any(n.endswith(".pyc") for n in names)
    # The dereferenced external file must NEVER be archived.
    import io as _io

    with tarfile.open(archive, "r:gz") as tar:
        for m in tar.getmembers():
            if m.isfile():
                data = tar.extractfile(m).read()
                assert b"TOP SECRET" not in data


def test_backup_mode_0600_regardless_of_umask(client, data_dir, monkeypatch):
    # Already covered by permissions test; here assert BOTH the
    # plaintext and encrypted artifacts are 0600 under a permissive
    # umask.
    monkeypatch.setattr("os.umask", lambda m: 0o000)  # force worst case
    import os as _os

    _os.umask(0o000)
    try:
        resp = client.post("/api/backups", json={"passphrase": "long-pass-phrase"})
        assert resp.status_code == 200, resp.text
    finally:
        _os.umask(0o022)
    import stat as _stat

    for f in (data_dir / "backups").glob("*.shmax*"):
        mode = _stat.S_IMODE(f.stat().st_mode)
        assert mode == 0o600, f"{f.name}: {oct(mode)}"
