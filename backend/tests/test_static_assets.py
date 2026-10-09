"""Live acceptance A12 — binary static assets must be served as
binary with correct content type (the production build previously
corrupted PNGs through HTMLResponse(read_text) → broken mascot)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shmax.api.app import create_app
from shmax.config import RuntimeMode, Settings


@pytest.fixture()
def client(data_dir, monkeypatch, tmp_path):
    monkeypatch.setenv(
        "SHMAX_TELEGRAM_REACHABILITY_URL", "http://127.0.0.1:59999"
    )
    # Stage a FAKE dist and point the app at it — never touch the
    # real frontend/dist (a regression here once broke the whole
    # e2e suite with a stub index.html).
    staged = tmp_path / "dist"
    staged.mkdir()
    png = staged / "shmax-icon-256.png"
    png.write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806"
        "0000001f15c4890000000d49444154789c6260000000060005"
        "27de41ba0000000049454e44ae426082"
    ))
    (staged / "favicon.png").write_bytes(png.read_bytes())
    (staged / "index.html").write_text("<html>spa</html>")
    import shmax.api.app as app_module

    monkeypatch.setattr(
        app_module, "static_dir_candidates", lambda: [staged]
    )
    settings = Settings(
        host="127.0.0.1", port=0, data_dir=data_dir,
        log_level="WARNING", runtime_mode=RuntimeMode.TEST,
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def test_binary_assets_served_with_content_type(client, tmp_path, monkeypatch):

    for name, ctype in (
        ("shmax-icon-256.png", "image/png"),
        ("favicon.png", "image/png"),
    ):
        resp = client.get(f"/{name}")
        assert resp.status_code == 200, name
        assert resp.headers["content-type"].startswith(ctype), (
            f"{name}: {resp.headers.get('content-type')}"
        )
        assert resp.content[:8] == b"\x89PNG\r\n\x1a\n", (
            f"{name}: binary content must be intact"
        )

    # A path that is NOT a file still falls back to the SPA shell.
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert b"spa" in resp.content


def test_canonical_mascot_exists_in_source():
    """The canonical asset (bottom-right quadrant, unmodified) is
    committed and non-empty."""
    root = Path(__file__).resolve().parents[2] / "frontend" / "public"
    for name in ("shmax-icon.png", "shmax-icon-256.png", "favicon.png"):
        f = root / name
        assert f.is_file() and f.stat().st_size > 500, name
        assert f.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_index_html_references_real_assets():
    html = (
        Path(__file__).resolve().parents[2] / "frontend" / "index.html"
    ).read_text()
    assert "/favicon.png" in html
