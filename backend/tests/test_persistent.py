"""Tests for the persistent-data layer."""

from __future__ import annotations


def test_health_reports_all_persistent_subdirs_as_existing(client, data_dir):
    """After startup, PathLayout.ensure() must have created every persistent dir."""
    r = client.get("/health")
    body = r.json()
    existing = set(body["persistent"]["existing"])
    expected = {
        "db",
        "config",
        "secrets",
        "sessions/max",
        "sessions/telegram",
        "state",
        "backups",
        "cache",
    }
    assert expected.issubset(existing), f"missing: {expected - existing}"


def test_persistent_root_is_data_dir(client, data_dir):
    body = client.get("/health").json()
    assert body["persistent"]["root"] == str(data_dir)
