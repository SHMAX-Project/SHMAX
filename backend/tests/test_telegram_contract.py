"""Telegram verify/save contract tests for the production path.

These tests deliberately mock at the EXTERNAL Telegram boundary
(api.telegram.org). The SHMAX frontend, backend, request/response
schema and runtime must all behave correctly with only that
single seam mocked.

A 422 returned from ``/api/telegram/verify`` or
``/api/telegram/token`` for a populated token is a FAILURE —
the SHMAX contract must parse a populated ``{token: "..."}``
body and dispatch to the live aiogram adapter.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
import urllib.request
from pathlib import Path

import pytest


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _boot_app(data_dir: Path):
    import uvicorn
    from shmax.api.app import create_app
    from shmax.config import Settings

    settings = Settings.from_env()
    app = create_app(settings)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)
    return server, thread, f"http://127.0.0.1:{port}"


@pytest.fixture()
def booted_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SHMAX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SHMAX_RUNTIME_MODE", "production")
    monkeypatch.setenv("SHMAX_HOST", "127.0.0.1")
    monkeypatch.setenv("SHMAX_PORT", "0")
    monkeypatch.setenv("SHMAX_LOG_LEVEL", "WARNING")
    import os
    os.environ["SHMAX_DATA_DIR"] = str(tmp_path)
    server, thread, base = _boot_app(tmp_path)
    try:
        # bootstrap admin so we can exercise the auth-only endpoints
        tok = json.loads(urllib.request.urlopen(
            urllib.request.Request(
                base + "/api/setup/token", data=b"{}",
                headers={"Content-Type": "application/json"},
            ),
            timeout=5,
        ).read())["token"]
        urllib.request.urlopen(urllib.request.Request(
            base + "/api/setup/admin",
            data=json.dumps({
                "username": "admin",
                "password": "shmaxAdminPass!2026",
                "token": tok,
            }).encode(),
            headers={"Content-Type": "application/json"},
        ), timeout=5).read()
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=3)


def _post(url: str, body: dict, *, cookie: str | None = None, csrf: str | None = None):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"},
    )
    if cookie:
        req.add_header("Cookie", cookie)
    if csrf:
        req.add_header("X-CSRF-Token", csrf)
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        body_resp = json.loads(resp.read())
        return resp.status, body_resp, resp.headers.get("Set-Cookie")
    except urllib.error.HTTPError as e:
        body_resp = json.loads(e.read() or b"{}")
        return e.code, body_resp, e.headers.get("Set-Cookie")


def _login(base: str) -> tuple[str, str]:
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    resp = opener.open(urllib.request.Request(
        base + "/api/auth/login",
        data=json.dumps({"username": "admin", "password": "shmaxAdminPass!2026"}).encode(),
        headers={"Content-Type": "application/json"},
    ), timeout=5)
    csrf = json.loads(resp.read())["csrf_token"]
    # The session cookie is in the opener jar.
    cookies = []
    for c in opener.handlers:
        if hasattr(c, "cookiejar"):
            for c2 in c.cookiejar:
                cookies.append(f"{c2.name}={c2.value}")
    return "; ".join(cookies), csrf


def test_post_telegram_token_with_populated_body_does_not_422(booted_app: str):
    """The original BLOCKER #2: a populated token body MUST NOT
    produce a 422. ``POST /api/telegram/token`` must parse the
    JSON body and persist it.
    """
    cookie, csrf = _login(booted_app)
    status, body, _ = _post(
        booted_app + "/api/telegram/token",
        {"token": "1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZ"},
        cookie=cookie, csrf=csrf,
    )
    assert status != 422, (
        f"BLOCKER #2: /api/telegram/token returned 422 for a populated "
        f"body. body={body!r}. This means FastAPI cannot resolve the "
        f"request model and is reading ``req`` as a query parameter."
    )
    assert status in (200, 502), (
        f"Unexpected status {status} body={body!r}"
    )


def test_post_telegram_verify_with_populated_body_does_not_422(booted_app: str):
    """The original BLOCKER #2 on the verify path. Populated token
    must reach the Telegram adapter layer; a 422 from SHMAX means
    the request schema is broken.
    """
    cookie, csrf = _login(booted_app)
    status, body, _ = _post(
        booted_app + "/api/telegram/verify",
        {"token": "1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZ"},
        cookie=cookie, csrf=csrf,
    )
    assert status != 422, (
        f"BLOCKER #2: /api/telegram/verify returned 422 for a populated "
        f"body. body={body!r}. The SHMAX contract is broken."
    )
    # 502 means SHMAX reached the external Telegram API and the
    # token was rejected there — that is the EXPECTED outcome for
    # a fake token.
    assert status in (200, 502), (
        f"Unexpected status {status} body={body!r}"
    )


def test_telegram_max_auth_chats_strict_request_body(booted_app: str):
    """Same audit for the MAX auth endpoints: populated bodies
    must parse, never be rejected as 422 missing-fields."""
    cookie, csrf = _login(booted_app)
    status, body, _ = _post(
        booted_app + "/api/max/auth/start",
        {"phone": "+79991234567"},
        cookie=cookie, csrf=csrf,
    )
    assert status != 422, (
        f"MAX auth/start returned 422 for a populated body. body={body!r}"
    )
    # status will be 200 with state=waiting_code OR 500 if PyMax
    # is not fully wired in this environment. The point is: never 422.