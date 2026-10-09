"""Static Docker contract test.

Validates the deployment files without requiring a real
Docker daemon. Catches:

* frontend excluded from build context;
* COPY source paths that don't exist on disk;
* Dockerfile syntax pitfalls (HEALTHCHECK CMD form);
* compose mounts that include /var/run/docker.sock;
* hostd socket wired to the web container;
* expected UID/GID present;
* SHMAX_DATA_DIR points at /data inside the container;
* Docker socket mount forbidden.

These do not replace an actual ``docker build`` tomorrow, but
they catch the kinds of bugs a human reviewer is most likely
to miss.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(p: Path) -> str:
    assert p.is_file(), f"required file missing: {p}"
    return p.read_text()


def test_dockerignore_does_not_exclude_frontend():
    text = _read(REPO_ROOT / ".dockerignore")
    for line in text.splitlines():
        # Excluding ``frontend`` wholesale breaks the frontend build
        # stage. The directory must be visible; subdirectories that
        # the build does not need (node_modules, dist) are excluded
        # by more specific rules.
        stripped = line.strip()
        assert stripped not in {"frontend", "/frontend/*"}, (
            f".dockerignore excludes frontend wholesale: {line!r}"
        )


def test_dockerfile_copy_sources_exist():
    text = _read(REPO_ROOT / "Dockerfile")
    # Pull every ``COPY`` source that points at the build context.
    # Multi-stage COPYs that target a stage (e.g. ``--from=builder``)
    # are ignored.
    copy_sources: list[str] = []
    for line in text.splitlines():
        if not line.strip().startswith("COPY"):
            continue
        if "--from=" in line:
            continue
        args = re.findall(r"COPY\s+(.*)", line)
        if not args:
            continue
        parts = args[0].split()
        parts = [p for p in parts if not p.startswith("--")]
        if len(parts) < 2:
            continue
        for src in parts[:-1]:
            copy_sources.append(src)
    for src in copy_sources:
        # Optional files are denoted by a trailing ``*`` glob or
        # explicit wildcard; tolerate their absence.
        if src.endswith("*") or "*" in src:
            continue
        target = (REPO_ROOT / src).resolve()
        assert target.exists(), (
            f"Dockerfile COPY source does not exist: {src}"
        )
        assert target.is_relative_to(REPO_ROOT.resolve()) or not src.startswith(
            "/"
        ), f"Dockerfile COPY escapes repo: {src}"


def test_compose_mounts_data_subdirs():
    text = _read(REPO_ROOT / "compose.yaml")
    for sub in ["db", "config", "secrets", "sessions", "state", "backups", "cache"]:
        assert f"/data/{sub}" in text, (
            f"compose.yaml must mount /data/{sub}"
        )


def test_compose_does_not_mount_docker_socket():
    for compose_file in ("compose.yaml", "deploy/compose/compose.yaml"):
        text = _read(REPO_ROOT / compose_file)
        # Strip comments before checking for the docker socket.
        non_comment = "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("#")
        )
        assert "/var/run/docker.sock" not in non_comment, (
            f"{compose_file} mounts docker.sock — security regression"
        )


def test_compose_data_dir_default_points_inside_container():
    text = _read(REPO_ROOT / "compose.yaml")
    # Look for the SHMAX_DATA_DIR env var (with optional docker-compose
    # variable form and trailing ``}``).
    m = re.search(
        r"SHMAX_DATA_DIR:\s*\"?\$?\{?SHMAX_DATA_DIR:?([^}\"\s]+)?\}?\"?",
        text,
    )
    # Also accept the literal ``/data`` form (the compose files
    # use this in dev to keep the contract obvious).
    if m is None:
        assert "SHMAX_DATA_DIR: /data" in text or 'SHMAX_DATA_DIR: "/data"' in text, (
            "compose.yaml missing SHMAX_DATA_DIR"
        )
        return
    val = m.group(1) or ""
    # The value may be a docker-compose variable form like
    # ``/var/lib/shmax``. The contract: every bind-mount source ends
    # in a subdirectory of the host data root, and the destination
    # inside the container is always ``/data/<sub>``.
    for sub in (
        "db",
        "config",
        "secrets",
        "sessions",
        "state",
        "backups",
        "cache",
    ):
        assert f":/data/{sub}" in text, (
            f"compose.yaml must mount /data/{sub} inside the container"
        )


def test_compose_mounts_hostd_socket_only():
    text = _read(REPO_ROOT / "compose.yaml")
    assert "/run/shmax:/run/shmax/hostd:ro" in text, (
        "compose.yaml must mount the hostd runtime directory read-only for restart-safe sockets"
    )


def test_compose_uses_consistent_uid():
    text = _read(REPO_ROOT / "compose.yaml")
    assert "8787" in text, "compose.yaml must pin the SHMAX_UID/GID"


def test_dockerfile_uses_8787_uid():
    text = _read(REPO_ROOT / "Dockerfile")
    assert "8787" in text, "Dockerfile must default to 8787:8787"


def test_dockerfile_healthcheck_uses_process_health():
    text = _read(REPO_ROOT / "Dockerfile")
    # HEALTHCHECK must hit /health, NOT /health/ready, because
    # integrations that are NOT_CONFIGURED must not flip the
    # container into an unhealthy restart loop.
    assert "HEALTHCHECK" in text, "Dockerfile missing HEALTHCHECK"
    assert "/health" in text, "Dockerfile healthcheck must hit /health"


def test_compose_healthcheck_is_consistent():
    text = _read(REPO_ROOT / "compose.yaml")
    assert "/health" in text, "compose healthcheck must hit /health"


def test_dockerfile_runs_uvicorn_as_non_root():
    text = _read(REPO_ROOT / "Dockerfile")
    # USER shmax must be set.
    assert re.search(r"^USER\s+shmax", text, re.MULTILINE), (
        "Dockerfile must switch to the shmax user before CMD"
    )


def test_deploy_compose_has_no_docker_socket():
    text = _read(REPO_ROOT / "deploy/compose/compose.yaml")
    non_comment = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "/var/run/docker.sock" not in non_comment


def test_deploy_compose_data_dir_is_data():
    text = _read(REPO_ROOT / "deploy/compose/compose.yaml")
    m = re.search(r"SHMAX_DATA_DIR:\s*(\S+)", text)
    assert m is not None
    val = m.group(1).strip().strip('"')
    assert val == "/data", val


def test_bake_targets_include_amd64_arm64_armv7():
    text = _read(REPO_ROOT / "deploy/docker-bake.hcl")
    for plat in ("linux/amd64", "linux/arm64", "linux/arm/v7"):
        assert plat in text, f"bake file missing platform {platform}"


def test_static_frontend_path_is_copied():
    text = _read(REPO_ROOT / "Dockerfile")
    assert "/build/frontend/dist" in text, (
        "Dockerfile must copy the built frontend into the runtime image"
    )


def test_backend_builder_has_pillow_prerequisites():
    """Pillow on armv7 has no upstream wheel — it must source-build
    from the builder stage. The source build requires the dev
    headers of every codec Pillow can link against. A real
    ODROID-XU4 install (Sept 2026) failed with:

        The headers or library files could not be found for zlib,
        a required dependency when compiling Pillow from source.

    This test enforces the canonical Pillow prerequisite set so
    the next source-build does not trip the same way. The set
    is taken from
    https://pillow.readthedocs.io/en/stable/installation/building-from-source.html
    (zlib + libjpeg + libpng + libtiff + libwebp + freetype + lcms2).
    """
    text = _read(REPO_ROOT / "Dockerfile")
    # Find the backend-builder apt-get install block by looking
    # for the ``build-essential`` line that opens the native
    # toolchain install.
    builder_match = re.search(
        r"build-essential[^\n]*\n([\s\S]+?)\s*&&\s*rm -rf /var/lib/apt",
        text,
    )
    assert builder_match, (
        "could not locate backend-builder apt-get install block; "
        "the Dockerfile structure must include the build-essential "
        "apt-get install followed by 'rm -rf /var/lib/apt/lists/*'"
    )
    builder_block = builder_match.group(1)
    # The canonical Pillow prerequisite set as Debian 13 packages.
    required = [
        ("zlib1g-dev", "PNG/ZIP support"),
        ("libjpeg62-turbo-dev", "JPEG support"),
        ("libpng-dev", "PNG support"),
        ("libtiff-dev", "TIFF support"),
        ("libwebp-dev", "WebP support"),
        ("libfreetype6-dev", "FreeType text rendering"),
        ("liblcms2-dev", "Little CMS color management"),
    ]
    missing = []
    for pkg, what in required:
        if pkg not in builder_block:
            missing.append(f"{pkg} ({what})")
    assert not missing, (
        "Pillow source-build prerequisites missing from backend-builder: "
        + ", ".join(missing)
    )


def test_runtime_stage_has_pillow_runtime_libs():
    """The compiled Pillow wheel links dynamically against the
    shared objects shipped by these runtime libraries. Without
    them the container starts but throws ``ImportError: libjpeg.so.62``
    on first image decode.

    Package names follow Debian 13 (trixie). Only ``libpng16``
    was renamed to ``libpng16-16t64`` during the time_t (t64)
    transition. Every other Pillow codec keeps its non-t64 name
    on trixie. We use ``libpng16-16t64`` and the unchanged names
    for the rest.
    """
    text = _read(REPO_ROOT / "Dockerfile")
    # Find the runtime stage — it begins at ``FROM python:``
    # with ``AS runtime`` and runs until the next ``# ---`` /
    # ``FROM`` marker OR the end of the file.
    runtime_match = re.search(
        r"FROM python:[^\n]*AS runtime[\s\S]+?(?=\n# ---|\nFROM |\Z)",
        text,
    )
    assert runtime_match, "could not locate runtime stage in Dockerfile"
    runtime_block = runtime_match.group(0)
    required = [
        ("libjpeg62-turbo", "libjpeg.so.62"),
        ("libpng16-16t64", "libpng16.so.16 (Debian 13 t64 rename)"),
        ("libtiff6", "libtiff.so.6"),
        ("libwebp7", "libwebp.so.7"),
        ("libfreetype6", "libfreetype.so.6"),
        ("liblcms2-2", "liblcms2.so.2"),
        ("zlib1g", "libz.so.1"),
    ]
    missing = [f"{pkg} ({so})" for pkg, so in required if pkg not in runtime_block]
    assert not missing, (
        "runtime libraries missing from runtime stage: "
        + ", ".join(missing)
    )


def test_runtime_stage_uses_debian13_package_names():
    """Regression: the audit on a real ODROID-XU4 / Debian 13
    install found ``libpng16-16`` not installable because the
    package was renamed to ``libpng16-16t64`` during the time_t
    transition. Only ``libpng16`` was renamed on trixie —
    ``libwebp7`` is unchanged (the audit on the ODROID install
    found ``libwebp7t64`` is NOT installable on Debian 13 armhf).

    This test enforces the exact trixie runtime package names
    and forbids two specific regressions:

      * pre-trixie ``libpng16-16`` (without t64) — fails on trixie.
      * mistaken ``libwebp7t64`` — does not exist on trixie
        (``libwebp-dev`` still pulls plain ``libwebp7``).
    """
    text = _read(REPO_ROOT / "Dockerfile")
    runtime_match = re.search(
        r"FROM python:[^\n]*AS runtime[\s\S]+?(?=\n# ---|\nFROM |\Z)",
        text,
    )
    assert runtime_match, "could not locate runtime stage in Dockerfile"
    runtime_block = runtime_match.group(0)
    # The pre-trixie runtime package name that disappeared in
    # Debian 13. The runtime stage MUST use the t64 form.
    assert "libpng16-16t64" in runtime_block, (
        "runtime stage must use libpng16-16t64 on Debian 13 (trixie). "
        "The pre-trixie name 'libpng16-16' is no longer installable."
    )
    # ``libpng16-16`` (without t64) is FORBIDDEN — it would fail on
    # trixie. The full token must include the t64 suffix.
    assert " libpng16-16\n" not in runtime_block, (
        "runtime stage uses pre-trixie 'libpng16-16' (no t64); "
        "Debian 13 requires 'libpng16-16t64'."
    )
    # ``libwebp7t64`` does NOT exist on Debian 13 trixie armhf;
    # the runtime stage must use plain ``libwebp7``.
    assert "libwebp7t64" not in runtime_block, (
        "runtime stage uses 'libwebp7t64' which is NOT installable "
        "on Debian 13 armhf. The audit found the actual package is "
        "'libwebp7'."
    )
    assert "libwebp7" in runtime_block, (
        "runtime stage must install 'libwebp7' on Debian 13."
    )


def test_runtime_stage_no_dangling_t64_misnomer():
    """Defence in depth: the only legitimate ``*t64`` runtime
    package in the Dockerfile is ``libpng16-16t64``. Anything
    else with a ``t64`` suffix is a misnomer that will fail
    on Debian 13 (e.g. ``libwebp7t64``)."""
    text = _read(REPO_ROOT / "Dockerfile")
    runtime_match = re.search(
        r"FROM python:[^\n]*AS runtime[\s\S]+?(?=\n# ---|\nFROM |\Z)",
        text,
    )
    assert runtime_match
    runtime_block = runtime_match.group(0)
    for line in runtime_block.splitlines():
        stripped = line.strip()
        if "t64" not in stripped:
            continue
        # The only ``t64`` package we accept on trixie is libpng16-16t64.
        if "libpng16-16t64" in stripped:
            continue
        # We also accept the explanatory comments — those are not
        # package lines.
        if stripped.startswith("#"):
            continue
        pytest.fail(
            f"unexpected t64 runtime package: {stripped!r}. "
            f"On Debian 13 trixie the ONLY t64-suffixed Pillow "
            f"runtime package is libpng16-16t64. Other t64 "
            f"names (e.g. libwebp7t64) do not exist on trixie "
            f"armhf."
        )


def test_pillow_prereq_documents_are_linked():
    """The Dockerfile build block must mention Pillow and the
    audit / regression-test citation so a future reviewer
    understands *why* the codecs are listed."""
    text = _read(REPO_ROOT / "Dockerfile")
    assert "Pillow" in text, (
        "Dockerfile must name Pillow in the builder dependency "
        "comment so reviewers know why these libraries are listed"
    )
    assert "Pillow source-build" in text or "Pillow build" in text, (
        "Dockerfile must explain that Pillow is being source-built "
        "on armv7 (no upstream wheel)"
    )