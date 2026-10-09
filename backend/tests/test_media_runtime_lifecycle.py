"""BLOCKER 12 — Media persistent cache + unified kind.

The runtime MUST:

* keep the cached file alive while the queue item is in
  pending / processing / retry (re-tries need the file);
* release the cached file when the item transitions to
  delivered or failed;
* normalise the media kind to photo / file / video / voice
  for the adapter call sites.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shmax.media.pipeline import (
    MediaLimits,
    MediaPipeline,
    StoredMedia,
)
from shmax.runtime import _canonical_media_kind


def test_canonical_kind_accepts_aliases():
    for raw, expected in [
        ("photo", "photo"),
        ("image", "photo"),
        ("file", "file"),
        ("document", "file"),
        ("video", "video"),
        ("voice", "voice"),
        ("audio", "voice"),
    ]:
        assert _canonical_media_kind(raw) == expected


def test_canonical_kind_unknown_falls_back_to_file():
    assert _canonical_media_kind("foo") == "file"
    assert _canonical_media_kind(None) == "file"


def test_media_lives_under_persistent_media_subdir(tmp_path: Path):
    pipe = MediaPipeline(tmp_path)
    stored = pipe.store("photo.bin", lambda p: __import__("shmax.media.pipeline", fromlist=["_write_bytes"])._write_bytes(p, 8) if False else (p.write_bytes(b"\x00" * 8) or 8))
    # The file lives under cache/media/<id>, NOT directly under
    # the cache root, so unrelated cache artifacts aren't disturbed.
    assert stored.path.parent == pipe._media
    assert stored.path.exists()


def test_cleanup_expired_only_touches_media_subdir(tmp_path: Path):
    pipe = MediaPipeline(tmp_path, limits=MediaLimits(cache_ttl_seconds=0))
    stored = pipe.store("x.bin", lambda p: (p.write_bytes(b"\x00" * 4) or 4))
    # Touch mtime to past so it counts as expired.
    import os as _os
    _os.utime(stored.path, (10_000, 10_000))
    # Place a stray file at the cache root — it must survive.
    stray = tmp_path / "keep.txt"
    stray.write_text("must stay")
    cleaned = pipe.cleanup_expired()
    assert cleaned >= 1
    # The stray file is preserved.
    assert stray.is_file()