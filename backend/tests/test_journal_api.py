import pytest
from tests.test_xray_endpoints import client


def test_journal_authorization_and_bounds(client):
    response = client.get("/api/journal")
    assert response.status_code == 200
    assert client.get("/api/journal?limit=10000").status_code == 400
    assert client.get("/api/journal?severity=bogus").status_code == 400
    assert client.get("/api/journal?before=invalid").status_code == 400
    assert client.get("/api/journal?since=2026-01-01").status_code == 400
    client.cookies.clear()
    assert client.get("/api/journal").status_code == 401


def test_real_backend_start_event_persisted(client):
    rt = client.app.state.runtime
    client.portal.call(rt.journal.flush)
    rows = client.get("/api/journal?category=SYSTEM").json()["events"]
    assert any(r["event_type"] == "system.started" for r in rows)
    assert all("timestamp" in r and "description" in r for r in rows)
