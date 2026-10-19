"""Xray child-process runtime (surgical acceptance, item 7).

Owns EXACTLY ONE Xray child process. The Telegram Telegram adapter
talks to its local SOCKS5 inbound (127.0.0.1:<port>) — the port is
never exposed on a LAN interface. The runtime config exists only
ephemerally (0600); secrets never touch SQLite or logs.

Bounded start/stop/restart; the application stays alive if Xray
fails (Telegram becomes DEGRADED). No infinite restart loop — the
manager never spawns a replacement without an explicit caller.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Bounded lifecycle (seconds).
START_TIMEOUT = 10.0
STOP_TIMEOUT = 5.0
HEALTH_PROBE_TIMEOUT = 5.0


class XrayError(RuntimeError):
    pass


# Orphan guard: every started manager registers here; an atexit hook
# synchronously terminates children so a dying parent (test runner,
# uvicorn teardown) never leaves a stub/real xray holding the SOCKS
# port.
_LIVE_MANAGERS: "set[XrayRuntimeManager]" = set()


def _terminate_all() -> None:
    for mgr in list(_LIVE_MANAGERS):
        proc = mgr._process
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except (OSError, ProcessLookupError):
                pass


import atexit as _atexit

_atexit.register(_terminate_all)


def _chain_signal(sig: int) -> None:
    """Terminate xray children, then run the previous handler.

    SIGTERM's default action kills the process WITHOUT running
    atexit hooks — orphaning the child. Chaining keeps teardown
    complete for uvicorn / test-runner shutdowns.
    """
    import signal

    previous = signal.getsignal(sig)

    def _handler(signum, frame):
        _terminate_all()
        if callable(previous):
            previous(signum, frame)
        elif previous is signal.SIG_DFL:
            import signal as _signal

            _signal.signal(sig, _signal.SIG_DFL)
            os.kill(os.getpid(), sig)

    try:
        signal.signal(sig, _handler)
    except ValueError:
        # Not in the main thread — skip (main-thread import only).
        pass


try:
    import signal as _signal_mod

    _chain_signal(_signal_mod.SIGTERM)
    _chain_signal(_signal_mod.SIGINT)
except Exception:  # noqa: BLE001
    pass


@dataclass
class XrayStatus:
    state: str                 # "not_configured" | "starting" | "running" | "stopped" | "failed"
    detail: str = ""
    socks_port: Optional[int] = None
    pid: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "detail": self.detail,
            "socks_port": self.socks_port,
            "pid": self.pid,
        }


@dataclass(eq=False)  # identity hash: managers live in a set
class XrayRuntimeManager:
    """Single-owner Xray child process manager."""

    binary_path: str
    config_dir: Path = field(default_factory=lambda: Path("/run/shmax/xray"))
    socks_port: int = 10808

    _process: Optional[asyncio.subprocess.Process] = field(
        default=None, repr=False
    )
    _config_path: Optional[Path] = field(default=None, repr=False)
    _last_exit: Optional[int] = field(default=None, repr=False)
    _supervisor_task: Optional[asyncio.Task] = field(default=None, repr=False)
    # Surgical review: start/stop/restart are SERIALIZED — concurrent
    # start() can never launch two children.
    _lifecycle_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    # ------------------------------------------------------------------
    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    def status(self) -> XrayStatus:
        if self.is_running:
            return XrayStatus(
                state="running",
                socks_port=self.socks_port,
                pid=self._process.pid,
            )
        if self._last_exit is not None and self._last_exit != 0:
            return XrayStatus(
                state="failed",
                detail=f"xray exited with code {self._last_exit}",
                socks_port=self.socks_port,
            )
        if self._config_path is not None:
            return XrayStatus(state="stopped", socks_port=self.socks_port)
        return XrayStatus(state="not_configured")

    # ------------------------------------------------------------------
    def _write_ephemeral_config(self, config: dict) -> Path:
        """Write the runtime config to the ephemeral dir with 0600.

        /run/shmax/xray is preferred; if it is not writable (e.g.
        dev host without root) a private temp dir is used.
        """
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            target = self.config_dir / "config.json"
            probe = target.parent / ".probe"
            probe.write_text("")
            probe.unlink()
        except OSError:
            base = Path(tempfile.mkdtemp(prefix="shmax-xray-"))
            target = base / "config.json"
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            import json

            json.dump(config, fh, ensure_ascii=False)
        os.chmod(target, 0o600)
        self._config_path = target
        return target

    # ------------------------------------------------------------------
    async def start(self, config: dict) -> XrayStatus:
        """Start exactly one Xray process with ``config``.

        SERIALIZED (surgical review): concurrent start() calls wait
        on one lifecycle lock; the loser sees the running child and
        raises instead of spawning a second process.

        Readiness = the local SOCKS inbound actually ACCEPTS
        connections (bounded wait). "Process still alive" is NOT
        readiness: if SOCKS never appears, the start times out, the
        task is cancelled or the config is invalid, the created
        process is terminated/killed (bounded) and a failure is
        returned.
        """
        async with self._lifecycle_lock:
            return await self._start_locked(config)


    @staticmethod
    def _signal_child(proc, name: str) -> None:
        try:
            getattr(proc, name)()
        except ProcessLookupError:
            pass

    @staticmethod
    async def _poll_exit(proc, timeout: float) -> bool:
        """Poll returncode for up to ``timeout`` seconds; True when
        the child actually exited (reaped)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while proc.returncode is None:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.05, remaining))
        return True

    TERM_WAIT = 1.0
    KILL_WAIT = 3.0

    @staticmethod
    def _absorb_cancel() -> None:
        current = asyncio.current_task()
        if current is not None and hasattr(current, "uncancel"):
            while current.cancelling() > 0:
                current.uncancel()

    async def _reap_confirmed(self, proc) -> None:
        """THE cancellation-resistant cleanup primitive (limited
        repair): TERM → bounded wait → KILL → reap CONFIRMED.

        Absorbs UNLIMITED repeated cancellations — every fresh
        cancel is uncanceled and the wait resumes; this coroutine
        NEVER returns (or raises) before ``proc.returncode`` is
        set. SIGKILL cannot be ignored by the child, so the poll
        loop always terminates once the kernel finishes the reap.
        """
        sent_term = sent_kill = False
        while proc.returncode is None:
            try:
                if not sent_term:
                    sent_term = True
                    self._signal_child(proc, "terminate")
                if await self._poll_exit(proc, self.TERM_WAIT):
                    break
                if not sent_kill:
                    sent_kill = True
                    self._signal_child(proc, "kill")
                if await self._poll_exit(proc, self.KILL_WAIT):
                    break
                logger.error(
                    "xray pid %s survived SIGKILL wait — keep polling",
                    proc.pid,
                )
                await self._poll_exit(proc, self.KILL_WAIT)
            except asyncio.CancelledError:
                # Repeated cancellation mid-cleanup: absorb it and
                # resume the reap. Signals already sent are not
                # lost; nothing re-starts from scratch.
                self._absorb_cancel()

    async def _drain_stderr(self,proc):
        import time
        last_log=0.0
        try:
            while True:
                chunk=await proc.stderr.read(4096)
                if not chunk:return
                text=chunk.decode(errors="replace").lower()
                category=("TLSFailure" if "tls" in text or "certificate" in text else
                          "DNSFailure" if "dns" in text or "resolve" in text else
                          "TimeoutError" if "timeout" in text or "timed out" in text else "TransportUnavailable")
                journal=getattr(self,"journal",None)
                if journal:journal.emit("xray.transport_failure",component="xray",error_class=category,aggregate=True)
                if time.monotonic()-last_log>30:
                    logger.warning("xray diagnostic category=%s",category);last_log=time.monotonic()
        except asyncio.CancelledError:raise
        except Exception:
            logger.warning("xray diagnostic reader stopped")

    async def _start_locked(self, config: dict) -> XrayStatus:
        if self.is_running:
            raise XrayError("xray already running — stop() first")
        if not self.binary_path or not Path(self.binary_path).exists():
            raise XrayError(f"xray binary not found: {self.binary_path!r}")

        config_path = self._write_ephemeral_config(config)
        # NO shell=True: fixed argv, binary path from config.
        argv = [str(self.binary_path), "run", "-c", str(config_path)]
        try:
            self._process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, PermissionError) as e:
            self._last_exit = -1
            raise XrayError(f"failed to spawn xray: {e}") from e

        self._stderr_task=asyncio.create_task(self._drain_stderr(self._process),name="shmax.xray.stderr")
        try:
            exited = await self._wait_exit_or_stay(START_TIMEOUT)
        except asyncio.CancelledError:
            # Cancellation after spawn: the cancellation-resistant
            # primitive owns the process until the child is
            # confirmed reaped, THEN the original cancellation is
            # re-raised.
            proc = self._process
            self._process = None
            _LIVE_MANAGERS.discard(self)
            if proc is not None:
                await self._reap_confirmed(proc)
            raise
        if exited:
            self._last_exit = self._process.returncode
            self._process = None
            raise XrayError(f"xray exited during startup (code {self._last_exit})")
        # REAL readiness: local SOCKS must accept connections.
        try:
            socks_ready = await self._wait_socks_ready(START_TIMEOUT)
        except asyncio.CancelledError:
            proc = self._process
            self._process = None
            _LIVE_MANAGERS.discard(self)
            if proc is not None:
                await self._reap_confirmed(proc)
            raise
        if not socks_ready:
            logger.warning(
                "xray SOCKS 127.0.0.1:%s never appeared — terminating",
                self.socks_port,
            )
            await self._terminate_bounded()
            raise XrayError(
                f"xray local SOCKS 127.0.0.1:{self.socks_port} "
                "did not become ready in time"
            )
        logger.info(
            "xray started pid=%s socks=127.0.0.1:%s",
            self._process.pid, self.socks_port,
        )
        _LIVE_MANAGERS.add(self)
        return self.status()

    async def _wait_socks_ready(self, timeout: float) -> bool:
        elapsed = 0.0
        while elapsed < timeout:
            if self._process is None or self._process.returncode is not None:
                return False
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", self.socks_port),
                    timeout=0.5,
                )
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:  # noqa: BLE001
                    pass
                return True
            except (OSError, asyncio.TimeoutError):
                await asyncio.sleep(0.2)
                elapsed += 0.7
        return False

    async def _terminate_bounded(self) -> None:
        """Terminate then kill the child via the cancellation-
        resistant primitive; never returns before the reap is
        confirmed."""
        proc = self._process
        if proc is None:
            return
        await self._reap_confirmed(proc)
        self._finish_stop(proc)

    async def _wait_exit_or_stay(self, timeout: float) -> bool:
        """True when the process EXITED within ``timeout``; False if
        still running (healthy start)."""
        try:
            await asyncio.wait_for(self._process.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ------------------------------------------------------------------
    async def stop(self) -> None:
        """Stop the child, bounded. Safe to call when not running.
        Serialized with start/restart."""
        async with self._lifecycle_lock:
            await self._stop_locked()
            return

    async def _stop_locked(self) -> None:
        """TERM → bounded wait → (KILL via the cancellation-resistant
        primitive) → reap CONFIRMED. The manager keeps the process
        handle UNTIL the child is actually reaped; ANY cancellation
        during stop (including repeated ones) is resolved by
        :meth:`_reap_confirmed` before the cancellation propagates."""
        proc = self._process
        if proc is None:
            _LIVE_MANAGERS.discard(self)
            return
        if proc.returncode is not None:
            self._finish_stop(proc)
            return
        self._signal_child(proc, "terminate")
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.CancelledError:
            # Outer timeout cancelled stop: cleanup wins — reap is
            # confirmed first, THEN the cancellation re-raises.
            await self._reap_confirmed(proc)
            self._finish_stop(proc)
            logger.info("xray stopped after cancelled stop (reaped)")
            raise
        except asyncio.TimeoutError:
            logger.warning("xray did not exit in 2s — killing")
            await self._reap_confirmed(proc)
        self._finish_stop(proc)
        logger.info("xray stopped (reaped)")

    def _finish_stop(self, proc) -> None:
        reader=getattr(self,"_stderr_task",None)
        if reader is not None:
            reader.cancel()
            self._stderr_task=None
        """Clear manager state AFTER the child is confirmed dead."""
        # A negative code means our own SIGTERM — the expected stop
        # path, not a crash.
        self._last_exit = (
            proc.returncode if proc.returncode and proc.returncode > 0 else None
        )
        self._process = None
        _LIVE_MANAGERS.discard(self)

    # ------------------------------------------------------------------
    async def restart(self, config: dict) -> XrayStatus:
        # Single lock acquisition: stop then start atomically — no
        # window where another caller can spawn a second child.
        async with self._lifecycle_lock:
            await self._stop_locked()
            return await self._start_locked(config)

    # ------------------------------------------------------------------
    async def health(self) -> XrayStatus:
        """Cheap liveness: process alive + local SOCKS port accepts.

        Never spawns a replacement process (no restart loop).
        """
        if not self.is_running:
            return self.status()
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", self.socks_port),
                timeout=HEALTH_PROBE_TIMEOUT,
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            return self.status()
        except (OSError, asyncio.TimeoutError):
            return XrayStatus(
                state="failed",
                detail="xray process alive but local SOCKS not answering",
                socks_port=self.socks_port,
                pid=self._process.pid,
            )

    # ------------------------------------------------------------------
    async def test_config(self, config: dict, *, timeout: float = 15.0) -> tuple[bool, str]:
        """Validate a generated config with the PINNED Xray binary's
        config-test mode (``xray run -test -c file``). Bounded.
        Returns (ok, sanitized detail). Never spawns a long-lived
        child."""
        config_path = self._write_ephemeral_config(config)
        argv = [str(self.binary_path), "run", "-test", "-c", str(config_path)]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except (OSError, PermissionError) as e:
            return False, f"xray binary unavailable: {type(e).__name__}"
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            # Timeout: the child is a real spawned process — run the
            # cancellation-resistant primitive (TERM→KILL→confirmed
            # reap) BEFORE reporting the timeout; a cancellation
            # arriving DURING this cleanup cannot leave the child
            # alive.
            await self._reap_confirmed(proc)
            return False, "config test timed out"
        except asyncio.CancelledError:
            # Cancellation after confirmed spawn: keep ownership of
            # the handle until the child is confirmed reaped (any
            # number of repeated cancellations absorbed by the
            # primitive), then re-raise the original cancellation.
            await self._reap_confirmed(proc)
            raise
        if proc.returncode == 0:
            return True, "config ok"
        text = out.decode(errors="replace")
        # Sanitized: keep only the last meaningful line.
        last = [l for l in text.splitlines() if l.strip()]
        return False, (last[-1] if last else "config rejected")[:200]

    async def test_node(self, config: dict, *, timeout: float = 12.0) -> tuple[bool, str]:
        """Test a node config: restart xray with it, confirm the local
        SOCKS answers, then report. Caller decides whether to keep
        the node (does not restore the previous config itself)."""
        await self.restart(config)
        status = await self.health()
        if status.state == "running":
            return True, "Telegram доступен через выбранный узел"
        return False, f"Прокси недоступен: {status.detail or status.state}"


__all__ = ["XrayRuntimeManager", "XrayStatus", "XrayError",
           "START_TIMEOUT", "STOP_TIMEOUT"]
