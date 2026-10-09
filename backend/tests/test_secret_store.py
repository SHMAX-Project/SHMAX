"""SecretStore AES-GCM tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from shmax.security import SecretStore, SecretNotFound


def test_secret_store_round_trip(tmp_path):
    store = SecretStore(tmp_path)
    assert store.has_master_key()
    store.set_secret("bot_token", "TGNlqwert12345")
    assert store.has_secret("bot_token")
    assert store.get_secret("bot_token") == "TGNlqwert12345"


def test_secret_store_persists_across_instances(tmp_path):
    s1 = SecretStore(tmp_path)
    s1.set_secret("api_hash", "supersecret")
    # Fresh instance should reload from disk.
    s2 = SecretStore(tmp_path)
    assert s2.has_secret("api_hash")
    assert s2.get_secret("api_hash") == "supersecret"


def test_secret_store_unknown_name_raises(tmp_path):
    store = SecretStore(tmp_path)
    with pytest.raises(SecretNotFound):
        store.get_secret("nope")


def test_secret_store_delete(tmp_path):
    store = SecretStore(tmp_path)
    store.set_secret("a", "1")
    assert store.delete_secret("a") is True
    assert store.delete_secret("a") is False
    with pytest.raises(SecretNotFound):
        store.get_secret("a")


def test_secret_store_repr_does_not_leak(tmp_path):
    store = SecretStore(tmp_path)
    store.set_secret("token", "very-private")
    r = repr(store)
    assert "very-private" not in r


def test_secret_store_master_key_permissions(tmp_path):
    SecretStore(tmp_path)
    # On POSIX, master.key must be 0600.
    if hasattr(os, "stat"):
        st = (tmp_path / "master.key").stat()
        assert oct(st.st_mode & 0o777) == "0o600"
