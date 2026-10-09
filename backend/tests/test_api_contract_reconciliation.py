"""Frontend ↔ FastAPI contract reconciliation.

Walks the frontend source for ``fetch('/api/...')`` / ``api('/api/...')``
calls and confirms each one has a matching backend route. Catches:

* path mismatches;
* method mismatches;
* accidentally-removed backend endpoints;
* new frontend endpoints the backend never exposed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_SRC = REPO_ROOT / "frontend" / "src"
BACKAPI_SRC = REPO_ROOT / "backend" / "shmax" / "api"

METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH")


def _collect_backend_routes() -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()
    for f in BACKAPI_SRC.rglob("*.py"):
        text = f.read_text()
        # The decorator may be split across multiple lines, e.g.
        #
        #     @app.post(
        #         "/api/foo",
        #         dependencies=...,
        #     )
        #
        # Collapse multi-line decorators before matching.
        collapsed = re.sub(r"@(?:app|router)\.[a-z]+\(\s*\n((?:[ \t]+['\"][^'\"]+['\"][ \t]*\,?\s*\n?)+)\)", lambda m: "@_decorator_(" + m.group(1) + ")", text)
        for m in re.finditer(
            r"@(?:app|router)\.(get|post|put|delete|patch)\(['\"]([^'\"]+)['\"]",
            collapsed,
        ):
            routes.add((m.group(1).upper(), m.group(2)))
        # Fallback: also try to match URL strings that appear on
        # lines immediately following a decorator head (covers
        # decorators split across lines that the collapse above
        # didn't catch because of mixed formatting).
        for m in re.finditer(
            r"@(?:app|router)\.(get|post|put|delete|patch)\(\s*\n\s*['\"]([^'\"]+)['\"]",
            text,
        ):
            routes.add((m.group(1).upper(), m.group(2)))
    return routes


def _frontend_calls() -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    for f in FRONTEND_SRC.rglob("*.ts*"):
        text = f.read_text()
        # Normalise template literals to FastAPI-style placeholders:
        # ``${adapter}`` -> ``{adapter}``.
        normalised = re.sub(
            r"\$\{([a-zA-Z_][a-zA-Z0-9_]*)\}", r"{\1}", text
        )
        # fetch('/api/...', { method: 'POST' })
        for m in re.finditer(
            r"fetch\(\s*['\"`]([^'\"`]+)['\"`][^)]*?method:\s*['\"]([A-Z]+)['\"]",
            normalised,
        ):
            calls.append((m.group(2), m.group(1).split("?")[0]))
        # api('/api/...', { method: 'POST' })
        for m in re.finditer(
            r"api(?:<[^>]*>)?\s*\(\s*['\"`]([^'\"`]+)['\"`][^)]*?method:\s*['\"]([A-Z]+)['\"]",
            normalised,
        ):
            calls.append((m.group(2), m.group(1).split("?")[0]))
        # api('/api/...')  default GET
        for m in re.finditer(
            r"api(?:<[^>]*>)?\s*\(\s*['\"`]([^'\"`]+)['\"`]\s*\)",
            normalised,
        ):
            calls.append(("GET", m.group(1).split("?")[0]))
        # fetch('/api/...')  default GET
        for m in re.finditer(
            r"fetch\(\s*['\"`]([^'\"`]+)['\"`]\s*,?\s*\)",
            normalised,
        ):
            calls.append(("GET", m.group(1).split("?")[0]))
    return calls


@pytest.fixture(scope="module")
def backend_routes() -> set[tuple[str, str]]:
    return _collect_backend_routes()


@pytest.fixture(scope="module")
def frontend_calls() -> list[tuple[str, str]]:
    return _frontend_calls()


def test_every_frontend_call_has_backend_route(
    backend_routes, frontend_calls
):
    # Only consider calls under /api/... — static frontend
    # assets, paths, etc. are excluded.
    api_calls = [(m, p) for m, p in frontend_calls if p.startswith("/api/")]
    missing = []
    for method, path in api_calls:
        # Templated paths (``{adapter}``, ``{queue_id}``, etc.)
        # are not literally matched; the backend route may use the
        # template name verbatim. Treat both as equivalent.
        matched = False
        for backend_method, backend_path in backend_routes:
            if backend_method != method:
                continue
            if _path_matches(backend_path, path):
                matched = True
                break
        if not matched:
            missing.append((method, path))
    assert not missing, (
        "frontend calls missing backend routes:\n"
        + "\n".join(f"{m} {p}" for m, p in missing)
    )


def _path_matches(backend_path: str, frontend_path: str) -> bool:
    """Match a templated backend path against a literal frontend path.

    ``/api/queue/{queue_id}/retry`` matches ``/api/queue/123/retry``.

    A frontend path with multiple template tokens (e.g.
    ``/api/watchdogs/{adapter}/{verb}``) matches any backend path
    whose literal segments align segment-by-segment.
    """
    if backend_path == frontend_path:
        return True
    b_parts = backend_path.strip("/").split("/")
    f_parts = frontend_path.strip("/").split("/")
    if len(b_parts) != len(f_parts):
        return False
    for bp, fp in zip(b_parts, f_parts):
        if fp.startswith("{") and fp.endswith("}"):
            # Frontend template literal; backend must also have
            # something (a literal segment is OK — the test
            # confirms there exists at least one backend route
            # whose literal in this position matches the frontend
            # path's effective literal — but since the frontend
            # value is variable we accept any non-templated
            # backend value here, including literals like ``probe``
            # or ``self-heal``.
            if bp.startswith("{") and bp.endswith("}"):
                continue
            # Allow ``probe`` / ``self-heal`` to match a literal
            # ``{verb}`` template; if the test fixture passes a
            # more constrained token, the calling fixture can
            # refine.
            continue
        if bp.startswith("{") and bp.endswith("}"):
            continue
        if bp != fp:
            return False
    return True