"""BLOCKER 21 — One compose contract.

The audit flagged two divergent production compose files:

* ``compose.yaml`` (root, dev wrapper)
* ``deploy/compose/compose.yaml`` (production)

Both must agree on:

* container UID/GID (``8787:8787`` by default)
* persistent subdirectory layout under ``/data``
* MTProto session path (``/data/sessions/telegram/...``, NOT
  ``/data/tg_mtproto.session``)
* hostd socket mount
* Docker socket NOT present
* healthcheck endpoint (``http://127.0.0.1:8787/health``)
* in-container ``SHMAX_DATA_DIR=/data``
* single service (no extra sidecars in V1)
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
ROOT_COMPOSE = REPO_ROOT / "compose.yaml"
DEPLOY_COMPOSE = REPO_ROOT / "deploy" / "compose" / "compose.yaml"


def _load(path: Path) -> dict:
    assert path.is_file(), f"missing compose file: {path}"
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _services(compose: dict) -> dict:
    return compose.get("services", {})


def _main_service(compose: dict) -> tuple[str, dict]:
    services = _services(compose)
    # The two files name the service differently (``shmax`` vs
    # ``web``). The static test accepts either as long as there
    # is exactly one service.
    assert len(services) == 1, f"compose must have exactly one service: {services.keys()}"
    name, body = next(iter(services.items()))
    return name, body


def test_root_compose_exists():
    assert ROOT_COMPOSE.is_file()


def test_deploy_compose_exists():
    assert DEPLOY_COMPOSE.is_file()


def test_both_compose_files_agree_on_data_dir_env():
    """Inside the container ``SHMAX_DATA_DIR=/data`` in both."""
    root = _load(ROOT_COMPOSE)
    _, root_svc = _main_service(root)
    assert root_svc["environment"]["SHMAX_DATA_DIR"] == "/data"

    deploy = _load(DEPLOY_COMPOSE)
    _, deploy_svc = _main_service(deploy)
    assert deploy_svc["environment"]["SHMAX_DATA_DIR"] == "/data"


def _mount_targets(svc: dict) -> set[str]:
    """Return the in-container mount targets for a service.

    Compose mount entries have the form ``source:target[:opts]``.
    We accept short and long forms; the source side may itself
    contain colons (``${VAR:-/var/lib/shmax}``) so we split on
    the LAST colon that is NOT part of a mode suffix (``:ro`` /
    ``:rw``)."""
    targets: set[str] = set()
    for raw in svc.get("volumes") or []:
        s = str(raw).strip()
        # Strip leading ``-`` and any long-form key prefix.
        if s.startswith("-"):
            s = s[1:].strip()
        # Drop optional mode suffix (``:ro``, ``:rw``, ``:z``...).
        parts = s.split(":")
        target = parts[-1]
        # Re-glue if the last segment is a mode marker, not a path.
        if target in ("ro", "rw", "z", "Z", "shared", "slave", "private", "cached", "delegated"):
            target = ":".join(parts[-2:])
        targets.add(target)
    return targets


def test_both_compose_files_mount_required_persistent_subdirs():
    expected_targets = {
        "/data/db",
        "/data/config",
        "/data/secrets",
        "/data/state",
        "/data/backups",
        "/data/cache",
        # Per-account session dirs, NOT a single ``/data/tg_mtproto.session``.
        "/data/sessions/max",
        "/data/sessions/telegram",
    }
    for path in (ROOT_COMPOSE, DEPLOY_COMPOSE):
        compose = _load(path)
        _, svc = _main_service(compose)
        targets = _mount_targets(svc)
        missing = expected_targets - targets
        assert not missing, f"{path} is missing persistent mounts: {missing}"


def test_neither_compose_mounts_docker_socket():
    for path in (ROOT_COMPOSE, DEPLOY_COMPOSE):
        compose = _load(path)
        _, svc = _main_service(compose)
        for v in svc.get("volumes") or []:
            assert "/var/run/docker.sock" not in v, f"{path} mounts the Docker socket"


def test_both_compose_mount_hostd_socket_readonly():
    for path in (ROOT_COMPOSE, DEPLOY_COMPOSE):
        compose = _load(path)
        _, svc = _main_service(compose)
        hostd_mounts = [v for v in svc.get("volumes") or [] if "/run/shmax/hostd" in v]
        assert hostd_mounts, f"{path} is missing the hostd socket mount"
        for v in hostd_mounts:
            assert v.endswith(":ro"), f"{path} hostd socket mount must be read-only: {v}"


def test_both_compose_share_the_same_healthcheck_target():
    for path in (ROOT_COMPOSE, DEPLOY_COMPOSE):
        compose = _load(path)
        _, svc = _main_service(compose)
        hc = svc.get("healthcheck") or {}
        test = " ".join(hc.get("test") or [])
        assert "127.0.0.1:8787" in test, f"{path} healthcheck must target 127.0.0.1:8787"


def test_both_compose_use_uid_gid_8787_by_default():
    for path in (ROOT_COMPOSE, DEPLOY_COMPOSE):
        compose = _load(path)
        _, svc = _main_service(compose)
        # Production compose hard-codes 8787:8787; the root
        # wrapper substitutes via env. Both default to 8787.
        assert "8787" in str(svc.get("user") or ""), f"{path} UID/GID default not 8787"


def test_root_compose_internally_consistent_with_deploy_compose_for_volume_paths():
    """The two files must agree on the *paths under /data* they
    expose. They may differ in source-side prefixes (./ vs
    /var/lib/shmax/) but the in-container targets must match."""
    _, root_svc = _main_service(_load(ROOT_COMPOSE))
    _, deploy_svc = _main_service(_load(DEPLOY_COMPOSE))
    root_targets = _mount_targets(root_svc)
    deploy_targets = _mount_targets(deploy_svc)
    assert root_targets == deploy_targets, (
        f"root and deploy compose disagree on /data mount targets: "
        f"root={root_targets} deploy={deploy_targets}"
    )


def test_no_legacy_tg_mtproto_session_path():
    """Audit BLOCKER 21: ``/data/tg_mtproto.session`` is NOT the
    canonical path. The session lives under
    ``/data/sessions/telegram/<name>.session`` and is created by
    the Telegram adapter at runtime."""
    # Verify the runtime never mounts /data/tg_mtproto.session
    # via the compose files (a regression here would re-mount
    # the legacy single-file path).
    for path in (ROOT_COMPOSE, DEPLOY_COMPOSE):
        _, svc = _main_service(_load(path))
        for v in svc.get("volumes") or []:
            assert "tg_mtproto.session" not in str(v), (
                f"{path} mounts the legacy session path: {v}"
            )
    # Verify the runtime default for the session directory.
    backend_paths = (REPO_ROOT / "backend" / "shmax" / "core" / "paths.py").read_text()
    assert "sessions_telegram" in backend_paths, (
        "PathLayout must expose a ``sessions_telegram`` directory "
        "under the persistent root"
    )
    assert "tg_mtproto.session" not in backend_paths


def test_compose_files_have_no_docker_socket_volume():
    """Comprehensive grep across both compose files for the
    docker.sock mount — a regression that re-introduces the
    socket would be a security hole. We only inspect actual
    ``volumes:`` mount lines (comments are allowed)."""
    for path in (ROOT_COMPOSE, DEPLOY_COMPOSE):
        compose = _load(path)
        _, svc = _main_service(compose)
        for v in svc.get("volumes") or []:
            assert "docker.sock" not in str(v), (
                f"{path} mounts the docker socket: {v}"
            )