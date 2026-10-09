"""In-memory login rate limiter.

Production deployments behind multiple workers should swap
this for a Redis / DB-backed implementation; the API is the
same so the caller doesn't change.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class RateLimitConfig:
    max_attempts: int = 5
    window_seconds: int = 60
    lockout_seconds: int = 300


class RateLimiter:
    """Sliding-window counter keyed by ``(scope, key)``."""

    def __init__(self, config: RateLimitConfig | None = None) -> None:
        self._config = config or RateLimitConfig()
        self._attempts: dict[tuple[str, str], deque[float]] = {}
        self._lockouts: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    def _now(self) -> float:
        return time.monotonic()

    def is_locked(self, scope: str, key: str) -> bool:
        with self._lock:
            until = self._lockouts.get((scope, key))
            if until is None:
                return False
            if self._now() >= until:
                self._lockouts.pop((scope, key), None)
                self._attempts.pop((scope, key), None)
                return False
            return True

    def lockout_remaining(self, scope: str, key: str) -> float:
        with self._lock:
            until = self._lockouts.get((scope, key))
            if until is None:
                return 0.0
            return max(0.0, until - self._now())

    def record_attempt(self, scope: str, key: str, *, success: bool) -> None:
        with self._lock:
            now = self._now()
            attempts = self._attempts.setdefault((scope, key), deque())
            # prune window
            while attempts and now - attempts[0] > self._config.window_seconds:
                attempts.popleft()
            if success:
                attempts.clear()
                self._lockouts.pop((scope, key), None)
                return
            attempts.append(now)
            if len(attempts) >= self._config.max_attempts:
                self._lockouts[(scope, key)] = now + self._config.lockout_seconds

    def reset(self, scope: str, key: str) -> None:
        with self._lock:
            self._attempts.pop((scope, key), None)
            self._lockouts.pop((scope, key), None)


__all__ = ["RateLimiter", "RateLimitConfig"]
