"""Production application runtime.

:class:`AppRuntime` is the single owner of every long-lived
service in SHMAX:

* Database + SecretStore
* Adapters (MAX + Telegram) and their transport managers
* BridgeEngine with ACL + routes + message_map + queue
* Delivery workers (the actual queue drainers)
* Watchdogs + persistent watchdog state
* Notification outbox + dispatcher
* Supervisor (per-component restart on failure)

The FastAPI lifespan simply calls ``await runtime.start()``
and ``await runtime.stop()``; the rest of the application
talks to ``app.state.runtime``.

Lifecycle contract:

``start`` (in order)
  1. Migrations (idempotent)
  2. Database + SecretStore open
  3. Core services (routes, message_map, ACL)
  4. Adapters — if configured; otherwise keep Fakes so the
     bridge still flows end-to-end in offline tests
  5. Delivery workers
  6. Watchdogs (MAX + Telegram) — start in monitor mode
  7. Notification outbox drainer
  8. Event listeners — supervise in their own supervisor
     tasks so a crashing adapter does not kill the web UI

``stop`` (reverse order, idempotent)
  1. Stop intake flag
  2. Watchdogs stop
  3. Notification outbox drainer stops
  4. Delivery workers stop (after current claim passes)
  5. Adapters disconnect
  6. DB dispose

Adapters may be ``None`` (not configured). The runtime
still starts, the Web UI serves, and the diagnostics
report ``NOT_CONFIGURED`` for the missing side.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from .adapters.max.base import MaxAdapter
from .adapters.max.fake import FakeMaxAdapter
from .adapters.telegram.base import TelegramAdapter
from .adapters.telegram.fake import FakeTelegramAdapter
from .adapters.telegram.base import NotConfiguredTelegramAdapter
from .adapters.telegram.manager import (
    TelegramAutoStateStore,
    TelegramTransportManager,
)
from .bridge.engine import BridgeEngine, BridgeConfig
from .bridge.message_map import MessageMapRepository, outbound_marker
from .core.paths import PathLayout
from .media.pipeline import MediaPipeline
from .database import Database
from .domain.enums import DeliveryState, Direction
from .domain.models import DeliveryOutcome, ReplyRef
from .notifications.outbox import NotificationOutbox
from .notifications.dispatcher import (
    LoggingNotificationDispatcher,
    NotificationDispatcher,
)
from .notifications.outbox_dispatcher import OutboxBackedDispatcher
from .notifications.sender import NotificationSender
from .queue.repository import DeliveryQueueRepository
from .routing.repository import RoutesRepository
from .security import SecretStore
from .watchdog.base import WatchdogStateStore, _DispatcherSlot
from .watchdog.max_watchdog import MaxWatchdog
from .watchdog.telegram_watchdog import TelegramWatchdog


logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Adapter -> NormalizedMessage adapter
# ----------------------------------------------------------------------
# The actual translation lives in ``shmax.adapters.normalize`` so the
# runtime, the bridge, and the tests share the same rich path. Do
# NOT re-introduce a simplified normalizer here — the audit flagged
# that as a silent-regression source.
from .adapters.normalize import normalize_inbound as _normalize_inbound


def _max_event_to_normalized(event: Any) -> Any:
    """Backward-compat shim. Delegates to the shared normalizer
    so production code never loses rich fields."""
    return _normalize_inbound("max", event)


def _telegram_event_to_normalized(event: Any) -> Any:
    """Backward-compat shim. Delegates to the shared normalizer."""
    return _normalize_inbound("telegram", event)


def _canonical_media_kind(raw: Any) -> str:
    """Normalize a media kind string to ``photo | file | video | voice``.

    The runtime, the bridge, the queue payload, and the
    adapter call sites all accept these four values. The
    aliases ``image`` / ``document`` / ``audio`` collapse
    into the canonical form. Anything unknown falls back
    to ``file`` (the safe default for adapter-side media).
    """
    s = str(raw or "").strip().lower()
    if s in {"photo", "image"}:
        return "photo"
    if s == "sticker":
        return "sticker"
    if s in {"video"}:
        return "video"
    if s in {"voice", "audio"}:
        return "voice"
    if s in {"file", "document", "doc", "attachment"}:
        return "file"
    return "file"


WATCHDOG_INTERVAL_S = 60.0
QUEUE_LEASE_S = 60.0
QUEUE_POLL_S = 1.0
OUTBOX_POLL_S = 2.0


class ComponentSupervisor:
    """Run a coroutine factory; restart on failure with bounded backoff.

    The supervisor is intentionally generic — adapters,
    workers, watchdogs all share the same lifecycle.
    """

    def __init__(
        self,
        *,
        name: str,
        factory: Callable[[], Awaitable[None]],
        backoff_initial: float = 1.0,
        backoff_cap: float = 60.0,
    ) -> None:
        self.name = name
        self._factory = factory
        self._backoff_initial = backoff_initial
        self._backoff_cap = backoff_cap
        self._task: Optional[asyncio.Task[None]] = None
        self._stop_event = asyncio.Event()
        self._restart_count = 0
        self._last_error: str | None = None

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name=f"shmax.{self.name}")
        logger.debug("supervisor %s started; task=%s", self.name, self._task)

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    @property
    def restart_count(self) -> int:
        return self._restart_count

    @property
    def last_error(self) -> str | None:
        return self._last_error

    async def _run(self) -> None:
        delay = self._backoff_initial
        while not self._stop_event.is_set():
            try:
                await self._factory()
                # Normal return = the component finished its work
                # (e.g. a listener that refused to start because the
                # adapter is not configured, or whose event stream
                # ended). Exit the loop instead of hot-spinning the
                # factory; an explicit start() re-arms us.
                logger.info("component %s finished", self.name)
                return
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001
                self._restart_count += 1
                self._last_error = repr(e)
                logger.warning(
                    "component %s crashed: %r; restarting in %.1fs",
                    self.name,
                    e,
                    delay,
                )
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                delay = min(self._backoff_cap, delay * 2)


@dataclass
class RuntimeConfig:
    watchdog_interval_seconds: float = WATCHDOG_INTERVAL_S
    queue_lease_seconds: float = QUEUE_LEASE_S
    worker_poll_seconds: float = QUEUE_POLL_S
    outbox_poll_seconds: float = OUTBOX_POLL_S
    max_concurrent_workers: int = 2
    start_workers: bool = True


@dataclass
class RuntimeStatus:
    started_at: _dt.datetime
    components: dict[str, str] = field(default_factory=dict)
    last_tick: dict[str, int] = field(default_factory=dict)


class AppRuntime:
    """Single SHMAX production gateway core."""

    def __init__(
        self,
        *,
        database: Database,
        secret_store: SecretStore,
        layout: PathLayout,
        config: RuntimeConfig | None = None,
        runtime_mode=None,
    ) -> None:
        self.db = database
        self._db = database  # internal short alias
        self.secrets = secret_store
        self.layout = layout
        self.config = config or RuntimeConfig()
        # Wire optional PyMax debug logging. Phone numbers,
        # OTPs and session tokens are NEVER logged — PyMax's
        # own DEBUG logger does not emit secrets either, but
        # we additionally pass the masked phone hint to the
        # SHMAX auth state machine for owner-side diagnostics.
        # We never call ``pymax`` directly here; the flip is
        # deferred until the adapter is built so an import
        # failure is not masked by the logging call.
        # Mode-aware adapter selection. Production defaults to
        # the real PyMax / Bot API adapters and forbids fakes.
        # Tests/dev opt in via SHMAX_RUNTIME_MODE=test|development.
        if runtime_mode is None:
            from .config import resolve_runtime_mode
            runtime_mode = resolve_runtime_mode()
        self.runtime_mode = runtime_mode
        # Optional PyMax debug diagnostics. The PyMax library
        # is chatty at DEBUG level (handshake, every opcode,
        # every dispatch) — we only flip it on when the
        # operator explicitly opted in via SHMAX_DEBUG_MAX_PYMAX.
        self.debug_max_pymax: bool = False
        self.started_at = _dt.datetime.now(_dt.timezone.utc)
        self.status = RuntimeStatus(started_at=self.started_at)
        self.started: bool = False

        # File-backed singletons.
        self.watchdog_store = WatchdogStateStore(layout.root / "state" / "watchdogs.json")
        self.tg_auto_state = TelegramAutoStateStore(layout.root / "state" / "telegram_auto.json")
        self.notification_outbox = NotificationOutbox(
            layout.root / "state" / "notifications.db"
        )
        # Media pipeline owns the ``$DATA/cache/`` directory.
        self.media_pipeline = MediaPipeline(layout.root / "cache")

        # Default dispatcher: outbox-backed. When the Telegram
        # adapter becomes available, swap in a TelegramNotifier.
        self._inner_dispatcher: NotificationDispatcher = LoggingNotificationDispatcher()
        # Watchdogs hold a mutable slot so swapping the active
        # dispatcher in ``start()`` (logging -> outbox-backed) is
        # immediately visible to them.
        self._dispatcher_slot = _DispatcherSlot(self._inner_dispatcher)
        self.dispatcher: NotificationDispatcher = self._inner_dispatcher
        self._outbox_dispatcher: OutboxBackedDispatcher | None = None

        # Adapter selection is mode-aware. Production forbids
        # fakes and falls back to ``_select_default_adapters``,
        # which raises if the real adapter cannot be loaded.
        # See ``_select_default_adapters`` below.
        self._max: MaxAdapter
        self._tg: TelegramAdapter
        self._tg_manager: TelegramTransportManager | None = None
        self._integration_probes: dict[str, dict] = {}
        self._select_default_adapters()

        # Watchdogs — created against the current adapters.
        self.max_watchdog = MaxWatchdog(
            adapter=self._max,
            store=self.watchdog_store,
            dispatcher=self._dispatcher_slot,
        )
        self.tg_watchdog = TelegramWatchdog(
            adapter=self._tg,
            store=self.watchdog_store,
            dispatcher=self._dispatcher_slot,
        )

        # MAX auth state machine. The fake adapter in offline
        # mode is paired with a no-op service that always
        # reports AUTHENTICATED; the operator upgrades this via
        # :py:meth:`install_max_auth`.
        from .auth.max import (
            CredentialFuture,
            MaxAuthService,
            MaxAuthStateStore,
        )
        self.max_sms_future: Optional[CredentialFuture] = None
        self.max_password_future: Optional[CredentialFuture] = None
        self.max_auth: Optional[MaxAuthService] = None
        self.max_auth_state_store = MaxAuthStateStore(
            layout.root / "state" / "max_auth.json"
        )
        if isinstance(self._max, FakeMaxAdapter):
            # Wire a permissive auth state so the offline UI
            # immediately sees AUTHENTICATED against the fake.
            sms_f = CredentialFuture(kind="sms")
            pwd_f = CredentialFuture(kind="password")
            self.max_sms_future = sms_f
            self.max_password_future = pwd_f
            from .auth.max import build_providers
            sms_provider, pwd_provider = build_providers(
                sms_future=sms_f, password_future=pwd_f
            )
            # Inject the queued providers into the fake for
            # parity with the production PyMax adapter.
            self._max._sms_provider = sms_provider
            self._max._password_provider = pwd_provider
            self.max_auth = MaxAuthService(
                adapter=self._max,
                store=self.max_auth_state_store,
                sms_future=sms_f,
                password_future=pwd_f,
            )
            self.max_auth.set_debug_log(self._max_auth_debug_log)
            self.max_auth.on_state_change = self._on_max_auth_state_change
        else:
            # Production path: real PyMax adapter. The auth
            # state machine is wired the same way; ``start()``
            # below will refuse to come up if PyMax failed to
            # load and a fake was silently substituted.
            sms_f = CredentialFuture(kind="sms")
            pwd_f = CredentialFuture(kind="password")
            self.max_sms_future = sms_f
            self.max_password_future = pwd_f
            from .auth.max import build_providers
            sms_provider, pwd_provider = build_providers(
                sms_future=sms_f, password_future=pwd_f
            )
            if hasattr(self._max, "_sms_provider"):
                try:
                    self._max._sms_provider = sms_provider  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001
                    pass
            if hasattr(self._max, "_password_provider"):
                try:
                    self._max._password_provider = pwd_provider  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001
                    pass
            self.max_auth = MaxAuthService(
                adapter=self._max,
                store=self.max_auth_state_store,
                sms_future=sms_f,
                password_future=pwd_f,
            )
            self.max_auth.set_debug_log(self._max_auth_debug_log)
            self.max_auth.on_state_change = self._on_max_auth_state_change

        self._supervisors: dict[str, ComponentSupervisor] = {}
        self._worker_tasks: list[asyncio.Task[None]] = []
        self._outbox_task: Optional[asyncio.Task[None]] = None
        self._shutdown = asyncio.Event()
        # Wakeup channel: callers (bridge.enqueue, runtime.stop)
        # call ``wakeup()`` to nudge the worker immediately
        # instead of waiting for the poll loop.
        self._wakeup_event = asyncio.Event()

    # ------------------------------------------------------------------
    # Adapter plumbing
    # ------------------------------------------------------------------
    def _select_default_adapters(self) -> None:
        """Install mode-appropriate default adapters.

        Production refuses fakes. Development and test allow them
        because developer workstations and the pytest suite do
        not have a live MAX or Telegram account.

        The selection rule is:

        * ``production`` -> real adapters only; raise if PyMax or
          aiogram is missing.
        * ``development`` -> real PyMax if available, else Fake
          with a warning.
        * ``test`` -> Fake by default; tests can call
          ``install_max_adapter`` to swap in a real one.
        """
        mode = self.runtime_mode

        def _is_fake_max(a: MaxAdapter) -> bool:
            return a.__class__.__name__ == "FakeMaxAdapter"

        def _is_fake_tg(a: TelegramAdapter) -> bool:
            return a.__class__.__name__ == "FakeTelegramAdapter"

        # ----- MAX adapter -----
        # Selection rule:
        # * production -> real PyMaxMaxAdapter; refuse to start if
        #   PyMax is not importable. Fakes are never acceptable.
        # * development/test -> FakeMaxAdapter by default. The
        #   workstation and pytest suite do not have a live MAX
        #   account; the production PyMax adapter cannot fake an
        #   authenticated state. Tests that need the real adapter
        #   call ``install_max_adapter(PyMaxMaxAdapter.from_env_or_default())``
        #   explicitly after construction.
        if mode.value == "production":
            try:
                from .adapters.max.pymax_adapter import PyMaxMaxAdapter
                max_adapter = PyMaxMaxAdapter.from_env_or_default(
                    session_dir=str(self.layout.root / "sessions" / "max")
                )
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    "SHMAX production startup refused: a real MAX adapter "
                    "could not be initialised. PyMax is required in "
                    "production. Set SHMAX_RUNTIME_MODE=development to "
                    "use a workstation fake. Underlying error: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
        else:
            logger.info(
                "RuntimeMode=%s: using FakeMaxAdapter. Do NOT ship this.",
                mode.value,
            )
            max_adapter = FakeMaxAdapter()

        # ----- Telegram adapter -----
        # Production: surface NOT_CONFIGURED via the sentinel. We
        # do NOT pre-build a real Bot API adapter because its
        # construction requires a configured token; instead the
        # operator installs one via ``install_telegram`` after
        # saving the token through ``/api/telegram/token``.
        # Development/test: FakeTelegramAdapter for the wizard to
        # flow end-to-end.
        if mode.value == "production":
            tg_adapter: TelegramAdapter = NotConfiguredTelegramAdapter()
        else:
            tg_adapter = FakeTelegramAdapter()

        self._max = max_adapter
        self._tg = tg_adapter
        # Hard invariant: production must never hold a fake.
        if mode.value == "production":
            if _is_fake_max(self._max) or _is_fake_tg(self._tg):
                raise RuntimeError(
                    "SHMAX production invariant violated: a Fake adapter "
                    "leaked into the production runtime."
                )
            logger.info(
                "RuntimeMode=production: MAX=%s, TG=%s",
                type(self._max).__name__,
                type(self._tg).__name__,
            )

    def install_max_adapter(self, adapter: MaxAdapter) -> None:
        # Hard production invariant: refuse fakes.
        if self.runtime_mode.value == "production" and not adapter.is_real():
            raise RuntimeError(
                f"SHMAX production invariant violated: refusing to install "
                f"non-real adapter {adapter.__class__.__name__!r}. "
                "Fake implementations are forbidden in production."
            )
        self._max = adapter
        self.max_watchdog = MaxWatchdog(
            adapter=adapter,
            store=self.watchdog_store,
            dispatcher=self._dispatcher_slot,
        )
        self.status.components["max"] = adapter.__class__.__name__
        # Re-wire the auth service to point at the new adapter
        # so /api/max/auth/* survives a runtime adapter swap.
        from .auth.max import (
            CredentialFuture,
            MaxAuthService,
            build_providers,
        )
        if self.max_sms_future is None:
            self.max_sms_future = CredentialFuture(kind="sms")
        if self.max_password_future is None:
            self.max_password_future = CredentialFuture(kind="password")
        sms_provider, pwd_provider = build_providers(
            sms_future=self.max_sms_future,
            password_future=self.max_password_future,
        )
        if hasattr(adapter, "_sms_provider"):
            try:
                adapter._sms_provider = sms_provider  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
        if hasattr(adapter, "_password_provider"):
            try:
                adapter._password_provider = pwd_provider  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
        self.max_auth = MaxAuthService(
            adapter=adapter,
            store=self.max_auth_state_store,
            sms_future=self.max_sms_future,
            password_future=self.max_password_future,
        )

    async def install_telegram(
        self,
        manager: TelegramTransportManager,
        adapter: TelegramAdapter,
        *,
        on_command: Optional["Callable[[str, dict], Awaitable[None]]"] = None,
    ) -> None:
        """Hot-swap the Telegram adapter and receive consumer.

        Stops the previously-running Telegram listener (if any)
        before swapping, so we never have two listener loops
        competing for the same Telegram update stream — that
        was the source of "another polling consumer" errors and
        Telegram 409 conflicts the audit caught.

        ``on_command`` is the slash-command handler used to
        implement ``/start``, ``/setname`` and ``/shmax_connect``
        (audit P0 #6). If the adapter exposes
        ``set_on_command`` we wire it; otherwise attach to the
        ``_on_command`` attribute directly (used by the production
        Bot API adapter and the unit-test fake).
        """
        # Hard production invariant: refuse fakes.
        if self.runtime_mode.value == "production" and not adapter.is_real():
            raise RuntimeError(
                f"SHMAX production invariant violated: refusing to install "
                f"non-real adapter {adapter.__class__.__name__!r}."
            )
        # Hot-swap protocol (audit P0 #4): exactly ONE active
        # transport/listener at any time.
        #
        #   1. stop the previous consumer task and AWAIT its exit;
        #   2. disconnect the PREVIOUS adapter (its polling task,
        #      long-poll request and aiohttp session die with it) —
        #      swapping without disconnecting leaked orphan
        #      getUpdates consumers and produced Telegram 409
        #      conflicts;
        #   3. swap the adapter and wire the command handler;
        #   4. start exactly one new listener.
        try:
            sup = self._supervisors.get("telegram_listener")
            if sup is not None and sup._task is not None and not sup._task.done():  # type: ignore[attr-defined]
                await sup.stop()
        except Exception:  # noqa: BLE001
            logger.exception("AppRuntime: stopping previous Telegram listener failed")
        previous_adapter = self._tg
        if previous_adapter is not None and previous_adapter is not adapter:
            try:
                await previous_adapter.disconnect()
            except Exception:  # noqa: BLE001
                logger.exception(
                    "AppRuntime: disconnecting previous Telegram adapter failed"
                )
        self._tg_manager = manager
        self._tg = adapter
        if on_command is not None:
            if hasattr(adapter, "set_on_command"):
                adapter.set_on_command(on_command)
            elif hasattr(adapter, "_on_command"):
                adapter._on_command = on_command
            self._tg_command_handler = on_command
        self.tg_watchdog = TelegramWatchdog(
            adapter=adapter,
            store=self.watchdog_store,
            dispatcher=self._dispatcher_slot,
        )
        if manager is not None:
            self.status.components["telegram"] = (
                f"{manager.config.mode.value}:{adapter.__class__.__name__}"
            )
        else:
            self.status.components["telegram"] = adapter.__class__.__name__
        # Start the new listener if the adapter is actually
        # configured (real Bot API / MTProto). NotConfigured and
        # fake adapters stay quiet so the UI can surface
        # "Telegram not yet activated" without crashing the
        # listener supervisor.
        if adapter.is_real():
            try:
                sup = self._supervisors.get("telegram_listener")
                if sup is not None:
                    # The supervisor may have self-retired after a
                    # clean factory return; start() recreates the
                    # task in that case, and is a no-op while the
                    # previous task is still running.
                    sup.start()
            except Exception:  # noqa: BLE001
                logger.exception("AppRuntime: starting Telegram listener failed")
            # Refresh the integration probe promptly so the UI and
            # the wizard's Finish gate see the real state without
            # waiting for the next watchdog tick.
            loop = asyncio.get_running_loop()
            loop.create_task(self.probe_integration("telegram"))

    @property
    def max_adapter(self) -> MaxAdapter:
        return self._max

    @property
    def telegram_adapter(self) -> TelegramAdapter:
        return self._tg

    @property
    def telegram_manager(self) -> Optional[TelegramTransportManager]:
        return self._tg_manager

    async def integration_snapshot(self) -> dict:
        from .integration_status import snapshot
        return await snapshot(self)

    async def probe_integration(self, name: str) -> None:
        from .integration_status import _counts_as_live
        adapter = self._max if name == "max" else self._tg
        configured = (await self.integration_snapshot())[name]["configured"]
        # In production only real adapters are probed; in the
        # test/development modes the fake IS the sanctioned
        # external boundary and is probed the same way so the
        # dashboard reflects honest evidence.
        if not configured or not _counts_as_live(adapter, self.runtime_mode):
            self._integration_probes.pop(name, None)
            return
        probe = {"adapter": adapter, "at": time.monotonic(), "wall_at": int(time.time()), "health": {}, "verified": False}
        try:
            async def verify():
                if name == "max":
                    profile = await adapter.get_profile()
                    await adapter.get_chats()  # Active remote request, not a local connected flag.
                    health = await adapter.health()
                    probe["verified"] = bool(profile.user_id and health.get("account_id"))
                else:
                    health = await adapter.health()  # get_me in both live Telegram adapters.
                    probe["verified"] = bool(health.get("bot_id"))
                probe["health"] = health
            await asyncio.wait_for(verify(), timeout=15)
        except Exception:
            probe["error"] = True
        self._integration_probes[name] = probe

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def _max_auth_debug_log(
        self,
        *,
        event: str,
        phone_hint: Optional[str],
        generation: Optional[int] = None,
        elapsed_ms: Optional[int] = None,
        error_class: Optional[str] = None,
    ) -> None:
        """Forward SHMAX MAX auth state transitions into the runtime log.

        Only emits when ``debug_max_pymax`` is enabled. The phone
        hint is the masked form (last 4 digits) — never the raw
        phone number. The event name corresponds to the SHMAX
        auth state machine vocabulary (connecting, requesting_code,
        provider_waiting_sms, provider_waiting_password, …) plus
        CLIENT_DISCONNECTED/CANCELLED/REPLACED where appropriate.
        """
        if not getattr(self, "debug_max_pymax", False):
            return
        # Use a child logger so the operator can scope the
        # capture without enabling every pymax.* channel.
        log = logging.getLogger("shmax.runtime.max_auth")
        log.info(
            "max_auth event=%s gen=%s phone_hint=%s elapsed_ms=%s err=%s",
            event,
            generation,
            phone_hint,
            elapsed_ms,
            error_class,
        )

    async def start(self) -> None:
        logger.info("AppRuntime starting")
        self._shutdown.clear()

        # Open the persistent notification outbox.
        await self.notification_outbox.open()

        # Resolve the system-topic destination. When Telegram is
        # configured and a system route already exists we send to
        # the operator's chat/topic; otherwise we fall back to a
        # local placeholder so the outbox still records the event.
        chat_id, thread_id = await self._resolve_system_topic()

        # Wrap the inner dispatcher in an OutboxBackedDispatcher so
        # watchdog events go to the persistent outbox first.
        self._outbox_dispatcher = OutboxBackedDispatcher(
            outbox=self.notification_outbox,
            inner=self._inner_dispatcher,
            destination_chat_id=chat_id,
            thread_id=thread_id,
        )
        self.dispatcher = self._outbox_dispatcher
        # Swap the slot's dispatcher so existing watchdogs (which
        # hold the slot, not the dispatcher) immediately dispatch
        # through the outbox-backed path.
        self._dispatcher_slot.current = self._outbox_dispatcher

        # The sender drains the outbox via the *inner* dispatcher,
        # never via the outbox-backed one. This avoids the
        # feedback loop where ``enqueue`` triggers ``drain``
        # which triggers another ``enqueue``.
        self._outbox_sender = NotificationSender(
            outbox=self.notification_outbox,
            dispatcher=self._inner_dispatcher,
        )

        # Start delivery workers.
        if self.config.start_workers:
            # Reclaim any leases left by a previous process that
            # crashed before finishing the rows it had claimed.
            async with self._db.session() as s:
                reclaimed = await DeliveryQueueRepository(s).reclaim_expired()
                logger.info("startup reclaimed %d expired leases", reclaimed)
            await self._restart_workers()
            self._outbox_task = asyncio.create_task(
                self._outbox_drain_loop(), name="shmax.outbox"
            )

        # Watchdog loops.
        self._supervisors["max_watchdog"] = ComponentSupervisor(
            name="max_watchdog",
            factory=self._max_watchdog_loop,
            backoff_initial=2.0,
            backoff_cap=30.0,
        )
        self._supervisors["telegram_watchdog"] = ComponentSupervisor(
            name="telegram_watchdog",
            factory=self._tg_watchdog_loop,
            backoff_initial=2.0,
            backoff_cap=30.0,
        )

        # Event listeners are supervised so an adapter crash
        # does not bring the web UI down.
        #
        # The MAX listener is NOT started automatically. PyMax's
        # client.connect() runs the SmsAuthFlow (request_code) on
        # any connection without a saved session — that is the
        # "three SMS" regression the owner caught on the ODROID.
        # The MAX listener must only run while a real, persistent
        # MAX session exists. start_max_listener() is the explicit
        # gate the auth task invokes on AUTHENTICATED.
        self._supervisors["max_listener"] = ComponentSupervisor(
            name="max_listener",
            factory=self._max_listener_loop,
            backoff_initial=2.0,
            backoff_cap=30.0,
        )
        self._supervisors["telegram_listener"] = ComponentSupervisor(
            name="telegram_listener",
            factory=self._telegram_listener_loop,
            backoff_initial=2.0,
            backoff_cap=30.0,
        )
        # Mark MAX listener as NOT started until auth succeeds.
        self._max_listener_started: bool = False

        # Telegram listener can start immediately when a token
        # is configured, but it does NOT block startup; the
        # dispatcher reports NOT_CONFIGURED until then.
        for name, sup in self._supervisors.items():
            if name == "max_listener":
                continue
            sup.start()
        self.started = True
        logger.info("AppRuntime started")

    # ------------------------------------------------------------------
    # Listener-lifecycle gating
    # ------------------------------------------------------------------
    async def start_max_listener(self) -> None:
        """Start the supervised MAX receive loop.

        Called explicitly by the MAX auth service when the
        adapter transitions to AUTHENTICATED. The MAX adapter's
        connect() runs the full SmsAuthFlow whenever there is
        no saved session on disk, so we MUST NOT auto-start the
        listener at runtime start — that path was the source
        of the three-SMS regression on the ODROID.

        The supervisor's start() is the idempotence gate: a
        running task is left alone; a self-retired task (clean
        factory return, e.g. no session at boot) is re-armed.

        Returns True when the supervisor exists and was armed.
        """
        sup = self._supervisors.get("max_listener")
        if sup is None:
            logger.warning("max_listener supervisor missing")
            return False
        sup.start()
        self._max_listener_started = True
        logger.info("AppRuntime: max_listener started")
        return True

    async def stop_max_listener(self) -> None:
        """Pause the MAX receive loop (e.g. on AUTH_REQUIRED).

        Does NOT clear the persistent session — only stops the
        receive consumer so a stale token cannot keep
        re-triggering MAX auth round-trips.
        """
        sup = self._supervisors.get("max_listener")
        if sup is None:
            return
        await sup.stop()
        self._max_listener_started = False
        logger.info("AppRuntime: max_listener paused")

    async def _on_max_auth_state_change(self, snap) -> None:
        """Bridge the MAX auth state machine into the listener
        lifecycle. The listener is started when the user has
        reached AUTHENTICATED and paused when the session has
        been revoked (AUTH_REQUIRED) or the operator cancelled
        (NOT_STARTED).

        On AUTHENTICATED the canonical MaxAccount row is also
        upserted so the integration snapshot and the bridge can
        see a configured MAX (the wizard-only flow used to leave
        the DB with no MAX account at all, which made the bridge
        permanently NOT_CONFIGURED).
        """
        try:
            from .auth.max.service import MaxAuthState
            state = snap.state
            if state == MaxAuthState.AUTHENTICATED:
                await self._upsert_max_account(snap)
                await self.start_max_listener()
                # Refresh the integration probe promptly so the UI
                # reflects reality without waiting for the next
                # watchdog tick.
                loop = asyncio.get_running_loop()
                loop.create_task(self.probe_integration("max"))
            elif state in (
                MaxAuthState.NOT_STARTED,
                MaxAuthState.FAILED,
                MaxAuthState.RATE_LIMITED,
            ):
                await self.stop_max_listener()
        except Exception:  # noqa: BLE001
            logger.exception("AppRuntime: listener-lifecycle gate failed")

    async def _upsert_max_account(self, snap) -> None:
        """Persist the canonical MAX account row after auth."""
        from sqlalchemy import select

        from .database.models import MaxAccount

        async with self.db.session() as s:
            row = (
                await s.execute(select(MaxAccount).order_by(MaxAccount.id.asc()))
            ).scalars().first()
            if row is None:
                row = MaxAccount(label="primary")
                s.add(row)
            # Only the masked hint is persisted alongside the
            # account — the full canonical phone stays in RAM.
            row.phone = getattr(snap, "phone_hint", None) or row.phone
            row.account_id = getattr(snap, "account_id", None) or row.account_id
            row.display_name = (
                getattr(snap, "display_name", None) or row.display_name
            )
            row.session_path = str(
                self.layout.root / "sessions" / "max"
            )
            row.status = "connected"
            row.last_error = None
            await s.commit()

    async def stop(self) -> None:
        logger.info("AppRuntime stopping")
        self._shutdown.set()

        for sup in list(self._supervisors.values()):
            await sup.stop()

        if self._outbox_task is not None:
            self._outbox_task.cancel()
            try:
                await self._outbox_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._outbox_task = None

        for task in self._worker_tasks:
            task.cancel()
        for task in self._worker_tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._worker_tasks.clear()

        try:
            await self._max.disconnect()
        except Exception:  # noqa: BLE001
            pass
        try:
            await self._tg.disconnect()
        except Exception:  # noqa: BLE001
            pass

        # Stop the APP-OWNED Xray child (bounded) so shutdown leaves
        # no orphan process behind. The reference is installed by the
        # app lifespan (surgical review: single manager ownership).
        xray_manager = getattr(self, "xray_manager_ref", None)
        if xray_manager is not None:
            # stop() internally bounds TERM(2s)→KILL(3s) and keeps
            # the handle until reaped — no outer timeout that could
            # cancel cleanup mid-kill and orphan the child.
            try:
                await xray_manager.stop()
            except asyncio.CancelledError:
                logger.warning("xray shutdown stop cancelled mid-cleanup")
            except Exception:  # noqa: BLE001
                logger.warning("xray stop on shutdown failed")

        try:
            await self.notification_outbox.close()
        except Exception:  # noqa: BLE001
            pass

        self.started = False
        logger.info("AppRuntime stopped")

    # ------------------------------------------------------------------
    # Workers
    # ------------------------------------------------------------------
    async def _restart_workers(self) -> None:
        for t in self._worker_tasks:
            t.cancel()
        for t in self._worker_tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._worker_tasks.clear()
        for i in range(self.config.max_concurrent_workers):
            task = asyncio.create_task(self._worker_loop(i), name=f"shmax.worker.{i}")
            self._worker_tasks.append(task)

    async def _worker_loop(self, idx: int) -> None:
        lease = self.config.queue_lease_seconds
        while not self._shutdown.is_set():
            try:
                claimed = await self._claim_one(lease)
                if claimed is None:
                    # Wait for either a wakeup event or the poll
                    # timeout. Wakeup makes the test deterministic
                    # and reduces latency under live load.
                    try:
                        await asyncio.wait_for(
                            self._wakeup_event.wait(),
                            timeout=self.config.worker_poll_seconds,
                        )
                        self._wakeup_event.clear()
                    except asyncio.TimeoutError:
                        pass
                    continue
                await self._drain_one(claimed)
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                logger.exception("delivery worker %d crashed", idx)
                await asyncio.sleep(self.config.worker_poll_seconds)

    async def wakeup(self) -> None:
        """Nudge every worker so it re-checks the queue immediately.

        Called by the bridge after :meth:`enqueue`. The wakeup is
        a thin ``asyncio.Event`` so multiple enqueues during a
        single idle window coalesce into a single wake.
        """
        self._wakeup_event.set()

    async def _claim_one(self, lease: float) -> Any | None:
        async with self.db.session() as s:
            repo = DeliveryQueueRepository(s)
            claimed = await repo.claim_batch(limit=1, lease_seconds=lease)
            return claimed[0] if claimed else None

    async def _drain_one(self, rec: Any) -> None:
        from .queue.worker import RetryPolicy

        policy = RetryPolicy()
        payload = rec.payload
        try:
            delivery_result = await self._deliver(rec)
        except Exception as e:  # noqa: BLE001 - capture every failure
            logger.exception("delivery failed for queue_id=%s", rec.id)
            async with self.db.session() as s:
                repo = DeliveryQueueRepository(s)
                attempts = rec.attempts + 1
                from .adapters.max.errors import MaxUnsupportedError, MaxAuthError, MaxSessionError
                if isinstance(e, (MaxUnsupportedError, MaxAuthError, MaxSessionError)) or attempts >= policy.max_attempts:
                    await repo.mark_failed(
                        queue_id=rec.id, last_error=repr(e)[:512]
                    )
                else:
                    next_at = (
                        _dt.datetime.now(_dt.timezone.utc)
                        + _dt.timedelta(seconds=policy.next_delay(attempts - 1))
                    )
                    await repo.mark_retry(
                        queue_id=rec.id,
                        last_error=repr(e)[:512],
                        next_attempt_at=next_at,
                    )
            return
        outcome = self._to_outcome(delivery_result)
        # Release the cached media file when the row leaves
        # the queue for good — DELIVERED or FAILED — not on
        # RETRY. Retries must keep the file alive.
        media_path_str = payload.get("media_path")
        finalized = False
        try:
            if outcome.state == DeliveryState.DELIVERED:
                # Persist the mapping FIRST so a crash between the
                # external send and our ACK cannot leave the queue
                # marked delivered with no mapping on disk.
                # P1 #4: a mapping failure propagates BEFORE the
                # queue row flips to delivered — so the row goes to
                # RETRY, the media file stays on disk, and the retry
                # can re-send it. The file is only removed once the
                # item is genuinely finalized (delivered with
                # mapping, or terminal failed).
                await self._record_mapping(rec, delivery_result)
                async with self.db.session() as s:
                    await DeliveryQueueRepository(s).mark_delivered(
                        queue_id=rec.id
                    )
                finalized = True
                return
            if outcome.state == DeliveryState.RETRY:
                async with self.db.session() as s:
                    repo = DeliveryQueueRepository(s)
                    attempts = rec.attempts + 1
                    next_at = (
                        _dt.datetime.now(_dt.timezone.utc)
                        + _dt.timedelta(seconds=policy.next_delay(attempts - 1))
                    )
                    await repo.mark_retry(
                        queue_id=rec.id,
                        last_error=outcome.error or "retry",
                        next_attempt_at=next_at,
                    )
                # Retry: the media file MUST survive for the next
                # attempt — never released here.
                return
            async with self.db.session() as s:
                await DeliveryQueueRepository(s).mark_failed(
                    queue_id=rec.id, last_error=outcome.error or "failed"
                )
            finalized = True
        finally:
            # Release the cached file only when the queue item is
            # genuinely finalized. On RETRY (or when marking the
            # retry/failed state itself raised) the file stays.
            if finalized and media_path_str:
                try:
                    Path(media_path_str).unlink(missing_ok=True)
                except OSError:
                    logger.exception(
                        "media release failed path=%s",
                        media_path_str,
                    )

    def _to_outcome(self, result: Any) -> DeliveryOutcome:
        """Translate the adapter result into a queue outcome.

        A duck-typed result carries a ``state`` attribute (e.g.
        ``"retry"`` / ``"failed"`` from an adapter's own outcome
        object); only a bare send result defaults to DELIVERED.
        The previous version coerced ANY non-DeliveryOutcome —
        including RETRY outcomes — into DELIVERED, which deleted
        the cached media file on the first attempt and re-sent
        nothing (audit P1 #4)."""
        if isinstance(result, DeliveryOutcome):
            return result
        state_attr = getattr(result, "state", None)
        if state_attr is not None:
            try:
                state = DeliveryState(state_attr)
            except ValueError:
                state = None
            if state is not None:
                return DeliveryOutcome(
                    state=state,
                    error=getattr(result, "error", None),
                    external_id=getattr(result, "external_message_id", None),
                )
        return DeliveryOutcome(
            state=DeliveryState.DELIVERED,
            external_id=getattr(result, "external_message_id", None),
        )

    async def _deliver(self, rec: Any) -> Any:
        payload = rec.payload
        direction = Direction(rec.direction)
        text = payload.get("text", "")
        target_chat_id = rec.target_chat_id
        thread_id = rec.target_thread_id
        # Field names differ per direction (see BridgeEngine).
        if direction == Direction.MAX_TO_TELEGRAM:
            reply_to_external_id = payload.get("reply_to_telegram_message_id")
            reply_to_chat_id = payload.get("target_chat_id")
        else:
            reply_to_external_id = payload.get("reply_to_max_message_id")
            reply_to_chat_id = payload.get("target_chat_id")
        reply_to_thread = payload.get("reply_to_thread_id")
        media_ref = payload.get("media_ref")
        media_kind = payload.get("media_kind")

        # Build a ReplyRef if the payload carries a reply
        # mapping.
        reply_ref: ReplyRef | None = None
        if reply_to_external_id and reply_to_chat_id:
            reply_ref = ReplyRef(
                transport="telegram" if direction == Direction.MAX_TO_TELEGRAM else "max",
                chat_external_id=reply_to_chat_id,
                message_external_id=reply_to_external_id,
                thread_id=reply_to_thread,
            )

        if direction == Direction.MAX_TO_TELEGRAM:
            if media_ref:
                # Caption composition is STRUCTURAL (surgical review):
                # duplicate suppression happened at the composition
                # boundary in the bridge, using known field
                # relationships. Here the pipeline-owned caption rides
                # the media and the payload text (body/attribution/
                # fallback) is the follow-up verbatim — no lexical
                # matching against free-form text, ever.
                caption_text = payload.get("caption")
                result = await self._tg.send_media(
                    chat_id=target_chat_id,
                    media_ref=str(media_ref),
                    thread_id=thread_id,
                    kind=_canonical_media_kind(media_kind),
                    caption=caption_text,
                    **({"reply_to": reply_ref} if reply_ref else {}),
                )
                logger.info("media delivery direction=%s category=%s operation=attachment.send outcome=MEDIA_DELIVERED", direction.value, _canonical_media_kind(media_kind))
                if text:
                    # Live-found A1: the follow-up is its own native
                    # Telegram message — its id MUST be registered
                    # for echo suppression, otherwise the follow-up
                    # can loop back into the bridge (the old hidden
                    # text marker used to mask this).
                    if reply_ref is not None:
                        follow = await self._tg.send_reply(
                            chat_id=target_chat_id,
                            reply_to=reply_ref,
                            text=text,
                            thread_id=thread_id,
                        )
                    else:
                        follow = await self._tg.send_message(
                            chat_id=target_chat_id,
                            text=text,
                            thread_id=thread_id,
                        )
                    return _multi_result(result, follow)
                return result
            if reply_ref is None:
                return await self._tg.send_message(
                    chat_id=target_chat_id,
                    text=text,
                    thread_id=thread_id,
                )
            return await self._tg.send_reply(
                chat_id=target_chat_id,
                reply_to=reply_ref,
                text=text,
                thread_id=thread_id,
            )
        if direction == Direction.TELEGRAM_TO_MAX:
            if media_ref:
                # Same structural composition on the MAX side.
                caption_text = payload.get("caption")
                result = await self._max.send_media(
                    chat_id=target_chat_id,
                    media_ref=str(media_ref),
                    kind=_canonical_media_kind(media_kind),
                    caption=caption_text,
                    **({"filename": payload.get("media_filename")} if _canonical_media_kind(media_kind)=="file" and getattr(self._max,"supports_media_filename",False) else {}),
                    **({"reply_to": reply_ref} if reply_ref else {}),
                )
                logger.info("media delivery direction=%s category=%s operation=attachment.send outcome=MEDIA_DELIVERED", direction.value, _canonical_media_kind(media_kind))
                if text:
                    # Live-found A1: follow-up MAX message id must be
                    # registered too (MAX echoes its own sends back
                    # to us — see _multi_result).
                    follow = await self._max.send_text(
                        chat_id=target_chat_id, text=text
                    )
                    return _multi_result(result, follow)
                return result
            if reply_ref is not None:
                return await self._max.send_reply(
                    chat_id=target_chat_id, reply_to=reply_ref, text=text
                )
            return await self._max.send_text(chat_id=target_chat_id, text=text)
        # Unknown direction: surface as failed.
        return _FailureResult(error=f"unknown direction {rec.direction}")

    async def _record_mapping(self, rec: Any, result: Any) -> None:
        """Persist the message mapping (audit P1 #4).

        All required account IDs / chat ids / thread ids /
        message ids are taken from the queue payload (no
        silent ``dict.get`` defaults). The marker is taken
        from the payload ``marker`` field — generated once in
        the bridge and reused through the entire lifecycle.

        Semantics: this runs AFTER the external send succeeded
        and BEFORE ``mark_delivered``. A mapping failure is
        allowed to propagate so the queue row goes to RETRY —
        the message is then re-sent on the next attempt
        (at-least-once delivery) instead of the queue reporting
        DELIVERED with no mapping on disk, which would silently
        break reply threading. Exactly-once is NOT guaranteed by
        design; the trade-off is documented and pinned by tests:
        external send succeeds + mapping write fails ⇒ retry with
        a duplicate send, never a silent unmapped delivery.
        """
        payload = rec.payload
        direction = Direction(rec.direction)
        external_id = getattr(result, "external_message_id", None)
        if not external_id:
            return
        marker = payload.get("marker")
        if not marker:
            logger.error(
                "_record_mapping: missing marker for queue_id=%s",
                getattr(rec, "id", None),
            )
            return
        # Live acceptance A1: EVERY native message produced by this
        # one logical delivery is registered — the primary carries
        # the original marker; follow-ups get fresh markers (unique
        # index) and the same logical ids so echo suppression and
        # reply resolution cover them.
        all_ids = getattr(result, "all_external_ids", None) or [
            str(external_id)
        ]
        markers = [marker] + [outbound_marker() for _ in all_ids[1:]]
        if direction == Direction.MAX_TO_TELEGRAM:
            telegram_account_id = payload["telegram_account_id"]
            async with self.db.session() as s:
                for ext_id, m in zip(all_ids, markers):
                    await MessageMapRepository(s).create_for_max_to_tg(
                        max_account_id=payload["max_account_id"],
                        max_chat_id=payload["max_chat_id"],
                        max_message_id=payload["max_message_id"],
                        telegram_account_id=telegram_account_id,
                        telegram_chat_id=rec.target_chat_id,
                        telegram_thread_id=rec.target_thread_id,
                        telegram_message_id=str(ext_id),
                        outbound_marker_value=m,
                    )
                await s.commit()
            await self._register_sent_native(
                "telegram", rec, all_ids, marker
            )
            return
        if direction == Direction.TELEGRAM_TO_MAX:
            telegram_account_id = payload["telegram_account_id"]
            async with self.db.session() as s:
                for ext_id, m in zip(all_ids, markers):
                    await MessageMapRepository(s).create_for_tg_to_max(
                        telegram_account_id=telegram_account_id,
                        telegram_chat_id=payload["source_telegram_chat_id"],
                        telegram_thread_id=payload.get(
                            "source_telegram_thread_id"
                        ),
                        telegram_message_id=payload["source_telegram_message_id"],
                        max_account_id=payload["max_account_id"],
                        max_chat_id=rec.target_chat_id,
                        max_message_id=str(ext_id),
                        outbound_marker_value=m,
                    )
                await s.commit()
            await self._register_sent_native("max", rec, all_ids, marker)

    async def _register_sent_native(
        self, transport: str, rec: Any, all_ids: list, marker: str
    ) -> None:
        """Register EVERY native id of this delivery in the echo
        registry (live A1). Best-effort: a registry failure must
        not fail the delivery — the primary mapping is already
        persisted."""
        from .bridge.sent_registry import SentNativeRepository

        try:
            async with self.db.session() as s:
                repo = SentNativeRepository(s)
                for ext_id in all_ids:
                    await repo.register(
                        transport=transport,
                        chat_external_id=rec.target_chat_id,
                        message_external_id=str(ext_id),
                        marker=marker,
                    )
        except Exception:  # noqa: BLE001
            logger.exception(
                "sent-native registry write failed transport=%s",
                transport,
            )

    # ------------------------------------------------------------------
    # Outbox drain
    # ------------------------------------------------------------------
    async def _outbox_drain_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                await self._outbox_sender.drain_once()
            except Exception:  # noqa: BLE001
                logger.exception("notification outbox drain failed")
            await asyncio.sleep(self.config.outbox_poll_seconds)

    # ------------------------------------------------------------------
    # System topic destination
    # ------------------------------------------------------------------
    async def _resolve_system_topic(self) -> tuple[str, Optional[str]]:
        """Return ``(chat_id, thread_id)`` for the system topic.

        When a system route is registered in the database the
        sender routes there. Otherwise we fall back to a local
        placeholder so the outbox still has a real destination
        string; the inner dispatcher will simply log the row.
        """
        try:
            from .routing.repository import RoutesRepository

            async with self._db.session() as s:
                rec = await RoutesRepository(s).find_system_route()
        except Exception:  # noqa: BLE001
            logger.exception("system topic lookup failed; falling back to placeholder")
            return ("self", None)
        if rec is None:
            return ("self", None)
        chat_id = rec.get("telegram_chat_id") or "self"
        thread_id = rec.get("telegram_thread_id")
        return (chat_id, thread_id)

    # ------------------------------------------------------------------
    # Watchdog loops
    # ------------------------------------------------------------------
    async def _max_watchdog_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                from .integration_status import live_adapter
                if live_adapter(self._max):
                    await self.max_watchdog.tick()
                    await self.probe_integration("max")
            except Exception:  # noqa: BLE001
                logger.exception("max_watchdog.tick failed")
            await asyncio.sleep(self.config.watchdog_interval_seconds)

    async def _tg_watchdog_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                from .integration_status import live_adapter
                if live_adapter(self._tg):
                    await self.tg_watchdog.tick()
                    await self.probe_integration("telegram")
            except Exception:  # noqa: BLE001
                logger.exception("tg_watchdog.tick failed")
            await asyncio.sleep(self.config.watchdog_interval_seconds)

    # ------------------------------------------------------------------
    # Listener loops (supervised)
    # ------------------------------------------------------------------
    async def _max_listener_loop(self) -> None:
        # Gate on session availability: PyMax's connect() runs
        # the full SmsAuthFlow (request_code → SmsCodeProvider)
        # whenever there is no saved session on disk. The owner
        # observed three SMS codes from a fresh install because
        # the listener supervisor restarted the loop and each
        # restart drove a fresh request_code. We refuse to
        # connect() without a session, and the supervisor pauses
        # until AUTHENTICATED via _on_max_auth_state_change.
        health = await self._max.health()
        if not health.get("session_available"):
            logger.warning(
                "max_listener: refusing to connect — no saved MAX session. "
                "Wizard must complete MAX auth first."
            )
            return
        # Idempotent connect; consume events; stop on shutdown.
        logger.info("max_listener: starting (adapter=%s)", type(self._max).__name__)
        try:
            await self._max.connect()
            logger.info("max_listener: adapter connected")
        except Exception:  # noqa: BLE001
            logger.exception("max.connect failed")
            raise
        async for event in self._max.listen_events():
            if self._shutdown.is_set():
                break
            try:
                normalized = _max_event_to_normalized(event)
                await self._on_inbound("max", normalized)
            except Exception:  # noqa: BLE001
                logger.exception("max inbound handler failed")

    async def _telegram_listener_loop(self) -> None:
        # Audit-mandated gate: only connect() if the adapter is
        # actually configured. NotConfiguredTelegramAdapter has
        # no token; the loop returns CLEANLY (no raise) so the
        # supervisor retires instead of crash-looping with a
        # backoff. The install_telegram hot-swap re-arms the
        # supervisor once a real adapter is in place.
        if not self._tg.is_real():
            logger.warning(
                "tg_listener: not configured (adapter=%s); awaiting token",
                type(self._tg).__name__,
            )
            return
        try:
            await self._tg.connect()
        except Exception:  # noqa: BLE001
            logger.exception("telegram.connect failed")
            raise
        async for event in self._tg.listen_events():
            if self._shutdown.is_set():
                break
            try:
                normalized = _telegram_event_to_normalized(event)
                await self._on_inbound("telegram", normalized)
            except Exception:  # noqa: BLE001
                logger.exception("telegram inbound handler failed")

    # ------------------------------------------------------------------
    # P0 #11 — real-pipeline media fetching
    # ------------------------------------------------------------------
    async def _fetch_media(self, source: str, attachment):
        """Download one inbound attachment into the shared media
        pipeline.

        Returns a :class:`~shmax.media.pipeline.StoredMedia` for
        supported kinds, or ``None`` when the attachment cannot be
        delivered (the bridge then emits a controlled text
        fallback instead of silently dropping it)."""
        from .domain.enums import AttachmentKind

        kind = (
            attachment.kind.value
            if isinstance(attachment.kind, AttachmentKind)
            else str(attachment.kind)
        )
        if kind == "audio":
            kind = "voice"
        if kind not in ("photo", "document", "video", "voice", "sticker"):
            return None
        payload = dict(attachment.payload or {})
        sticker = kind == "sticker"
        if sticker and source == "telegram" and (payload.get("is_animated") or payload.get("is_video")):
            thumbnail = payload.get("thumbnail_file_id")
            if not thumbnail:
                from .media.diagnostics import MediaFailure
                raise MediaFailure("sticker.preview", "preview_unavailable")
            payload["file_id"] = thumbnail
        if sticker:
            kind = "photo"  # portable static image; preserve transparency as PNG
        ref = (  # noqa: ECE001 — structured payloads may lack a URL ref
            payload.get("file_id")
            or payload.get("url")
            or payload.get("id")
        )
        adapter = self._max if source == "max" else self._tg
        if adapter is None:
            return None
        filename = str(
            payload.get("file_name") or payload.get("name") or f"{kind}-{ref or 'media'}"
        )
        mime = str(
            payload.get("mime") or payload.get("mime_type") or "application/octet-stream"
        )

        def _downloader(dest) -> int:
            # Structured download first: MAX FILE / VIDEO payloads
            # are id-based (Client.get_file_by_id / get_video_by_id)
            # and only the adapter can resolve the URL.
            if hasattr(adapter, "download_attachment"):
                return adapter.download_attachment(payload, dest)
            if not ref:
                raise ValueError("attachment payload carries no downloadable reference")
            return adapter.download_to(str(ref), dest)

        async def validated_download(dest):
            result = _downloader(dest)
            written = await result if asyncio.iscoroutine(result) else result
            if kind == "photo":
                from .media.image import prepare_image
                await prepare_image(dest, sticker=sticker, native=sticker and source == "max")
            return dest.stat().st_size if sticker else written

        stored = await self.media_pipeline.store_async(
            filename,
            validated_download,
            kind=kind,
            mime=mime,
            duration_ms=payload.get("duration"),
        )
        if kind == "document":
            from dataclasses import replace
            safe_name=filename.replace("\\","/").rsplit("/",1)[-1]
            safe_name="".join(c for c in safe_name if ord(c)>=32)[:255] or "document"
            stored=replace(stored,filename=safe_name)
        if kind == "photo":
            from dataclasses import replace
            with stored.path.open("rb") as image:
                magic = image.read(12)
            extension, detected_mime = (".png", "image/png") if magic.startswith(b"\x89PNG") else ((".webp", "image/webp") if magic[:4] == b"RIFF" else (".jpg", "image/jpeg"))
            renamed = stored.path.with_suffix(extension)
            stored.path.rename(renamed)
            stored = replace(stored, path=renamed, filename=renamed.name, mime=detected_mime,
                             kind="sticker" if sticker and source == "max" and stored.size <= 512 * 1024 else stored.kind)
        return stored

    async def _on_inbound(self, source: str, event) -> None:
        from sqlalchemy import select as _select

        from .database.models import TelegramTarget
        from .users.repository import TelegramUsersRepository

        # Audit P0 #7: the fail-closed TG→MAX ACL needs the
        # CONFIGURED target chat — loaded from the canonical
        # TelegramTarget, never guessed. When no target exists the
        # bridge config carries no target and every TG→MAX send
        # is denied ("not configured" ≠ "accept everything").
        target_chat_id: Optional[str] = None
        target_forum = False
        async with self.db.session() as s:
            target = (await s.execute(_select(TelegramTarget).limit(1))).scalars().first()
            if target is not None:
                target_chat_id = str(target.chat_id)
                target_forum = bool(target.is_forum)
        async with self.db.session() as s:
            engine = BridgeEngine(
                config=BridgeConfig(
                    max_account_id=1,
                    telegram_account_id=1,
                    telegram_target_chat_id=target_chat_id,
                    telegram_target_forum=target_forum,
                ),
                routes=RoutesRepository(s),
                mapping=MessageMapRepository(s),
                queue=DeliveryQueueRepository(s),
                max_adapter=self._max,
                telegram_adapter=self._tg,
                acl=__import__("shmax.auth.pairing", fromlist=["AclService"]).AclService(s),
                users=TelegramUsersRepository(self.db),
                on_enqueue=self.wakeup,
                media_fetcher=self._fetch_media,
                db=self.db,
            )
            try:
                if source == "max":
                    accepted = await engine.handle_max_event(event)
                else:
                    accepted = await engine.handle_telegram_event(event)
                logger.info(
                    "_on_inbound: %s event accepted=%s",
                    source,
                    accepted,
                )
            except Exception:  # noqa: BLE001
                logger.exception("bridge handle_inbound failed")


class _FailureResult:
    def __init__(self, error: str) -> None:
        self.state = DeliveryState.FAILED
        self.error = error


class _MultiResult:
    """One logical delivery that produced SEVERAL native messages
    (media + follow-up text). The primary id keeps the original
    semantics; ``all_external_ids`` lets _record_mapping register
    EVERY native id for echo suppression / reply mapping (live
    acceptance A1: without this the follow-up message could loop
    back into the bridge once the visible marker was removed)."""

    def __init__(self, primary: Any, *extras: Any) -> None:
        self._primary = primary
        ids: list[str] = []
        for r in (primary, *extras):
            rid = getattr(r, "external_message_id", None)
            if rid is not None:
                ids.append(str(rid))
        self.external_message_id = ids[0] if ids else getattr(
            primary, "external_message_id", None
        )
        self.all_external_ids = ids
        # Preserve failure/retry semantics of the primary result.
        state = getattr(primary, "state", None)
        if state is not None:
            self.state = state

    @property
    def error(self) -> Any:
        return getattr(self._primary, "error", None)


def _multi_result(primary: Any, *extras: Any) -> Any:
    if not extras:
        return primary
    return _MultiResult(primary, *extras)


__all__ = ["AppRuntime", "ComponentSupervisor", "RuntimeConfig", "RuntimeStatus"]
