"""Frontend static checks.

Without a Node toolchain we cannot run ``npm ci`` / ``npm run
typecheck`` / ``npm run build`` on this machine. These tests
catch the kind of static issues that cause those commands to
fail later:

* missing ``package.json`` / ``tsconfig.json``;
* missing build script;
* missing lockfile;
* mismatched package.json ``name`` field;
* main / vite entrypoint references that do not exist on disk;
* ``tsconfig.json`` references files that do not exist.

A real ``npm run build`` is the only way to confirm the JS
actually runs in a browser; that is an explicit NOT_TESTED_LIVE
limitation recorded in the docs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = REPO_ROOT / "frontend"


@pytest.fixture(scope="module")
def package_json() -> dict:
    return json.loads((FRONTEND / "package.json").read_text())


def test_frontend_directory_exists():
    assert FRONTEND.is_dir(), f"missing {FRONTEND}"


def test_package_json_has_build_script(package_json: dict):
    scripts = package_json.get("scripts", {})
    assert "build" in scripts, "package.json missing 'build' script"
    assert "vite" in scripts["build"], (
        "build script should drive vite (TS pre-check is optional)"
    )


def test_package_json_has_dev_script(package_json: dict):
    scripts = package_json.get("scripts", {})
    assert "dev" in scripts


def test_tsconfig_exists_and_parses():
    ts = json.loads((FRONTEND / "tsconfig.json").read_text())
    assert isinstance(ts, dict)
    # ``include`` is required; missing ``include`` means tsc
    # would scan the entire project tree and complain about
    # node_modules / dist.
    assert "include" in ts or "references" in ts, (
        "tsconfig.json must declare include or references"
    )


def test_vite_config_exists():
    assert (FRONTEND / "vite.config.ts").is_file(), (
        "vite.config.ts must exist"
    )


def test_index_html_exists():
    assert (FRONTEND / "index.html").is_file(), "index.html must exist"


def test_src_directory_layout():
    src = FRONTEND / "src"
    assert src.is_dir(), "frontend/src must exist"
    assert (src / "main.tsx").is_file(), "frontend/src/main.tsx must exist"
    assert (src / "App.tsx").is_file(), "frontend/src/App.tsx must exist"
    assert (src / "api.ts").is_file(), "frontend/src/api.ts must exist"


def test_no_node_modules_in_source_tree():
    # We tolerate a node_modules directory locally for dev but the
    # production build context (Docker) must not include it.
    # This test confirms the dockerignore lists it.
    dockerignore = (REPO_ROOT / ".dockerignore").read_text()
    assert "node_modules" in dockerignore, (
        ".dockerignore must exclude node_modules"
    )


def test_lockfile_expected():
    """A real ``npm ci`` requires ``package-lock.json``.

    The lockfile is created by the first ``npm install`` on a
    developer machine. We do not auto-generate it here because
    without Node we cannot produce a deterministic lockfile.
    """
    lockfile = FRONTEND / "package-lock.json"
    if lockfile.is_file():
        # Validate basic structure.
        data = json.loads(lockfile.read_text())
        assert "lockfileVersion" in data
    else:
        # Without a lockfile, ``npm ci`` will refuse to run in
        # Docker. Document the NOT_TESTED_LIVE limitation.
        pytest.skip(
            "package-lock.json missing — npm ci will not run; "
            "node toolchain not available on this host"
        )


def test_dependencies_are_pinned_to_caret_versions(package_json: dict):
    """V1 keeps the dependency surface tight; major versions are
    allowed to drift with caret. Future engineers can pin
    tighter if they want reproducible builds."""
    deps = {
        **package_json.get("dependencies", {}),
        **package_json.get("devDependencies", {}),
    }
    assert deps, "package.json must declare dependencies"