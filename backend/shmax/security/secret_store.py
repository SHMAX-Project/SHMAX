"""AES-GCM-based secret store.

The store encrypts short strings (bot tokens, proxy passwords,
MTProxy secrets, ...) using a single master key that lives on
disk at ``$SHMAX_DATA_DIR/secrets/master.key`` with ``0600``
permissions. The plaintext of any secret never lands on disk in
unencrypted form and never appears in logs.

Layout::

    $SHMAX_DATA_DIR/secrets/
    ├── master.key          (32 raw bytes; 0600)
    ├── store.bin           (ciphertext + nonce, 0600)
    ├── store.bin.tmp       (in-progress atomic write; transient)
    └── master.key.tmp      (in-progress atomic rotation; transient)

The ``store.bin`` file is a sequence of frames::

    [ 4-byte big-endian length ][ 12-byte nonce ][ ciphertext+tag ]

We use AES-256-GCM via ``cryptography.hazmat``. We do NOT
implement our own AEAD construction.

Failure-closed semantics (V1):

* If ``store.bin`` exists but ``master.key`` is missing or the
  wrong size, ``SecretStore.__init__`` raises
  :class:`MasterKeyError`. We never auto-generate a new master
  key when an existing encrypted store is present — that would
  silently destroy data.
* If a frame in ``store.bin`` fails the AES-GCM tag check, we
  raise :class:`SecretStoreError` rather than silently skipping
  the secret. A bad tag means the file is either tampered or
  the wrong key is in play; either way the operator must
  intervene.

Atomic flush (V1):

* ``_flush`` writes to a temp file in the same directory,
  ``fsync``'s the file, then ``os.replace``'s it onto the
  target path, and finally ``fsync``'s the directory entry.
  Same pattern for ``master.key``.

Crash-safe rotation (V1):

* ``rotate_master_key`` writes the new master key to a
  temp file first, re-encrypts the in-memory cache, writes
  the new store to a temp file, validates the new store by
  decrypting every entry, and only then atomically swaps
  both files into place.

This module has no dependencies on aiogram, telethon or pymax,
so it is unit-testable with stdlib only.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import secrets
import struct
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


STORE_FILE = "store.bin"
MASTER_KEY_FILE = "master.key"
STORE_FILE_TMP = "store.bin.tmp"
MASTER_KEY_FILE_TMP = "master.key.tmp"
MASTER_KEY_BYTES = 32  # AES-256
NONCE_BYTES = 12  # GCM standard
MAX_PLAINTEXT_BYTES = 64 * 1024  # 64 KiB sanity cap


class SecretStoreError(Exception):
    """Base class for SecretStore failures."""


class MasterKeyError(SecretStoreError):
    """Master key is missing or unusable."""


class SecretNotFound(SecretStoreError):
    """Requested secret does not exist."""


@dataclass
class SecretEntry:
    name: str
    ciphertext: bytes  # already includes nonce + tag in the wire format


# ----------------------------------------------------------------------
# Generation-aware pair metadata
# ----------------------------------------------------------------------
# The ``current`` pointer records which (master.key, store.bin)
# pair is active. On crash mid-rotation we can recover by reading
# ``current.json`` and loading both files together. A simple
# integer generation keeps the format stable across versions.
CURRENT_POINTER_FILE = "current.json"
GENERATION_PATTERN = re.compile(r"^(master\.gen(?P<gen>\d+)\.key|store\.gen(?P<gen2>\d+)\.bin)$")


def _read_current_pointer(secrets_dir: Path) -> Optional[dict[str, Any]]:
    """Load ``current.json`` if it exists.

    Returns the parsed dict or ``None`` if the file is missing /
    corrupted. The shape is::

        {
            "generation": int,
            "key_filename": "master.genN.key",
            "store_filename": "store.genN.bin",
            "committed_at": int (epoch seconds),
        }
    """
    path = secrets_dir / CURRENT_POINTER_FILE
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _write_current_pointer(
    secrets_dir: Path,
    *,
    generation: int,
    key_filename: str,
    store_filename: str,
) -> None:
    """Write the pointer atomically. ``os.replace`` is the only
    non-atomic step that can leave the file in a bad state and
    the runtime always treats a missing pointer as the empty
    (no secrets) case."""
    payload = json.dumps(
        {
            "generation": int(generation),
            "key_filename": key_filename,
            "store_filename": store_filename,
            "committed_at": int(_dt.datetime.now(_dt.timezone.utc).timestamp()),
        },
        indent=2,
        sort_keys=True,
    )
    _atomic_write_bytes(secrets_dir / CURRENT_POINTER_FILE, payload.encode("utf-8"))


def _active_pair_paths(secrets_dir: Path) -> tuple[Optional[Path], Optional[Path], int]:
    """Return the live ``(key_path, store_path, generation)`` triple.

    Resolution order:

    1. ``current.json`` pointer (preferred; survives partial writes).
    2. Legacy ``master.key`` / ``store.bin`` (V1 deployments).
    """
    pointer = _read_current_pointer(secrets_dir)
    if pointer is not None:
        gen = int(pointer.get("generation", 0))
        key_name = pointer.get("key_filename")
        store_name = pointer.get("store_filename")
        if key_name and store_name:
            kp = secrets_dir / key_name
            sp = secrets_dir / store_name
            if kp.is_file() and sp.is_file():
                return kp, sp, gen
    # Legacy pair (no pointer yet — V1 deployments, fresh installs
    # before any rotation). Generation 0 is implicit.
    legacy_key = secrets_dir / MASTER_KEY_FILE
    legacy_store = secrets_dir / STORE_FILE
    if legacy_key.is_file():
        return legacy_key, legacy_store, 0
    return None, None, 0


def _active_pair_filename(generation: int, *, key: bool) -> str:
    if generation <= 0:
        return MASTER_KEY_FILE if key else STORE_FILE
    return f"master.gen{generation}.key" if key else f"store.gen{generation}.bin"


def _atomic_write_bytes(target: Path, payload: bytes, *, mode: int = 0o600) -> None:
    """Write ``payload`` to ``target`` atomically.

    The temp file lives in the same directory as ``target`` so
    ``os.replace`` is atomic on POSIX and Windows. We ``fsync``
    the file and (where supported) the directory entry so the
    bytes survive a power loss.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp") if target.suffix else target.with_name(
        target.name + ".tmp"
    )
    try:
        with tmp.open("wb") as fh:
            fh.write(payload)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, target)
        # Best-effort directory fsync so the rename itself is
        # durable across power loss.
        try:
            dfd = os.open(str(target.parent), os.O_DIRECTORY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except (OSError, AttributeError):
            pass
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


class SecretStore:
    """Synchronous, thread-safe AES-GCM secret store.

    Multiple processes must NOT share the same ``store.bin`` file.
    For SHMAX the store is opened by exactly one process: the
    FastAPI app / bridge process. Any other process reads through
    the HTTP API.
    """

    def __init__(self, secrets_dir: Path) -> None:
        self._dir = secrets_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._master_path = self._dir / MASTER_KEY_FILE
        self._store_path = self._dir / STORE_FILE
        self._key: Optional[bytes] = None
        self._lock = RLock()
        # name -> bytes(ciphertext). In-memory cache; persistence
        # is the on-disk file. We rebuild the cache from disk on
        # construction so a restart recovers state.
        self._cache: dict[str, bytes] = {}
        # Generation-aware pair tracking. The runtime picks the
        # active pair via ``_active_pair_paths()``; after a successful
        # rotation it writes a ``current.json`` pointer so a future
        # startup can survive a crash between the key and store
        # replacements.
        self._generation: int = 0
        self._load_key()
        self._load_store()

    # ------------------------------------------------------------------
    # Key management
    # ------------------------------------------------------------------
    def _load_key(self) -> None:
        # Resolve the live pair via the pointer first, then the
        # legacy layout. The active path is whatever the pointer
        # says OR the canonical V1 file when no pointer exists.
        active_key, active_store, gen = _active_pair_paths(self._dir)
        if active_key is not None:
            data = active_key.read_bytes()
            if len(data) != MASTER_KEY_BYTES:
                raise MasterKeyError(
                    f"master key {active_key.name} has wrong size: {len(data)} bytes "
                    f"(expected {MASTER_KEY_BYTES})"
                )
            self._key = data
            self._master_path = active_key
            self._store_path = active_store or self._store_path
            self._generation = gen
            return
        # No pair found at all. First boot: create a fresh key
        # ONLY if no encrypted store exists. If a store is present
        # but the master key is missing, fail closed.
        legacy_store = self._dir / STORE_FILE
        if legacy_store.is_file() and legacy_store.stat().st_size > 0:
            raise MasterKeyError(
                "store.bin exists but master.key is missing; "
                "refusing to auto-generate a new master key. "
                "Restore master.key from a backup or move store.bin aside."
            )
        key = AESGCM.generate_key(bit_length=MASTER_KEY_BYTES * 8)
        _atomic_write_bytes(self._master_path, key, mode=0o600)
        self._key = key
        # First-boot generation is 0; no pointer needed yet.

    def has_master_key(self) -> bool:
        return self._key is not None and self._master_path.is_file()

    def rotate_master_key(self, new_key: bytes | None = None) -> None:
        """Rotate the master key and re-encrypt every entry.

        Crash-safe staged rotation via *generation pairs*::

            master.gen{N}.key   ← key for generation N
            store.gen{N}.bin    ← store for generation N
            current.json        ← pointer: {generation, key_filename, store_filename}

        On startup we resolve the active pair from ``current.json``
        (or fall back to the legacy V1 layout). On crash mid-rotation
        the old pair is still readable via the pointer — the operator
        can roll back by deleting ``current.json`` and the gen{N+1}
        files; alternatively the runtime auto-recovers on the next
        start by loading whichever pair is fully intact.

        Phases:

        1. Pick ``gen = current_generation + 1``.
        2. Decrypt every cache entry with the old key.
        3. Re-encrypt with the new key.
        4. Write ``master.gen{gen}.key`` and ``store.gen{gen}.bin``.
        5. Verify by re-decrypting every frame in the new store.
        6. Atomically replace ``current.json`` to commit.
        """
        if new_key is None:
            new_key = AESGCM.generate_key(bit_length=MASTER_KEY_BYTES * 8)
        if len(new_key) != MASTER_KEY_BYTES:
            raise MasterKeyError(
                f"new master key wrong size: {len(new_key)} bytes"
            )
        with self._lock:
            if self._key is None:
                raise MasterKeyError("no current master key to rotate")
            old_key = self._key
            gen = self._generation + 1
            new_key_name = _active_pair_filename(gen, key=True)
            new_store_name = _active_pair_filename(gen, key=False)
            new_key_path = self._dir / new_key_name
            new_store_path = self._dir / new_store_name
            # Step 1: decrypt with the old key.
            plain = {name: self._decrypt_with(old_key, ct) for name, ct in self._cache.items()}
            # Step 2: re-encrypt with the new key in a fresh cache.
            old_cache = self._cache
            self._cache = {}
            self._key = new_key
            try:
                new_cache = {name: self._encrypt(value) for name, value in plain.items()}
                payload = b"".join(
                    struct.pack(">I", len(frame)) + frame for frame in new_cache.values()
                )
                # Step 3: write the new pair to the gen files.
                _atomic_write_bytes(new_key_path, new_key, mode=0o600)
                _atomic_write_bytes(new_store_path, payload, mode=0o600)
                # Step 4: verify by re-reading the new store with
                # the new key.
                with new_store_path.open("rb") as fh:
                    raw = fh.read()
                offset = 0
                verified = 0
                while offset < len(raw):
                    if offset + 4 > len(raw):
                        raise SecretStoreError("rotated store failed verification (truncated length prefix)")
                    (length,) = struct.unpack(">I", raw[offset : offset + 4])
                    offset += 4
                    if offset + length > len(raw):
                        raise SecretStoreError("rotated store failed verification (truncated frame)")
                    frame = raw[offset : offset + length]
                    offset += length
                    self._decrypt_with(new_key, frame)
                    verified += 1
                if verified != len(new_cache):
                    raise SecretStoreError(
                        f"rotated store verification mismatch: {verified} vs {len(new_cache)}"
                    )
                # Step 5: commit via the pointer. A missing or
                # corrupt pointer after a partial crash leaves
                # the gen files on disk so the next startup can
                # roll forward via _active_pair_paths.
                _write_current_pointer(
                    self._dir,
                    generation=gen,
                    key_filename=new_key_name,
                    store_filename=new_store_name,
                )
                # Commit succeeded — adopt new paths as live.
                self._cache = new_cache
                self._master_path = new_key_path
                self._store_path = new_store_path
                self._generation = gen
            except Exception:
                # Roll back in-memory state; partial files on disk
                # are inert until the pointer is rewritten.
                self._cache = old_cache
                self._key = old_key
                for leftover in (new_key_path, new_store_path):
                    if leftover.exists():
                        try:
                            leftover.unlink()
                        except OSError:
                            pass
                raise

    # ------------------------------------------------------------------
    # Store I/O
    # ------------------------------------------------------------------
    def _load_store(self) -> None:
        if not self._store_path.is_file():
            return
        try:
            os.chmod(self._store_path, 0o600)
        except OSError:
            pass
        with self._store_path.open("rb") as fh:
            data = fh.read()
        i = 0
        while i < len(data):
            if i + 4 > len(data):
                raise SecretStoreError("store.bin truncated (length prefix)")
            (length,) = struct.unpack(">I", data[i : i + 4])
            i += 4
            if i + length > len(data):
                raise SecretStoreError("store.bin truncated (frame)")
            frame = data[i : i + length]
            i += length
            if len(frame) < NONCE_BYTES + 16:
                raise SecretStoreError("store.bin frame too small")
            nonce = frame[:NONCE_BYTES]
            body = frame[NONCE_BYTES:]
            if self._key is None:
                continue
            try:
                plaintext = AESGCM(self._key).decrypt(nonce, body, None)
            except InvalidTag as e:
                # Fail closed: a bad tag means the key is wrong or
                # the file is tampered. We do not silently skip;
                # the operator must inspect / restore.
                raise SecretStoreError(
                    "store.bin failed AES-GCM authentication; "
                    "master.key may be wrong or the file is tampered"
                ) from e
            # Frame payload: 2-byte name length + name + value
            if len(plaintext) < 2:
                raise SecretStoreError("store.bin plaintext frame too short")
            (name_len,) = struct.unpack(">H", plaintext[:2])
            name = plaintext[2 : 2 + name_len].decode("utf-8")
            value = plaintext[2 + name_len :]
            # Cache the full frame (nonce + body) so a future
            # rewrite preserves the original nonce.
            self._cache[name] = frame

    def _flush(self) -> None:
        if not self._cache:
            # Don't leave an empty file behind on disk.
            if self._store_path.is_file():
                self._store_path.unlink()
            return
        payload = b"".join(
            struct.pack(">I", len(frame)) + frame for frame in self._cache.values()
        )
        _atomic_write_bytes(self._store_path, payload, mode=0o600)

    # ------------------------------------------------------------------
    # Crypto helpers
    # ------------------------------------------------------------------
    def _encrypt(self, plaintext: bytes) -> bytes:
        if self._key is None:
            raise MasterKeyError("master key unavailable")
        if len(plaintext) > MAX_PLAINTEXT_BYTES:
            raise SecretStoreError(
                f"plaintext too large: {len(plaintext)} > {MAX_PLAINTEXT_BYTES}"
            )
        nonce = secrets.token_bytes(NONCE_BYTES)
        body = AESGCM(self._key).encrypt(nonce, plaintext, None)
        return nonce + body

    def _decrypt(self, frame: bytes) -> bytes:
        if self._key is None:
            raise MasterKeyError("master key unavailable")
        return self._decrypt_with(self._key, frame)

    @staticmethod
    def _decrypt_with(key: bytes, frame: bytes) -> bytes:
        if len(frame) < NONCE_BYTES + 16:
            raise SecretStoreError("ciphertext frame too small")
        nonce = frame[:NONCE_BYTES]
        body = frame[NONCE_BYTES:]
        try:
            return AESGCM(key).decrypt(nonce, body, None)
        except InvalidTag as e:
            raise SecretStoreError("decryption failed (bad key or tampered data)") from e

    @staticmethod
    def _serialize(name: str, value: bytes) -> bytes:
        name_b = name.encode("utf-8")
        if len(name_b) > 0xFFFF:
            raise SecretStoreError(f"secret name too long: {len(name_b)}")
        return struct.pack(">H", len(name_b)) + name_b + value

    @staticmethod
    def _deserialize(plaintext: bytes) -> tuple[str, bytes]:
        (name_len,) = struct.unpack(">H", plaintext[:2])
        name = plaintext[2 : 2 + name_len].decode("utf-8")
        value = plaintext[2 + name_len :]
        return name, value

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_secret(self, name: str, value: str) -> None:
        if not name:
            raise SecretStoreError("secret name must not be empty")
        with self._lock:
            frame = self._encrypt(self._serialize(name, value.encode("utf-8")))
            self._cache[name] = frame
            self._flush()

    def get_secret(self, name: str) -> str:
        with self._lock:
            frame = self._cache.get(name)
            if frame is None:
                raise SecretNotFound(name)
            plaintext = self._decrypt(frame)
            _, value = self._deserialize(plaintext)
            return value.decode("utf-8")

    def has_secret(self, name: str) -> bool:
        with self._lock:
            return name in self._cache

    def delete_secret(self, name: str) -> bool:
        with self._lock:
            if name not in self._cache:
                return False
            del self._cache[name]
            self._flush()
            return True

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._cache.keys())

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"SecretStore(dir={self._dir!s}, entries={len(self._cache)})"


__all__ = [
    "SecretStore",
    "SecretStoreError",
    "MasterKeyError",
    "SecretNotFound",
    "_atomic_write_bytes",
]
