"""BLOCKER 13 — Watchdog exception path + dispatcher wiring.

The watchdog's ``_on_failure`` is keyword-only and must be
called with explicit ``detail`` / ``error_class`` / ``now``
kwargs. The runtime wires the watchdog to the persistent
notification dispatcher (``OutboxBackedDispatcher``) so
incident events flow through the same outbox the runtime
drains.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from shmax.adapters.max.fake import FakeMaxAdapter
from shmax.watchdog.base import (
    AdapterHealthState,
    NotificationDispatcher,
    WatchdogConfig,
    WatchdogEvent,
    WatchdogStateStore,
)
from shmax.watchdog.max_watchdog import MaxWatchdog
from shmax.config import RuntimeMode


class _RecordingDispatcher(NotificationDispatcher):
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    async def send(
        self,
        *,
        event_type: str,
        chat_id: str,
        thread_id: str | None,
        payload: dict,
    ) -> None:
        self.events.append((event_type, chat_id, payload))


def test_on_failure_is_keyword_only():
    sig = inspect.signature(MaxWatchdog._on_failure)
    # All non-self parameters must be keyword-only.
    params = list(sig.parameters.values())
    # Drop self; the rest must be KEYWORD_ONLY.
    rest = params[1:]
    assert all(p.kind == inspect.Parameter.KEYWORD_ONLY for p in rest), (
        f"_on_failure parameters must be keyword-only; got {sig}"
        " — a regression would silently drop args at call sites"
    )


@pytest.mark.asyncio
async def test_probe_exception_routes_through_keyword_args(tmp_path):
    dispatcher = _RecordingDispatcher()
    adapter = FakeMaxAdapter()

    def boom(*a, **k):
        raise RuntimeError("probe boom")

    adapter.health = boom  # type: ignore[assignment]
    store = WatchdogStateStore(tmp_path / "wd.json")
    cfg = WatchdogConfig(down_threshold=2, downtime_grace_seconds=0)
    clock = lambda: 1_700_000_000  # noqa: E731
    wd = MaxWatchdog(
        adapter=adapter,
        store=store,
        dispatcher=dispatcher,
        config=cfg,
        clock=clock,
    )
    # Trigger the exception path. If the call to ``_on_failure``
    # was malformed (positional) the test fails with TypeError.
    await wd._tick()
    assert dispatcher.events, "no events were dispatched"


@pytest.mark.asyncio
async def test_dispatcher_used_by_runtime_is_the_outbox_backed_dispatcher(tmp_path):
    """The runtime's ``_inner_dispatcher`` is the same object
    exposed on ``app.state`` and reachable from the
    watchdogs; the watchdogs must NOT be wired to a different
    dispatcher instance after construction."""
    from shmax.runtime import AppRuntime, RuntimeConfig
    from shmax.core.paths import PathLayout
    from shmax.security import SecretStore
    from shmax.database import Database, DatabaseSettings
    from shmax.database.migrations import run_migrations_sync
    from shmax.notifications.outbox_dispatcher import OutboxBackedDispatcher

    data = tmp_path / "shmax-data-dispatcher"
    settings = DatabaseSettings(data_dir=data)
    db = Database(settings)
    run_migrations_sync(settings.url)
    layout = PathLayout(data)
    secret_store = SecretStore(layout.root / "secrets")
    rt = AppRuntime(
        database=db,
        secret_store=secret_store,
        layout=layout,
        config=RuntimeConfig(start_workers=False),
        runtime_mode=RuntimeMode.TEST,
    )
    # Before any swap both watchdogs share the Logging fallback.
    assert rt.max_watchdog._dispatcher is rt.tg_watchdog._dispatcher
    inner = rt._inner_dispatcher
    assert inner is not None
    assert rt.max_watchdog._dispatcher is inner
    # Mutating the runtime's dispatcher slot is immediately visible
    # to the watchdogs. Production's ``start()`` does exactly this.
    new_dispatcher = OutboxBackedDispatcher(
        outbox=rt.notification_outbox,
        inner=inner,
        destination_chat_id="self",
        thread_id=None,
    )
    rt._dispatcher_slot.current = new_dispatcher
    assert rt.max_watchdog._dispatcher is new_dispatcher
    assert rt.tg_watchdog._dispatcher is new_dispatcher
    assert rt.max_watchdog._dispatcher is not inner
    await db.dispose()
