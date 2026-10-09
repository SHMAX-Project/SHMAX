"""Notification dispatcher + media pipeline tests."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from shmax.adapters.telegram.fake import FakeTelegramAdapter
from shmax.media.pipeline import MediaLimits, MediaPipeline, MediaTooLarge
from shmax.notifications.dispatcher import (
    BufferingNotificationDispatcher,
    TelegramNotificationDispatcher,
    format_event,
)
from shmax.security import SecretStore
from shmax.watchdog.base import (
    AdapterHealthState,
    LoggingNotificationDispatcher,
    WatchdogEvent,
    WatchdogSnapshot,
)


def test_format_event_down_includes_downtime():
    snap = WatchdogSnapshot(
        adapter="max",
        state=AdapterHealthState.DOWN,
        down_since=1_700_000_000,
        last_success_at=1_699_900_000,
        last_error_class="Disconnected",
        consecutive_failures=3,
    )
    text = format_event(WatchdogEvent(kind="down", adapter="max", state=AdapterHealthState.DOWN, snapshot=snap))
    assert "MAX недоступен" in text
    assert "Disconnected" in text


def test_format_event_recovered():
    snap = WatchdogSnapshot(
        adapter="max",
        state=AdapterHealthState.HEALTHY,
        down_since=1_700_000_000,
        last_success_at=1_700_100_000,
    )
    text = format_event(WatchdogEvent(kind="recovered", adapter="max", state=AdapterHealthState.HEALTHY, snapshot=snap))
    assert "снова работает" in text


def test_format_event_hourly_reminder():
    snap = WatchdogSnapshot(adapter="max", state=AdapterHealthState.DOWN, down_since=1_700_000_000)
    text = format_event(WatchdogEvent(kind="hourly_reminder", adapter="max", state=AdapterHealthState.DOWN, snapshot=snap))
    assert "всё ещё недоступен" in text


def test_buffering_dispatcher_persists_to_disk(tmp_path):
    inner = LoggingNotificationDispatcher()
    buffered = BufferingNotificationDispatcher(tmp_path / "buf.jsonl", inner)
    snap = WatchdogSnapshot(adapter="max", state=AdapterHealthState.DOWN)
    asyncio.run(buffered.dispatch(WatchdogEvent(kind="down", adapter="max", state=AdapterHealthState.DOWN, snapshot=snap)))
    assert buffered.buffered_count() == 1
    assert (tmp_path / "buf.jsonl").is_file()


def test_buffering_dispatcher_flushes_to_telegram(tmp_path):
    inner = LoggingNotificationDispatcher()
    buffered = BufferingNotificationDispatcher(tmp_path / "buf.jsonl", inner)
    snap = WatchdogSnapshot(adapter="max", state=AdapterHealthState.DOWN)
    asyncio.run(buffered.dispatch(WatchdogEvent(kind="down", adapter="max", state=AdapterHealthState.DOWN, snapshot=snap)))
    sent = asyncio.run(buffered.flush(FakeTelegramAdapter()))
    assert sent == 1
    assert buffered.buffered_count() == 0


def test_telegram_dispatcher_sends_text(tmp_path):
    tg = FakeTelegramAdapter()
    dispatch = TelegramNotificationDispatcher(tg, chat_id="123", thread_id="456")
    snap = WatchdogSnapshot(adapter="max", state=AdapterHealthState.DOWN)
    asyncio.run(dispatch.dispatch(WatchdogEvent(kind="down", adapter="max", state=AdapterHealthState.DOWN, snapshot=snap)))
    assert any(m["kind"] == "message" and "MAX недоступен" in m["text"] for m in tg.sent_messages)


def test_media_pipeline_size_limit(tmp_path):
    pipe = MediaPipeline(tmp_path / "cache", MediaLimits(max_media_size_bytes=10))
    big_path = tmp_path / "big.bin"
    big_path.write_bytes(b"x" * 100)

    def _writer(target: Path) -> int:
        data = big_path.read_bytes()
        target.write_bytes(data)
        return len(data)

    with pytest.raises(MediaTooLarge):
        with pipe.store("big", _writer):
            pass


def test_media_pipeline_cleanup_expired(tmp_path):
    pipe = MediaPipeline(tmp_path / "cache", MediaLimits(cache_ttl_seconds=0))
    f = tmp_path / "cache" / "media" / "old.bin"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"x" * 4)
    # Set mtime to the past.
    import os, time

    os.utime(f, (time.time() - 100, time.time() - 100))
    removed = pipe.cleanup_expired()
    assert removed == 1
    assert not f.exists()


def test_media_pipeline_enforce_size_limit(tmp_path):
    pipe = MediaPipeline(
        tmp_path / "cache",
        MediaLimits(cache_limit_bytes=10, cache_ttl_seconds=999999),
    )
    files = []
    media_dir = tmp_path / "cache" / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    for i in range(5):
        p = media_dir / f"f{i}.bin"
        p.write_bytes(b"x" * 10)
        files.append(p)
    pipe._enforce_size_limit()
    # Only the most-recent file should survive.
    survivors = [f for f in media_dir.iterdir() if f.is_file()]
    assert len(survivors) == 1
