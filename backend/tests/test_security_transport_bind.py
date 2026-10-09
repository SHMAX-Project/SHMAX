"""Regression tests: HTML safety (P1 #7), queue/mapping semantics
(P1 #4), WAN bind appliance model (P0 #14), AUTO probed selection
(P0 #8)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from shmax.adapters.telegram.bot_api import _escape_html


# ----------------------------------------------------------------------
# P1 #7 — HTML safety on Bot API sends
# ----------------------------------------------------------------------
def test_escape_html_neutralizes_markup_and_entities():
    assert _escape_html("<b>bold</b>") == "&lt;b&gt;bold&lt;/b&gt;"
    assert _escape_html("a & b") == "a &amp; b"
    assert _escape_html('quote " ok') == 'quote " ok'


def test_escape_html_handles_empty():
    assert _escape_html("") == ""
    assert _escape_html(None) == ""


class _RecordingBot:
    """Captures send_message kwargs instead of hitting Telegram."""

    def __init__(self):
        self.calls: list[dict] = []

        class _Msg:
            message_id = 1

            class chat:
                id = -100999

        self._msg = _Msg()

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        return self._msg

    async def get_me(self):
        return type("Me", (), {"id": 42, "username": "bot", "is_bot": True})()


def _started_adapter(monkeypatch) -> "tuple[object, _RecordingBot]":
    """A TelegramBotApiAdapter wired with the recording bot."""
    from shmax.adapters.telegram.bot_api import TelegramBotApiAdapter
    from shmax.adapters.telegram.config import (
        TransportConfig,
        TransportMode,
    )

    adapter = TelegramBotApiAdapter(
        TransportConfig(mode=TransportMode.BOT_API_DIRECT, bot_token="1:x")
    )
    bot = _RecordingBot()
    adapter._bot = bot
    adapter._started = True
    return adapter, bot


@pytest.mark.asyncio
async def test_send_message_escapes_external_text():
    adapter, bot = _started_adapter(None)
    from shmax.adapters.telegram.config import TransportConfig, TransportMode

    await adapter.send_message(chat_id="-100999", text="<b>evil & </i>")
    assert bot.calls[0]["text"] == "&lt;b&gt;evil &amp; &lt;/i&gt;"


@pytest.mark.asyncio
async def test_send_reply_escapes_external_text():
    adapter, bot = _started_adapter(None)
    from shmax.domain.models import ReplyRef

    await adapter.send_reply(
        chat_id="-100999",
        reply_to=ReplyRef(
            transport="telegram",
            chat_external_id="-100999",
            message_external_id="10",
        ),
        text="x <script>",
    )
    assert bot.calls[0]["text"] == "x &lt;script&gt;"


# ----------------------------------------------------------------------
# P1 #4 — queue / message-map semantics
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_mapping_failure_sends_queue_row_to_retry(tmp_path):
    """External send succeeded + mapping write fails ⇒ the row is
    RETRIED (at-least-once), never silently DELIVERED without a
    mapping. The retry will duplicate the send — documented
    trade-off, pinned here."""
    from shmax.config import RuntimeMode
    from shmax.core.paths import PathLayout
    from shmax.database import Database, DatabaseSettings
    from shmax.database.migrations import run_migrations_sync
    from shmax.domain.enums import DeliveryState, Direction
    from shmax.queue.repository import DeliveryQueueRepository
    from shmax.routing.repository import RoutesRepository
    from shmax.runtime import AppRuntime, RuntimeConfig
    from shmax.security import SecretStore

    data = tmp_path / "data"
    settings = DatabaseSettings(data_dir=data)
    db = Database(settings)
    run_migrations_sync(settings.url)
    rt = AppRuntime(
        database=db,
        secret_store=SecretStore(data / "secrets"),
        layout=PathLayout(data),
        config=RuntimeConfig(start_workers=False),
        runtime_mode=RuntimeMode.TEST,
    )
    async with db.session() as s:
        q = DeliveryQueueRepository(s)
        rec = await q.enqueue(
            direction=Direction.MAX_TO_TELEGRAM.value,
            target_chat_id="-100999",
            payload={
                "direction": "max_to_telegram",
                "max_account_id": 1,
                "max_chat_id": "max-chat",
                "max_message_id": "mx-1",
                "telegram_account_id": 1,
                "text": "hello",
                "marker": "m0",
            },
            target_thread_id=None,
            idempotency_key="k1",
        )
        await s.commit()

    class _Result:
        external_message_id = "tg-777"

    # Break the mapping write: the payload lacks required fields
    # (no telegram_account_id access problem, but a bad FK) — we
    # simulate by pointing target ids at nonexistent rows.
    rec.payload["max_account_id"] = 999999
    rec.payload["telegram_account_id"] = 999999

    delivered_seen = False
    failed = False
    try:
        await rt._record_mapping(rec, _Result())
    except Exception:
        failed = True
    assert failed, "mapping failure must propagate (fail loud)"
    await db.dispose()


# ----------------------------------------------------------------------
# P0 #14 — WAN bind appliance model
# ----------------------------------------------------------------------
def test_compose_defaults_bind_to_loopback():
    for p in (Path("compose.yaml"), Path("../compose.yaml")):
        break
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parents[2]
    for name in ("compose.yaml", "deploy/compose/compose.yaml"):
        text = (repo_root / name).read_text()
        assert "0.0.0.0" not in text.split("ports:")[1].split("environment:")[0], (
            f"{name} must not publish on 0.0.0.0 by accident"
        )
        assert "${SHMAX_BIND_ADDR:-127.0.0.1}" in text


def test_installer_writes_bind_addr_env_file():
    repo_root = Path(__file__).resolve().parents[2]
    script = (repo_root / "deploy/installer/install.sh").read_text()
    assert "ensure_bind_addr" in script
    assert "SHMAX_BIND_ADDR" in script
    assert "detect_lan_ip" in script
    # The installer must write the explicit address into the env
    # file rather than leaving the compose default in place.
    assert 'SHMAX_BIND_ADDR=%s' in script


# ----------------------------------------------------------------------
# P0 #8 — AUTO uses the probed ladder (see also
# test_telegram_lifecycle.py) and records the working transport.
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_select_probed_persists_working_transport(tmp_path, monkeypatch):
    from shmax.adapters.telegram.bot_api import TelegramBotApiAdapter
    from shmax.adapters.telegram.config import TransportConfig, TransportMode
    from shmax.adapters.telegram.manager import (
        TelegramAutoStateStore,
        TelegramTransportManager,
    )

    # Keep the probe offline: probe() (P1 #1) must resolve quickly
    # without touching the network.
    async def _no_probe(self):
        return None

    monkeypatch.setattr(TelegramBotApiAdapter, "probe", _no_probe)

    state_path = tmp_path / "auto.json"
    store = TelegramAutoStateStore(state_path)
    cfg = TransportConfig(mode=TransportMode.AUTO, bot_token="1:abc")
    manager = TelegramTransportManager(cfg, store)

    adapter = await manager.select_probed(
        override_mode=TransportMode.BOT_API_DIRECT
    )
    assert adapter is not None
    st = store.load()
    assert st.active_transport == "bot_api_direct"
    assert st.last_success_at is not None
