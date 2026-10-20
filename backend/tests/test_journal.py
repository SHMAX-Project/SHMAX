import asyncio, json, sqlite3
import pytest
from shmax.journal import EventJournal
from tests.test_runtime_offline import runtime


@pytest.mark.asyncio
async def test_persistence_filters_pagination_privacy_aggregation(tmp_path):
    j = EventJournal(tmp_path / "events.db")
    await j.start()
    for i in range(5):
        j.emit("message.received", component="max", correlation_id=f"{i:024x}")
    for _ in range(12):
        j.emit(
            "max.reconnect_failure",
            component="max",
            error_class="TimeoutError",
            aggregate=True,
        )
    j.emit("max.reconnect_success", component="max")
    j.emit(
        "max.reconnect_failure",
        component="max",
        error_class="TimeoutError",
        aggregate=True,
    )
    j.emit("injected token=secret")
    j.emit(
        "delivery.failed",
        error_class="secret-cookie=<script>",
        correlation_id="password=secret",
        result="raw-secret",
    )
    await j.flush()
    a = j.query(limit=3)
    b = j.query(limit=3, before=a["next_cursor"])
    assert not {x["id"] for x in a["events"]} & {x["id"] for x in b["events"]}
    assert len(j.query(category="MAX")["events"]) == 3
    assert sorted(x["occurrences"] for x in j.query(severity="WARNING")["events"]) == [
        1,
        12,
    ]
    assert len(j.query(search="message.received")["events"]) == 5
    data = json.dumps(j.query(limit=200))
    assert "secret" not in data and "<script>" not in data
    await j.close()
    other = EventJournal(j.path)
    await other.start()
    assert len(other.query(limit=200)["events"]) == 9
    await other.close()


@pytest.mark.asyncio
async def test_age_count_retention_isolation(tmp_path):
    now = [1000000.0]
    j = EventJournal(
        tmp_path / "events.db", retention_days=1, max_records=5, clock=lambda: now[0]
    )
    await j.start()
    (tmp_path / "session.db").write_bytes(b"keep")
    for _ in range(8):
        j.emit("message.received")
    await j.flush()
    assert len(j.query()["events"]) == 5
    now[0] += 86401
    j.emit("system.started")
    await j.flush()
    assert len(j.query()["events"]) == 1
    assert (tmp_path / "session.db").read_bytes() == b"keep"
    await j.close()


@pytest.mark.asyncio
async def test_write_failure_bounded_nonfatal(tmp_path, monkeypatch):
    j = EventJournal(tmp_path / "events.db", queue_size=8)
    await j.start()

    def fail(batch):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(j, "_write", fail)
    for _ in range(100):
        j.emit("message.received")
    await j.flush()
    assert j.dropped == 100 and j.failures > 0 and j.queue.qsize() == 0
    await j.close()


@pytest.mark.asyncio
async def test_size_budget_concurrent_producers(tmp_path):
    j = EventJournal(tmp_path / "events.db", max_bytes=512 * 1024, queue_size=1024)
    await j.start()

    async def produce(group):
        for i in range(1200):
            j.emit("delivery.success", correlation_id=f"{group*1200+i:024x}")
            if i % 50 == 0:
                await j.flush()

    await asyncio.gather(*(produce(n) for n in range(4)))
    await j.flush()
    assert j.storage()["bytes"] <= j.max_bytes
    with sqlite3.connect(j.path) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    await j.close()


@pytest.mark.asyncio
async def test_journal_failure_does_not_stop_actual_delivery(runtime, monkeypatch):
    from shmax.adapters.max.base import MaxEvent

    def unavailable(batch):
        raise OSError("synthetic disk full")

    monkeypatch.setattr(runtime.journal, "_write", unavailable)
    await runtime.max_adapter.inject_message(
        MaxEvent("journal-fault", "max-dialog-1", "m1", "synthetic delivery")
    )
    async with asyncio.timeout(5):
        while not runtime.telegram_adapter.sent_messages:
            await asyncio.sleep(0.01)
    await runtime.journal.flush()
    assert runtime.journal.failures > 0
    assert any(
        "synthetic delivery" in m["text"]
        for m in runtime.telegram_adapter.sent_messages
    )

@pytest.mark.asyncio
async def test_worker_stop_is_recorded_on_normal_shutdown(runtime):
    await runtime.stop()
    rows=runtime.journal.query(search='worker.stopped')['events']
    assert len(rows)==runtime.config.max_concurrent_workers


@pytest.mark.asyncio
async def test_inbound_processing_failure_is_visible_without_exception_contents(runtime, monkeypatch):
    from shmax.bridge.engine import BridgeEngine
    from shmax.adapters.max.base import MaxEvent

    async def fail(*args, **kwargs):
        raise AttributeError('private-message-secret')

    monkeypatch.setattr(BridgeEngine, 'handle_max_event', fail)
    await runtime._on_inbound('max', MaxEvent('processing-fault', 'max-dialog-1', 'm1', 'private-body'))
    await runtime.journal.flush()
    rows = runtime.journal.query(search='message.processing_failed')['events']
    assert len(rows) == 1
    row = rows[0]
    assert row['severity'] == 'ERROR'
    assert row['error_class'] == 'AttributeError'
    assert row['direction'] == 'max_to_telegram'
    assert row['correlation_id']
    assert 'private-' not in json.dumps(rows)
