"""Restricted host helper protocol — ``shmax-hostd``.

The web application talks to ``shmax-hostd`` over a Unix
socket (default ``/run/shmax/hostd.sock``) using a JSON-line
protocol. Only the operations enumerated here are accepted;
arbitrary shell execution is not in the vocabulary, by design.

The Python host helper is installed in a dedicated host-side runtime
by ``deploy/hostd/install-runtime.sh``. It creates its own Unix socket;
there is no socket activation unit.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Optional


logger = logging.getLogger(__name__)


class HostOp(str, Enum):
    STATUS = "status"
    RESTART = "restart"
    UPDATE = "update"
    ROLLBACK = "rollback"
    BACKUP = "backup"
    VERSION = "version"
    # The orchestrator-only ops below are not exposed to the
    # web app directly; they are invoked by the update manager
    # in coordination with the hostd subprocess.
    SWITCH_VERSION = "switch_version"
    MIGRATE = "migrate"
    STOP = "stop"
    PORTABLE_SNAPSHOT = "portable_snapshot"
    PORTABLE_RESTORE = "portable_restore"


ALLOWED_OPS = frozenset(op.value for op in HostOp)

SOCKET_PATH = Path(os.environ.get("SHMAX_HOSTD_SOCKET", "/run/shmax/hostd.sock"))


@dataclass
class HostRequest:
    op: str
    args: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps(
            {"op": self.op, "args": self.args},
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, line: str) -> "HostRequest":
        obj = json.loads(line)
        op = str(obj.get("op", ""))
        if op not in ALLOWED_OPS:
            raise ValueError(f"op not allowed: {op!r}")
        return cls(op=op, args=dict(obj.get("args", {})))

    @classmethod
    def validate(cls, line: str) -> "HostRequest":
        obj = json.loads(line)
        if not isinstance(obj, dict):
            raise ValueError("request must be a JSON object")
        op = str(obj.get("op", ""))
        if op not in ALLOWED_OPS:
            raise ValueError(f"op not allowed: {op!r}")
        args = obj.get("args", {})
        if not isinstance(args, dict):
            raise ValueError("args must be an object")
        # Validate args per op.
        if op == HostOp.UPDATE.value:
            channel = args.get("channel", "stable")
            if channel not in ("stable", "beta", "disabled"):
                raise ValueError(f"invalid channel: {channel}")
        if op in {HostOp.PORTABLE_SNAPSHOT.value, HostOp.PORTABLE_RESTORE.value}:
            import re
            if set(args) != {"job_id"} or not re.fullmatch(r"[a-f0-9]{32}", str(args.get("job_id", ""))):
                raise ValueError("invalid portable job")
        if op == HostOp.VERSION.value and args:
            raise ValueError("version takes no args")
        return cls(op=op, args=args)


@dataclass
class HostResponse:
    ok: bool
    detail: str = ""
    data: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.data is None:
            self.data = {}

    def to_json(self) -> str:
        return json.dumps(
            {"ok": self.ok, "detail": self.detail, "data": self.data},
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, line: str) -> "HostResponse":
        obj = json.loads(line)
        return cls(
            ok=bool(obj.get("ok", False)),
            detail=str(obj.get("detail", "")),
            data=dict(obj.get("data", {})),
        )


class HostHelperError(RuntimeError):
    pass


class HostHelperProtocol:
    """Client side: connect to ``shmax-hostd`` over a Unix socket."""

    def __init__(self, socket_path: Path = SOCKET_PATH, *, timeout_seconds: float = 5.0) -> None:
        self._path = socket_path
        self._timeout = timeout_seconds

    async def _send(self, request: HostRequest) -> HostResponse:
        if not self._path.exists():
            return HostResponse(ok=False, detail="host helper unavailable")
        reader, writer = await asyncio.open_unix_connection(str(self._path))
        try:
            writer.write(request.to_json().encode("utf-8") + b"\n")
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=self._timeout)
            if not line:
                return HostResponse(ok=False, detail="empty response")
            return HostResponse.from_json(line.decode("utf-8"))
        except asyncio.TimeoutError:
            return HostResponse(ok=False, detail="timeout")
        except OSError as e:
            return HostResponse(ok=False, detail=f"socket error: {e}")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def portable_snapshot(self, job_id: str) -> HostResponse:
        return await self._send(HostRequest(HostOp.PORTABLE_SNAPSHOT.value, {"job_id":job_id}))

    async def portable_restore(self, job_id: str) -> HostResponse:
        return await self._send(HostRequest(HostOp.PORTABLE_RESTORE.value, {"job_id":job_id}))

    async def status(self) -> HostResponse:
        return await self._send(HostRequest(op=HostOp.STATUS.value, args={}))

    async def restart(self) -> HostResponse:
        return await self._send(HostRequest(op=HostOp.RESTART.value, args={}))

    async def update(self, channel: str = "stable") -> HostResponse:
        return await self._send(
            HostRequest(op=HostOp.UPDATE.value, args={"channel": channel})
        )

    async def rollback(self) -> HostResponse:
        return await self._send(HostRequest(op=HostOp.ROLLBACK.value, args={}))

    async def backup(self) -> HostResponse:
        return await self._send(HostRequest(op=HostOp.BACKUP.value, args={}))

    async def version(self) -> HostResponse:
        return await self._send(HostRequest(op=HostOp.VERSION.value, args={}))

    async def switch_version(self, *, target_version: str) -> HostResponse:
        return await self._send(
            HostRequest(
                op=HostOp.SWITCH_VERSION.value,
                args={"target_version": str(target_version)},
            )
        )

    async def migrate(self) -> HostResponse:
        return await self._send(
            HostRequest(op=HostOp.MIGRATE.value, args={})
        )

    async def stop(self) -> HostResponse:
        return await self._send(
            HostRequest(op=HostOp.STOP.value, args={})
        )


class InProcessHostHelper(HostHelperProtocol):
    """In-process test double."""

    def __init__(self) -> None:
        super().__init__(socket_path=Path("/nonexistent/hostd.sock"))

    async def _send(self, request: HostRequest) -> HostResponse:
        return HostResponse(ok=False, detail="host helper unavailable")


__all__ = [
    "ALLOWED_OPS",
    "HostHelperError",
    "HostHelperProtocol",
    "HostOp",
    "HostRequest",
    "HostResponse",
    "InProcessHostHelper",
    "SOCKET_PATH",
]
