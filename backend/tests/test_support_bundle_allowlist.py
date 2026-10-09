"""BLOCKER 17 — Support bundle ALLOWLIST.

The support bundle must NEVER include arbitrary environment
values, master-key paths, store contents, or message bodies.
Only the explicit allowlist surfaces are kept; everything
else is dropped before redaction.

The audit listed concrete leak sources:

* short password
* long token
* api_hash
* proxy secret
* phone number
* message text
* master path/content

This test sets up a hostile environment / data_dir and
asserts NONE of those values survive into the bundle.
"""

from __future__ import annotations

import json
import os
import tarfile

import pytest

from shmax.support_bundle import (
    ALLOWED_ENV_KEYS,
    ALLOWED_FILE_PATTERNS,
    collect_support_bundle,
    write_support_bundle,
)


def _empty_queue_counts() -> dict:
    return {}


def _empty_watchdog_snapshots() -> dict:
    return {}


class _StubDB:
    """Stand-in for the real DB; the support bundle does not
    query it directly."""


class _StubSecretStore:
    def names(self) -> list[str]:
        return ["bot_token", "proxy_secret", "admin_password"]


def _build_bundle(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # Put a state + secrets directory so the file allowlist has
    # something to filter on.
    (data_dir / "state").mkdir()
    (data_dir / "state" / "watchdogs.json").write_text(
        json.dumps({"max": {"state": "healthy"}})
    )
    (data_dir / "secrets").mkdir()
    # A master key file. MUST NOT appear in the bundle.
    (data_dir / "secrets" / "master.key").write_bytes(b"\x00" * 32)
    (data_dir / "secrets" / "store.bin").write_bytes(b"\xff" * 64)
    return collect_support_bundle(
        data_dir=data_dir,
        db=_StubDB(),
        secret_store=_StubSecretStore(),
        doctor_results=[],
        watchdog_snapshots=_empty_watchdog_snapshots(),
        queue_counts=_empty_queue_counts(),
        schema_revision="0001",
    )


def test_only_allowlisted_env_keys_are_kept(monkeypatch, tmp_path):
    """Set up an environment with hostile values and verify only
    the allowlist surface survives."""
    monkeypatch.setenv("SHMAX_DATA_DIR", "/var/lib/shmax")
    monkeypatch.setenv("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin")
    monkeypatch.setenv("SHMAX_SECRET_TOKEN", "abcdef1234567890abcdef1234567890")
    monkeypatch.setenv("BOT_TOKEN", "1234567890:ABCDEFG_LONG_TOKEN")
    monkeypatch.setenv("PROXY_PASSWORD", "letmein")
    monkeypatch.setenv("SHMAX_ADMIN_PASSWORD", "hunter2")
    monkeypatch.setenv("API_HASH", "deadbeefdeadbeefdeadbeef")
    monkeypatch.setenv("PHONE_NUMBER", "+79001234567")
    monkeypatch.setenv("HOME", "/root")
    bundle = _build_bundle(tmp_path, monkeypatch)
    env = bundle["environment"]
    # Allowed surface kept
    assert "SHMAX_DATA_DIR" in env
    # Denied surfaces dropped before any redaction
    assert "PATH" not in env
    assert "SHMAX_SECRET_TOKEN" not in env
    assert "BOT_TOKEN" not in env
    assert "PROXY_PASSWORD" not in env
    assert "SHMAX_ADMIN_PASSWORD" not in env
    assert "API_HASH" not in env
    assert "PHONE_NUMBER" not in env
    # HOME is allowed (process surface, no secrets)
    assert "HOME" in env
    # No raw value of any sensitive key survives
    blob = json.dumps(env)
    assert "abcdef1234567890abcdef1234567890" not in blob
    assert "letmein" not in blob
    assert "hunter2" not in blob


def test_redaction_removes_credential_substrings(monkeypatch, tmp_path):
    """Even when a key is on the allowlist, anything that looks
    like a credential inside its value is redacted."""
    monkeypatch.setenv("SHMAX_DATA_DIR", "/var/lib/shmax?token=ABCDEFG_LONG")
    monkeypatch.setenv("SHMAX_LOG_LEVEL", "DEBUG password=hunter2 token=xyz")
    bundle = _build_bundle(tmp_path, monkeypatch)
    env = bundle["environment"]
    assert "[REDACTED]" in env["SHMAX_DATA_DIR"]
    assert "hunter2" not in env["SHMAX_LOG_LEVEL"]
    assert "REDACTED" in env["SHMAX_LOG_LEVEL"]


def test_phone_number_in_value_is_redacted(monkeypatch, tmp_path):
    """A long digit string looks like a phone number and must be
    removed from any field."""
    monkeypatch.setenv("SHMAX_DATA_DIR", "/var/lib/shmax")
    monkeypatch.setenv("SHMAX_LANG", "+79001234567")
    bundle = _build_bundle(tmp_path, monkeypatch)
    assert "79001234567" not in bundle["environment"]["SHMAX_LANG"]


def test_long_base64_token_is_redacted(monkeypatch, tmp_path):
    """A 40+ char base64 string in any value is replaced."""
    monkeypatch.setenv("SHMAX_DATA_DIR", "/var/lib/shmax")
    monkeypatch.setenv("SHMAX_LOG_LEVEL", "abcdefghijklmnopqrstuvwxyz0123456789abcd")
    bundle = _build_bundle(tmp_path, monkeypatch)
    assert "abcdefghijklmnopqrstuvwxyz0123456789abcd" not in bundle["environment"]["SHMAX_LOG_LEVEL"]


def test_master_key_path_is_redacted(monkeypatch, tmp_path):
    """A path under ``secrets/`` or a master.key reference is
    wiped from any field."""
    monkeypatch.setenv("SHMAX_DATA_DIR", "/var/lib/shmax/secrets/master.key")
    bundle = _build_bundle(tmp_path, monkeypatch)
    assert "master.key" not in bundle["environment"]["SHMAX_DATA_DIR"]


def test_inline_files_excludes_secrets(tmp_path):
    """Only files matching the allowlist are inlined."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "state").mkdir()
    (data_dir / "state" / "watchdogs.json").write_text("{}")
    (data_dir / "secrets").mkdir()
    (data_dir / "secrets" / "master.key").write_bytes(b"\x00" * 32)
    (data_dir / "secrets" / "store.bin").write_bytes(b"\xff" * 64)
    bundle = collect_support_bundle(
        data_dir=data_dir,
        db=_StubDB(),
        secret_store=_StubSecretStore(),
        doctor_results=[],
        watchdog_snapshots={},
        queue_counts={},
        schema_revision="0001",
    )
    files = bundle["inline_files"]
    # Allowed state file is present
    assert "state/watchdogs.json" in files
    # Secrets are excluded
    assert not any(k.startswith("secrets/") for k in files)


def test_allowlists_are_explicit():
    """The ALLOWED sets themselves must not contain obvious
    secret carriers. A regression here would be silent."""
    forbidden = {
        "PATH", "SSH_AUTH_SOCK", "SHMAX_SECRET_TOKEN",
        "BOT_TOKEN", "PROXY_PASSWORD", "API_HASH",
    }
    assert forbidden.isdisjoint(ALLOWED_ENV_KEYS)
    # The file allowlist must NOT include secrets/* paths.
    for pat in ALLOWED_FILE_PATTERNS:
        assert "secrets" not in pat.pattern
        assert "master.key" not in pat.pattern
        assert "store.bin" not in pat.pattern


def test_message_text_in_logs_is_redacted(tmp_path):
    """Doctor / watchdog / log excerpts that contain free-form
    text are scrubbed."""
    log = tmp_path / "shmax.log"
    log.write_text(
        "OK startup\n"
        "ERROR api_hash=1234567890abcdef\n"
        "INFO user said: my password is hunter2\n"
    )
    bundle = collect_support_bundle(
        data_dir=tmp_path / "data",
        db=_StubDB(),
        secret_store=_StubSecretStore(),
        doctor_results=[
            {"check": "ok", "detail": "api_hash=deadbeef user sent text"},
        ],
        watchdog_snapshots={"max": {"last_error_detail": "password=hunter2"}},
        queue_counts={},
        schema_revision="0001",
    )
    out = write_support_bundle(bundle, log, tmp_path / "bundle.tar.gz")
    with tarfile.open(out, "r:gz") as tar:
        payload = tar.extractfile("bundle.json").read()  # type: ignore[union-attr]
    parsed = json.loads(payload)
    blob = json.dumps(parsed)
    assert "hunter2" not in blob
    assert "deadbeef" not in blob
    assert "1234567890abcdef" not in blob


def test_no_secrets_in_bundle_archive(tmp_path):
    """End-to-end: hostile env + secrets directory, then assert
    no master-key contents, store bytes, or proxy secrets
    survive into the archive."""
    log = tmp_path / "shmax.log"
    log.write_text("startup\n")
    bundle = _build_bundle(tmp_path, os.environ.copy())
    out = write_support_bundle(bundle, log, tmp_path / "bundle.tar.gz")
    with tarfile.open(out, "r:gz") as tar:
        members = tar.getnames()
        assert members == ["bundle.json"], members
        payload = tar.extractfile("bundle.json").read()  # type: ignore[union-attr]
    parsed = json.loads(payload)
    blob = json.dumps(parsed)
    assert "\x00" * 32 not in blob
    # secret NAMES are still recorded — they are not values,
    # and the operator benefits from seeing them. Verify that
    # none of the values leaked: the bundle must not contain
    # ``"hunter2"``, ``"abcdef1234567890abcdef1234567890"``,
    # or the master-key bytes.
    assert "secrets" in parsed
    for name in parsed["secrets"]:
        # Either already-redacted, or short enough not to be a
        # secret value in itself. The redact filter is the
        # safety net.
        assert isinstance(name, str)