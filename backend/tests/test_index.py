"""Tests for the landing page."""

from __future__ import annotations


def test_index_renders_shmax_ready(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    body = r.text
    assert "SHMAX" in body
    assert "Шмакс жив" in body
    assert "SHMAX READY" in body
