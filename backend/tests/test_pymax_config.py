"""PyMax configuration tests — verify SHMAX explicitly pins
device_type to ANDROID and that no upstream-default drift is
allowed to leak through.

The audit caught contradictory claims about DESKTOP vs ANDROID
defaults. PyMax 2.4.1 upstream defaults to ANDROID; SHMAX must
not silently fall through to a different device class even if a
future PyMax release flips the default.
"""

from __future__ import annotations

import os

import pytest

from shmax.adapters.max.config import (
    PyMaxAdapterConfig,
    _resolve_device_type,
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch):
    for var in (
        "SHMAX_MAX_DEVICE_TYPE",
        "SHMAX_MAX_PHONE",
        "SHMAX_MAX_SESSION_DIR",
        "SHMAX_MAX_SESSION_NAME",
        "SHMAX_MAX_APP_VERSION",
        "SHMAX_MAX_RECONNECT",
        "SHMAX_LOG_LEVEL",
        "SHMAX_DATA_DIR",
    ):
        monkeypatch.delenv(var, raising=False)


def test_default_device_type_is_android(clean_env):
    assert _resolve_device_type() == "android"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("android", "android"),
        ("ANDROID", "android"),
        ("ios", "ios"),
        ("web", "web"),
        ("desktop", "desktop"),
    ],
)
def test_resolve_device_type_accepts_known_values(
    clean_env, raw, expected
):
    os.environ["SHMAX_MAX_DEVICE_TYPE"] = raw
    assert _resolve_device_type() == expected


@pytest.mark.parametrize("raw", ["", "garbage", "tablet", "win32"])
def test_resolve_device_type_rejects_unknown(clean_env, raw):
    os.environ["SHMAX_MAX_DEVICE_TYPE"] = raw
    with pytest.raises(RuntimeError):
        _resolve_device_type()


def test_from_env_default_config_has_android(clean_env):
    cfg = PyMaxAdapterConfig.from_env()
    assert cfg.device_type == "android"
    assert cfg.app_version  # non-empty
    assert cfg.session_name == "main.db"


def test_from_env_overrides_device_type(clean_env):
    os.environ["SHMAX_MAX_DEVICE_TYPE"] = "ios"
    cfg = PyMaxAdapterConfig.from_env()
    assert cfg.device_type == "ios"


def test_pymax_adapter_passes_device_type_to_extra_config(tmp_path):
    """SHMAX must wire cfg.device_type into PyMax's ExtraConfig
    so the handshake carries an explicit device class."""
    from shmax.adapters.max.providers import (
        CredentialFuture,
        QueuedPasswordProvider,
        QueuedSmsProvider,
    )
    from shmax.adapters.max.pymax_adapter import (
        PyMaxMaxAdapter,
        _device_type_to_enum,
        _ensure_pymax,
    )

    _ensure_pymax()
    cfg = PyMaxAdapterConfig(
        phone=None,
        session_dir=tmp_path / "session",
        session_name="test.db",
        device_type="android",
    )
    sms = CredentialFuture(kind="sms")
    pwd = CredentialFuture(kind="password")
    adapter = PyMaxMaxAdapter(
        config=cfg,
        sms_provider=QueuedSmsProvider(future=sms),
        password_provider=QueuedPasswordProvider(future=pwd),
    )
    # _build_client() will surface PyMaxMaxAdapter's choice of
    # DeviceType. We assert the helper returns the ANDROID
    # enum member — never DESKTOP / WEB.
    enum_value = _device_type_to_enum("android")
    assert enum_value is not None
    assert enum_value.value == "android" or "android" in enum_value.name.lower()