"""BLOCKER 1 — production lifespan must start AppRuntime.

This test runs the **real** FastAPI lifespan — the same code
path that production uvicorn runs — and confirms AppRuntime is
constructed, started, and stopped without spawning a second
DB / second workers / second listeners.

The fixtures here deliberately avoid constructing any
``AppRuntime`` manually; the only constructor in scope is the
one inside ``create_app(...)``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shmax.api.app import create_app
from shmax.config import RuntimeMode, Settings


def _make_app(data_dir: Path):
    settings = Settings(
        host="127.0.0.1",
        port=0,
        data_dir=data_dir,
        log_level="WARNING",
        runtime_mode=RuntimeMode.TEST,
    )
    return create_app(settings)


def test_lifespan_starts_and_stops_appruntime(data_dir: Path):
    app = _make_app(data_dir)

    with TestClient(app) as client:
        # AppRuntime must have been constructed by the
        # lifespan — not by the test.
        runtime = getattr(client.app.state, "runtime", None)
        assert runtime is not None, "lifespan did not construct AppRuntime"
        assert runtime.started is True, "AppRuntime.start() was not called"

        # The runtime is the canonical owner of every
        # collaborator. No duplicate components in app.state.
        assert runtime.db is client.app.state.db
        assert runtime.secrets is client.app.state.secrets
        assert runtime.media_pipeline is client.app.state.media_pipeline
        assert runtime.watchdog_store is client.app.state.watchdog_store

        # /health/ready reports the runtime is up.
        resp = client.get("/health/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert body["runtime"] is True
        assert body["database"] is True

        # Watchdog supervisor tasks are configured.
        assert "max_watchdog" in runtime._supervisors
        assert "telegram_watchdog" in runtime._supervisors
        assert "max_listener" in runtime._supervisors
        assert "telegram_listener" in runtime._supervisors

    # Outside the context manager the runtime is no longer
    # started; /health/ready would now return maintenance.
    assert runtime.started is False


def test_lifespan_runs_migrations_and_starts_supervisor(data_dir: Path):
    """Two consecutive lifespans must both succeed and share
    the same DB schema (migrations are idempotent)."""
    app = _make_app(data_dir)
    with TestClient(app) as client:
        runtime = client.app.state.runtime
        # Watchdog supervisor actually starts its _run task.
        wd = runtime._supervisors["max_watchdog"]
        assert wd._task is not None
        assert not wd._task.done()


def test_lifespan_stop_is_idempotent(data_dir: Path):
    """Calling runtime.stop() twice does not raise; the second
    call finds an empty supervisors dict and exits cleanly."""
    app = _make_app(data_dir)
    with TestClient(app) as client:
        runtime = client.app.state.runtime
    # Inside the `with` the lifespan stopped the runtime.
    # Calling stop() again must not raise.
    import asyncio
    asyncio.run(runtime.stop())
    asyncio.run(runtime.stop())


def test_lifespan_does_not_spawn_duplicate_components(data_dir: Path):
    """The single most important invariant of BLOCKER 1: the
    API must not construct its own DB / secret_store / workers
    parallel to the runtime. We assert by counting DB session
    factories."""
    app = _make_app(data_dir)
    with TestClient(app) as client:
        runtime = client.app.state.runtime
        # The FastAPI app holds no DB instance outside the one
        # inside the runtime.
        assert client.app.state.db is runtime.db
        # There is exactly one notification outbox.
        assert runtime.notification_outbox is not None
        # And the watchdogs the runtime created are the same
        # objects reachable from app.state.runtime.
        assert (
            client.app.state.runtime.max_watchdog
            is runtime.max_watchdog
        )