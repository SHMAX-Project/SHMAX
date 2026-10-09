"""Offline tests for the PyMaxMaxAdapter.

No real MAX connection is established here. We exercise:

* configuration of the persistent session directory;
* health-state contract (no secrets leak);
* translation of fake PyMax-like objects into our domain types.

Real MAX integration tests are intentionally NOT included in the
default ``pytest`` run — they require a real account, an SMS code
and network access to MAX, and live under
``tests/integration/``.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from shmax.adapters.max.base import MaxChat, MaxEvent, MaxProfile
from shmax.adapters.max.config import PyMaxAdapterConfig
from shmax.adapters.max.errors import MaxSessionError, MaxUnsupportedError
from shmax.adapters.max.providers import (
    InteractivePasswordProvider,
    InteractiveSmsProvider,
)
from shmax.adapters.max.pymax_adapter import PyMaxMaxAdapter


# ---------------------------------------------------------------------
# Construction / configuration
# ---------------------------------------------------------------------
def test_construction_creates_session_dir(isolated_paths):
    cfg = PyMaxAdapterConfig(
        phone="+1234",
        session_dir=isolated_paths / "sessions" / "max",
    )
    PyMaxMaxAdapter(config=cfg)
    assert (isolated_paths / "sessions" / "max").is_dir()


def test_session_path_points_under_data_root(isolated_paths):
    cfg = PyMaxAdapterConfig(
        phone="+1234",
        session_dir=isolated_paths / "sessions" / "max",
    )
    adapter = PyMaxMaxAdapter(config=cfg)
    expected = (isolated_paths / "sessions" / "max" / "main.db").resolve()
    assert adapter.session_path == expected


def test_repr_does_not_include_phone(isolated_paths):
    cfg = PyMaxAdapterConfig(
        phone="+79991234567",
        session_dir=isolated_paths / "sessions" / "max",
    )
    adapter = PyMaxMaxAdapter(config=cfg)
    assert "+79991234567" not in repr(adapter)


# ---------------------------------------------------------------------
# State / health
# ---------------------------------------------------------------------
def test_health_before_connect_is_disconnected(isolated_paths):
    cfg = PyMaxAdapterConfig(
        phone="+1234",
        session_dir=isolated_paths / "sessions" / "max",
    )
    adapter = PyMaxMaxAdapter(config=cfg)
    h = asyncio.run(adapter.health())
    assert h["connected"] is False
    assert h["authenticated"] is False
    assert h["account_id"] is None
    assert h["session_available"] is False


def test_health_after_connect_attempt_without_session_reports_unavailable(
    isolated_paths,
):
    cfg = PyMaxAdapterConfig(
        phone=None,  # restore mode — but no session file yet
        session_dir=isolated_paths / "sessions" / "max",
    )
    adapter = PyMaxMaxAdapter(config=cfg)
    h = asyncio.run(adapter.health())
    assert h["session_available"] is False
    assert h["account_id"] is None


# ---------------------------------------------------------------------
# Domain-object mapping (no PyMax runtime needed)
# ---------------------------------------------------------------------
def test_from_pymax_message_translates_minimum_fields():
    class FakeMessage:
        id = 42
        chat_id = 999
        sender = 7
        text = "hello"

    ev = PyMaxMaxAdapter._from_pymax_message(FakeMessage())
    assert isinstance(ev, MaxEvent)
    assert ev.event_id == "42"
    assert ev.chat_id == "999"
    assert ev.sender_id == "7"
    assert ev.text == "hello"


def test_from_pymax_message_handles_missing_optional_fields():
    class FakeMessage:
        id = 1
        chat_id = None
        sender = None
        text = ""

    ev = PyMaxMaxAdapter._from_pymax_message(FakeMessage())
    assert ev.chat_id == ""
    assert ev.sender_id == ""
    assert ev.text == ""


def test_from_pymax_chat_translates_enum_value():
    from pymax.types.domain.enums import ChatType

    class FakeChat:
        id = 12345
        type = ChatType.DIALOG
        title = None

    ch = PyMaxMaxAdapter._from_pymax_chat(FakeChat())
    assert isinstance(ch, MaxChat)
    assert ch.chat_id == "12345"
    assert ch.kind == "dialog"
    assert ch.title is None


def test_from_pymax_profile_handles_unnamed_account():
    class FakeName:
        first_name = ""
        last_name = ""

    class FakeContact:
        id = 555
        names = [FakeName()]
        link = "https://max.ru/username"

    class FakeMe:
        contact = FakeContact()

    class FakeClient:
        me = FakeMe()

    profile = PyMaxMaxAdapter._from_pymax_profile(profile=FakeMe(), client=FakeClient())
    assert isinstance(profile, MaxProfile)
    assert profile.user_id == "555"
    assert profile.display_name == "(unnamed)"
    assert profile.handle == "https://max.ru/username"


# ---------------------------------------------------------------------
# Error path: API must reject sends before connect()
# ---------------------------------------------------------------------
def test_send_text_before_connect_raises_session_error(isolated_paths):
    cfg = PyMaxAdapterConfig(
        phone="+1234",
        session_dir=isolated_paths / "sessions" / "max",
    )
    adapter = PyMaxMaxAdapter(config=cfg)
    with pytest.raises(MaxSessionError):
        asyncio.run(adapter.send_text("1", "hello"))


def test_send_reply_before_connect_raises_session_error(isolated_paths):
    cfg = PyMaxAdapterConfig(
        phone="+1234",
        session_dir=isolated_paths / "sessions" / "max",
    )
    adapter = PyMaxMaxAdapter(config=cfg)
    from shmax.domain.models import ReplyRef
    with pytest.raises(MaxSessionError):
        asyncio.run(
            adapter.send_reply(
                "1", ReplyRef(transport="max", chat_external_id="1", message_external_id="2"), "hello"
            )
        )


def test_send_media_before_connect_raises_session_error(isolated_paths, tmp_path):
    cfg = PyMaxAdapterConfig(
        phone="+1234",
        session_dir=isolated_paths / "sessions" / "max",
    )
    adapter = PyMaxMaxAdapter(config=cfg)
    real_file = tmp_path / "real.jpg"
    real_file.write_bytes(b"\x00")
    with pytest.raises(MaxSessionError):
        asyncio.run(adapter.send_media("1", f"photo:{real_file}"))


def test_send_media_with_unknown_kind_raises(isolated_paths, tmp_path):
    cfg = PyMaxAdapterConfig(
        phone="+1234",
        session_dir=isolated_paths / "sessions" / "max",
    )
    adapter = PyMaxMaxAdapter(config=cfg)
    # ``MaxUnsupportedError`` is raised by ``send_media`` before any
    # connect() happens, since the kind is parsed up front.
    with pytest.raises(MaxUnsupportedError):
        asyncio.run(adapter.send_media("1", f"weird:{tmp_path}/x"))


# ---------------------------------------------------------------------
# Core must NOT import PyMax directly.
# ---------------------------------------------------------------------
def test_core_module_does_not_import_pymax():
    """The whole point of the adapter layer."""
    from shmax import core

    src = inspect.getsource(core)
    # The core package (paths, diagnostics, lifecycle) must not
    # touch PyMax. Adapter code lives elsewhere.
    assert "import pymax" not in src
    assert "from pymax" not in src


def test_adapter_module_imports_max_error_base():
    """Regression: ``PyMaxMaxAdapter`` previously referenced the
    ``MaxError`` base class without importing it, so a PyMax
    ``RuntimeError`` raised during ``connect()`` was swallowed by
    a ``NameError`` instead of being translated. The import must
    be present so ``isinstance(translated, MaxError)`` resolves.
    """
    from shmax.adapters.max import pymax_adapter

    assert hasattr(pymax_adapter, "MaxError")
    # And it's the real base class from our errors module.
    from shmax.adapters.max.errors import MaxAuthError, MaxError

    assert pymax_adapter.MaxError is MaxError
    assert issubclass(MaxAuthError, MaxError)
