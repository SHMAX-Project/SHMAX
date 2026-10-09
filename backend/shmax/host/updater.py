"""Production host-side update transaction (live acceptance A6).

Runs ON THE HOST (via ``shmax update apply`` invoked by
``shmax-hostd``). The web container never sees Docker — this
module is the only component allowed to touch /opt/shmax and the
Docker CLI, and only through a FIXED command set (no shell
strings, no web-controlled arguments).

Flow (A6.1):

  1. resolve the release server-side (GitHub API, strict trust)
  2. download the release artifact (allowlisted origins only)
  3. verify SHA256 of the DOWNLOADED bytes (A5)
  4. create a safety backup of persistent data
  5. stage + validate the archive (paths/symlinks/special files)
  6. keep the previous deployment until the new one is healthy
  7. install under /opt/shmax (preserving deployment config:
     compose.yaml + .env — the operator's bind address)
  8. recreate the service
  9. host-visible readiness check (published port, not the
     container-internal healthcheck)
 10. failure ⇒ automatic rollback of the PREVIOUS deployment and
     its host-visible health; rollback failure is reported as
     such — never disguised as success.

APPLICATION IS DISPOSABLE. USER DATA (/var/lib/shmax) IS NEVER
TOUCHED by this module.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import io
import logging
import shutil
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_OPT_DIR = Path("/opt/shmax")
DEFAULT_DATA_DIR = Path("/var/lib/shmax")
# Deployment configuration preserved across updates (operator's
# bind address / environment live here).
PRESERVED_DEPLOY_FILES = ("compose.yaml", ".env")
MAX_ARTIFACT_BYTES = 256 * 1024 * 1024
READY_TIMEOUT_SECONDS = 180.0


class UpdateError(Exception):
    """Raised when the update transaction fails.

    ``rollback_failed`` marks the high-severity state where even
    the automatic rollback did not succeed — the message then
    carries explicit manual recovery instructions."""

    def __init__(self, message: str, *, rollback_failed: bool = False) -> None:
        super().__init__(message)
        self.rollback_failed = rollback_failed


@dataclass
class UpdateResult:
    version: str
    backup_path: str = ""
    stages: list = field(default_factory=list)
    rolled_back: bool = False


# Type aliases for the injectable boundaries (tests stage fakes;
# production wires real ones in HostUpdater.__init__ defaults).
Runner = Callable[[list], Awaitable["Any"]]   # argv -> result(ok, stdout, stderr)
Fetcher = Callable[[str], Awaitable[bytes]]   # url -> bytes (trust-checked)
Readiness = Callable[[str], Awaitable[bool]]  # url -> host-visible health


async def _default_runner(argv: list) -> Any:
    from types import SimpleNamespace

    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await proc.communicate()
    except asyncio.CancelledError:
        if proc.returncode is None:proc.kill()
        await proc.wait()
        raise
    return SimpleNamespace(
        returncode=proc.returncode,
        stdout=stdout.decode(errors="replace"),
        stderr=stderr.decode(errors="replace"),
    )


async def _default_fetcher(url: str) -> bytes:
    """Production downloader: same trust boundary as the update
    check (allowlisted GitHub origins, validated redirects), with a
    larger artifact cap."""
    from ..update.service import _http_get, assert_url_allowed, UpdateCheckError

    assert_url_allowed(url)
    try:
        import aiohttp

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=600)
        ) as session:
            current = url
            for _hop in range(6):
                async with session.get(
                    current, allow_redirects=False
                ) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        from ..update.service import GITHUB_API_ORIGIN

                        current = resp.headers.get("Location") or ""
                        # NOTE: asset hosts redirect among themselves;
                        # the allowlist check below covers them.
                        assert_url_allowed(current)
                        continue
                    if resp.status != 200:
                        raise UpdateError(f"artifact download: HTTP {resp.status}")
                    chunks, received = [], 0
                    async for chunk in resp.content.iter_any():
                        received += len(chunk)
                        if received > MAX_ARTIFACT_BYTES:
                            raise UpdateError("artifact exceeds size cap")
                        chunks.append(chunk)
                    return b"".join(chunks)
        raise UpdateError("too many redirects")
    except UpdateCheckError as e:
        raise UpdateError(f"artifact download blocked: {e.kind}") from e


async def _default_readiness(url: str) -> bool:
    import aiohttp

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5)
        ) as session:
            async with session.get(url) as resp:
                return resp.status == 200
    except Exception:  # noqa: BLE001
        return False


def _resolve_bind(compose_text: str) -> tuple[str, str]:
    """Best-effort (host, port) from the operator's compose.yaml
    ``ports:`` section; falls back to 127.0.0.1:8787."""
    import re

    for m in re.finditer(r'"?(\d+\.\d+\.\d+\.\d+):(\d+):\d+"?', compose_text):
        return m.group(1), m.group(2)
    for m in re.finditer(r'"?(\d+):8787"?', compose_text):
        return "127.0.0.1", m.group(1)
    return "127.0.0.1", "8787"


class HostUpdater:
    def __init__(
        self,
        *,
        opt_dir: Path = DEFAULT_OPT_DIR,
        data_dir: Path = DEFAULT_DATA_DIR,
        current_version: str = "0.0.0",
        channel: str = "stable",
        runner: Optional[Runner] = None,
        fetcher: Optional[Fetcher] = None,
        readiness: Optional[Readiness] = None,
        releases_resolver: Optional[Callable[[str], Awaitable[dict]]] = None,
        compose_file: str = "compose.yaml",
    ) -> None:
        self._opt = Path(opt_dir)
        self._data = Path(data_dir)
        self._current = current_version
        self._channel = channel
        self._runner = runner or _default_runner
        self._fetcher = fetcher or _default_fetcher
        self._readiness = readiness or _default_readiness
        self._resolver = releases_resolver
        self._compose_file = compose_file
        self._stages: list[str] = []

    # ---- stage log -----------------------------------------------------
    def _stage(self, name: str) -> None:
        self._stages.append(name)
        logger.info("update stage: %s", name)

    # ---- steps ----------------------------------------------------------
    async def _resolve_release(self) -> dict:
        """Server-side revalidation: pick the release for the
        channel and verify its manifest/SHA trust chain."""
        self._stage("preparing")
        if self._resolver is not None:
            meta = await self._resolver(self._channel)
        else:
            from ..update.service import UpdateService, UpdateCheckError
            import sqlite3

            svc = UpdateService(
                current_version=self._current,
                secret_store=_NullSecrets(),
                db=None,
                data_dir=self._data,
            )
            try:
                status = await svc.check(persist=False)
            except UpdateCheckError as e:
                raise UpdateError(f"release resolution failed: {e.kind}") from e
            meta = status.get("available") or {}
        if not meta or not meta.get("artifact_url") or not meta.get("sha256"):
            raise UpdateError("release metadata incomplete; refusing update")
        return meta

    async def _download_and_verify(self, meta: dict) -> bytes:
        self._stage("downloading")
        data = await self._fetcher(str(meta["artifact_url"]))
        self._stage("verifying")
        from ..update.service import (
            UpdateCheckError,
            verify_artifact_bytes,
        )

        try:
            verify_artifact_bytes(data, str(meta["sha256"]))
        except UpdateCheckError as e:
            raise UpdateError(f"artifact verification failed: {e.kind}") from e
        return data

    async def _safety_backup(self) -> Path:
        self._stage("backup")
        from ..backup.manager import create_backup

        self._data.mkdir(parents=True, exist_ok=True)
        bdir = self._data / "backups"
        bdir.mkdir(parents=True, exist_ok=True)
        out = bdir / f"pre-update-{int(_dt.datetime.now().timestamp())}.shmax"
        manifest = await asyncio.to_thread(
            create_backup,
            data_dir=self._data,
            out_path=out,
            shmax_version=self._current,
        )
        if not manifest.entries:
            raise UpdateError("safety backup produced an empty archive")
        return out

    def _safe_extract(self, archive: bytes, staging: Path) -> None:
        """Validate EVERY member (path/kind — no symlinks, no
        traversal, no devices) before anything touches disk."""
        from ..backup.manager import (
            _validate_member_kind,
            _validate_member_path,
        )

        staging.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            members = list(tar.getmembers())
            resolved = staging.resolve()
            for m in members:
                _validate_member_path(m.name, resolved)
                _validate_member_kind(m)
            for m in members:
                if m.isdir():
                    (resolved / m.name).mkdir(parents=True, exist_ok=True)
                    continue
                if m.isfile():
                    fh = tar.extractfile(m)
                    assert fh is not None
                    target = resolved / m.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(fh.read())

    def _verify_release_structure(self, staging: Path) -> None:
        self._stage("staging")
        if not (staging / self._compose_file).is_file():
            raise UpdateError(
                "release archive has no compose.yaml; refusing install"
            )
        if not any(staging.iterdir()):
            raise UpdateError("release archive is empty")

    def _compose_argv(self, *args: str) -> list:
        return [
            "docker", "compose", "-f",
            str(self._opt / self._compose_file), *args,
        ]

    async def _recreate(self) -> None:
        self._stage("installing")
        r = await self._runner(self._compose_argv("up", "-d", "--build"))
        if getattr(r, "returncode", 1) != 0:
            raise UpdateError(
                f"docker compose up failed: {getattr(r, 'stderr', '')[:300]}"
            )

    async def _host_ready(self, compose_text: str) -> bool:
        host, port = _resolve_bind(compose_text)
        deadline = asyncio.get_running_loop().time() + READY_TIMEOUT_SECONDS
        url = f"http://{host}:{port}/health/ready"
        self._stage("restarting")
        while asyncio.get_running_loop().time() < deadline:
            if await self._readiness(url):
                self._stage("checking")
                return True
            await asyncio.sleep(3)
        return False

    async def apply(self) -> UpdateResult:
        meta = await self._resolve_release()
        archive = await self._download_and_verify(meta)
        backup_path = await self._safety_backup()

        import tempfile

        prev = self._opt.parent / (self._opt.name + ".prev")
        if prev.exists():
            shutil.rmtree(prev, ignore_errors=True)

        live_touched = False
        try:
            # ONE staging scope: extract, structurally validate and
            # install. Structural failures here happen BEFORE the
            # live deployment is touched — no rollback needed.
            with tempfile.TemporaryDirectory(prefix="shmax-update-") as tmp:
                staging = Path(tmp) / "release"
                self._safe_extract(archive, staging)
                self._verify_release_structure(staging)

                # Preserve the operator's deployment config.
                preserved: dict[str, bytes] = {}
                if self._opt.is_dir():
                    for name in PRESERVED_DEPLOY_FILES:
                        f = self._opt / name
                        if f.is_file():
                            preserved[name] = f.read_bytes()
                    # Previous deployment stays restorable until
                    # the new one is healthy.
                    shutil.move(str(self._opt), str(prev))

                # From this point on the live deployment is being
                # replaced — failures below trigger rollback.
                live_touched = True
                shutil.copytree(staging, self._opt)
                for name, blob in preserved.items():
                    (self._opt / name).write_bytes(blob)

            compose_text = ""
            compose_f = self._opt / self._compose_file
            if compose_f.is_file():
                compose_text = compose_f.read_text(encoding="utf-8")
            await self._recreate()
            if not await self._host_ready(compose_text):
                raise UpdateError(
                    "host-visible readiness check timed out after update"
                )
        except UpdateError:
            if live_touched:
                await self._rollback(prev, backup_path)
            raise
        except Exception as e:  # noqa: BLE001
            if live_touched:
                await self._rollback(prev, backup_path)
            raise UpdateError(f"update failed: {e}") from e

        # Success: the previous deployment is no longer needed.
        if prev.exists():
            shutil.rmtree(prev, ignore_errors=True)
        self._stage("completed")
        return UpdateResult(
            version=str(meta.get("version", "")),
            backup_path=str(backup_path),
            stages=list(self._stages),
        )

    async def _rollback(self, prev: Path, backup_path: Path) -> None:
        """Restore the PREVIOUS deployment and verify its
        host-visible health. Never pretends success."""
        self._stage("rollback")
        try:
            if self._opt.exists():
                failed = self._opt.parent / (self._opt.name + ".failed")
                shutil.rmtree(failed, ignore_errors=True)
                shutil.move(str(self._opt), str(failed))
            if prev.exists():
                shutil.move(str(prev), str(self._opt))
            r = await self._runner(self._compose_argv("up", "-d", "--build"))
            if getattr(r, "returncode", 1) != 0:
                raise RuntimeError(f"compose up failed: {getattr(r, 'stderr', '')[:300]}")
            compose_text = ""
            f = self._opt / self._compose_file
            if f.is_file():
                compose_text = f.read_text(encoding="utf-8")
            if not await self._host_ready(compose_text):
                raise RuntimeError("previous version did not become healthy")
        except Exception as e:  # noqa: BLE001
            raise UpdateError(
                "UPDATE AND ROLLBACK BOTH FAILED — manual recovery "
                f"required: restore /opt/shmax from {prev} (or reinstall) "
                f"and run `docker compose up -d --build`; persistent data "
                f"in {self._data} is intact; safety backup: {backup_path}. "
                f"Root cause: {e}",
                rollback_failed=True,
            ) from e

    async def rollback_last(self) -> UpdateResult:
        """Operator-invoked rollback from the retained .prev
        deployment."""
        prev = self._opt.parent / (self._opt.name + ".prev")
        if not prev.is_dir():
            raise UpdateError("no previous deployment retained")
        backup_path = self._data / "backups"
        if self._opt.exists():
            shutil.move(
                str(self._opt),
                str(self._opt.parent / (self._opt.name + ".failed")),
            )
        shutil.move(str(prev), str(self._opt))
        r = await self._runner(self._compose_argv("up", "-d", "--build"))
        if getattr(r, "returncode", 1) != 0:
            raise UpdateError(
                f"rollback compose up failed: {getattr(r, 'stderr', '')[:300]}"
            )
        compose_text = (self._opt / self._compose_file).read_text(
            encoding="utf-8"
        )
        if not await self._host_ready(compose_text):
            raise UpdateError("rollback did not become healthy")
        return UpdateResult(version="previous", rolled_back=True)


class _NullSecrets:
    """Minimal SecretStore shape for host-side release resolution
    (the host process has the data dir; channel comes from argv)."""

    def has_secret(self, key: str) -> bool:
        return False

    def get_secret(self, key: str) -> str:
        return ""

    def set_secret(self, key: str, value: str) -> None:
        pass


__all__ = ["HostUpdater", "UpdateError", "UpdateResult"]
