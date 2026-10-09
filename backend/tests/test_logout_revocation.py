"""Regression tests for admin session DB verification + logout revocation."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def logged_in(monkeypatch, tmp_path):
    """Create a fresh app, an admin, and a logged-in client."""
    from shmax.api.app import create_app
    from shmax.auth.passwords import PasswordHasher
    from shmax.config import RuntimeMode, Settings
    from shmax.database.models import AdminUser

    data = tmp_path / "data"
    monkeypatch.setenv("SHMAX_DATA_DIR", str(data))
    settings = Settings(data_dir=data, host="127.0.0.1", port=0, log_level="warning",
        runtime_mode=RuntimeMode.TEST,
    )
    app = create_app(settings=settings)

    with TestClient(app) as client:

        async def _seed():
            db = app.state.db
            async with db.session() as s:
                s.add(
                    AdminUser(
                        username="owner",
                        password_hash=PasswordHasher().hash("longenough"),
                        role="owner",
                    )
                )
                await s.commit()

        asyncio.run(_seed())

        r = client.post(
            "/api/auth/login",
            json={"username": "owner", "password": "longenough"},
        )
        assert r.status_code == 200, r.text
        yield client, app
        client.post("/api/auth/logout")


def test_admin_endpoint_requires_session(logged_in):
    client, _ = logged_in
    client.cookies.clear()
    r = client.get("/api/status")
    assert r.status_code == 401, r.text


def test_logout_revokes_session(logged_in):
    client, _ = logged_in
    r = client.get("/api/status")
    assert r.status_code == 200, r.text
    r = client.post("/api/auth/logout")
    assert r.status_code == 200, r.text
    r = client.get("/api/status")
    assert r.status_code == 401, r.text


def test_disabled_user_blocked(logged_in):
    client, app = logged_in
    from shmax.database.models import AdminUser
    from sqlalchemy import select

    async def _disable():
        async with app.state.db.session() as s:
            row = (await s.execute(select(AdminUser))).scalars().first()
            row.disabled = True
            await s.commit()

    asyncio.run(_disable())
    r = client.get("/api/status")
    assert r.status_code == 401, r.text
