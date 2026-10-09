"""BLOCKER 16 — SecretStore rotation pair safety.

The audit identified a crash window during ``rotate_master_key``
where ``master.key`` could be replaced before ``store.bin`` and
the operator would be locked out.

The fix replaces the linear swap with a *generation-pair* scheme
backed by ``current.json``:

* Each generation gets its own ``master.genN.key`` and
  ``store.genN.bin`` files.
* ``current.json`` is the single source of truth for which
  generation is active.
* On startup the loader reads ``current.json`` and resolves the
  pair. Missing or corrupt pointer falls back to the legacy V1
  layout.

A crash anywhere inside ``rotate_master_key`` leaves the new
generation files on disk but never advances ``current.json`` —
the runtime comes back online with the prior generation.
"""

from __future__ import annotations

import json

import pytest

from shmax.security.secret_store import (
    CURRENT_POINTER_FILE,
    SecretStore,
    _active_pair_paths,
)


def test_first_boot_creates_legacy_layout(tmp_path):
    """Fresh install: no pointer, generation 0, files at
    ``master.key`` and ``store.bin``."""
    s = SecretStore(tmp_path / "secrets")
    assert s.has_master_key()
    assert s._generation == 0
    assert s._master_path.name == "master.key"
    assert s._store_path.name == "store.bin"
    # No pointer file on first boot (gen 0).
    assert not (tmp_path / "secrets" / CURRENT_POINTER_FILE).is_file()


def test_set_and_get_secrets_works(tmp_path):
    s = SecretStore(tmp_path / "secrets")
    s.set_secret("alpha", "value-a")
    s.set_secret("beta", "value-b")
    assert s.get_secret("alpha") == "value-a"
    assert s.get_secret("beta") == "value-b"


def test_rotation_creates_generation_pair(tmp_path):
    """After rotation the active paths move to gen1 and the
    pointer file records the new generation."""
    s = SecretStore(tmp_path / "secrets")
    s.set_secret("token", "secret-token")
    s.rotate_master_key()
    # The active pair now points to gen1 files.
    assert s._generation == 1
    assert s._master_path.name == "master.gen1.key"
    assert s._store_path.name == "store.gen1.bin"
    # Pointer is written.
    pointer = json.loads((tmp_path / "secrets" / CURRENT_POINTER_FILE).read_text())
    assert pointer["generation"] == 1
    assert pointer["key_filename"] == "master.gen1.key"
    assert pointer["store_filename"] == "store.gen1.bin"
    # Existing secrets still decrypt with the new key.
    assert s.get_secret("token") == "secret-token"


def test_repeated_rotation_advances_generation(tmp_path):
    s = SecretStore(tmp_path / "secrets")
    s.set_secret("k", "v")
    s.rotate_master_key()
    s.rotate_master_key()
    s.rotate_master_key()
    assert s._generation == 3
    assert s.get_secret("k") == "v"


def test_startup_resolves_via_pointer(tmp_path):
    """After a successful rotation a fresh process picks up the
    pointer and reads the gen1 pair."""
    s = SecretStore(tmp_path / "secrets")
    s.set_secret("k", "v")
    s.rotate_master_key()
    del s
    s2 = SecretStore(tmp_path / "secrets")
    assert s2._generation == 1
    assert s2._master_path.name == "master.gen1.key"
    assert s2._store_path.name == "store.gen1.bin"
    assert s2.get_secret("k") == "v"


def test_startup_falls_back_to_legacy_when_no_pointer(tmp_path):
    """A V1 deployment has master.key / store.bin but no
    pointer. The loader falls back to that legacy layout."""
    legacy = tmp_path / "secrets"
    legacy.mkdir()
    # Simulate a legacy install: create the store and rotate it
    # through the legacy code path (no pointer is written).
    s = SecretStore(legacy)
    s.set_secret("k", "v")
    # Remove the pointer, leave only the gen0 legacy files.
    assert not (legacy / CURRENT_POINTER_FILE).is_file()
    assert (legacy / "master.key").is_file()
    assert (legacy / "store.bin").is_file()
    # Reload — must still resolve.
    s2 = SecretStore(legacy)
    assert s2.get_secret("k") == "v"


def test_crash_before_pointer_keeps_old_generation(tmp_path):
    """A simulated crash mid-rotation: gen1 files exist on disk
    but the pointer is missing. Reload must keep using the old
    generation and ignore the orphan gen1 files."""
    legacy = tmp_path / "secrets"
    s = SecretStore(legacy)
    s.set_secret("k", "v")
    # Simulate the rotation writing gen1 files but failing before
    # the pointer was written.
    s._generation = 0
    s.rotate_master_key()
    # At this point gen1 is live and the pointer points there.
    # Now simulate the next crash by writing gen2 files
    # (representing a half-finished rotation) WITHOUT a pointer.
    s2 = SecretStore(legacy)
    assert s2._generation == 1
    # Mutate the active paths to simulate a partial write of gen2.
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    new_key = AESGCM.generate_key(bit_length=256)
    gen2_key_path = legacy / "master.gen2.key"
    gen2_store_path = legacy / "store.gen2.bin"
    gen2_key_path.write_bytes(new_key)
    gen2_store_path.write_bytes(b"")
    # No pointer written → runtime still uses gen1.
    s3 = SecretStore(legacy)
    assert s3._generation == 1
    assert s3._master_path.name == "master.gen1.key"
    assert s3.get_secret("k") == "v"
    # The orphan gen2 files are not active but remain on disk
    # for forensics / manual recovery.
    assert gen2_key_path.is_file()
    assert gen2_store_path.is_file()


def test_active_pair_paths_prefers_pointer(tmp_path):
    """When both legacy and gen1 pairs exist the pointer wins."""
    legacy = tmp_path / "secrets"
    legacy.mkdir()
    # Create a legacy master.key and store.bin.
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = AESGCM.generate_key(bit_length=256)
    (legacy / "master.key").write_bytes(key)
    (legacy / "store.bin").write_bytes(b"")
    # Also create a gen1 pair (orphaned) and a pointer that points
    # somewhere completely different to confirm priority.
    other_key = AESGCM.generate_key(bit_length=256)
    (legacy / "master.gen1.key").write_bytes(other_key)
    (legacy / "store.gen1.bin").write_bytes(b"")
    (legacy / CURRENT_POINTER_FILE).write_text(
        json.dumps(
            {
                "generation": 1,
                "key_filename": "master.gen1.key",
                "store_filename": "store.gen1.bin",
            }
        )
    )
    kp, sp, gen = _active_pair_paths(legacy)
    assert gen == 1
    assert kp.name == "master.gen1.key"
    assert sp.name == "store.gen1.bin"


def test_active_pair_paths_handles_corrupt_pointer(tmp_path):
    """A pointer file that does not parse as JSON is ignored —
    the loader falls back to the legacy layout."""
    legacy = tmp_path / "secrets"
    legacy.mkdir()
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = AESGCM.generate_key(bit_length=256)
    (legacy / "master.key").write_bytes(key)
    (legacy / CURRENT_POINTER_FILE).write_text("not json")
    kp, sp, gen = _active_pair_paths(legacy)
    assert gen == 0
    assert kp.name == "master.key"


def test_rotation_failure_leaves_orphan_gen_ignored(tmp_path):
    """If ``rotate_master_key`` raises mid-flight (e.g. disk full
    writing the new store) the in-memory state is rolled back and
    the gen files are removed."""
    s = SecretStore(tmp_path / "secrets")
    s.set_secret("k", "v")
    s.set_secret("k2", "v2")
    # Force the verification step to fail by patching _encrypt to
    # raise after the first encrypt during rotation.
    calls = {"n": 0}
    original_encrypt = s._encrypt

    def boom(plaintext):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("simulated disk full")
        return original_encrypt(plaintext)

    s._encrypt = boom  # type: ignore[assignment]
    with pytest.raises(RuntimeError):
        s.rotate_master_key()
    # In-memory state rolled back to the old key.
    assert s._generation == 0
    assert s._master_path.name == "master.key"
    assert s._store_path.name == "store.bin"
    # Orphan files were cleaned up.
    assert not (tmp_path / "secrets" / "master.gen1.key").exists()
    assert not (tmp_path / "secrets" / "store.gen1.bin").exists()
    # Old secrets still decrypt.
    assert s.get_secret("k") == "v"
    assert s.get_secret("k2") == "v2"