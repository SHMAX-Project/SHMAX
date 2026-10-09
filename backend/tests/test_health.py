"""Tests for /health."""

from __future__ import annotations


def test_health_returns_200(client):
    r = client.get("/health")
    assert r.status_code == 200


def test_health_status_ok(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"


def test_health_service_name(client):
    body = client.get("/health").json()
    assert body["service"] == "shmax"


def test_health_version_present(client):
    body = client.get("/health").json()
    assert isinstance(body.get("version"), str)
    assert body["version"]
