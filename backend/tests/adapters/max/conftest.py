"""Pytest fixtures shared by MAX adapter tests."""

from __future__ import annotations

import pytest


@pytest.fixture()
def isolated_paths(tmp_path, monkeypatch):
    """Make every environment-driven path resolve under ``tmp_path``."""
    monkeypatch.setenv("SHMAX_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SHMAX_MAX_SESSION_DIR", raising=False)
    monkeypatch.delenv("SHMAX_MAX_PHONE", raising=False)
    return tmp_path
