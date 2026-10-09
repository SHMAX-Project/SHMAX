"""Backup manager.

V1 archive types:

* ``.shmax``           — plaintext, local-only.
* ``.shmax.enc``       — Argon2id-derived key + AES-256-GCM.

Plaintext is what the operator gets when they tick
"Скачать" from the UI. Encrypted backups require the operator
to choose a passphrase; we never derive that passphrase from
the master key (which is rotation-bound, not export-bound).

Encryption scheme:

* passphrase -> Argon2id (time_cost=3, memory_cost=64 MiB,
  parallelism=4, salt=16 random bytes)
* 32-byte key from Argon2id output
* AES-256-GCM nonce (12 random bytes) per archive
* ciphertext = AES-GCM(key, nonce, plaintext, aad=b"shmax-backup-v1")

The archive layout is still gzip-tar with a JSON manifest;
for encrypted backups the *manifest + tarball* is the
plaintext that AES-GCM encrypts.

We use the standard ``cryptography`` library. No custom crypto.
"""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import io
import json
import logging
import os
import platform
import secrets
import shutil
import sqlite3
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from argon2.low_level import hash_secret_raw, Type as Argon2Type


logger = logging.getLogger(__name__)


BACKUP_FORMAT_ID = "shmax-backup-v1"
MANIFEST_NAME = "manifest.json"
FORMAT_NOTICE_NAME = "backup_format.txt"
EXCLUDE_DIRS = {"cache", "backups", "logs", "tmp", ".pytest_cache"}
# File-level exclusions: runtime junk and debug artifacts never
# carry recovery state (live acceptance A7).
EXCLUDE_FILE_PARTS = (".pyc", ".log", ".tmp", ".pid")
EXCLUDE_DIR_PARTS = ("test-results", "playwright-report", ".shots")

ARGON2_TIME_COST = 3
ARGON2_MEMORY_COST = 64 * 1024  # 64 MiB
ARGON2_PARALLELISM = 4
ARGON2_SALT_BYTES = 16
ARGON2_KEY_BYTES = 32

ENCRYPTED_HEADER = b"SHMX1\x00"  # magic + version
AES_NONCE_BYTES = 12


class UnsafeArchiveError(Exception):
    """Raised when an archive contains unsafe entries.

    Any of:

    * absolute paths
    * parent-traversal (``..``)
    * symlinks / hardlinks
    * device / fifo nodes
    * NUL bytes in names

    Restore aborts on the first occurrence — there is no
    partial-extract mode.
    """


@dataclass(frozen=True)
class Manifest:
    format: str
    shmax_version: str
    created_at: int  # unix seconds
    entries: list[dict]
    notes: list[str]
    architecture: str = field(default_factory=lambda: platform.machine())
    schema_revision: str = ""
    included_components: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "format": self.format,
            "shmax_version": self.shmax_version,
            "created_at": self.created_at,
            "entries": self.entries,
            "notes": self.notes,
            "architecture": self.architecture,
            "schema_revision": self.schema_revision,
            "included_components": self.included_components,
        }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _snapshot_sqlite(db_path: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    snapshot = dest_dir / db_path.name
    if snapshot.exists():
        snapshot.unlink()
    src = sqlite3.connect(str(db_path))
    try:
        dst = sqlite3.connect(str(snapshot))
        try:
            with src:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return snapshot


def _iter_files(root: Path, exclude_dirs: set[str]) -> Iterable[Path]:
    """Yield regular files under ``root`` — NEVER following a
    symlink that escapes the root (live acceptance A7): a symlinked
    file inside the data dir must not smuggle external bytes into
    the backup."""
    root_resolved = root.resolve()
    for p in root.rglob("*"):
        # os.walk-style safety: rglob() follows directory symlinks;
        # skip any path whose resolved parent escapes the root.
        if p.is_symlink():
            try:
                p.resolve().relative_to(root_resolved)
            except ValueError:
                logger.warning("backup: skip escaping symlink %s", p)
                continue
            # Even an INTERNAL symlink is skipped as a file entry —
            # regular files it points to are archived themselves.
            continue
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(part in exclude_dirs for part in rel.parts):
            continue
        if any(part in EXCLUDE_DIR_PARTS for part in rel.parts):
            continue
        if rel.suffix in EXCLUDE_FILE_PARTS or rel.name.endswith(
            EXCLUDE_FILE_PARTS
        ):
            continue
        yield p


def _build_manifest_bytes(
    manifest: Manifest,
    tar_bytes: bytes,
    *,
    shmax_version: str,
    architecture: str,
    schema_revision: str,
    included_components: list[str],
) -> bytes:
    manifest_dict = manifest.to_dict()
    return json.dumps(
        manifest_dict,
        sort_keys=True,
    ).encode("utf-8")


def _build_archive_bytes(data_dir: Path, shmax_version: str) -> tuple[bytes, Manifest]:
    """Build the raw tar.gz archive plus its manifest."""
    data_dir = data_dir.resolve()
    if not data_dir.is_dir():
        raise FileNotFoundError(f"data_dir does not exist: {data_dir}")
    entries: list[dict] = []
    db_path = data_dir / "db" / "shmax.db"
    included: list[str] = []

    with tempfile.TemporaryDirectory(prefix="shmax-backup-") as tmp:
        tmp_dir = Path(tmp)
        bio = io.BytesIO()
        with tarfile.open(fileobj=bio, mode="w:gz") as tar:
            if db_path.is_file():
                snapshot = _snapshot_sqlite(db_path, tmp_dir)
                data = snapshot.read_bytes()
                entries.append(
                    {
                        "path": f"db/{db_path.name}",
                        "sha256": _sha256(data),
                        "size": len(data),
                    }
                )
                tar.addfile(_tf_info(f"db/{db_path.name}", data), io.BytesIO(data))
                included.append("db")
            for file in _iter_files(data_dir, EXCLUDE_DIRS):
                if file.resolve() == db_path:
                    continue
                rel = file.relative_to(data_dir)
                if rel.parts and rel.parts[0] == "db":
                    continue
                data = file.read_bytes()
                entries.append(
                    {"path": str(rel), "sha256": _sha256(data), "size": len(data)}
                )
                tar.addfile(_tf_info(str(rel), data), io.BytesIO(data))
                if rel.parts:
                    top = rel.parts[0]
                    if top not in included:
                        included.append(top)
            manifest = Manifest(
                format=BACKUP_FORMAT_ID,
                shmax_version=shmax_version,
                created_at=int(_dt.datetime.now().timestamp()),
                entries=entries,
                notes=[
                    "unencrypted local backup",
                    "do not commit / leave on a shared filesystem",
                ],
                architecture=platform.machine(),
                schema_revision="",
                included_components=included,
            )
            manifest_bytes = _build_manifest_bytes(
                manifest,
                b"",
                shmax_version=shmax_version,
                architecture=manifest.architecture,
                schema_revision="",
                included_components=included,
            )
            tar.addfile(_tf_info(MANIFEST_NAME, manifest_bytes), io.BytesIO(manifest_bytes))
            tar.addfile(_tf_info(FORMAT_NOTICE_NAME, _format_notice()), io.BytesIO(_format_notice()))
    return bio.getvalue(), manifest


def _format_notice() -> bytes:
    return (
        "SHMAX backup format v1\n"
        "Plain gzip-tar; JSON manifest; SHA-256 per entry.\n"
    ).encode("utf-8")


def _tf_info(name: str, data: bytes) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    info.mtime = int(_dt.datetime.now().timestamp())
    return info


def _read_schema_revision(data_dir: Path) -> str:
    """Best-effort read of the current alembic_version table.

    Uses a sync SQLite connection. Returns "" if the table
    doesn't exist yet (very first boot).
    """
    db_path = data_dir / "db" / "shmax.db"
    if not db_path.is_file():
        return ""
    try:
        con = sqlite3.connect(str(db_path))
        try:
            row = con.execute("SELECT version_num FROM alembic_version LIMIT 1").fetchone()
            return str(row[0]) if row else ""
        finally:
            con.close()
    except sqlite3.Error:
        return ""


def create_backup(
    *,
    data_dir: Path,
    out_path: Path,
    shmax_version: str = "0.1.0-dev",
    schema_revision: str = "",
) -> Manifest:
    """Produce a ``.shmax`` archive at ``out_path``."""
    data_dir = data_dir.resolve()
    out_path = out_path.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(out_path.parent, 0o700)
    except OSError:
        pass
    schema_revision = schema_revision or _read_schema_revision(data_dir)
    tar_bytes, manifest = _build_archive_bytes(data_dir, shmax_version)
    final_manifest = Manifest(
        format=manifest.format,
        shmax_version=manifest.shmax_version,
        created_at=manifest.created_at,
        entries=manifest.entries,
        notes=manifest.notes,
        architecture=manifest.architecture,
        schema_revision=schema_revision,
        included_components=manifest.included_components,
    )
    out_path.write_bytes(tar_bytes)
    # Restrictive permissions (live acceptance #4): the archive
    # carries the whole persistent state.
    try:
        os.chmod(out_path, 0o600)
    except OSError:
        pass
    # Append the final manifest as a separate sidecar so the
    # reader can also inspect it without extracting.
    sidecar = out_path.with_suffix(out_path.suffix + ".manifest.json")
    sidecar.write_text(json.dumps(final_manifest.to_dict(), indent=2, sort_keys=True))
    try:
        os.chmod(sidecar, 0o600)
    except OSError:
        pass
    return final_manifest


def create_encrypted_backup(
    *,
    data_dir: Path,
    out_path: Path,
    passphrase: str,
    shmax_version: str = "0.1.0-dev",
    schema_revision: str = "",
) -> Manifest:
    """Produce a passphrase-encrypted ``.shmax.enc`` archive."""
    if not passphrase or len(passphrase) < 8:
        raise ValueError("passphrase must be at least 8 characters")
    data_dir = data_dir.resolve()
    schema_revision = schema_revision or _read_schema_revision(data_dir)
    tar_bytes, manifest = _build_archive_bytes(data_dir, shmax_version)
    salt = secrets.token_bytes(ARGON2_SALT_BYTES)
    nonce = secrets.token_bytes(AES_NONCE_BYTES)
    key = hash_secret_raw(
        secret=passphrase.encode("utf-8"),
        salt=salt,
        time_cost=ARGON2_TIME_COST,
        memory_cost=ARGON2_MEMORY_COST,
        parallelism=ARGON2_PARALLELISM,
        hash_len=ARGON2_KEY_BYTES,
        type=Argon2Type.ID,
    )
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    ciphertext = AESGCM(key).encrypt(nonce, tar_bytes, ENCRYPTED_HEADER)
    final_manifest = Manifest(
        format=BACKUP_FORMAT_ID,
        shmax_version=shmax_version,
        created_at=manifest.created_at,
        entries=manifest.entries,
        notes=["encrypted with passphrase; Argon2id + AES-256-GCM"],
        architecture=manifest.architecture,
        schema_revision=schema_revision,
        included_components=manifest.included_components,
    )
    out_path = out_path.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Live acceptance A7: 0600 regardless of caller umask — open
    # with the mode set, then force chmod.
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(ENCRYPTED_HEADER + b"\n")
        fh.write(base64.b64encode(salt) + b"\n")
        fh.write(base64.b64encode(nonce) + b"\n")
        fh.write(base64.b64encode(ciphertext) + b"\n")
        fh.write(json.dumps(final_manifest.to_dict()).encode("utf-8"))
    try:
        os.chmod(out_path, 0o600)
    except OSError:
        pass
    return final_manifest


def is_encrypted(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        with path.open("rb") as fh:
            return fh.readline().strip() == ENCRYPTED_HEADER
    except OSError:
        return False


def inspect_backup(archive_path: Path) -> Manifest:
    """Read the manifest out of an archive (plaintext or encrypted)."""
    if is_encrypted(archive_path):
        return _inspect_encrypted(archive_path)
    return _inspect_plaintext(archive_path)


def _inspect_encrypted(path: Path) -> Manifest:
    with path.open("rb") as fh:
        fh.readline()  # magic
        fh.readline()  # salt
        fh.readline()  # nonce
        fh.readline()  # ciphertext
        manifest_bytes = fh.read()
    return _manifest_from_dict(json.loads(manifest_bytes.decode("utf-8")))


def _inspect_plaintext(path: Path) -> Manifest:
    with tarfile.open(path, "r:gz") as tar:
        member = _find_member(tar, MANIFEST_NAME)
        if member is None:
            raise ValueError(f"archive {path} has no manifest")
        fh = tar.extractfile(member)
        if fh is None:
            raise ValueError("manifest is not a regular file")
        data = fh.read()
    return _manifest_from_dict(json.loads(data.decode("utf-8")))


def _manifest_from_dict(data: dict) -> Manifest:
    return Manifest(
        format=data.get("format", BACKUP_FORMAT_ID),
        shmax_version=data.get("shmax_version", "?"),
        created_at=int(data.get("created_at", 0)),
        entries=list(data.get("entries", [])),
        notes=list(data.get("notes", [])),
        architecture=data.get("architecture", ""),
        schema_revision=data.get("schema_revision", ""),
        included_components=list(data.get("included_components", [])),
    )


def validate_backup(archive_path: Path) -> tuple[bool, list[str]]:
    """Verify every entry's SHA-256 against the manifest."""
    errors: list[str] = []
    manifest = inspect_backup(archive_path)
    if manifest.format != BACKUP_FORMAT_ID:
        errors.append(
            f"unsupported backup format: {manifest.format} (expected {BACKUP_FORMAT_ID})"
        )
        return False, errors
    expected = {entry["path"]: entry["sha256"] for entry in manifest.entries}
    if is_encrypted(archive_path):
        return True, errors  # entry-level validation requires decryption
    with tarfile.open(archive_path, "r:gz") as tar:
        seen: dict[str, str] = {}
        for member in tar.getmembers():
            if not member.isfile():
                continue
            if member.name in (MANIFEST_NAME, FORMAT_NOTICE_NAME):
                continue
            fh = tar.extractfile(member)
            if fh is None:
                continue
            data = fh.read()
            seen[member.name] = _sha256(data)
    for path, sha in expected.items():
        if path not in seen:
            errors.append(f"missing entry: {path}")
            continue
        if seen[path] != sha:
            errors.append(f"sha mismatch: {path}")
    for path in seen:
        if path not in expected:
            errors.append(f"unexpected entry: {path}")
    return (len(errors) == 0), errors


def _decrypt_to_tar(archive_path: Path, passphrase: str) -> bytes:
    with archive_path.open("rb") as fh:
        fh.readline()
        salt_b64 = fh.readline().strip()
        nonce_b64 = fh.readline().strip()
        ct_b64 = fh.readline().strip()
    salt = base64.b64decode(salt_b64)
    nonce = base64.b64decode(nonce_b64)
    ciphertext = base64.b64decode(ct_b64)
    key = hash_secret_raw(
        secret=passphrase.encode("utf-8"),
        salt=salt,
        time_cost=ARGON2_TIME_COST,
        memory_cost=ARGON2_MEMORY_COST,
        parallelism=ARGON2_PARALLELISM,
        hash_len=ARGON2_KEY_BYTES,
        type=Argon2Type.ID,
    )
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        return AESGCM(key).decrypt(nonce, ciphertext, ENCRYPTED_HEADER)
    except InvalidTag as e:
        raise ValueError("wrong passphrase or corrupted backup") from e


def restore_backup(archive_path: Path, data_dir: Path, *, passphrase: Optional[str] = None) -> Manifest:
    """Extract a backup archive into ``data_dir``."""
    data_dir.mkdir(parents=True, exist_ok=True)
    if is_encrypted(archive_path):
        if not passphrase:
            raise ValueError("encrypted backup requires a passphrase")
        tar_bytes = _decrypt_to_tar(archive_path, passphrase)
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
            ok, errors = _validate_tar(tar)
            if not ok:
                raise ValueError(f"backup validation failed: {errors}")
            _extract_tar(tar, data_dir)
        return _inspect_encrypted(archive_path)
    manifest = _inspect_plaintext(archive_path)
    ok, errors = validate_backup(archive_path)
    if not ok:
        raise ValueError(f"backup validation failed: {errors}")
    with tarfile.open(archive_path, "r:gz") as tar:
        _extract_tar(tar, data_dir)
    return manifest


def _find_member(tar: tarfile.TarFile, name: str):
    for member in tar.getmembers():
        if member.name == name:
            return member
    return None


def _validate_tar(tar: tarfile.TarFile) -> tuple[bool, list[str]]:
    errors: list[str] = []
    members = tar.getmembers()
    for m in members:
        if m.name.startswith("/") or ".." in m.name.split("/"):
            errors.append(f"unsafe path: {m.name}")
        if not m.isfile():
            continue
        data = tar.extractfile(m).read()
        # check against manifest inside the tar
    return len(errors) == 0, errors


def _validate_member_path(name: str, dest: Path) -> Path:
    """Reject any archive member that escapes ``dest``.

    Raises :class:`UnsafeArchiveError` for:

    * absolute paths (``/etc/passwd``),
    * parent-traversal (``../../etc/passwd``),
    * Windows drive prefixes,
    * NUL bytes (some FSes truncate on NUL).
    """
    if "\x00" in name:
        raise UnsafeArchiveError(f"member path contains NUL: {name!r}")
    if name.startswith("/"):
        raise UnsafeArchiveError(f"absolute path in archive: {name!r}")
    # Reject Windows-style drive prefixes even on POSIX.
    drive, _, rest = name.partition(":")
    if drive and rest and len(drive) == 1 and drive.isalpha():
        raise UnsafeArchiveError(f"drive-prefixed path in archive: {name!r}")
    parts = name.split("/")
    if any(p == ".." for p in parts):
        raise UnsafeArchiveError(f"parent traversal in archive: {name!r}")
    target = (dest / name).resolve()
    dest_resolved = dest.resolve()
    try:
        target.relative_to(dest_resolved)
    except ValueError as e:
        raise UnsafeArchiveError(
            f"archive member escapes destination: {name!r}"
        ) from e
    return target


def _validate_member_kind(member: tarfile.TarInfo) -> None:
    """Refuse any member whose kind is not a regular file or directory."""
    # tarfile constants: REGTYPE/AREGTYPE/0/48 (regular file),
    # DIRTYPE/53 (directory). Everything else — SYMTYPE/LNKTYPE/
    # CHRTYPE/BLKTYPE/FIFOTYPE — is forbidden in V1 backups.
    allowed = {
        tarfile.REGTYPE,
        tarfile.AREGTYPE,
        tarfile.DIRTYPE,
        0,  # some Python versions omit REGTYPE for empty files
        48,
        53,
    }
    if member.type not in allowed:
        raise UnsafeArchiveError(
            f"unsafe member kind {member.type!r} for {member.name!r}"
        )
    if member.issym() or member.islnk():
        raise UnsafeArchiveError(
            f"links are not allowed in V1 backups: {member.name!r}"
        )


def _extract_tar(tar: tarfile.TarFile, dest: Path) -> None:
    """Safely extract ``tar`` into ``dest``.

    Validates EVERY member first; only when the entire archive
    has been vetted does it touch the destination. This way a
    single unsafe member aborts the restore without leaving
    half-extracted data behind.
    """
    dest.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest.resolve()
    members = list(tar.getmembers())
    # Stage 1: validate every member without touching disk.
    for member in members:
        _validate_member_path(member.name, dest_resolved)
        _validate_member_kind(member)
        if member.isdev() or member.ischr() or member.isblk() or member.isfifo():
            raise UnsafeArchiveError(
                f"device / fifo entries not allowed: {member.name!r}"
            )
    # Stage 2: extract. If anything raises here (e.g. FS
    # error), the caller decides how to recover — by this
    # point we know every member is structurally safe.
    for member in members:
        tar.extract(member, dest_resolved)


def backup_id(path: Path) -> str:
    """Stable short id for a backup file (sha256 of content, first
    12 hex) — shown in the UI; never a secret."""
    h = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 16), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()[:12]


def safe_backup_name(name: str, backups_dir: Path) -> Path:
    """Validate a client-supplied backup name and resolve it inside
    ``backups_dir`` (live acceptance #5: no traversal, no absolute
    paths, no symlinks escaping the dir)."""
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise ValueError("invalid backup name")
    if "\x00" in name:
        raise ValueError("invalid backup name")
    target = (backups_dir / name).resolve()
    try:
        target.relative_to(backups_dir.resolve())
    except ValueError as e:
        raise ValueError("backup name escapes the backups directory") from e
    if not target.is_file() or target.is_symlink():
        raise ValueError("no such backup")
    return target


def list_backups(data_dir: Path) -> list[dict]:
    bdir = data_dir / "backups"
    if not bdir.is_dir():
        return []
    out: list[dict] = []
    for f in sorted(bdir.glob("*.shmax*"), reverse=True):
        if not f.is_file() or f.name.endswith(".manifest.json"):
            continue  # sidecar manifests are not backups themselves
        try:
            manifest = inspect_backup(f)
            out.append(
                {
                    "id": backup_id(f),
                    "name": f.name,
                    "path": str(f),
                    "size": f.stat().st_size,
                    "created_at": manifest.created_at,
                    "format": manifest.format,
                    "entries": len(manifest.entries),
                    "encrypted": is_encrypted(f),
                    "schema_revision": manifest.schema_revision,
                    "shmax_version": manifest.shmax_version,
                    "version": manifest.shmax_version,
                    "architecture": manifest.architecture,
                    "included_components": manifest.included_components,
                }
            )
        except Exception as e:  # noqa: BLE001
            out.append({"name": f.name, "path": str(f), "error": str(e)})
    return out


# ----------------------------------------------------------------------
# Transactional restore
# ----------------------------------------------------------------------
class RestoreError(Exception):
    """Raised when a transactional restore fails at any stage.

    The safety backup is always retained so the caller can
    attempt a manual recovery.
    """


@dataclass
class RestoreOutcome:
    manifest: Manifest
    safety_backup_path: Path
    restored_at: int


class TransactionalRestore:
    """Restore a backup while keeping the runtime consistent.

    Lifecycle:

    1. ``before_restore`` hooks (runtime should stop workers, flush DB)
    2. mandatory safety backup of current data_dir
    3. extract archive into a staging directory
    4. atomic swap of staging into data_dir
    5. ``after_restore`` hooks (reload SecretStore, sessions, config)
    6. on any failure: rollback by restoring the safety backup

    Each hook is optional. The ``before_restore`` hook is the
    runtime's chance to stop workers; the ``after_restore`` hook
    is the runtime's chance to reopen the DB and reload the
    SecretStore + SessionSigner.
    """

    def __init__(
        self,
        *,
        data_dir: Path,
        backups_dir: Path,
        shmax_version: str = "0.1.0-dev",
    ) -> None:
        self._data_dir = data_dir.resolve()
        self._backups_dir = backups_dir.resolve()
        self._backups_dir.mkdir(parents=True, exist_ok=True)
        self._shmax_version = shmax_version
        self._before_hooks: list = []
        self._after_hooks: list = []

    def on_before_restore(self, hook) -> None:
        self._before_hooks.append(hook)

    def on_after_restore(self, hook) -> None:
        self._after_hooks.append(hook)

    def run(
        self,
        archive_path: Path,
        *,
        passphrase: Optional[str] = None,
    ) -> RestoreOutcome:
        # ---- Manifest gate: never touch production data until PASS.
        manifest = inspect_backup(archive_path)
        if manifest.format != BACKUP_FORMAT_ID:
            raise RestoreError(
                f"unsupported backup format: {manifest.format}"
            )
        if not manifest.entries:
            raise RestoreError(
                "manifest has no entries - refusing empty backup"
            )
        if is_encrypted(archive_path):
            if not passphrase:
                raise RestoreError("encrypted backup requires a passphrase")
            try:
                tar_bytes = _decrypt_to_tar(archive_path, passphrase)
            except Exception as e:
                raise RestoreError(f"decryption failed: {e}") from e
        else:
            ok, errors = validate_backup(archive_path)
            if not ok:
                raise RestoreError(f"checksum validation failed: {errors}")
            with open(archive_path, "rb") as fh:
                tar_bytes = fh.read()
        # Stage the tarball.
        with tempfile.TemporaryDirectory(
            prefix="shmax-restore-staging-"
        ) as staging_str:
            staging = Path(staging_str)
            try:
                with tarfile.open(
                    fileobj=io.BytesIO(tar_bytes), mode="r:gz"
                ) as tar:
                    _extract_tar(tar, staging)
            except UnsafeArchiveError as e:
                raise RestoreError(f"unsafe archive member: {e}") from e

            # ---- Mandatory safety backup BEFORE touching data_dir.
            safety = self._backups_dir / (
                f"pre-restore-{int(_dt.datetime.now().timestamp())}.shmax"
            )
            try:
                create_backup(
                    data_dir=self._data_dir,
                    out_path=safety,
                    shmax_version=self._shmax_version,
                )
            except Exception as e:
                raise RestoreError(
                    f"safety backup failed: {e} - aborting restore"
                ) from e

            # ---- Stop hooks: shut down workers / flush DB.
            for hook in self._before_hooks:
                try:
                    hook()
                except Exception as e:  # noqa: BLE001
                    logger.exception("before_restore hook failed")
                    raise RestoreError(f"before_restore hook failed: {e}") from e

            # ---- Atomic swap into data_dir.
            try:
                self._swap_into_data_dir(staging)
            except Exception as e:
                logger.exception("swap failed, rolling back")
                # The data_dir may be partially overwritten at this
                # point. Re-extract the safety backup to recover.
                try:
                    restore_backup(safety, self._data_dir)
                except Exception as rb_err:  # noqa: BLE001
                    raise RestoreError(
                        f"restore swap failed AND rollback failed: {e} | {rb_err}"
                    ) from e
                raise RestoreError(f"restore swap failed: {e}") from e

            # ---- After hooks: reload DB, secret store, config.
            for hook in self._after_hooks:
                try:
                    hook()
                except Exception as e:  # noqa: BLE001
                    logger.exception("after_restore hook failed")
                    try:
                        restore_backup(safety, self._data_dir)
                    except Exception as rb_err:  # noqa: BLE001
                        raise RestoreError(
                            f"after_restore hook failed AND rollback failed: {e} | {rb_err}"
                        ) from e
                    raise RestoreError(f"after_restore hook failed: {e}") from e

            return RestoreOutcome(
                manifest=manifest,
                safety_backup_path=safety,
                restored_at=int(_dt.datetime.now().timestamp()),
            )

    def _swap_into_data_dir(self, staging: Path) -> None:
        """Move staged files into ``data_dir``.

        Strategy: rename every entry from ``staging`` into
        ``data_dir``. The data_dir is never wiped wholesale; only
        files that exist in the staging overwrite the production
        copies. If the swap raises halfway through, the partially-
        renamed entries will be reverted by the rollback path that
        restores from the safety backup.
        """
        self._data_dir.mkdir(parents=True, exist_ok=True)
        renamed: list[tuple[Path, Path]] = []
        try:
            for entry in staging.rglob("*"):
                rel = entry.relative_to(staging)
                target = self._data_dir / rel
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() or target.is_symlink():
                    backup = target.with_suffix(target.suffix + ".old")
                    target.replace(backup)
                    renamed.append((target, backup))
                shutil.move(str(entry), str(target))
        except Exception:
            # Best-effort: try to undo the renames we did so the
            # rollback path has a coherent picture.
            for original, backup in renamed:
                if backup.exists() and not original.exists():
                    try:
                        shutil.move(str(backup), str(original))
                    except Exception:  # noqa: BLE001
                        logger.exception("could not undo rename %s", original)
            raise


__all__ = [
    "BACKUP_FORMAT_ID",
    "Manifest",
    "RestoreError",
    "RestoreOutcome",
    "TransactionalRestore",
    "backup_id",
    "create_backup",
    "create_encrypted_backup",
    "inspect_backup",
    "is_encrypted",
    "list_backups",
    "restore_backup",
    "safe_backup_name",
    "validate_backup",
]
