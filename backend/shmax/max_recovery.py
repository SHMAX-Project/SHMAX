"""One owner for saved-session reconnect and listener generations; never SMS."""

from __future__ import annotations
import asyncio
import random
import time
from .adapters.max.errors import MaxAuthError, MaxSessionError


class MaxRecovery:
    def __init__(
        self,
        runtime,
        *,
        initial_delay=2.0,
        retry_cap=60.0,
        health_interval=5.0,
        timeout=20.0,
        jitter=None,
    ):
        self.runtime = runtime
        self.initial_delay = initial_delay
        self.retry_cap = retry_cap
        self.health_interval = health_interval
        self.timeout = timeout
        self.jitter = jitter or (lambda d: random.uniform(0.8, 1.2) * d)
        self.task = None
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._force = False
        self._closed = False
        self.status = {
            "state": "CONNECTING",
            "attempts": 0,
            "failure": None,
            "retry_in_seconds": None,
            "last_success_at": None,
        }

    def start(self):
        if not self._closed and (self.task is None or self.task.done()):
            self.task = asyncio.create_task(self.run(), name="shmax.max.recovery")

    def trigger(self):
        self._force = True
        self._wake.set()

    def notify_authenticated(self):
        self.status.update(state="CONNECTING", failure=None)
        self._wake.set()

    def _emit(self, typ, **kw):
        self.runtime.journal.emit(typ, component="max", **kw)

    def listener_alive(self):
        sup = self.runtime._supervisors.get("max_listener")
        return bool(
            sup
            and sup._task
            and not sup._task.done()
            and self.runtime._max_listener_generation
            == getattr(self.runtime.max_adapter, "generation", None)
        )

    async def attempt(self):
        async with self._lock:
            rt = self.runtime
            adapter = rt.max_adapter
            if self._closed or rt._shutdown.is_set():
                return False
            auth_task = getattr(getattr(rt, "max_auth", None), "_auth_task", None)
            if auth_task is not None and not auth_task.done():
                self.status.update(state="CONNECTING", failure=None)
                return False
            health = await asyncio.wait_for(adapter.health(), timeout=5)
            if not health.get("session_available"):
                self.status.update(
                    state="AUTH_REQUIRED", failure="saved_session_unavailable"
                )
                return False
            forced = self._force
            self._force = False
            if (
                not forced
                and not rt._integration_probes.get("max", {}).get("error")
                and health.get("connected")
                and health.get("authenticated")
                and self.listener_alive()
            ):
                self.status.update(
                    state="HEALTHY",
                    failure=None,
                    retry_in_seconds=None,
                    last_success_at=self.status["last_success_at"] or int(time.time()),
                )
                return True
            if self.status["state"] == "HEALTHY":
                self._emit("max.disconnected", aggregate=True)
            self.status.update(state="RECONNECTING", retry_in_seconds=None)
            self.status["attempts"] += 1
            self._emit(
                "max.reconnect_attempt", attempt=self.status["attempts"], aggregate=True
            )
            await rt.stop_max_listener()
            try:
                await asyncio.wait_for(adapter.disconnect(), timeout=5)
                network=getattr(rt,'max_network',None)
                if network is not None:await network.ensure()
                # This method installs an auth flow which rejects *before* SMS is
                # requested. A saved file alone does not prove it contains a token.
                connect = getattr(adapter, "connect_saved", None)
                if connect is None:
                    raise MaxSessionError("saved-session recovery unsupported")
                await asyncio.wait_for(connect(), timeout=self.timeout)
                await asyncio.wait_for(adapter.get_chats(), timeout=self.timeout)
                await rt.start_max_listener()
                await asyncio.sleep(0)
                if not self.listener_alive():
                    raise ConnectionError("listener unavailable")
                self.status.update(
                    state="HEALTHY", failure=None, last_success_at=int(time.time())
                )
                self._emit("max.reconnect_success", result="success")
                await rt.probe_integration("max")
                return True
            except asyncio.CancelledError:
                await asyncio.wait_for(adapter.disconnect(), timeout=5)
                raise
            except (MaxAuthError, MaxSessionError) as exc:
                await asyncio.wait_for(adapter.disconnect(), timeout=5)
                self.status.update(state="AUTH_REQUIRED", failure=type(exc).__name__)
                self._emit(
                    "max.auth_required",
                    error_class=type(exc).__name__,
                    aggregate=True,
                    result="permanent",
                )
                return False
            except Exception as exc:
                await asyncio.wait_for(adapter.disconnect(), timeout=5)
                self.status.update(
                    state="VERIFICATION_UNAVAILABLE", failure=type(exc).__name__
                )
                self._emit(
                    "max.reconnect_failure",
                    error_class=type(exc).__name__,
                    aggregate=True,
                    result="temporary",
                )
                return False

    async def run(self):
        delay = self.initial_delay
        while not self._closed:
            try:
                ok = await self.attempt()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                ok = False
                self.status.update(
                    state="VERIFICATION_UNAVAILABLE", failure=type(exc).__name__
                )
                self._emit(
                    "max.reconnect_failure",
                    error_class=type(exc).__name__,
                    aggregate=True,
                )
            if ok:
                delay = self.initial_delay
                wait = self.health_interval
            elif self.status["state"] == "AUTH_REQUIRED":
                wait = None
            else:
                wait = min(self.retry_cap, self.jitter(delay))
                delay = min(self.retry_cap, delay * 2)
            self.status["retry_in_seconds"] = wait
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def close(self):
        self._closed = True
        self._wake.set()
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
