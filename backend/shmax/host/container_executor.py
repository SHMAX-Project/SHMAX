"""Container executor — abstraction over docker compose / podman.

The hostd daemon uses the executor to perform SWITCH_VERSION and
MIGRATE operations. The audit required that production
orchestration code be the same as test orchestration code: tests
inject a :class:`FakeContainerExecutor` and the production code
goes through :class:`DockerComposeExecutor`.

The executor is intentionally narrow: it owns three operations:

* ``switch_version(target_version)`` — flip the active image
  reference (docker compose ``up`` with the new tag).
* ``migrate()`` — run ``alembic upgrade head`` against the
  live database (or any other schema migration command).
* ``stop()`` — bring the web container down cleanly.
* ``rollback(target_version)`` — fall back to a previous image
  and rerun migrations.
"""

from __future__ import annotations

import abc
import asyncio
import json
import logging
import os
import shlex
from pathlib import Path
from typing import Any, Optional


logger = logging.getLogger(__name__)


class ContainerExecutor(abc.ABC):
    """Abstract executor. Implementations run real commands in
    production; tests use a fake."""

    @abc.abstractmethod
    async def switch_version(self, *, target_version: str) -> dict[str, Any]:
        ...

    @abc.abstractmethod
    async def migrate(self) -> dict[str, Any]:
        ...

    @abc.abstractmethod
    async def stop(self) -> dict[str, Any]:
        ...

    @abc.abstractmethod
    async def rollback(self, *, target_version: str) -> dict[str, Any]:
        ...


class DockerComposeExecutor(ContainerExecutor):
    """Production executor backed by ``docker compose``.

    Each method returns a dict with at least ``returncode``,
    ``stdout``, and ``stderr`` so the hostd server can decide
    success the same way it does for every other command.
    """

    def __init__(
        self,
        *,
        compose_file: Path,
        service: str = "shmax",
        env: Optional[dict[str, str]] = None,
    ) -> None:
        self._compose_file = compose_file
        self._service = service
        self._env = env or dict(os.environ)

    def _compose_args(self, *args: str) -> list[str]:
        return [
            "docker",
            "compose",
            "-f",
            str(self._compose_file),
            *args,
        ]

    async def _run(self, argv: list[str], *, timeout: float = 600) -> dict[str, Any]:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"returncode": -1, "stdout": "", "stderr": "timeout"}
        return {
            "returncode": int(proc.returncode),
            "stdout": stdout_b.decode("utf-8", errors="replace"),
            "stderr": stderr_b.decode("utf-8", errors="replace"),
        }

    async def switch_version(self, *, target_version: str) -> dict[str, Any]:
        # Pull the target tag and bring the service up with it.
        # ``SHMAX_IMAGE_TAG`` overrides the compose default so a
        # single ``up -d`` flips the running image.
        env = dict(self._env)
        env["SHMAX_IMAGE_TAG"] = target_version
        proc = await asyncio.create_subprocess_exec(
            *self._compose_args("pull", self._service),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout_b, stderr_b = await proc.communicate()
        pull_rc = int(proc.returncode)
        if pull_rc != 0:
            return {
                "returncode": pull_rc,
                "stdout": stdout_b.decode("utf-8", errors="replace"),
                "stderr": stderr_b.decode("utf-8", errors="replace"),
                "phase": "pull",
            }
        return await self._run(self._compose_args("up", "-d", self._service))

    async def migrate(self) -> dict[str, Any]:
        # Run alembic inside the running container so the
        # migration sees the live database URL.
        return await self._run(
            self._compose_args(
                "exec",
                "-T",
                self._service,
                "alembic",
                "upgrade",
                "head",
            )
        )

    async def stop(self) -> dict[str, Any]:
        return await self._run(self._compose_args("stop", self._service))

    async def rollback(self, *, target_version: str) -> dict[str, Any]:
        env = dict(self._env)
        env["SHMAX_IMAGE_TAG"] = target_version
        return await self._run(self._compose_args("up", "-d", self._service), )


class FakeContainerExecutor(ContainerExecutor):
    """In-memory executor for unit tests.

    Records every call so the test can assert the orchestration
    sequence. ``script`` is a list of return dicts; the next
    call pops the next script entry, or returns ``default`` if
    the script is exhausted.
    """

    def __init__(
        self,
        *,
        script: Optional[list[dict[str, Any]]] = None,
        default: Optional[dict[str, Any]] = None,
    ) -> None:
        self._script = list(script or [])
        self._default = default or {
            "returncode": 0,
            "stdout": "",
            "stderr": "",
        }
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _next(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((op, dict(args)))
        if self._script:
            return self._script.pop(0)
        return dict(self._default)

    async def switch_version(self, *, target_version: str) -> dict[str, Any]:
        return self._next("switch_version", {"target_version": target_version})

    async def migrate(self) -> dict[str, Any]:
        return self._next("migrate", {})

    async def stop(self) -> dict[str, Any]:
        return self._next("stop", {})

    async def rollback(self, *, target_version: str) -> dict[str, Any]:
        return self._next("rollback", {"target_version": target_version})


__all__ = [
    "ContainerExecutor",
    "DockerComposeExecutor",
    "FakeContainerExecutor",
]