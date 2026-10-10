"""One cancellable owner for saved Telegram startup/runtime recovery.

No polling here: probes authenticate only; AppRuntime owns the sole listener.
Only sanitized categories leave this module. No DB/secret edits or MAX reconnects.
"""
from __future__ import annotations

import asyncio
import logging
import random

logger = logging.getLogger(__name__)


def failure_kind(exc):
    if getattr(exc, "permanent", False):
        return True, getattr(exc, "failure_class", "invalid_configuration")
    if exc is None:
        return True, "no_configured_transport"
    name = type(exc).__name__
    if name in {"TelegramUnauthorizedError", "TokenValidationError", "AuthKeyError", "ApiIdInvalidError"}:
        return True, "invalid_credentials"
    if isinstance(exc, (ValueError, TypeError)):
        return True, "invalid_configuration"
    if name in {"ProxyError", "ProxyConnectionError", "TelegramNetworkError"}:
        # Classify proxy authentication without ever returning external text.
        text = str(exc).lower()
        if "407" in text or "authentication" in text or "auth method" in text:
            return True, "proxy_credentials_rejected"
    return False, name if name in {"TimeoutError", "OSError", "ConnectionError", "TelegramNetworkError", "TelegramServerError", "TelegramRetryAfter"} else "transport_failure"


async def proxy_accepting(proxy, *, timeout=2.0):
    """TCP dependency readiness only; authenticated getMe proves actual usability."""
    reader, writer = await asyncio.wait_for(asyncio.open_connection(proxy.host, proxy.port), timeout)
    writer.close()
    await asyncio.wait_for(writer.wait_closed(), timeout)


class TelegramRecovery:
    def __init__(self, runtime, *, restore, health_interval=15.0, initial_delay=2.0,
                 retry_cap=60.0, permanent_delay=300.0, jitter=None, prepare=None):
        self.runtime = runtime
        self.restore = restore
        self.prepare = prepare
        self.health_interval = health_interval
        self.initial_delay = initial_delay
        self.retry_cap = retry_cap
        self.permanent_delay = permanent_delay
        self.jitter = jitter or (lambda delay: random.uniform(0.8, 1.2) * delay)
        self.task = None
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._closed = False
        self.status = {"state": "CONNECTING", "attempts": 0, "failure": None, "retry_in_seconds": None}

    def start(self):
        if not self._closed and (self.task is None or self.task.done()):
            self.task = asyncio.create_task(self.run(), name="shmax.telegram.recovery")

    def trigger(self):
        self._wake.set()

    async def attempt(self):
        async with self._lock:
            if self._closed or self.runtime._shutdown.is_set():
                return {"restored": False, "state": "DOWN", "detail": "shutting_down"}
            if self.prepare is not None:
                await self.prepare()
            adapter = self.runtime.telegram_adapter
            # Healthy polling is left untouched. This check never selects a new
            # transport or starts a second consumer. Actual health is bounded.
            if adapter.is_real() and not self.runtime._telegram_failure:
                try:
                    health = await asyncio.wait_for(adapter.health(), timeout=10)
                    if health.get("connected") and health.get("authenticated") and health.get("polling") is not False:
                        self.status.update(state="HEALTHY", failure=None, retry_in_seconds=None)
                        return {"restored": True, "state": "HEALTHY", "detail": "listener operational"}
                    if health.get("polling") is True:
                        # aiogram owns retries of its existing long-poll. A failed
                        # health getMe must not replace a still-live consumer.
                        self.status.update(state="DEGRADED", failure="telegram_unreachable")
                        return {"restored": False, "state": "DEGRADED", "detail": "telegram_unreachable"}
                except Exception:
                    polling = getattr(adapter, "_polling_task", None)
                    if polling is not None and not polling.done():
                        self.status.update(state="DEGRADED", failure="health_probe_unavailable")
                        return {"restored": False, "state": "DEGRADED", "detail": "health_probe_unavailable"}
            self.status.update(state="CONNECTING", retry_in_seconds=None)
            self.status["attempts"] += 1
            result = await self.restore()
            state = result["state"]
            self.status.update(state=state, failure=None if result["restored"] else result["detail"])
            logger.info("telegram recovery state=%s attempt=%s failure=%s", state, self.status["attempts"], self.status["failure"])
            return result

    async def run(self):
        delay = self.initial_delay
        while not self._closed:
            try:
                result = await self.attempt()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                permanent, category = failure_kind(exc)
                result = {"restored": False, "state": "AUTH_REQUIRED" if permanent else "DEGRADED", "detail": category}
                self.status.update(state=result["state"], failure=category)
            if result["restored"]:
                delay = self.initial_delay
                wait = self.health_interval
            elif result["state"] in {"NOT_CONFIGURED", "AUTH_REQUIRED", "ERROR"}:
                wait = self.permanent_delay
            else:
                wait = min(self.retry_cap, max(0.01, self.jitter(delay)))
                delay = min(self.retry_cap, delay * 2)
            self.status["retry_in_seconds"] = wait
            # Wake is cleared before waiting, without cancelling an in-progress
            # attempt. API triggers coalesce; at most one task/attempt exists.
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def close(self):
        self._closed = True
        self._wake.set()
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
