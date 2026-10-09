"""Secret-input providers for PyMax.

These providers exist so that ``PyMaxMaxAdapter`` itself never
logs, prints, or persists the phone / SMS / 2FA values. PyMax
calls ``get_code()`` and ``get_password()`` at most once each
per authentication, and we read them directly from the user at
that moment.

Three families are provided:

* :class:`InteractiveSmsProvider` / :class:`InteractivePasswordProvider`
  — prompt on stdin. Used for developer shells, CI scripts, and
  legacy behaviour. They MUST NOT be wired into a long-running
  SHMAX runtime (Docker has no controlling terminal).
* :class:`QueuedSmsProvider` / :class:`QueuedPasswordProvider` —
  wait on a ``CredentialFuture`` that the API layer fulfils via
  ``/api/max/auth/code`` / ``/api/max/auth/password``. This is
  the production path: secrets stay in RAM, never touch stdin,
  never get logged.
* :class:`StaticSmsProvider` / :class:`StaticPasswordProvider`
  — fixed codes for tests and one-shot scripts.

The provider objects deliberately have no ``__repr__`` that
could leak the buffered secret.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import termios
from dataclasses import dataclass, field
from typing import Callable, Optional


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Static providers (tests / scripts)
# ---------------------------------------------------------------------
@dataclass
class StaticSmsProvider:
    """SMS-code provider with a pre-set code.

    Useful for tests and one-off scripts; production must use
    :class:`QueuedSmsProvider` instead.
    """

    code: Optional[str] = None
    prompt: str = "(unused)"

    async def get_code(self, phone: str) -> str:
        if self.code is None:
            raise RuntimeError("StaticSmsProvider has no code set")
        return self.code

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "StaticSmsProvider(code=<redacted>)"


@dataclass
class StaticPasswordProvider:
    """2FA password provider with a pre-set password."""

    password: Optional[str] = None

    async def get_password(self, hint: Optional[str] = None) -> str:
        if self.password is None:
            raise RuntimeError("StaticPasswordProvider has no password set")
        return self.password

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "StaticPasswordProvider(password=<redacted>)"


# ---------------------------------------------------------------------
# Queued providers (production HTTP path)
# ---------------------------------------------------------------------
class CredentialFuture:
    """Single-shot async credential slot.

    PyMax calls :py:meth:`SmsProvider.get_code` and
    :py:meth:`PasswordProvider.get_password` exactly once per
    authentication round. The API layer fulfils the future via
    :py:meth:`fulfill`; if the user starts a new login while a
    future is still pending we reject the stale one via
    :py:meth:`reject`.

    A ``on_waiting`` callback can be installed to notify the auth
    state machine the *instant* PyMax reaches the credential
    await. This is the authoritative signal that the upstream
    server accepted the verification request and SHMAX can now
    legally transition to ``WAITING_CODE`` / ``WAITING_PASSWORD``.
    """

    def __init__(self, *, kind: str) -> None:
        self.kind = kind  # "sms" | "password"
        self._future: Optional[asyncio.Future[str]] = None
        self._generation = 0
        self._on_waiting: Optional[Callable[[str, int], None]] = None
        self._on_invalidated: Optional[Callable[[], None]] = None

    def set_callbacks(
        self,
        *,
        on_waiting: Optional[Callable[[str, int], None]] = None,
        on_invalidated: Optional[Callable[[], None]] = None,
    ) -> None:
        """Install (or clear) lifecycle callbacks.

        ``on_waiting`` is fired inside :py:meth:`wait` *before*
        the credential future is awaited. It receives the kind
        plus the *future generation* the wait belongs to, so the
        auth state machine can drop publications from a stale
        round (audit P1 #2: an old auth task must never overwrite
        the state of a newer one).

        ``on_invalidated`` is fired when an in-flight future is
        cancelled by a new login or by ``cancel()``.
        """
        self._on_waiting = on_waiting
        self._on_invalidated = on_invalidated

    def _next_generation(self) -> int:
        self._generation += 1
        return self._generation

    def current_generation(self) -> int:
        return self._generation

    async def wait(self, *, generation: int, timeout: float = 120.0) -> str:
        # A stale generation means the auth state machine has
        # already moved past this future (e.g. cancelled /
        # resend). Fail fast with a RuntimeError so callers do
        # not silently hang on an orphaned credential slot.
        if generation != self._generation:
            raise RuntimeError(
                f"stale generation: caller={generation} current={self._generation}"
            )
        loop = asyncio.get_running_loop()
        self._future = loop.create_future()
        # Notify the state machine *before* awaiting so
        # WAITING_CODE / WAITING_PASSWORD can be published
        # synchronously — PyMax has finished the upstream
        # handshake / request_code by the time we get here, and
        # will block until the future is fulfilled.
        # P1 #2: the callback receives the generation captured
        # when the PROVIDER was created — reading the shared
        # current generation here is too late (a concurrent
        # invalidate bumps it between the capture and the
        # publish, letting a stale round speak for the new one).
        cb = self._on_waiting
        if cb is not None:
            try:
                cb(self.kind, generation)
            except Exception:  # noqa: BLE001
                logger.exception("CredentialFuture.on_waiting callback raised")
        try:
            return await asyncio.wait_for(self._future, timeout=timeout)
        except asyncio.TimeoutError:
            self._future = None
            raise
        finally:
            # Caller is responsible for invalidating the
            # generation when starting a new flow; we keep the
            # reference so a fulfill() with a stale generation
            # raises immediately.
            del generation  # silence unused warning

    def fulfill(self, *, generation: int, value: str) -> bool:
        if generation != self._generation:
            return False
        future = self._future
        if future is None or future.done():
            return False
        future.set_result(value)
        return True

    def reject(self, *, generation: int, reason: str = "rejected") -> bool:
        if generation != self._generation:
            return False
        future = self._future
        if future is None or future.done():
            return False
        future.set_exception(RuntimeError(reason))
        return True

    def invalidate(self, *, generation: int | None = None) -> None:
        # Cancel any pending future (e.g. when starting a new
        # login or shutting down).
        future = self._future
        if future is not None and not future.done():
            future.set_exception(RuntimeError("invalidated"))
        self._future = None
        if generation is None:
            self._generation = self._next_generation()
        else:
            self._generation = max(self._generation, generation)
        cb = self._on_invalidated
        if cb is not None:
            try:
                cb()
            except Exception:  # noqa: BLE001
                logger.exception("CredentialFuture.on_invalidated callback raised")


@dataclass
class QueuedSmsProvider:
    """SMS-code provider that waits on a :class:`CredentialFuture`.

    The provider does not log or echo the value. It is
    intentionally minimal: the API layer is responsible for
    putting the secret into the future.

    P1 #3 (surgical): the generation is captured AT CONSTRUCTION
    and is IMMUTABLY bound to this provider instance — one auth
    attempt owns one generation forever. Reading
    ``future.current_generation()`` at get_code() time let an OLD
    provider (invoked after invalidate) silently adopt the NEW
    generation and defeat stale-attempt protection.

    Note that :meth:`get_code` is invoked by PyMax *after* the
    upstream SmsAuthFlow has successfully called
    ``request_code(phone)`` and the server has accepted the
    verification request. This is the authoritative signal that
    the SHMAX auth state machine can transition to
    ``WAITING_CODE`` — installed via ``CredentialFuture.set_callbacks``.
    """

    future: CredentialFuture
    generation: int = -1  # captured at construction (immutable)

    def __post_init__(self) -> None:
        if self.generation == -1:
            self.generation = self.future.current_generation()

    async def get_code(self, phone: str) -> str:
        return await self.future.wait(generation=self.generation)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "QueuedSmsProvider(future=<redacted>)"


@dataclass
class QueuedPasswordProvider:
    """2FA password provider that waits on a :class:`CredentialFuture`.

    Invoked by PyMax after the upstream server has demanded 2FA.
    The transition to ``WAITING_PASSWORD`` is driven by the
    ``CredentialFuture.on_waiting`` callback (see
    :class:`QueuedSmsProvider` for the same pattern).

    P1 #3: generation captured at construction, immutable — same
    ownership rule as :class:`QueuedSmsProvider`.
    """

    future: CredentialFuture
    generation: int = -1  # captured at construction (immutable)

    def __post_init__(self) -> None:
        if self.generation == -1:
            self.generation = self.future.current_generation()

    async def get_password(self, hint: Optional[str] = None) -> str:
        return await self.future.wait(generation=self.generation)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "QueuedPasswordProvider(future=<redacted>)"


# ---------------------------------------------------------------------
# QR handler — exposes the upstream ``QrHandler`` protocol via a
# ``CredentialFuture`` so the HTTP API can render the QR URL
# without spinning up a terminal.
# ---------------------------------------------------------------------
@dataclass
class QueuedQrHandler:
    """QR handler that captures the upstream ``qr_url`` into a future.

    :class:`QrAuthFlow` invokes :py:meth:`show_qr` exactly once
    per round with the URL the MAX mobile app must scan. The
    SHMAX HTTP API awaits the future via
    :py:meth:`consume_url` and renders the URL itself.
    """

    future: "QrFuture"

    async def show_qr(self, qr_url: str) -> None:
        # PyMax invokes show_qr synchronously inside the auth
        # task; queue the URL and let the API retrieve it.
        self.future.put(qr_url)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "QueuedQrHandler(future=<redacted>)"


class QrFuture:
    """Single-shot QR URL slot.

    Slightly broader than :class:`CredentialFuture` because the
    QR URL must be returned (not just awaited) and is replaced
    on every fresh authentication round.
    """

    def __init__(self) -> None:
        self._future: Optional[asyncio.Future[str]] = None

    def put(self, value: str) -> None:
        future = self._future
        if future is None or future.done():
            return
        future.set_result(value)

    async def consume_url(self, *, timeout: float = 60.0) -> str:
        loop = asyncio.get_running_loop()
        self._future = loop.create_future()
        return await asyncio.wait_for(self._future, timeout=timeout)

    def invalidate(self) -> None:
        future = self._future
        if future is not None and not future.done():
            future.set_exception(RuntimeError("invalidated"))
        self._future = None


# ---------------------------------------------------------------------
# Interactive providers (developer shells, CI, legacy)
# ---------------------------------------------------------------------
@dataclass
class InteractiveSmsProvider:
    """SMS-code provider.

    If ``code`` is supplied at construction, PyMax will use it
    directly. Otherwise the provider prompts on stdin.

    The provider never prints the code itself. PyMax's own
    console prompt is bypassed entirely when this provider is
    attached, which removes one surface where a stray log line
    could leak the secret.
    """

    code: Optional[str] = None
    prompt: str = "Enter MAX SMS code: "

    async def get_code(self, phone: str) -> str:
        # We deliberately do not log ``phone`` either.
        if self.code is not None:
            return self.code
        return await asyncio.get_event_loop().run_in_executor(
            None, _read_secret_line, self.prompt
        )

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "InteractiveSmsProvider(code=<redacted>)"


@dataclass
class InteractivePasswordProvider:
    """2FA password provider."""

    password: Optional[str] = None
    prompt: str = "Enter MAX 2FA password: "

    async def get_password(self, hint: Optional[str] = None) -> str:
        # ``hint`` is what the server tells the user; we echo it
        # so the user knows which account is being asked, but we
        # never print the password we read back.
        if hint:
            print(f"[shmax] 2FA hint: {hint}", file=sys.stderr)
        if self.password is not None:
            return self.password
        return await asyncio.get_event_loop().run_in_executor(
            None, _read_secret_line, self.prompt
        )

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "InteractivePasswordProvider(password=<redacted>)"


def _read_secret_line(prompt: str) -> str:
    """Read one line of stdin, optionally without echoing.

    On POSIX we use ``termios`` to disable echo; on platforms
    without ``termios`` we fall back to plain input and rely on
    the user's terminal handling.

    When stdin is not a TTY (e.g. piped or redirected input),
    ``termios.tcgetattr`` raises ``termios.error``. In that case
    we fall back to a plain ``readline`` so that scripted /
    redirected invocations still work — the probe will simply
    not hide the secret in those runs.
    """
    sys.stdout.write(prompt)
    sys.stdout.flush()
    try:
        import tty

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            buf: list[str] = []
            while True:
                ch = sys.stdin.read(1)
                if ch in ("\r", "\n"):
                    break
                if ch == "\x03":  # Ctrl+C
                    raise KeyboardInterrupt
                if ch == "\x7f" or ch == "\x08":  # backspace
                    if buf:
                        buf.pop()
                    continue
                buf.append(ch)
            sys.stdout.write("\n")
            sys.stdout.flush()
            return "".join(buf)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except (ImportError, AttributeError):
        # Non-POSIX: plain readline.
        return sys.stdin.readline().rstrip("\n").rstrip("\r")
    except termios.error:
        # stdin is not a TTY (piped, redirected, subprocess
        # without a controlling terminal). Fall back to a
        # plain ``readline`` so the probe still works for
        # scripted / redirected runs.
        try:
            return sys.stdin.readline().rstrip("\n").rstrip("\r")
        except EOFError:
            raise


__all__ = [
    "CredentialFuture",
    "InteractiveSmsProvider",
    "InteractivePasswordProvider",
    "QueuedSmsProvider",
    "QueuedPasswordProvider",
    "QueuedQrHandler",
    "QrFuture",
    "StaticSmsProvider",
    "StaticPasswordProvider",
]
