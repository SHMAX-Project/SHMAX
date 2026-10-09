"""Dependency lock + armv7 build deps tests.

These tests are static — they confirm the dependency surface is
pinned and that the Dockerfile builder stage has the toolchain
needed to compile C-extension wheels for armv7.
"""

from __future__ import annotations

import importlib.metadata
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "backend" / "pyproject.toml"
LOCKFILE = REPO_ROOT / "backend" / "requirements.lock"
DOCKERFILE = REPO_ROOT / "Dockerfile"


def test_python_lockfile_exists():
    assert LOCKFILE.is_file(), (
        "requirements.lock is missing; backend Python dependencies are "
        "not pinned for Docker builds"
    )


def test_python_lockfile_lists_all_runtime_packages():
    """Every package in the venv that the SHMAX source imports
    must be present in requirements.lock.

    The self-package (``shmax``) is excluded: it is installed
    via ``pip install .`` from the build context (see
    Dockerfile backend-builder stage) so it MUST NOT appear in
    the lockfile — that would make the lock non-installable in
    environments where the local ``shmax`` source is not
    present (CI, Docker build contexts, etc.).

    ``pip``, ``setuptools`` and ``wheel`` are build-time
    tooling, not runtime dependencies. ``playwright`` and
    ``pyee`` are dev-only test runners. Both groups are
    intentionally absent from requirements.lock.
    """
    text = LOCKFILE.read_text()
    excluded = {
        "shmax",  # self
        "pip", "setuptools", "wheel",  # build tooling
        "playwright", "pyee",  # dev-only browser test
    }
    for dist in importlib.metadata.distributions():
        name = dist.metadata["Name"]
        if not name:
            continue
        if name.lower() in excluded:
            continue
        assert f"{name}==" in text or f"{name.replace('_', '-')}==" in text, (
            f"runtime package missing from requirements.lock: {name}"
        )


def test_python_lockfile_pins_exact_versions():
    """Each entry must use ``==`` (no ``>=`` / ``~=``)."""
    for line in LOCKFILE.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        assert "==" in line and ">" not in line and "<" not in line, (
            f"lockfile line not pinned: {line}"
        )


def test_pyproject_minimum_versions_match_lockfile():
    """Whenever pyproject declares ``>=`` for a runtime dep, the
    lockfile must pin a version that satisfies the constraint."""
    import re

    if not PYPROJECT.is_file():
        pytest.skip("pyproject.toml missing")
    py = PYPROJECT.read_text()
    deps: dict[str, str] = {}
    in_deps = False
    for line in py.splitlines():
        if line.strip().startswith("[") and line.strip() == "[project]":
            in_deps = True
            continue
        if line.strip().startswith("[") and in_deps:
            in_deps = False
        if not in_deps:
            continue
        m = re.match(r'\s*"([A-Za-z0-9_.-]+)\s*([><=~^]+)\s*([^"]+)"', line)
        if m:
            deps[m.group(1).lower()] = m.group(3).strip()
    lock = LOCKFILE.read_text()
    for name, min_version in deps.items():
        m = re.search(rf"^{re.escape(name)}==(\S+)$", lock, re.MULTILINE | re.IGNORECASE)
        assert m, f"pyproject dep {name} missing from lockfile"
        # Very light version comparison.
        pin = m.group(1)
        # If the spec is ">=X", the pinned version must start with X
        # or be newer; we only check the major prefix here.
        pin_major = pin.split(".")[0]
        spec_major = min_version.split(".")[0]
        if spec_major.isdigit() and pin_major.isdigit():
            assert int(pin_major) >= int(spec_major), (
                f"pyproject {name}>={min_version} but lockfile pins {pin}"
            )


def test_dockerfile_builder_has_native_toolchain():
    """armv7 often needs to compile wheels from source. The
    builder stage must carry build-essential, libssl-dev,
    libffi-dev. Runtime stage must NOT."""
    text = DOCKERFILE.read_text()
    builder_section = text.split("# --- Runtime")[0]
    assert "build-essential" in builder_section, (
        "Dockerfile builder missing build-essential"
    )
    assert "libssl-dev" in builder_section, (
        "Dockerfile builder missing libssl-dev"
    )
    assert "libffi-dev" in builder_section, (
        "Dockerfile builder missing libffi-dev"
    )
    runtime_section = text.split("# --- Runtime")[1]
    assert "build-essential" not in runtime_section, (
        "Dockerfile runtime must NOT carry build-essential"
    )


def test_dockerfile_installs_from_lockfile():
    text = DOCKERFILE.read_text()
    assert "requirements.lock" in text, (
        "Dockerfile must pip install from requirements.lock"
    )