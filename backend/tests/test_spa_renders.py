"""Regression test: the SHMAX SPA must render visible UI text
when the owner opens ``/setup?t=...`` (or any other SPA route).

This is the BLOCKER-22 / live-deploy regression: HTTP 200 is
not enough. The page must contain real visible React content
that the operator can interact with.

We use Playwright (system Python on this Mac) to drive a
real Chromium headless shell against the running container
on the VM (or any URL passed via ``SHMAX_SPA_URL``). The
test is skipped when the URL is unreachable so it does not
break a regular ``pytest`` run on a developer machine.

Usage::

    SHMAX_SPA_URL=http://localhost:8787 \
    SHMAX_SETUP_TOKEN=<LOCAL_EPHEMERAL_SETUP_TOKEN> \
    pytest backend/tests/test_spa_renders.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


PLAYWRIGHT_URL = os.environ.get("SHMAX_SPA_URL")
SETUP_TOKEN = os.environ.get("SHMAX_SETUP_TOKEN")


def _has_playwright() -> bool:
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except Exception:
        return False
    return True


pytestmark = pytest.mark.skipif(
    not PLAYWRIGHT_URL,
    reason=(
        "SHMAX_SPA_URL not set; Playwright smoke test is opt-in "
        "so it does not run in a normal CI pytest invocation."
    ),
)


def _new_token_via_backend(base_url: str) -> str | None:
    """Mint a fresh setup token without consuming the owner's."""
    import urllib.request

    secret_path = Path("/var/lib/shmax/state/setup_secret")
    if not secret_path.is_file():
        return None
    secret = secret_path.read_text(encoding="utf-8").strip()
    req = urllib.request.Request(
        f"{base_url}/api/setup/token",
        method="POST",
        headers={"X-Shmax-Setup-Secret": secret},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            return data.get("token")
    except Exception:
        return None


def _resolve_token() -> str:
    if SETUP_TOKEN:
        return SETUP_TOKEN
    token = _new_token_via_backend(PLAYWRIGHT_URL)
    if not token:
        pytest.skip(
            "no SHMAX_SETUP_TOKEN and no /var/lib/shmax/state/setup_secret "
            "reachable from this test host"
        )
    return token


@pytest.mark.asyncio
async def test_setup_page_renders_visible_content():
    from playwright.async_api import async_playwright

    if not _has_playwright():
        pytest.skip("playwright not installed")
    setup_token = _resolve_token()
    url = f"{PLAYWRIGHT_URL.rstrip('/')}/setup?t={setup_token}"
    console_errors: list[str] = []
    network_failures: list[str] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            page = await context.new_page()

            def _on_console(msg):
                if msg.type in ("error", "warning"):
                    console_errors.append(f"{msg.type}: {msg.text}")

            def _on_failed(request):
                network_failures.append(
                    f"{request.method} {request.url} :: {request.failure}"
                )

            page.on("console", _on_console)
            page.on("requestfailed", _on_failed)

            try:
                await page.goto(url, wait_until="networkidle", timeout=15_000)
                await page.wait_for_timeout(750)
                body_text = await page.locator("body").inner_text(timeout=5_000)
                assert body_text.strip(), (
                    "SPA rendered an empty body — React never mounted "
                    "or no Route matched the URL"
                )
                assert any(
                    needle in body_text
                    for needle in (
                        "SHMAX",
                        "Добро пожаловать",
                        "Создать администратора",
                        "🦆",
                    )
                ), f"SPA body has no expected UI text. Got: {body_text!r}"
            finally:
                await context.close()
        finally:
            await browser.close()

    # 401 from /api/auth/session and 403 from /api/setup/admin
    # are EXPECTED during the unauthenticated setup phase — the
    # SPA explicitly probes these endpoints to decide whether
    # to render the wizard or the dashboard. Only fail on
    # JavaScript errors (uncaught exceptions / module load
    # failures / etc).
    fatal = [
        e
        for e in console_errors
        if e.startswith("error:")
        and "401" not in e
        and "403" not in e
    ]
    assert not fatal, f"console errors during page load: {fatal}"
    # Network failures that are merely HTTP 401/403 are not
    # actually failed requests — they are completed
    # transactions the SPA chose to treat as denials.
    fatal_net = [
        n for n in network_failures if "401" not in n and "403" not in n
    ]
    assert not fatal_net, (
        f"network failures during page load: {fatal_net}"
    )


@pytest.mark.asyncio
async def test_navigation_to_admin_step():
    from playwright.async_api import async_playwright

    if not _has_playwright():
        pytest.skip("playwright not installed")
    setup_token = _resolve_token()
    url = f"{PLAYWRIGHT_URL.rstrip('/')}/setup?t={setup_token}"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="networkidle", timeout=15_000)
                await page.wait_for_timeout(500)
                target = page.locator("a[href='/setup/admin']").first
                await target.wait_for(state="visible", timeout=5_000)
                await target.click()
                await page.wait_for_timeout(500)
                await page.wait_for_selector(
                    "input[type='password']", timeout=5_000
                )
                count = await page.locator("input[type='password']").count()
                assert count >= 1, (
                    "admin form did not render a password input"
                )
            finally:
                await context.close()
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_dashboard_route_loads():
    """The root ``/`` route must also render visible content.

    The Dashboard is the operator's first page after the setup
    wizard — it must not be a black screen either."""
    from playwright.async_api import async_playwright

    if not _has_playwright():
        pytest.skip("playwright not installed")
    url = f"{PLAYWRIGHT_URL.rstrip('/')}/"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="networkidle", timeout=15_000)
                await page.wait_for_timeout(500)
                body_text = await page.locator("body").inner_text(timeout=5_000)
                assert body_text.strip(), (
                    "Dashboard rendered an empty body — root route broken"
                )
            finally:
                await context.close()
        finally:
            await browser.close()
