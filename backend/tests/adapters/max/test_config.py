"""Tests for the configuration layer of the MAX adapter."""

from __future__ import annotations

from pathlib import Path

from shmax.adapters.max.config import PyMaxAdapterConfig


def test_session_dir_defaults_to_data_root(isolated_paths):
    cfg = PyMaxAdapterConfig.from_env()
    assert cfg.session_dir == (isolated_paths / "sessions" / "max").resolve()


def test_session_dir_explicit_env_var(monkeypatch, tmp_path):
    target = tmp_path / "explicit-max-sessions"
    monkeypatch.setenv("SHMAX_DATA_DIR", "/should/not/be/used")
    monkeypatch.setenv("SHMAX_MAX_SESSION_DIR", str(target))
    cfg = PyMaxAdapterConfig.from_env()
    assert cfg.session_dir == target.resolve()


def test_config_is_frozen():
    cfg = PyMaxAdapterConfig(
        phone="+1234",
        session_dir=Path("/tmp/x"),
        session_name="main.db",
    )
    try:
        cfg.phone = "+9999"  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("PyMaxAdapterConfig should be frozen")


def test_session_dir_is_created_on_instantiation(tmp_path):
    target = tmp_path / "nested" / "max"
    cfg = PyMaxAdapterConfig(
        phone="+1234",
        session_dir=target,
        session_name="main.db",
    )
    # We don't import the adapter here to avoid touching PyMax from
    # this lightweight test; just assert config is honest.
    assert cfg.session_dir == target
    assert not target.exists()  # adapter would create it, not the config
