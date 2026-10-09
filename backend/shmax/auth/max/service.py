"""MAX auth state machine.

Drives a real :class:`PyMaxMaxAdapter` through interactive
SMS / password prompts from the HTTP API instead of stdin.

State machine
=============

::

    NOT_STARTED
        |
        | start(phone)
        v
    CONNECTING        (background task: connect() in flight)
        |
        | adapter.connect() returned, SmsAuthFlow reached
        | request_code() boundary
        v
    REQUESTING_CODE   (request_code(phone) in flight to MAX)
        |
        | MAX accepted verification request -> SmsAuthFlow
        | invokes SmsCodeProvider.get_code(phone)
        v
    WAITING_CODE      (server has accepted, PyMax is waiting
                       for the operator to submit the OTP)
        |
        | submit code (POST /api/max/auth/code)
        v
    VERIFYING_CODE    (send_code round-trip in flight)
        |
        | server demands 2FA -> PasswordProvider.get_password()
        v
    WAITING_PASSWORD  (operator must submit 2FA password)
        |
        | submit password
        v
    AUTHENTICATED     (terminal; profile available)
        |
    RATE_LIMITED      (server-side cooldown; UI shows countdown)
        |
    FAILED            (terminal; needs operator decision)

WAITING_CODE / WAITING_PASSWORD are driven by the
``CredentialFuture.on_waiting`` callback, which fires the
instant PyMax invokes ``SmsCodeProvider.get_code`` /
``PasswordProvider.get_password``. By upstream contract,
those providers are only called AFTER the corresponding
upstream ``request_code`` / ``login_with_password`` has
returned successfully, so the callback is the authoritative
signal that the server has accepted the verification round.
Nothing before that callback may legally transition to
``WAITING_CODE`` / ``WAITING_PASSWORD``.

Persistence
===========

The current state, phone hint and last error are kept in a
file-backed store (``MaxAuthStateStore``) so the UI can recover
across restarts. The state file deliberately holds NO secrets
(SMS code, password). The :class:`CredentialFuture` keeps the
pending secret only in RAM and is invalidated on restart.

Thread safety
=============

The service is built around an ``asyncio.Lock``. The state
machine only ever advances through coroutines that hold the
lock, so two concurrent ``/api/max/auth/*`` calls cannot race
each other.

Auth task lifecycle
===================

There is at most one live auth coroutine per account setup.
``start`` cancels any in-flight task before launching a fresh
one. ``resend`` does the same and additionally invalidates
the credential futures and disconnects the adapter so the
next ``connect()`` triggers a clean ``request_code``. ``cancel``
performs the same cleanup and resets the state to NOT_STARTED.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from ...adapters.max.base import MaxAdapter, MaxProfile
from ...adapters.max.errors import MaxAuthError, MaxError, MaxSessionError
from ...adapters.max.providers import (
    CredentialFuture,
    QueuedPasswordProvider,
    QueuedSmsProvider,
)
from ...util.phone import (
    NormalisedPhone,
    PhoneNormalisationError,
    parse as parse_phone,
)


logger = logging.getLogger(__name__)


class MaxAuthState(str, Enum):
    NOT_STARTED = "not_started"
    CONNECTING = "connecting"
    REQUESTING_CODE = "requesting_code"
    WAITING_CODE = "waiting_code"
    VERIFYING_CODE = "verifying_code"
    WAITING_PASSWORD = "waiting_password"
    # P0 #1 (surgical review): an OBSERVABLE state published while
    # the submitted password is being verified upstream. Without it
    # the UI treats waiting_password as stable, stops observing and
    # never learns about a late AUTHENTICATED.
    VERIFYING_PASSWORD = "verifying_password"
    AUTHENTICATED = "authenticated"
    RATE_LIMITED = "rate_limited"
    FAILED = "failed"


@dataclass
class MaxAuthSnapshot:
    state: MaxAuthState
    phone_hint: Optional[str]  # last 4 digits only
    account_id: Optional[str]
    display_name: Optional[str]
    handle: Optional[str]
    last_error: Optional[str]
    last_error_class: Optional[str]
    next_action: str  # "submit_code" | "submit_password" | "done" | "retry"
    updated_at: int


def _mask_phone(phone: str) -> str:
    """Return only the last 4 digits of ``phone``.

    The full phone is held in the live :class:`MaxAccount` row
    in the DB; the auth snapshot on disk only ever carries the
    masked form so a stolen status file cannot impersonate the
    user against MAX.
    """
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone)
    if len(digits) <= 4:
        return f"***{digits}"
    return f"***{digits[-4:]}"


def _normalize_phone(phone: str) -> str:
    """Canonicalise a phone to E.164.

    Delegates to :mod:`shmax.util.phone`, which uses Google's
    libphonenumber for authoritative country / length detection.
    The previous implementation mishandled three audit findings:

      - Russian trunk-8 prefix (``89643258888``) became
        ``+89643258888`` — country code 89 does not exist.
      - International numbers were assumed Russian.
      - Extensions were silently kept.

    Raises :class:`PhoneNormalisationError` with a friendly
    Russian message for every rejection path.
    """
    normalised = parse_phone(phone)
    return normalised.e164


class MaxAuthStateStore:
    """File-backed JSON store for the auth snapshot."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> Optional[MaxAuthSnapshot]:
        if not self._path.exists():
            return None
        try:
            raw = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        try:
            return MaxAuthSnapshot(
                state=MaxAuthState(raw["state"]),
                phone_hint=raw.get("phone_hint"),
                account_id=raw.get("account_id"),
                display_name=raw.get("display_name"),
                handle=raw.get("handle"),
                last_error=raw.get("last_error"),
                last_error_class=raw.get("last_error_class"),
                next_action=raw.get("next_action", "done"),
                updated_at=int(raw.get("updated_at", 0)),
            )
        except (KeyError, ValueError):
            return None

    def save(self, snap: MaxAuthSnapshot) -> None:
        data = {
            "state": snap.state.value,
            "phone_hint": snap.phone_hint,
            "account_id": snap.account_id,
            "display_name": snap.display_name,
            "handle": snap.handle,
            "last_error": snap.last_error,
            "last_error_class": snap.last_error_class,
            "next_action": snap.next_action,
            "updated_at": snap.updated_at,
        }
        # The store may be created against a non-existent parent
        # directory (test fixtures build it under a temp dir that
        # does not pre-create the file). Make sure the directory
        # exists before writing; otherwise the auth task crashes
        # and the test hangs forever waiting on a state transition.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, indent=2, sort_keys=True))

    def clear(self) -> None:
        if self._path.exists():
            self._path.unlink()


class MaxAuthService:
    """Production MAX auth state machine.

    Owns:

    * a live :class:`MaxAdapter` (PyMaxMaxAdapter or fake).
    * two :class:`CredentialFuture` instances — one for the SMS
      code, one for the 2FA password.
    * a :class:`MaxAuthStateStore` for restart survival.
    """

    _CODE_TIMEOUT = 120.0  # seconds
    _PASSWORD_TIMEOUT = 120.0
    # P1 #1: the connection/handshake BEFORE the provider is
    # invoked must also be bounded. Without this the UI can sit
    # in "Запрашиваем код у MAX…" forever when the MAX endpoint
    # is unreachable or the TLS handshake stalls.
    _CONNECT_TIMEOUT = 30.0
    # States in which a MAX auth round is considered "in
    # flight" — repeated start() calls while one of these is
    # active are a no-op so we never create another
    # Client / request_code sequence from a double-click or a
    # stale React fetch.
    _IN_FLIGHT_STATES: frozenset = frozenset({
        MaxAuthState.CONNECTING,
        MaxAuthState.REQUESTING_CODE,
        MaxAuthState.WAITING_CODE,
        MaxAuthState.VERIFYING_CODE,
        MaxAuthState.WAITING_PASSWORD,
    })

    def __init__(
        self,
        *,
        adapter: MaxAdapter,
        store: MaxAuthStateStore,
        sms_future: CredentialFuture,
        password_future: CredentialFuture,
        on_state_change: Optional[callable] = None,
    ) -> None:
        self._adapter = adapter
        self._store = store
        self._sms = sms_future
        self._password = password_future
        self._on_state_change = on_state_change
        self._lock = asyncio.Lock()
        # Auth-task singleton: there is at most one live auth
        # coroutine per account setup. start/resend/cancel all
        # funnel through the same lifecycle so we never leak a
        # stale background task.
        self._auth_task: Optional[asyncio.Task[None]] = None
        self._auth_task_generation: int = 0
        # Credential-future generations captured when the current
        # auth round started (P1 #2). A provider callback whose
        # generation no longer matches belongs to a stale round
        # and MUST NOT publish state over the newer attempt.
        self._active_sms_gen: int = -1
        self._active_password_gen: int = -1
        # Canonical E.164 phone of the active auth attempt.
        # Set by start(), cleared by cancel(). resend_code()
        # MUST use this — never the masked phone_hint — so the
        # upstream request_code reaches MAX with a real number.
        self._canonical_phone: Optional[str] = None
        # Optional PyMax debug diagnostics. The publisher may
        # only emit masked phone hints — never full numbers,
        # passwords, codes, or session tokens.
        self._debug_log: Optional[callable] = None
        # Install credential-await hooks: the moment PyMax calls
        # SmsCodeProvider.get_code / PasswordProvider.get_password
        # we publish WAITING_CODE / WAITING_PASSWORD. Per upstream
        # PyMax 2.4.1, those providers are only invoked AFTER the
        # server has accepted the verification request, so this
        # callback is the authoritative signal that the round-trip
        # succeeded upstream.
        self._sms.set_callbacks(
            on_waiting=lambda kind, gen: self._on_provider_waiting(kind, gen),
            on_invalidated=self._on_provider_invalidated,
        )
        self._password.set_callbacks(
            on_waiting=lambda kind, gen: self._on_provider_waiting(kind, gen),
            on_invalidated=self._on_provider_invalidated,
        )
        # Initial state — AUTHENTICATED if the adapter already
        # has a valid session on disk; otherwise NOT_STARTED.
        snapshot = store.load()
        if snapshot is not None:
            self._state = snapshot.state
            self._snapshot = snapshot
        else:
            self._state = MaxAuthState.NOT_STARTED
            self._snapshot = MaxAuthSnapshot(
                state=MaxAuthState.NOT_STARTED,
                phone_hint=None,
                account_id=None,
                display_name=None,
                handle=None,
                last_error=None,
                last_error_class=None,
                next_action="start",
                updated_at=int(datetime.now(tz=timezone.utc).timestamp()),
            )

    def set_debug_log(self, log_fn: Optional[callable]) -> None:
        """Install a debug logger for the MAX auth flow.

        ``log_fn`` is invoked with an ``event`` keyword (e.g.
        ``"connecting"``, ``"requesting_code"``, ``"provider_waiting"``)
        and a ``phone_hint`` keyword carrying the masked phone.
        It MUST NOT receive full phone numbers, OTPs, passwords,
        or session tokens. Use for owner-side ODROID diagnostics.
        """
        self._debug_log = log_fn

    @property
    def on_state_change(self) -> Optional[callable]:
        """Return the listener-lifecycle hook (may be a coroutine)."""
        return self._on_state_change

    @on_state_change.setter
    def on_state_change(self, hook: Optional[callable]) -> None:
        """Install a hook invoked after every state publish.

        The hook receives a :class:`MaxAuthSnapshot`; it may
        return a coroutine which the runtime will schedule on
        the active event loop. Errors raised by the hook are
        logged but never propagated into the auth state machine.
        """
        self._on_state_change = hook

    def _debug(self, *, event: str, phone_hint: Optional[str] = None,
              generation: Optional[int] = None,
              elapsed_ms: Optional[int] = None,
              error_class: Optional[str] = None) -> None:
        cb = self._debug_log
        if cb is None:
            return
        try:
            cb(
                event=event,
                phone_hint=phone_hint,
                generation=generation if generation is not None else self._auth_task_generation,
                elapsed_ms=elapsed_ms,
                error_class=error_class,
            )
        except Exception:  # noqa: BLE001
            logger.exception("max auth debug log callback raised")

    @property
    def state(self) -> MaxAuthState:
        return self._state

    @property
    def snapshot(self) -> MaxAuthSnapshot:
        return self._snapshot

    def _publish(self, *, snap: MaxAuthSnapshot) -> None:
        self._state = snap.state
        self._snapshot = snap
        self._store.save(snap)
        if self._on_state_change is not None:
            try:
                cb = self._on_state_change(snap)
                if asyncio.iscoroutine(cb):
                    # Schedule the callback; if no loop is running yet
                    # we silently drop the notification (best-effort).
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(cb)
                    except RuntimeError:
                        pass
            except Exception:  # noqa: BLE001
                logger.exception("max auth on_state_change failed")

    def _make_snapshot(self, *, state: MaxAuthState, **overrides) -> MaxAuthSnapshot:
        return MaxAuthSnapshot(
            state=state,
            phone_hint=overrides.get("phone_hint", self._snapshot.phone_hint),
            account_id=overrides.get("account_id", self._snapshot.account_id),
            display_name=overrides.get("display_name", self._snapshot.display_name),
            handle=overrides.get("handle", self._snapshot.handle),
            last_error=overrides.get("last_error"),
            last_error_class=overrides.get("last_error_class"),
            next_action=overrides.get(
                "next_action",
                {
                    MaxAuthState.NOT_STARTED: "start",
                    MaxAuthState.CONNECTING: "wait",
                    MaxAuthState.REQUESTING_CODE: "wait",
                    MaxAuthState.WAITING_CODE: "submit_code",
                    MaxAuthState.VERIFYING_CODE: "wait",
                    MaxAuthState.WAITING_PASSWORD: "submit_password",
                    MaxAuthState.VERIFYING_PASSWORD: "wait",
                    MaxAuthState.AUTHENTICATED: "done",
                    MaxAuthState.RATE_LIMITED: "retry",
                    MaxAuthState.FAILED: "retry",
                }.get(state, "done"),
            ),
            updated_at=int(datetime.now(tz=timezone.utc).timestamp()),
        )

    # ------------------------------------------------------------------
    # P1 #3 (surgical): generation ownership. ONE auth attempt owns
    # ONE generation. After invalidate() bumps the future's
    # generation, the adapter gets FRESH provider objects capturing
    # that new generation — old provider objects keep their old
    # generation forever and can never publish or fulfill for the
    # new attempt.
    # ------------------------------------------------------------------
    def _rearm_providers_for_new_attempt(self) -> None:
        from ...adapters.max.providers import (
            QueuedPasswordProvider,
            QueuedSmsProvider,
        )

        self._sms.invalidate()
        self._password.invalidate()
        sms_provider = QueuedSmsProvider(future=self._sms)
        password_provider = QueuedPasswordProvider(future=self._password)
        self._active_sms_gen = sms_provider.generation
        self._active_password_gen = password_provider.generation
        for attr, provider in (
            ("_sms_provider", sms_provider),
            ("_password_provider", password_provider),
        ):
            if hasattr(self._adapter, attr):
                try:
                    setattr(self._adapter, attr, provider)  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001
                    pass

    # ------------------------------------------------------------------
    # Public coroutines
    # ------------------------------------------------------------------
    async def start(self, *, phone: str) -> MaxAuthSnapshot:
        """Begin the SMS-code round-trip.

        Caller supplies the raw phone; we normalise and persist
        only the masked hint. We then cancel any in-flight auth
        task, invalidate the credential futures, disconnect the
        adapter (best-effort) and launch a fresh background
        coroutine that talks to PyMax.

        WAITING_CODE is NOT published here — it is published by
        the on_waiting callback the moment PyMax's SmsCodeProvider
        is invoked, which only happens after the upstream server
        has accepted the verification request. Until that
        callback fires, the state stays in CONNECTING /
        REQUESTING_CODE and the UI renders "Запрашиваем код у
        MAX…".
        """
        # P1 #2: the busy guard runs BEFORE any validation or
        # state mutation. Phone validation used to run first, so
        # an invalid secondary request overwrote the active
        # round's state with FAILED — mutating an in-flight
        # attempt from a request that must be a no-op.
        async with self._lock:
            # Busy guard: while a MAX auth round is in flight we
            # treat repeated start() calls as a no-op. The audit
            # caught that "three SMS" codes were being sent
            # because every UI click (or every stale React fetch)
            # created a fresh Client and a fresh request_code.
            # Returning the current snapshot is the user-visible
            # "your code is on its way" without driving another
            # MAX verification request.
            if (
                self._auth_task is not None
                and not self._auth_task.done()
                and self._state in self._IN_FLIGHT_STATES
            ):
                logger.info(
                    "max auth: start() ignored — round already in flight state=%s",
                    self._state.value,
                )
                return self._snapshot
            try:
                parsed: NormalisedPhone = parse_phone(phone)
            except PhoneNormalisationError as exc:
                snap = self._make_snapshot(
                    state=MaxAuthState.FAILED,
                    phone_hint=_mask_phone(phone),
                    last_error=str(exc)[:512],
                    last_error_class="PhoneInvalid",
                    next_action="retry",
                )
                self._publish(snap=snap)
                return snap
            normalized = parsed.e164
            hint = parsed.hint
            # Cancel any prior auth task so we never have more
            # than one live background coroutine per account.
            await self._cancel_auth_task()
            # Drop any in-flight credentials — they belong to
            # the previous round and would otherwise be fulfilled
            # against the new auth flow. Fresh providers capture
            # the new attempt's generation immutably (P1 #3).
            self._rearm_providers_for_new_attempt()
            # Store the canonical E.164 phone so resend_code()
            # hands MAX a real number — never the masked phone_hint.
            self._canonical_phone = normalized
            snap = self._make_snapshot(
                state=MaxAuthState.CONNECTING,
                phone_hint=hint,
                last_error=None,
                last_error_class=None,
                next_action="wait",
            )
            self._publish(snap=snap)
            self._debug(event="connecting", phone_hint=hint)
            # Spawn the singleton auth task. The task lives until
            # the operator either submits the OTP and we reach
            # AUTHENTICATED, the server returns FAILED, the
            # operator cancels, or another start()/resend() reaps
            # it via _cancel_auth_task().
            self._auth_task_generation += 1
            gen = self._auth_task_generation
            self._auth_task = asyncio.create_task(
                self._run_auth_task(gen=gen, normalized=normalized),
                name=f"max-auth-{gen}",
            )
            return self._snapshot

    async def _cancel_auth_task(self) -> None:
        """Cancel the singleton auth task and await its exit.

        Used by start(), resend(), and cancel(). A fresh
        background task may be created immediately after this
        returns; the awaited cancellation guarantees we never
        leak a stale background coroutine.
        """
        task = self._auth_task
        if task is None or task.done():
            self._auth_task = None
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            # Swallow exceptions raised during cancellation —
            # the next start()/resend() will publish a fresh
            # snapshot. We deliberately do not re-raise.
            pass
        finally:
            self._auth_task = None

    async def _run_auth_task(self, *, gen: int, normalized: str) -> None:
        """Background coroutine that drives PyMax through one auth round.

        Lifecycle:

        * CONNECTING (already published by start())
        * ``adapter.connect()`` is called.
        * If the adapter exposes ``prepare_sms`` we prefer that
          path; it lets us drive ``request_code`` without going
          through the full handshake every time we resend.
        * Otherwise we run the full PyMax handshake via
          ``connect()``. PyMax will:

          1. Open the TCP connection and run the handshake.
          2. Call ``SmsAuthFlow.authenticate(app)``.
          3. Inside that flow, call ``request_code(phone)``.
          4. If the server accepts, invoke
             ``SmsCodeProvider.get_code(phone)`` which fires the
             ``on_waiting`` callback that publishes WAITING_CODE.

        * After the operator submits the code via
          ``/api/max/auth/code``, PyMax's auth flow resumes
          (VERIFYING_CODE), then either:

          - Sends ``send_code`` → AUTHENTICATED on success.
          - Demands 2FA → PasswordProvider.get_password() is
            invoked → WAITING_PASSWORD is published via the
            callback.
        """
        hint = _mask_phone(normalized)
        # Sanity guard: if a newer start() has been issued
        # concurrently this task is stale; bail out without
        # mutating the state machine.
        if gen != self._auth_task_generation:
            logger.debug("max auth: stale task generation=%d exiting", gen)
            return
        try:
            # Bump the visible state into REQUESTING_CODE so
            # the UI can distinguish "still talking to MAX"
            # from "WAITING_CODE: server already accepted".
            if self._state == MaxAuthState.CONNECTING:
                snap = self._make_snapshot(
                    state=MaxAuthState.REQUESTING_CODE,
                    phone_hint=hint,
                    last_error=None,
                    last_error_class=None,
                    next_action="wait",
                )
                self._publish(snap=snap)
                self._debug(event="requesting_code", phone_hint=hint)
            prepare = getattr(self._adapter, "prepare_sms", None)

            async def _provider_reached() -> None:
                # Resolves once the credential provider has been
                # invoked — i.e. the state machine left the
                # pre-provider states. From that moment the round
                # legitimately waits for operator input and must
                # NOT be killed by the connect timeout.
                while self._state in (
                    MaxAuthState.CONNECTING,
                    MaxAuthState.REQUESTING_CODE,
                ):
                    await asyncio.sleep(0.1)

            connect_task = asyncio.ensure_future(
                prepare(phone=normalized)
                if callable(prepare)
                else self._adapter.connect()
            )
            watcher = asyncio.ensure_future(_provider_reached())
            try:
                done, _pending = await asyncio.wait(
                    {connect_task, watcher},
                    timeout=self._CONNECT_TIMEOUT,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if gen != self._auth_task_generation:
                    # Superseded while we waited — leave quietly.
                    connect_task.cancel()
                    return
                if connect_task in done:
                    if connect_task.cancelled():
                        raise asyncio.CancelledError
                    exc = connect_task.exception()
                    if exc is not None:
                        raise exc
                    # connect() completed without the provider
                    # firing — fall through to the profile probe
                    # below (one-shot login path).
                elif self._state in (
                    MaxAuthState.CONNECTING,
                    MaxAuthState.REQUESTING_CODE,
                ):
                    # P1 #1: the handshake never reached the
                    # credential provider within the bounded
                    # window. Surface a useful FAILED state instead
                    # of an eternal "Запрашиваем код у MAX…".
                    # P1 #2: the cancellation is AWAITED and the
                    # adapter disconnected — an orphaned half-open
                    # connection must not leak past the timeout.
                    connect_task.cancel()
                    try:
                        await connect_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    try:
                        await self._adapter.disconnect()
                    except Exception:  # noqa: BLE001
                        logger.debug("adapter.disconnect on timeout failed")
                    snap = self._make_snapshot(
                        state=MaxAuthState.FAILED,
                        phone_hint=hint,
                        last_error=(
                            f"MAX не ответил за {int(self._CONNECT_TIMEOUT)} с — "
                            "проверьте подключение к интернету и попробуйте ещё раз"
                        ),
                        last_error_class="ConnectTimeout",
                        next_action="retry",
                    )
                    self._publish(snap=snap)
                    self._debug(
                        event="connect_timeout",
                        phone_hint=hint,
                        error_class="ConnectTimeout",
                    )
                    return
                else:
                    # Provider invoked (WAITING_CODE / WAITING_PASSWORD
                    # published): wait out the operator-input phase.
                    # submit_code / submit_password fulfil the future
                    # and this task then completes the round.
                    await connect_task
            except asyncio.CancelledError:
                # P1 #2: cancellation is awaited — a fire-and-forget
                # cancel can leave the connect coroutine running.
                connect_task.cancel()
                try:
                    await connect_task
                except (asyncio.CancelledError, Exception):
                    pass
                raise
            finally:
                watcher.cancel()
                try:
                    await watcher
                except (asyncio.CancelledError, Exception):
                    pass
            # If we reach this line without WAITING_CODE ever
            # being published, the adapter did not invoke the
            # SmsCodeProvider (e.g. PyMax completed login in one
            # shot, or the flow short-circuited). Probe the
            # profile to decide whether we landed at AUTHENTICATED.
            if gen != self._auth_task_generation:
                return
            profile = await self._safe_profile()
            if profile.user_id:
                snap = self._make_snapshot(
                    state=MaxAuthState.AUTHENTICATED,
                    phone_hint=hint,
                    account_id=profile.user_id,
                    display_name=profile.display_name,
                    handle=profile.handle,
                    last_error=None,
                    last_error_class=None,
                    next_action="done",
                )
                self._publish(snap=snap)
                self._debug(event="authenticated", phone_hint=hint)
            else:
                # No provider was invoked AND no profile — treat
                # as an upstream-no-op failure so the operator
                # is never led to believe MAX accepted a request.
                snap = self._make_snapshot(
                    state=MaxAuthState.FAILED,
                    phone_hint=hint,
                    last_error="auth_flow_returned_without_provider",
                    last_error_class="AuthFlowNoProvider",
                    next_action="retry",
                )
                self._publish(snap=snap)
                self._debug(event="failed_no_provider", phone_hint=hint)
        except asyncio.CancelledError:
            logger.debug("max auth task cancelled generation=%d", gen)
            raise
        except (MaxAuthError, MaxError) as e:
            if gen != self._auth_task_generation:
                return
            err_class = type(e).__name__
            # PyMax signals rate-limit / too-many-tries via a
            # dedicated exception class; honour it so the UI
            # can show a countdown.
            state = (
                MaxAuthState.RATE_LIMITED
                if err_class in {"MaxRateLimited", "RateLimitedError"}
                else MaxAuthState.FAILED
            )
            snap = self._make_snapshot(
                state=state,
                phone_hint=hint,
                last_error=str(e)[:512],
                last_error_class=err_class,
                next_action="retry",
            )
            self._publish(snap=snap)
            self._debug(event="auth_error", phone_hint=hint)
        except Exception as e:  # noqa: BLE001
            if gen != self._auth_task_generation:
                return
            logger.exception("max auth task crashed")
            snap = self._make_snapshot(
                state=MaxAuthState.FAILED,
                phone_hint=hint,
                last_error=str(e)[:512],
                last_error_class=type(e).__name__,
                next_action="retry",
            )
            self._publish(snap=snap)
            self._debug(event="auth_crashed", phone_hint=hint)

    # ------------------------------------------------------------------
    # Credential-await hooks (the authoritative signal)
    # ------------------------------------------------------------------
    def _on_provider_waiting(self, kind: str, generation: int = -1) -> None:
        """Invoked the instant PyMax reaches the credential await.

        Per upstream PyMax 2.4.1 this is only called after the
        corresponding upstream verification step has succeeded
        (request_code / login_with_password). Publishing
        WAITING_CODE / WAITING_PASSWORD here is therefore safe —
        the server has actually accepted the round.

        P1 #2: the callback carries the credential-future
        generation. If it does not match the generation captured
        when the current round started, the publication belongs
        to a stale auth task and is dropped — an old round can
        never overwrite the state of a newer one.
        """
        if kind == "sms":
            target = MaxAuthState.WAITING_CODE
            event = "provider_waiting_sms"
            active_gen = self._active_sms_gen
        elif kind == "password":
            target = MaxAuthState.WAITING_PASSWORD
            event = "provider_waiting_password"
            active_gen = self._active_password_gen
        else:  # pragma: no cover - defensive
            logger.warning("unknown credential kind=%r", kind)
            return
        if generation != -1 and active_gen != -1 and generation != active_gen:
            logger.info(
                "max auth: dropping stale provider_waiting kind=%s "
                "gen=%s (active=%s)",
                kind,
                generation,
                active_gen,
            )
            return
        snap = self._make_snapshot(
            state=target,
            phone_hint=self._snapshot.phone_hint,
            last_error=None,
            last_error_class=None,
        )
        self._publish(snap=snap)
        self._debug(event=event, phone_hint=self._snapshot.phone_hint)

    def _on_provider_invalidated(self) -> None:
        # Diagnostic hook; the actual state transition is driven
        # by the next start()/resend().
        self._debug(
            event="provider_invalidated",
            phone_hint=self._snapshot.phone_hint,
        )

    async def submit_code(self, *, code: str) -> MaxAuthSnapshot:
        """Forward the operator's SMS code to PyMax.

        Behaviour:

        * The ``WAITING_CODE`` snapshot was driven by the
          SmsCodeProvider being awaited, which by contract only
          happens after the upstream ``request_code`` succeeded.
        * We publish ``VERIFYING_CODE`` so the UI can render
          "Отправляем код…" while PyMax's ``send_code`` round
          trip is in flight.
        * We do NOT pre-emptively publish WAITING_PASSWORD or
          AUTHENTICATED here — the next ``/api/max/auth/state``
          poll observes whichever state PyMax actually lands in.
          If PyMax's PasswordProvider is invoked the
          ``on_waiting`` callback publishes WAITING_PASSWORD;
          if login completes, the auth task itself publishes
          AUTHENTICATED after a ``_safe_profile`` probe.
        """
        if not code or not code.strip():
            snap = self._make_snapshot(
                state=MaxAuthState.WAITING_CODE,
                last_error="code_empty",
                last_error_class="CodeEmpty",
                next_action="submit_code",
            )
            self._publish(snap=snap)
            return snap
        if self._state != MaxAuthState.WAITING_CODE:
            # P1 #2: an invalid secondary request must NOT mutate
            # an already active auth attempt — return the current
            # snapshot untouched instead of publishing an error
            # over a healthy in-flight round.
            logger.info(
                "submit_code ignored: state=%s (expected waiting_code)",
                self._state.value,
            )
            return self._snapshot
        async with self._lock:
            snap = self._make_snapshot(
                state=MaxAuthState.VERIFYING_CODE,
                last_error=None,
                last_error_class=None,
                next_action="wait",
            )
            self._publish(snap=snap)
            self._debug(event="verifying_code", phone_hint=self._snapshot.phone_hint)
            # P1 #3: fulfill with the generation captured when THIS
            # attempt started — a fresh read could adopt a newer
            # generation than the round being answered.
            generation = self._active_sms_gen
            self._sms.fulfill(generation=generation, value=code.strip())
            # Yield to the auth task so it can advance through
            # send_code -> (optional) password challenge.
            await asyncio.sleep(0)
        # Outside the lock: probe the resulting state. PyMax
        # either completes, demands 2FA (PasswordProvider
        # awaits — on_waiting publishes WAITING_PASSWORD), or
        # raises (auth task publishes FAILED).
        await asyncio.sleep(0.2)
        return self._snapshot

    async def submit_password(self, *, password: str) -> MaxAuthSnapshot:
        if not password:
            snap = self._make_snapshot(
                state=MaxAuthState.WAITING_PASSWORD,
                last_error="password_empty",
                last_error_class="PasswordEmpty",
                next_action="submit_password",
            )
            self._publish(snap=snap)
            return snap
        if self._state != MaxAuthState.WAITING_PASSWORD:
            # P1 #2: do not mutate the active attempt on a stale
            # password submission either.
            logger.info(
                "submit_password ignored: state=%s (expected waiting_password)",
                self._state.value,
            )
            return self._snapshot
        async with self._lock:
            # P0 #1: publish the OBSERVABLE verifying state BEFORE
            # fulfilling the future — the UI keeps polling while the
            # upstream password verification is in flight and learns
            # about AUTHENTICATED (or another challenge) on its own.
            snap = self._make_snapshot(
                state=MaxAuthState.VERIFYING_PASSWORD,
                last_error=None,
                last_error_class=None,
                next_action="wait",
            )
            self._publish(snap=snap)
            self._debug(event="verifying_password", phone_hint=self._snapshot.phone_hint)
            # P1 #3: same ownership rule as submit_code.
            generation = self._active_password_gen
            self._password.fulfill(generation=generation, value=password)
            await asyncio.sleep(0)
        await asyncio.sleep(0.2)
        return self._snapshot

    async def resend_code(self) -> MaxAuthSnapshot:
        """Ask the server to send a fresh SMS code.

        Semantics:

        * Cancel any in-flight auth task so the previous
          ``connect()`` does not leak.
        * Invalidate the SMS future so a stale code submission
          cannot fulfil the new round.
        * Disconnect the adapter so the next ``connect()``
          triggers a fresh ``request_code`` rather than reusing
          the previous handshake.
        * Launch a new auth task. The transition to
          WAITING_CODE happens via the on_waiting callback when
          PyMax's SmsCodeProvider is invoked — not earlier.
        """
        if self._state not in (
            MaxAuthState.WAITING_CODE,
            MaxAuthState.CONNECTING,
            MaxAuthState.REQUESTING_CODE,
        ):
            return self._snapshot
        # Use the canonical E.164 phone stored by start().
        # The previous implementation reused the masked phone_hint
        # (``***4567``) which MAX then rejected because it does
        # not parse as a real number. Masked hints are for UI / logs
        # only — never for the upstream request_code call.
        phone = self._canonical_phone
        if not phone:
            snap = self._make_snapshot(
                state=self._state,
                last_error="no_active_phone",
                last_error_class="StateMismatch",
                next_action="retry",
            )
            self._publish(snap=snap)
            return snap
        async with self._lock:
            await self._cancel_auth_task()
            self._rearm_providers_for_new_attempt()
            try:
                await self._adapter.disconnect()
            except Exception:  # noqa: BLE001
                logger.debug("adapter.disconnect on resend failed (best-effort)")
            snap = self._make_snapshot(
                state=MaxAuthState.CONNECTING,
                phone_hint=self._snapshot.phone_hint,
                last_error=None,
                last_error_class=None,
                next_action="wait",
            )
            self._publish(snap=snap)
            self._debug(event="resend", phone_hint=self._snapshot.phone_hint)
            self._auth_task_generation += 1
            gen = self._auth_task_generation
            self._auth_task = asyncio.create_task(
                self._run_auth_task(gen=gen, normalized=phone),
                name=f"max-auth-resend-{gen}",
            )
            return self._snapshot

    def _phone_from_hint(self) -> str:
        # The full phone is held in the live MaxAccount DB row.
        # The state snapshot only carries the masked hint; if
        # the operator closed the browser between /start and
        # /resend the resend will simply re-prompt with the
        # last-known hint.
        return self._snapshot.phone_hint or ""

    async def cancel(self) -> MaxAuthSnapshot:
        """Stop any in-flight auth task and reset to NOT_STARTED.

        Cancels the singleton auth task, invalidates the SMS and
        password futures, disconnects the adapter (best-effort)
        and publishes NOT_STARTED. Safe to call from any prior
        state — including AUTHENTICATED, in which case the
        adapter is closed and the operator must re-auth.
        """
        async with self._lock:
            await self._cancel_auth_task()
            self._rearm_providers_for_new_attempt()
            # Drop the canonical phone so resend_code() can no
            # longer hand MAX the previous round's number. The
            # next start() must supply a fresh phone.
            self._canonical_phone = None
            snap = self._make_snapshot(
                state=MaxAuthState.NOT_STARTED,
                phone_hint=None,
                account_id=None,
                display_name=None,
                handle=None,
                next_action="start",
                last_error=None,
                last_error_class=None,
            )
            self._publish(snap=snap)
            try:
                await self._adapter.disconnect()
            except Exception:  # noqa: BLE001
                logger.debug("adapter.disconnect on cancel failed (best-effort)")
            return snap

    async def refresh(self) -> MaxAuthSnapshot:
        """Re-read the adapter's profile (e.g. after restart).

        Does NOT publish WAITING_CODE. The only way to reach
        WAITING_CODE / WAITING_PASSWORD is through the
        CredentialFuture on_waiting callback, which fires only
        when PyMax has actually called the provider. If we are
        in CONNECTING / REQUESTING_CODE we leave the state
        alone — the auth task itself will publish the next
        transition.

        A terminal :data:`MaxAuthState.FAILED` is preserved as-is
        so the operator can still see the last error after a
        page refresh (audit-mandated behaviour).
        """
        # If an auth task is in flight, never overwrite its
        # state with refresh() — let the auth task and provider
        # callbacks author the state.
        if self._auth_task is not None and not self._auth_task.done():
            return self._snapshot
        # A terminal FAILED snapshot is sticky — refresh() must
        # NOT silently downgrade it to NOT_STARTED. The
        # operator needs to see the last error after a reload.
        if self._snapshot.state == MaxAuthState.FAILED:
            return self._snapshot
        async with self._lock:
            profile = await self._safe_profile()
            if profile.user_id:
                snap = self._make_snapshot(
                    state=MaxAuthState.AUTHENTICATED,
                    account_id=profile.user_id,
                    display_name=profile.display_name,
                    handle=profile.handle,
                    next_action="done",
                )
            else:
                snap = self._make_snapshot(
                    state=MaxAuthState.NOT_STARTED,
                    next_action="start",
                )
            self._publish(snap=snap)
            return snap

    async def _safe_profile(self) -> MaxProfile:
        try:
            return await self._adapter.get_profile()
        except (MaxSessionError, MaxError):
            return MaxProfile(user_id="", display_name="", handle=None)
        except Exception:  # noqa: BLE001
            return MaxProfile(user_id="", display_name="", handle=None)


def build_providers(
    *, sms_future: Optional[CredentialFuture] = None,
    password_future: Optional[CredentialFuture] = None,
) -> tuple[QueuedSmsProvider, QueuedPasswordProvider]:
    """Helper that wires the queued providers with fresh futures."""
    sms_f = sms_future or CredentialFuture(kind="sms")
    pwd_f = password_future or CredentialFuture(kind="password")
    return QueuedSmsProvider(future=sms_f), QueuedPasswordProvider(future=pwd_f)


__all__ = [
    "MaxAuthService",
    "MaxAuthSnapshot",
    "MaxAuthState",
    "MaxAuthStateStore",
    "build_providers",
]