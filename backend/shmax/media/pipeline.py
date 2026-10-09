"""Media pipeline.

Inbound attachments are downloaded from their source
transport (PyMax / aiogram / Telethon) into a cache directory
under ``$SHMAX_DATA_DIR/cache/``. The pipeline validates the
file (size, mime) and hands the metadata to the outbound
adapter through the queue payload.

Lifecycle:

* ``store(name_hint, downloader)`` -> ``StoredMedia``
  - caller passes a closure that fills the destination path
  - pipeline enforces size limit and computes SHA-256
  - on failure, the file is unlinked before raising
* ``release(stored)`` -> removes the cache file; called by
  the runtime after delivery completes (success or final-failure)
* ``cleanup_expired()`` -> periodic TTL reclaim for files that
  escaped ``release`` because of an aborted process

The queue payload carries:

* ``media_kind``   - photo / document / video / voice
* ``media_filename`` - safe filename hint
* ``media_mime``   - detected mime
* ``media_size``   - bytes written
* ``media_ref``    - ``kind:/abs/path`` form for adapter send_media
* ``media_sha256`` - integrity digest

Retries keep the file alive: ``release`` is only called when
the worker reports DELIVERED or FAILED (not RETRY).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
import time
import secrets
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Optional


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MediaLimits:
    max_media_size_bytes: int = 50 * 1024 * 1024  # 50 MiB
    cache_ttl_seconds: int = 3600
    cache_limit_bytes: int = 200 * 1024 * 1024  # 200 MiB


@dataclass(frozen=True)
class StoredMedia:
    """File saved to disk and ready for outbound upload.

    The runtime attaches this metadata to the queue payload
    so the delivery worker can pass ``media_ref`` to the
    adapter's ``send_media``.
    """

    path: Path
    size: int
    mime: str
    sha256: str
    kind: str = "document"
    filename: str = "media"
    duration_ms: Optional[int] = None

    @property
    def media_ref(self) -> str:
        """``kind:/abs/path`` form for ``send_media`` adapters."""
        return f"{self.kind}:{self.path}"


class MediaError(Exception):
    pass


class MediaTooLarge(MediaError):
    pass


class MediaNotFound(MediaError):
    pass


# A downloader fills the destination path and returns bytes written.
Downloader = Callable[[Path], int]


class MediaPipeline:
    """Stateless pipeline around a single cache directory.

    The runtime owns one instance and serialises concurrent
    downloads via the queue worker pool. Cached files live
    under ``<cache>/media/<id>`` so cleanup-after-delivery
    doesn't accidentally remove other artifacts.
    """

    def __init__(
        self,
        cache_dir: Path,
        limits: MediaLimits | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._cache = cache_dir
        self._cache.mkdir(parents=True, exist_ok=True)
        self._media = cache_dir / "media"
        self._media.mkdir(parents=True, exist_ok=True)
        self._limits = limits or MediaLimits()
        self._clock = clock

    def _media_path(self, name_hint: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name_hint)[:48] or "media"
        return self._media / f"{int(self._clock() * 1000)}_{secrets.token_hex(8)}_{safe}"

    def store(
        self,
        name_hint: str,
        downloader: Downloader,
        *,
        kind: str = "file",
        mime: str = "application/octet-stream",
        duration_ms: Optional[int] = None,
    ) -> StoredMedia:
        """Run ``downloader(target)`` and return stored metadata.

        ``downloader`` may be either synchronous (returns int)
        or async (returns coroutine returning int). The
        pipeline streams the resulting file through SHA-256
        for integrity and rejects sizes above the limit.
        """
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name_hint)[:48] or "media"
        path = self._media / f"{int(self._clock() * 1000)}_{secrets.token_hex(8)}_{safe}"
        try:
            result = downloader(path)
            if asyncio.iscoroutine(result):
                # Adapt: run the coroutine synchronously here
                # is impossible without an event loop, so we
                # require callers to pass sync downloaders in
                # the synchronous path. The async pipeline
                # variant is exposed as ``store_async``.
                raise TypeError(
                    "use store_async() for coroutine downloaders"
                )
            written = int(result)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        if written <= 0:
            path.unlink(missing_ok=True)
            raise MediaError("downloader wrote 0 bytes")
        if written > self._limits.max_media_size_bytes:
            path.unlink(missing_ok=True)
            raise MediaTooLarge(
                f"media exceeds {self._limits.max_media_size_bytes} bytes"
            )
        sha = hashlib.sha256()
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                sha.update(chunk)
        return StoredMedia(
            path=path,
            size=written,
            mime=mime,
            sha256=sha.hexdigest(),
            kind=kind,
            filename=safe,
            duration_ms=duration_ms,
        )

    async def store_async(
        self,
        name_hint: str,
        downloader: Callable[[Path], "asyncio.Future[int] | int"],
        *,
        kind: str = "document",
        mime: str = "application/octet-stream",
        duration_ms: Optional[int] = None,
    ) -> StoredMedia:
        """Async variant: awaits coroutine downloaders.

        Writes into the managed ``<cache>/media/`` directory and
        enforces the same limits as the synchronous :meth:`store`
        — the previous split (sync into media/, async into
        cache/) broke TTL cleanup and release-after-delivery."""
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name_hint)[:48] or "media"
        path = self._media / f"{int(self._clock() * 1000)}_{secrets.token_hex(8)}_{safe}"
        try:
            result = downloader(path)
            if asyncio.iscoroutine(result):
                written = int(await result)
            else:
                written = int(result)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        if written <= 0:
            path.unlink(missing_ok=True)
            raise MediaError("downloader wrote 0 bytes")
        if written > self._limits.max_media_size_bytes:
            path.unlink(missing_ok=True)
            raise MediaTooLarge(
                f"media exceeds {self._limits.max_media_size_bytes} bytes"
            )
        sha = hashlib.sha256()
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                sha.update(chunk)
        return StoredMedia(
            path=path,
            size=written,
            mime=mime,
            sha256=sha.hexdigest(),
            kind=kind,
            filename=safe,
            duration_ms=duration_ms,
        )

    def release(self, stored: StoredMedia) -> bool:
        """Remove the cache file. Called after delivery finishes.

        Returns True if a file was actually removed; False when
        the file was already gone or unlink failed.
        """
        try:
            if not stored.path.exists():
                return False
            stored.path.unlink()
            return True
        except OSError:
            logger.exception("media release failed path=%s", stored.path)
            return False

    def exists(self, stored: StoredMedia) -> bool:
        """Check whether the cached file is still on disk.

        Workers call this on retry: if the file vanished (e.g.
        the runtime crashed and the cache was wiped) the
        delivery cannot be reproduced and the worker must
        mark the queue item FAILED.
        """
        return stored.path.is_file()

    def cleanup_expired(self) -> int:
        now = self._clock()
        ttl = self._limits.cache_ttl_seconds
        cleaned = 0
        # Only iterate the media subdirectory; do not touch
        # other artifacts in the cache root.
        for f in self._media.iterdir():
            if not f.is_file():
                continue
            if now - f.stat().st_mtime > ttl:
                try:
                    f.unlink()
                    cleaned += 1
                except OSError:
                    logger.exception("failed to remove expired cache file %s", f)
        self._enforce_size_limit()
        return cleaned

    def _enforce_size_limit(self) -> None:
        files = sorted(
            (
                (f, f.stat().st_size, f.stat().st_mtime)
                for f in self._media.iterdir()
                if f.is_file()
            ),
            key=lambda t: t[2],
        )
        total = sum(size for _, size, _ in files)
        for f, size, _ in files:
            if total <= self._limits.cache_limit_bytes:
                break
            try:
                f.unlink()
                total -= size
            except OSError:
                logger.exception("failed to remove cache file %s", f)

    def clear_cache(self) -> int:
        cleaned = 0
        for f in self._media.iterdir():
            if f.is_file():
                try:
                    f.unlink()
                    cleaned += 1
                except OSError:
                    pass
        return cleaned

    def cache_size(self) -> int:
        return sum(f.stat().st_size for f in self._media.iterdir() if f.is_file())


__all__ = [
    "Downloader",
    "MediaError",
    "MediaLimits",
    "MediaNotFound",
    "MediaPipeline",
    "MediaTooLarge",
    "StoredMedia",
]