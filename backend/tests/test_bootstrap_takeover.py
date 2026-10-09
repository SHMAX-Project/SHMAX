"""Regression tests for setup-token takeover protection."""

from __future__ import annotations

import secrets

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def fresh_app(monkeypatch, tmp_path):
    data = tmp_path / "data"
    monkeypatch.setenv("SHMAX_DATA_DIR", str(data))
    from shmax.api.app import create_app
    from shmax.config import RuntimeMode, Settings

    settings = Settings(data_dir=data, host="127.0.0.1", port=0, log_level="warning",
        runtime_mode=RuntimeMode.TEST,
    )
    app = create_app(settings=settings)
    with TestClient(app) as client:
        yield client


def test_setup_token_lan_request_without_secret_denied(fresh_app):
    """A LAN client that does not provide X-Shmax-Setup-Secret
    MUST NOT be able to mint a setup token."""
    r = fresh_app.post("/api/setup/token")
    assert r.status_code == 403, r.text


def test_setup_token_with_wrong_secret_denied(fresh_app):
    secret_path = fresh_app.app.state.paths.root / "state" / "setup_secret"
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret_path.write_text(secrets.token_urlsafe(32))
    r = fresh_app.post(
        "/api/setup/token",
        headers={"X-Shmax-Setup-Secret": "wrong-secret"},
    )
    assert r.status_code == 403, r.text


def test_setup_token_with_correct_secret_allowed(fresh_app):
    secret_path = fresh_app.app.state.paths.root / "state" / "setup_secret"
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_urlsafe(32)
    secret_path.write_text(secret)
    r = fresh_app.post(
        "/api/setup/token",
        headers={"X-Shmax-Setup-Secret": secret},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert "token" in data and "expires_at" in data


@pytest.mark.asyncio
async def test_setup_token_refused_after_admin_exists(fresh_app):
    """Even with the secret, you cannot mint a setup token
    after the first admin has been created."""
    from shmax.database.models import AdminUser
    from shmax.auth.passwords import PasswordHasher

    secret_path = fresh_app.app.state.paths.root / "state" / "setup_secret"
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_urlsafe(32)
    secret_path.write_text(secret)
    hasher = PasswordHasher()
    db = fresh_app.app.state.db
    async with db.session() as s:
        s.add(
            AdminUser(
                username="owner",
                password_hash=hasher.hash("longenough"),
                role="owner",
            )
        )
        await s.commit()
    r = fresh_app.post(
        "/api/setup/token",
        headers={"X-Shmax-Setup-Secret": secret},
    )
    assert r.status_code == 409, r.text


def test_setup_admin_requires_valid_token(fresh_app):
    """Bootstrap endpoint validates the token even though the
    caller is now authenticated by the LAN."""
    r = fresh_app.post(
        "/api/setup/admin",
        json={"token": "not-a-token", "username": "owner", "password": "longenough"},
    )
    assert r.status_code == 400, r.text
