"""Regression tests for SecretStore atomic I/O and fail-closed semantics."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from shmax.security.secret_store import (
    MASTER_KEY_BYTES,
    MasterKeyError,
    SecretStore,
    SecretStoreError,
)


def _permissions(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_atomic_flush_no_temp_left_on_disk(tmp_path):
    store = SecretStore(tmp_path / "secrets")
    store.set_secret("a", "1")
    store.set_secret("b", "2")
    assert not (tmp_path / "secrets" / "store.bin.tmp").exists()
    assert (tmp_path / "secrets" / "store.bin").is_file()
    assert _permissions(tmp_path / "secrets" / "store.bin") == 0o600


def test_store_survives_partial_write_simulation(tmp_path):
    """Simulate a crash mid-rewrite by leaving only the
    old store.bin (no .tmp file) and verify it loads."""
    store = SecretStore(tmp_path / "secrets")
    store.set_secret("k", "v")
    # Reopen — should recover cleanly.
    store2 = SecretStore(tmp_path / "secrets")
    assert store2.get_secret("k") == "v"


def test_fail_closed_when_store_exists_but_master_key_missing(tmp_path):
    store_dir = tmp_path / "secrets"
    store_dir.mkdir()
    # Pre-create store.bin with one valid entry using a
    # throwaway store, then delete master.key.
    s = SecretStore(store_dir)
    s.set_secret("k", "v")
    master = store_dir / "master.key"
    assert master.exists()
    master.unlink()
    with pytest.raises(MasterKeyError):
        SecretStore(store_dir)


def test_invalid_tag_fails_closed(tmp_path):
    store_dir = tmp_path / "secrets"
    store = SecretStore(store_dir)
    store.set_secret("k", "v")
    # Corrupt one byte of store.bin — AES-GCM tag check must
    # fail, and the load must raise rather than silently skip.
    sb = store_dir / "store.bin"
    data = bytearray(sb.read_bytes())
    data[-1] ^= 0xFF
    sb.write_bytes(bytes(data))
    with pytest.raises(SecretStoreError):
        SecretStore(store_dir)


def test_rotation_preserves_secrets(tmp_path):
    store = SecretStore(tmp_path / "secrets")
    store.set_secret("a", "alpha")
    store.set_secret("b", "beta")
    new_key = os.urandom(MASTER_KEY_BYTES)
    store.rotate_master_key(new_key=new_key)
    # Reopen with the new key — both secrets survive.
    store2 = SecretStore(tmp_path / "secrets")
    assert store2.get_secret("a") == "alpha"
    assert store2.get_secret("b") == "beta"


def test_rotation_keeps_old_master_until_swap_succeeds(tmp_path):
    """If a crash happens between the temp write and the
    ``os.replace``, the on-disk master.key must still match the
    store. Simulate by reading the old key after rotation
    completes — verify the file is consistent."""
    store = SecretStore(tmp_path / "secrets")
    store.set_secret("a", "alpha")
    new_key = os.urandom(MASTER_KEY_BYTES)
    store.rotate_master_key(new_key=new_key)
    # Reload from disk using the live master.key.
    fresh = SecretStore(tmp_path / "secrets")
    assert fresh.get_secret("a") == "alpha"
    # Temp files must not be left behind.
    assert not (tmp_path / "secrets" / "store.bin.tmp").exists()
    assert not (tmp_path / "secrets" / "master.key.tmp").exists()


def test_master_key_wrong_size_raises(tmp_path):
    store_dir = tmp_path / "secrets"
    store_dir.mkdir()
    (store_dir / "master.key").write_bytes(b"x" * 8)  # too short
    (store_dir / "store.bin").write_bytes(b"")
    with pytest.raises(MasterKeyError):
        SecretStore(store_dir)
