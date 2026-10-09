"""Live acceptance A6 — the production host-side update
transaction, exercised in a STAGED fake host environment.

Real code paths: release resolution (staged metadata), artifact
download (staged bytes), SHA verification, safe archive
extraction, deployment install/rollback under a temp
/opt/shmax, host-visible readiness. Persistent data is a staged
/var/lib equivalent that must NEVER change.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from shmax.host.updater import HostUpdater, UpdateError


def _release_archive(version: str, *, compose: str | None = None) -> bytes:
    """A REAL release tar.gz: compose.yaml + app tree."""
    compose_text = compose if compose is not None else (
        'services:\n  web:\n    ports:\n      - "192.168.10.20:8787:8787"\n'
    )
    bio = io.BytesIO()
    with tarfile.open(fileobj=bio, mode="w:gz") as tar:
        for name, data in [
            ("compose.yaml", compose_text.encode()),
            ("backend/shmax/VERSION", version.encode()),
            ("backend/requirements.txt", b"fastapi\n"),
        ]:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return bio.getvalue()


class _Staged:
    """Fake host: fetcher, runner, readiness; staged metadata."""

    def __init__(self, archive: bytes, version: str, sha: str):
        self.archive = archive
        self.version = version
        self.sha = sha
        self.runner_calls: list[list] = []
        self.healthy = False
        self.fail_runner_on: set[str] = set()

    async def fetch(self, url: str) -> bytes:
        assert url.startswith("https://")
        return self.archive

    async def resolve(self, channel: str) -> dict:
        return {
            "version": self.version,
            "channel": channel,
            "artifact_url": "https://github.com/o/r/releases/download/x/a.tar.gz",
            "sha256": self.sha,
        }

    async def run(self, argv: list):
        from types import SimpleNamespace

        self.runner_calls.append(argv)
        if "up" in argv and "-d" in argv and self.fail_runner_on:
            # Simulate a compose failure when armed.
            if self.fail_runner_on == {"up-fail"}:
                return SimpleNamespace(returncode=1, stdout="", stderr="boom")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    async def ready(self, url: str) -> bool:
        return self.healthy

    def set_healthy(self) -> None:
        self.healthy = True


def _staged_updater(tmp_path: Path, staged: _Staged, *, version="0.1.0"):
    opt = tmp_path / "opt" / "shmax"
    data = tmp_path / "var" / "lib" / "shmax"
    data.mkdir(parents=True)
    (data / "db").mkdir()
    import sqlite3 as _sq

    con = _sq.connect(str(data / "db" / "shmax.db"))
    con.execute("CREATE TABLE user_data (k TEXT)")
    con.execute("INSERT INTO user_data VALUES ('user-data')")
    con.commit()
    con.close()
    (data / "secrets").mkdir()
    (data / "secrets" / "master.key").write_bytes(b"key-material")
    return HostUpdater(
        opt_dir=opt,
        data_dir=data,
        current_version=version,
        runner=staged.run,
        fetcher=staged.fetch,
        readiness=staged.ready,
        releases_resolver=staged.resolve,
    )


def test_successful_update_full_flow(tmp_path):
    archive = _release_archive("0.2.0")
    sha = hashlib.sha256(archive).hexdigest()
    staged = _Staged(archive, "0.2.0", sha)
    # Mark healthy BEFORE constructing: the updater binds the
    # readiness callable in its constructor.
    staged.healthy = True
    updater = _staged_updater(tmp_path, staged)
    result = asyncio.run(updater.apply())
    assert result.version == "0.2.0"
    assert result.stages[-1] == "completed"
    # Compose was invoked with the fixed command set.
    assert any("compose" in c and "up" in c for c in staged.runner_calls)
    # Previous deployment cleaned up after success.
    assert not (tmp_path / "opt" / "shmax.prev").exists()
    # Persistent data untouched.
    import sqlite3 as _sq

    con = _sq.connect(str(tmp_path / "var/lib/shmax/db/shmax.db"))
    assert con.execute("SELECT k FROM user_data").fetchall() == [("user-data",)]
    con.close()
    assert (tmp_path / "var/lib/shmax/secrets/master.key").read_bytes() == b"key-material"


def test_sha_mismatch_never_installs(tmp_path):
    archive = _release_archive("0.2.0")
    staged = _Staged(archive, "0.2.0", "a" * 64)  # wrong sha
    updater = _staged_updater(tmp_path, staged)
    with pytest.raises(UpdateError):
        asyncio.run(updater.apply())
    # Nothing installed, nothing recreated.
    assert not (tmp_path / "opt" / "shmax").exists()
    assert staged.runner_calls == []


def test_readiness_timeout_triggers_rollback(tmp_path):
    archive = _release_archive("0.2.0")
    sha = hashlib.sha256(archive).hexdigest()
    staged = _Staged(archive, "0.2.0", sha)
    # NEVER healthy → readiness timeout → rollback restores prev.
    opt = tmp_path / "opt" / "shmax"
    opt.mkdir(parents=True)
    prev_compose = 'services:\n  web:\n    ports:\n      - "192.168.10.20:8787:8787"\n'
    (opt / "compose.yaml").write_text(prev_compose)
    updater = _staged_updater(tmp_path, staged)
    # Shrink the readiness wait for the test.
    import shmax.host.updater as up

    old_timeout = up.READY_TIMEOUT_SECONDS
    up.READY_TIMEOUT_SECONDS = 2.0
    try:
        with pytest.raises(UpdateError):
            asyncio.run(updater.apply())
    finally:
        up.READY_TIMEOUT_SECONDS = old_timeout
    # The PREVIOUS deployment is back in place.
    assert (opt / "compose.yaml").read_text() == prev_compose
    import sqlite3 as _sq

    con = _sq.connect(str(tmp_path / "var/lib/shmax/db/shmax.db"))
    assert con.execute("SELECT k FROM user_data").fetchall() == [("user-data",)]
    con.close()


def test_rollback_failure_reported_not_disguised(tmp_path):
    archive = _release_archive("0.2.0")
    sha = hashlib.sha256(archive).hexdigest()
    staged = _Staged(archive, "0.2.0", sha)
    staged.fail_runner_on = {"up-fail"}  # compose up fails ALWAYS
    opt = tmp_path / "opt" / "shmax"
    opt.mkdir(parents=True)
    (opt / "compose.yaml").write_text("services: {}\n")
    updater = _staged_updater(tmp_path, staged)
    import shmax.host.updater as up

    up.READY_TIMEOUT_SECONDS = 1.0
    try:
        with pytest.raises(UpdateError) as ei:
            asyncio.run(updater.apply())
    finally:
        up.READY_TIMEOUT_SECONDS = 180.0
    # Never pretend rollback succeeded.
    assert ei.value.rollback_failed is True
    assert "manual recovery" in str(ei.value).lower()


def test_deployment_config_preserved(tmp_path):
    archive = _release_archive(
        "0.2.0", compose="services:\n  web:\n    ports: []\n"
    )
    sha = hashlib.sha256(archive).hexdigest()
    staged = _Staged(archive, "0.2.0", sha)
    opt = tmp_path / "opt" / "shmax"
    opt.mkdir(parents=True)
    local_compose = 'services:\n  web:\n    ports:\n      - "10.0.0.5:8787:8787"\n'
    (opt / "compose.yaml").write_text(local_compose)
    (opt / ".env").write_text("SHMAX_PORT=8787\n")
    seen_urls: list[str] = []

    async def _ready(url):
        # The readiness URL must reflect the PRESERVED bind.
        assert "10.0.0.5:8787" in url
        seen_urls.append(url)
        return True

    staged.ready = _ready
    updater = _staged_updater(tmp_path, staged)
    result = asyncio.run(updater.apply())
    assert result.version == "0.2.0"
    assert (opt / "compose.yaml").read_text() == local_compose
    assert (opt / ".env").read_text() == "SHMAX_PORT=8787\n"


def test_bad_archive_structure_refused(tmp_path):
    # An archive WITHOUT compose.yaml → refused before install.
    bio = io.BytesIO()
    with tarfile.open(fileobj=bio, mode="w:gz") as tar:
        data = b"not-a-release"
        info = tarfile.TarInfo(name="random.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    staged = _Staged(bio.getvalue(), "0.2.0", hashlib.sha256(bio.getvalue()).hexdigest())
    updater = _staged_updater(tmp_path, staged)
    with pytest.raises(UpdateError):
        asyncio.run(updater.apply())
    assert not (tmp_path / "opt" / "shmax").exists()


def test_cli_apply_is_real_updater(tmp_path, monkeypatch, capsys):
    """The `shmax update apply` CLI wires the REAL HostUpdater —
    not the in-process fake that answered 'host helper
    unavailable' (live acceptance A6 blocker)."""
    from shmax.cli import main as cli

    data = tmp_path / "data"
    (data / "db").mkdir(parents=True)
    import sqlite3 as _sq

    con = _sq.connect(str(data / "db" / "shmax.db"))
    con.execute("CREATE TABLE user_data (k TEXT)")
    con.commit()
    con.close()
    monkeypatch.setenv("SHMAX_DATA_DIR", str(data))
    archive = _release_archive("0.2.0")
    sha = hashlib.sha256(archive).hexdigest()
    staged = _Staged(archive, "0.2.0", sha)

    async def _ready(url):
        return True

    staged.ready = _ready
    def _fake_init(self, **kw):
        # NEVER touch the real /opt/shmax in tests.
        setattr(self, "_opt", tmp_path / "opt" / "shmax")
        setattr(self, "_data", Path(kw.get("data_dir")))
        setattr(self, "_current", kw.get("current_version"))
        setattr(self, "_channel", kw.get("channel", "stable"))
        setattr(self, "_runner", staged.run)
        setattr(self, "_fetcher", staged.fetch)
        setattr(self, "_readiness", staged.ready)
        setattr(self, "_resolver", staged.resolve)
        setattr(self, "_compose_file", "compose.yaml")
        setattr(self, "_stages", [])

    monkeypatch.setattr(
        "shmax.host.updater.HostUpdater.__init__", _fake_init
    )
    rc = cli.main(["update", "apply", "--channel", "stable"])
    out = capsys.readouterr()
    assert rc == 0, out.err + out.out
    assert "update complete: 0.2.0" in out.out
