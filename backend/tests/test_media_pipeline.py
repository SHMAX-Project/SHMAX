"""Media pipeline tests.

The pipeline downloads inbound attachments to a cache
directory, validates size + mime, and stores SHA-256. The
runtime releases the file only after delivery is reported
DELIVERED or FAILED.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from shmax.media.pipeline import (
    MediaLimits,
    MediaPipeline,
    MediaTooLarge,
    StoredMedia,
)


def _write_bytes(path: Path, n: int) -> int:
    path.write_bytes(b"\x00" * n)
    return n


def test_store_writes_and_computes_sha(tmp_path: Path):
    pipe = MediaPipeline(tmp_path)
    stored = pipe.store("hello.bin", lambda p: _write_bytes(p, 1024))
    assert stored.path.is_file()
    assert stored.size == 1024
    assert len(stored.sha256) == 64
    pipe.release(stored)
    assert not stored.path.exists()


def test_store_rejects_oversize(tmp_path: Path):
    limits = MediaLimits(max_media_size_bytes=10)
    pipe = MediaPipeline(tmp_path, limits=limits)
    with pytest.raises(MediaTooLarge):
        pipe.store("big.bin", lambda p: _write_bytes(p, 64))
    # Media subdir may exist but contains no orphans.
    assert list(pipe._media.iterdir()) == []


def test_store_rejects_zero_bytes(tmp_path: Path):
    pipe = MediaPipeline(tmp_path)
    with pytest.raises(Exception):
        pipe.store("empty.bin", lambda p: 0)
    assert list(pipe._media.iterdir()) == []


def test_release_is_idempotent(tmp_path: Path):
    pipe = MediaPipeline(tmp_path)
    stored = pipe.store("x.bin", lambda p: _write_bytes(p, 16))
    assert pipe.release(stored) is True
    assert pipe.release(stored) is False  # file already gone


def test_exists_after_release_is_false(tmp_path: Path):
    pipe = MediaPipeline(tmp_path)
    stored = pipe.store("x.bin", lambda p: _write_bytes(p, 16))
    pipe.release(stored)
    assert pipe.exists(stored) is False


def test_cleanup_expired_removes_old_files(tmp_path: Path):
    pipe = MediaPipeline(tmp_path, limits=MediaLimits(cache_ttl_seconds=0))
    stored = pipe.store("x.bin", lambda p: _write_bytes(p, 16))
    # Touch the mtime to the past so it's expired.
    os.utime(stored.path, (10_000, 10_000))
    n = pipe.cleanup_expired()
    assert n == 1
    assert not stored.path.exists()


@pytest.mark.asyncio
async def test_store_async_works(tmp_path: Path):
    pipe = MediaPipeline(tmp_path)

    async def downloader(path: Path) -> int:
        await asyncio.sleep(0)
        return _write_bytes(path, 100)

    stored = await pipe.store_async("async.bin", downloader)
    assert stored.size == 100
    pipe.release(stored)


def test_sanitises_filename_hint(tmp_path: Path):
    pipe = MediaPipeline(tmp_path)
    stored = pipe.store("../../etc/passwd", lambda p: _write_bytes(p, 8))
    # The cache file lives strictly inside the cache dir.
    assert stored.path.resolve().is_relative_to(tmp_path.resolve())
    pipe.release(stored)


def test_media_ref_format(tmp_path: Path):
    pipe = MediaPipeline(tmp_path)
    stored = pipe.store("img.bin", lambda p: _write_bytes(p, 8), kind="photo")
    assert stored.media_ref.startswith("photo:")
    assert stored.media_ref.endswith(str(stored.path))
    pipe.release(stored)