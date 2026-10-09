"""shmax-hostd — restricted host helper.

This module is the host-side daemon that performs
destructive operations (update, rollback, restart) on
behalf of the SHMAX web container. The web container
NEVER touches Docker or systemd directly; it talks to
this daemon over a Unix socket.

Protocol: JSON-line, one request per line, one response
per line. The only allowed operations are:

* ``status``
* ``restart``
* ``update``
* ``rollback``
* ``backup``
* ``version``
* ``switch_version`` (orchestrator; flips image reference)
* ``migrate`` (orchestrator; runs alembic upgrade head)
* ``stop`` (orchestrator; brings web container down cleanly)

Anything else is rejected. Subprocess invocations use a
fixed argv (no ``shell=True``, no ``bash -c <user>``); the
only variable inputs are validated version identifiers.

Socket file:

    /run/shmax/hostd.sock

owned ``root:shmax`` mode ``0660``. The web container can
``connect(2)`` but cannot ``accept(2)`` or ``unlink(2)``.

Usage::

    python -m shmax.host.server

Or programmatically::

    from shmax.host.server import HostdServer
    server = HostdServer(socket_path="/run/shmax/hostd.sock")
    await server.serve_forever()
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import socket
import stat
import signal
import fcntl
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from .container_executor import ContainerExecutor, DockerComposeExecutor
from .protocol import HostRequest, HostResponse, HostOp, ALLOWED_OPS


logger = logging.getLogger(__name__)


SOCKET_PATH = "/run/shmax/hostd.sock"
SOCKET_GROUP = "shmax"
SOCKET_MODE = 0o660

# Strict SemVer per https://semver.org/. We additionally
# allow the optional ``-dev.<n>`` suffix used by SHMAX.
SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z\-\.]+))?(?:\+(?P<build>[0-9A-Za-z\-\.]+))?$"
)


def _validate_version(version: str) -> str:
    if not version or not isinstance(version, str):
        raise ValueError("version must be a non-empty string")
    if not SEMVER_RE.match(version):
        raise ValueError(f"version does not match SemVer: {version!r}")
    return version


def _cmd_status() -> list[str]:
    return [
        "docker",
        "compose",
        "-f",
        "/opt/shmax/compose.yaml",
        "ps",
        "--format",
        "json",
    ]


def _cmd_restart() -> list[str]:
    return [
        "docker",
        "compose",
        "-f",
        "/opt/shmax/compose.yaml",
        "restart",
        "web",
    ]


def _cmd_update(channel: str = "stable") -> list[str]:
    if channel not in ("stable", "beta"):
        raise ValueError(f"invalid channel: {channel}")
    return [
        "shmax",
        "update",
        "apply",
        "--channel",
        channel,
    ]


def _cmd_rollback() -> list[str]:
    return ["shmax", "rollback"]


def _cmd_backup() -> list[str]:
    # ``shmax backup`` accepts ``--output`` / ``--passphrase`` only;
    # ``--reason`` was never a supported flag and would have
    # crashed argparse. The audit flagged this as a runtime bug.
    return ["shmax", "backup"]


def _cmd_version() -> list[str]:
    return ["shmax", "version"]


COMMAND_BUILDERS: dict[str, Callable[..., list[str]]] = {
    HostOp.STATUS.value: lambda **kw: _cmd_status(),
    HostOp.RESTART.value: lambda **kw: _cmd_restart(),
    HostOp.UPDATE.value: lambda **kw: _cmd_update(kw.get("channel", "stable")),
    HostOp.ROLLBACK.value: lambda **kw: _cmd_rollback(),
    HostOp.BACKUP.value: lambda **kw: _cmd_backup(),
    HostOp.VERSION.value: lambda **kw: _cmd_version(),
    # SWITCH_VERSION, MIGRATE and STOP are handled by the
    # container executor (see ``HostdServer._dispatch``). They
    # are not in COMMAND_BUILDERS on purpose — calling them
    # through the subprocess path would mean the orchestrator
    # cannot use a fake executor in tests.
}


# ----------------------------------------------------------------------
# Container executor routing — SWITCH / MIGRATE / STOP
# ----------------------------------------------------------------------
_EXECUTOR_OPS = frozenset(
    {
        HostOp.SWITCH_VERSION.value,
        HostOp.MIGRATE.value,
        HostOp.STOP.value,
    }
)


def _format_response(req: HostRequest, data: dict[str, Any], ok: bool = True) -> bytes:
    # ``HostResponse.to_json`` already returns the JSON wire
    # string. Re-encoding it through ``json.dumps`` was producing
    # the audit's "JSON string inside JSON" bug — clients would
    # receive a top-level string that needs a second parse.
    # Just emit the response bytes directly.
    return (HostResponse(ok=ok, detail="" if ok else data.get("error", "error"), data=data).to_json() + "\n").encode()


def _format_error(message: str) -> bytes:
    return (HostResponse(ok=False, detail=message, data={}).to_json() + "\n").encode()


class HostdServer:
    """Async Unix-socket server implementing the restricted protocol."""

    def __init__(
        self,
        *,
        socket_path: str = SOCKET_PATH,
        group: str = SOCKET_GROUP,
        mode: int = SOCKET_MODE,
        executor: Optional[ContainerExecutor] = None,
        strict_permissions: bool = False,
    ) -> None:
        from .portable import HostPortable
        self._portable = HostPortable(data=Path(os.environ.get("SHMAX_DATA_DIR", "/var/lib/shmax")))
        self._strict_permissions = strict_permissions
        self._socket_path = socket_path
        self._group = group
        self._mode = mode
        self._executor: ContainerExecutor = executor or DockerComposeExecutor(
            compose_file=Path("/opt/shmax/compose.yaml"), service="web"
        )
        self._server: asyncio.AbstractServer | None = None
        self._stop = asyncio.Event()

    async def serve_forever(self) -> None:
        path = Path(self._socket_path)
        if self._strict_permissions and path.parent.is_symlink():
            raise RuntimeError("socket parent must not be a symlink")
        path.parent.mkdir(parents=True, exist_ok=True)
        # A lock distinguishes a stale socket from another live hostd.
        lock_fd = os.open(path.parent / f".{path.name}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        inode = None
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if path.exists() or path.is_symlink():
                if not stat.S_ISSOCK(path.lstat().st_mode):
                    raise RuntimeError("refusing to replace a non-socket hostd path")
                path.unlink()
            self._server = await asyncio.start_unix_server(
                self._handle_client, path=str(path), start_serving=False
            )
            inode = path.stat().st_ino
            try:
                os.chown(path, os.geteuid(), _gid_for(self._group))
            except (PermissionError, OSError, KeyError):
                if self._strict_permissions:
                    raise
            os.chmod(path, self._mode)
            await self._server.start_serving()
            logger.info("shmax-hostd listening on %s", path)
            await self._stop.wait()
        finally:
            if self._server is not None:
                self._server.close()
                await self._server.wait_closed()
            if inode is not None and path.exists() and path.stat().st_ino == inode:
                path.unlink()
            os.close(lock_fd)

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            data = await reader.readline()
            if not data:
                return
            try:
                req = HostRequest.validate(data.decode("utf-8").strip())
            except (ValueError, json.JSONDecodeError) as e:
                writer.write(_format_error(f"bad request: {e}"))
                await writer.drain()
                return
            response_data = await self._dispatch(req)
            # ``ok`` is False if either the dispatch reported an
            # ``error`` key OR the subprocess returned non-zero.
            # The audit flagged a regression where non-zero
            # exit codes were silently treated as success.
            ok = "error" not in response_data and int(
                response_data.get("returncode", 0)
            ) == 0
            writer.write(_format_response(req, response_data, ok=ok))
            await writer.drain()
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def _dispatch(self, req: HostRequest) -> dict[str, Any]:
        # Serialize all host mutations with portable snapshot/restore.
        if req.op in ALLOWED_OPS and req.op not in {HostOp.STATUS.value, HostOp.VERSION.value, HostOp.PORTABLE_SNAPSHOT.value, HostOp.PORTABLE_RESTORE.value}:
            try:fd=self._portable.lock()
            except Exception:return {"error":"Another host operation is in progress."}
            try:return await self._dispatch_unlocked(req)
            finally:os.close(fd)
        return await self._dispatch_unlocked(req)

    async def _dispatch_unlocked(self, req: HostRequest) -> dict[str, Any]:
        if req.op in {HostOp.PORTABLE_SNAPSHOT.value, HostOp.PORTABLE_RESTORE.value}:
            try:
                if req.op == HostOp.PORTABLE_SNAPSHOT.value:
                    await self._portable.create_snapshot(req.args["job_id"])
                    return {"phase":"snapshot", "job_id":req.args["job_id"]}
                return self._portable.submit_restore(req.args["job_id"])
            except Exception:
                return {"error":"Portable operation failed; inspect its authenticated status."}
        if req.op not in ALLOWED_OPS:
            return {"error": f"unknown op {req.op!r}; allowed: {sorted(ALLOWED_OPS)}"}
        # Container executor path: SWITCH_VERSION, MIGRATE, STOP
        # are routed through the executor so tests can inject a
        # fake without duplicating the orchestration logic.
        if req.op in _EXECUTOR_OPS:
            return await self._dispatch_executor(req)
        builder = COMMAND_BUILDERS[req.op]
        try:
            argv = builder(**req.args)
        except ValueError as e:
            return {"error": str(e)}
        # Validate version argument strictly when update op.
        if req.op == HostOp.UPDATE.value:
            for a in argv:
                if a.startswith("v") and "/" in a:
                    return {"error": "version looks like a path; refused"}
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return {"error": f"command not found: {argv[0]}"}
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"error": "timeout"}
        return {
            "argv": argv,
            "returncode": proc.returncode,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
        }

    async def _dispatch_executor(self, req: HostRequest) -> dict[str, Any]:
        if req.op == HostOp.SWITCH_VERSION.value:
            target = str(req.args.get("target_version") or "").strip()
            if not _is_semver(target):
                return {"error": f"invalid target_version: {target!r}"}
            result = await self._executor.switch_version(target_version=target)
        elif req.op == HostOp.MIGRATE.value:
            result = await self._executor.migrate()
        elif req.op == HostOp.STOP.value:
            result = await self._executor.stop()
        else:  # pragma: no cover - guarded above
            return {"error": f"unhandled executor op {req.op!r}"}
        # Normalize the result so the response format matches
        # the subprocess path.
        return {
            "returncode": int(result.get("returncode", 0)),
            "stdout": str(result.get("stdout", "")),
            "stderr": str(result.get("stderr", "")),
            "phase": str(req.op),
        }

    async def stop(self) -> None:
        self._stop.set()


def _gid_for(name: str) -> int:
    import grp

    return grp.getgrnam(name).gr_gid


# ---------------------------------------------------------------------
# SemVer validation — applied to the SWITCH_VERSION target.
# Strict: ``MAJOR.MINOR.PATCH`` with optional ``-prerelease``.
# We refuse anything with a path separator so the executor never
# gets handed a malicious tag like ``../etc``.
# ---------------------------------------------------------------------
_SEMVER_RE = re.compile(
    r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*)?"
    r"(?:\+[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*)?$"
)


def _is_semver(value: str) -> bool:
    if not isinstance(value, str) or "/" in value or "\\" in value:
        return False
    if not value or len(value) > 64:
        return False
    return bool(_SEMVER_RE.match(value))


# ---------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------
def _cli() -> int:
    parser = argparse.ArgumentParser(prog="shmax-hostd")
    parser.add_argument(
        "--socket",
        default=SOCKET_PATH,
        help=f"Unix socket path (default: {SOCKET_PATH})",
    )
    parser.add_argument("--group", default=SOCKET_GROUP)
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s shmax-hostd %(message)s",
    )
    server = HostdServer(socket_path=args.socket, group=args.group, strict_permissions=True)
    async def run() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, server._stop.set)
        await server._portable.recover_interrupted()
        await server.serve_forever()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
