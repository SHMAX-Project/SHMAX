"""SHMAX FastAPI application factory.

Stage 1B V1 surface.

Routes (admin-only unless noted):

* ``GET  /health/live``       — process alive (Docker)
* ``GET  /health/ready``      — DB + storage ready (Docker)
* ``GET  /health``            — backward-compatible snapshot
* ``GET  /``                  — landing page
* ``POST /api/setup/token``   — bootstrap token
* ``POST /api/setup/admin``   — create first admin
* ``POST /api/auth/login``
* ``POST /api/auth/logout``
* ``GET  /api/status``        — dashboard
* ``GET  /api/doctor``
* ``GET  /api/queue``
* ``GET  /api/routes``        / ``POST /api/routes``
* ``POST /api/routes/{id}/toggle``
* ``POST /api/routes/{id}/recreate-topic``
* ``GET  /api/max/chats``
* ``POST /api/max/probe``
* ``POST /api/max/reconnect``
* ``GET  /api/telegram/chats``
* ``POST /api/telegram/verify``
* ``POST /api/telegram/system-topic``
* ``GET  /api/backups``       / ``POST /api/backups``  / ``POST /api/backups/{name}/download``  / ``POST /api/backups/{name}/restore``
* ``GET  /api/update/status`` / ``POST /api/update/check`` / ``POST /api/update/apply`` / ``POST /api/update/rollback``
* ``GET  /api/watchdogs/max`` / ``GET  /api/watchdogs/telegram``
* ``POST /api/watchdogs/{adapter}/probe`` / ``POST /api/watchdogs/{adapter}/self-heal``
* ``POST /api/support-bundle``
* ``POST /api/storage/clear-cache``
* ``GET  /api/storage``
* ``POST /api/pairing/{group,user}``   (admin-only)
* ``POST /api/pairing/consume``         (admin-only)
* ``GET  /api/logs``
* ``POST /api/danger-zone/purge``       (admin + body confirmation)
* ``GET  /api/recent-activity``
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Optional

from fastapi import (
    Body,
    Cookie,
    Depends,
    FastAPI,
    HTTPException,
    Header,
    Request,
    Response,
    status,
)
from fastapi.responses import HTMLResponse, JSONResponse, Response as FastResponse
from pydantic import field_validator, BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .. import __service_name__, __version__
from ..auth import PasswordHasher, RateLimiter, SessionSigner, SetupTokenService
from ..auth.pairing import PairingCodeService
from ..auth.rate_limit import RateLimitConfig
from ..backup.manager import (
    BACKUP_FORMAT_ID,
    TransactionalRestore,
    RestoreError,
    create_backup,
    create_encrypted_backup,
    inspect_backup,
    is_encrypted,
    list_backups,
    safe_backup_name,
    validate_backup,
)
from ..config import Settings
from ..core.paths import PathLayout
from ..database import Database, DatabaseSettings, run_migrations_sync
from ..database.models import AdminSession, AdminUser, Route, TelegramAccount, Setting
from ..diagnostics import Doctor
from ..host.protocol import InProcessHostHelper
from ..logging_setup import configure_logging
from ..media.pipeline import MediaPipeline
from ..queue.repository import DeliveryQueueRepository
from ..routing.repository import RoutesRepository
from ..security import SecretStore
from ..support_bundle import collect_support_bundle, write_support_bundle
from ..watchdog.base import (
    LoggingNotificationDispatcher,
    WatchdogStateStore,
)


logger = logging.getLogger(__name__)


SESSION_COOKIE = "shmax_session"
CSRF_COOKIE = "shmax_csrf"
CSRF_HEADER = "X-CSRF-Token"

# Final repair #3: pair-code returns a code ONLY after the
# persistent Telegram listener is confirmed alive within this
# bounded timeout (tests may shorten it via monkeypatch).
PAIR_LISTENER_READY_TIMEOUT = 10.0


async def _await_listener_ready(runtime, adapter, timeout: Optional[float] = None) -> bool:
    """Bounded wait until the persistent Telegram listener is
    actually RECEIVING: the supervisor task is alive AND the
    adapter finished connect() (``_started``) AND its polling task
    exists and is not done. False on timeout."""
    deadline = asyncio.get_running_loop().time() + (
        PAIR_LISTENER_READY_TIMEOUT if timeout is None else timeout
    )
    sup = getattr(runtime, "_supervisors", {}).get("telegram_listener")
    while asyncio.get_running_loop().time() < deadline:
        sup_task = getattr(sup, "_task", None)
        polling = getattr(adapter, "_polling_task", None)
        if (
            sup_task is not None
            and not sup_task.done()
            and bool(getattr(adapter, "_started", False))
            and polling is not None
            and not polling.done()
        ):
            return True
        await asyncio.sleep(0.2)
    return False


# ---------------------------------------------------------------------
# Pydantic shapes
# ---------------------------------------------------------------------
class StatusSnapshot(BaseModel):
    service: str
    version: str
    data_dir: str
    uptime_seconds: float
    max: dict
    telegram: dict
    bridge: dict
    queue: dict
    database: str
    storage: dict
    watchdogs: dict
    update: dict


class LoginRequest(BaseModel):
    username: str
    password: str


class SetupAdminRequest(BaseModel):
    username: str
    password: str
    token: str


class CreateRouteRequest(BaseModel):
    max_account_id: int
    max_chat_id: str
    telegram_account_id: int
    telegram_chat_id: str
    telegram_thread_id: Optional[str] = None
    label: Optional[str] = None
    enabled: bool = True


class ToggleRequest(BaseModel):
    enabled: bool


# ---------------------------------------------------------------------
# Module-level request models.
#
# FastAPI resolves function annotations against the function's
# ``__globals__``. With ``from __future__ import annotations`` the
# annotations are stored as strings, and a class defined inside a
# nested function (``create_app``) is NOT in any handler's globals
# — so FastAPI cannot resolve ``_SomeRequest`` and falls back to
# treating the parameter as a Query, returning 422 with
# ``"loc": ["query", "req"]`` for every POST that expects a JSON
# body.
#
# Hoist every request model used by a route handler here. New
# request models MUST be added in this section, never inside
# ``create_app``. Production safety net: any class definition found
# inside ``create_app`` whose name starts with ``_`` and ends with
# ``Request`` is a code smell and is rejected by the runtime
# startup self-check (see RuntimeMode).
# ---------------------------------------------------------------------
class MaxAuthStartRequest(BaseModel):
    phone: str


class MaxAuthCodeRequest(BaseModel):
    code: str


class MaxAuthPasswordRequest(BaseModel):
    password: str


class RouteCreateFromChatRequest(BaseModel):
    max_chat_id: str
    max_chat_title: str
    telegram_chat_id: str
    max_account_id: Optional[int] = None
    telegram_account_id: Optional[int] = None


class MemberSyncCredentials(BaseModel):
    api_id: int = Field(gt=0, le=2147483647)
    api_hash: str = Field(default="", max_length=32, pattern=r"^(?:[a-fA-F0-9]{32})?$")


class TelegramTokenRequest(BaseModel):
    token: str


class TelegramPairRequest(BaseModel):
    code: str
    chat_id: Optional[str] = None


class TelegramProxyRequest(BaseModel):
    mode: str
    proxy_url: Optional[str] = None
    # Structured proxy fields (network bootstrap): preferred over
    # proxy_url; credentials go straight to the SecretStore.
    proxy_host: Optional[str] = None
    proxy_port: Optional[int] = None
    proxy_username: Optional[str] = None
    proxy_password: Optional[str] = None
    api_id: Optional[int] = None
    api_hash: Optional[str] = None
    mtproxy_host: Optional[str] = None
    mtproxy_port: Optional[int] = None
    mtproxy_secret: Optional[str] = None
    # Network bootstrap (surgical): the Network step runs BEFORE the
    # bot token exists — skip the getMe probe entirely and only test
    # Telegram HTTPS reachability.
    skip_bot_probe: bool = False


class BackupRequest(BaseModel):
    passphrase: Optional[str] = Field(default=None, min_length=8)


class RestoreRequest(BaseModel):
    passphrase: Optional[str] = None


class PairingRequest(BaseModel):
    purpose: str = Field(default="group")  # "group" | "user"
    ttl_seconds: int = Field(default=900, ge=60, le=86_400)


class PairingConsumeRequest(BaseModel):
    code: str
    purpose: str = Field(default="group")


class XraySubscribeRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


class XraySelectRequest(BaseModel):
    node_id: str = Field(min_length=1, max_length=64)


class GroupPairingIssueRequest(BaseModel):
    """Audit P0 #6 / P0 #2: wizard-issued pairing code.

    ``max_chat_id`` is optional: the canonical V1 flow pairs the
    Telegram GROUP itself (routes are created later from the
    MAX-chats step); a code may optionally carry a MAX chat
    binding which the receiver turns into a route directly.
    """

    max_chat_id: Optional[str] = Field(default=None, max_length=128)
    max_chat_title: Optional[str] = Field(default=None, max_length=256)
    telegram_chat_hint: Optional[str] = Field(default=None, max_length=128)
    ttl_seconds: int = Field(default=600, ge=60, le=3600)


class UserApprovalRequest(BaseModel):
    """Audit P0 #7: operator-controlled approval / role."""

    approved: bool
    role: str = Field(default="read_only")  # one of Role enum


class UserBlockedRequest(BaseModel):
    """Audit P0 #7: operator-controlled block toggle."""

    blocked: bool


class UserNicknameRequest(BaseModel):
    """Audit P0 #7 / audit #8: operator-chosen nickname."""

    nickname: Optional[str] = None

    @field_validator("nickname")
    @classmethod
    def validate_nickname(cls, value: Optional[str]) -> Optional[str]:
        from ..users.nickname import normalize_nickname
        return normalize_nickname(value)


class DangerZoneRequest(BaseModel):
    username: str
    password: str
    confirmation: str  # must equal "SHMAX"
    purge: bool = False


class DoctorResponse(BaseModel):
    results: list[dict]


class WatchdogResponse(BaseModel):
    adapter: str
    state: str
    last_success_at: Optional[int]
    last_probe_at: Optional[int]
    down_since: Optional[int]
    consecutive_failures: int
    last_error_class: Optional[str]
    last_error_detail: Optional[str]


class StorageResponse(BaseModel):
    db_bytes: int
    sessions_bytes: int
    backups_bytes: int
    cache_bytes: int
    logs_bytes: int


# ---------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------
async def _run_wizard_test_probes(runtime) -> dict:
    """Audit P0 #12 / P0 #13: the Test step must ACTIVELY verify
    the runtime and classify every probe.

    Each entry in ``checks`` includes:

      * ``id`` — short step identifier
      * ``label`` — short human-readable description (Russian)
      * ``kind`` — ACTIVE EXTERNAL CHECK (a real call to MAX /
        Telegram), RUNTIME CHECK (live in-process machinery) or
        PERSISTENCE CHECK (validated DB state). The overall
        verdict requires all three kinds to pass — a non-empty
        dict or an existing report object is never health.
      * ``ok`` — boolean pass/fail
      * ``detail`` — diagnostic string (no secrets)
      * ``fix_step`` — wizard step to send the operator to when
        the check fails. Empty string when no fix is known.
    """
    from sqlalchemy import func as _func, select as _select

    checks: list[dict] = []

    # ---- MAX — ACTIVE EXTERNAL CHECK --------------------------
    max_adapter = getattr(runtime, "_max", None)
    max_ok = False
    max_detail = ""
    if max_adapter is not None:
        try:
            health = await runtime._max.health()  # type: ignore[attr-defined]
            session_available = bool(health.get("session_available"))
            if session_available:
                try:
                    profile = await runtime._max.get_profile()  # type: ignore[attr-defined]
                    max_ok = bool(profile.user_id)
                    max_detail = (
                        f"profile {profile.display_name} (@{profile.handle})"
                        if max_ok else "no profile"
                    )
                except Exception:  # noqa: BLE001
                    max_ok = False
                    max_detail = "get_profile failed — session unusable"
            else:
                max_detail = "no saved MAX session — complete phone + OTP"
        except Exception as e:  # noqa: BLE001
            max_ok = False
            max_detail = f"health() raised: {type(e).__name__}"
    else:
        max_detail = "MAX adapter not installed"

    checks.append({
        "id": "max.authenticated",
        "kind": "active_external",
        "label": "MAX подключён (проверка профиля)",
        "ok": max_ok,
        "detail": max_detail,
        "fix_step": "max" if not max_ok else "",
    })

    # MAX chat listing — ACTIVE EXTERNAL CHECK.
    max_chats_ok = False
    max_chats_detail = ""
    if max_ok:
        try:
            chats = await runtime._max.get_chats()  # type: ignore[attr-defined]
            max_chats_ok = len(chats) >= 1
            max_chats_detail = f"{len(chats)} chat(s)"
        except Exception as e:  # noqa: BLE001
            max_chats_detail = f"get_chats failed: {type(e).__name__}"
    checks.append({
        "id": "max.chats",
        "kind": "active_external",
        "label": "MAX отдаёт список чатов",
        "ok": max_chats_ok,
        "detail": max_chats_detail,
        "fix_step": "max" if not max_chats_ok else "",
    })

    # ---- Telegram getMe — ACTIVE EXTERNAL CHECK ----------------
    # P0 #12: interpret the ACTUAL health result. `bool(health)`
    # is always True for the non-empty dict this adapter returns —
    # the old check was a false PASS for a dead transport.
    tg_adapter = getattr(runtime, "_tg", None)
    tg_getme_ok = False
    tg_getme_detail = ""
    if tg_adapter is not None:
        try:
            health = await runtime._tg.health()  # type: ignore[attr-defined]
            bot_id = health.get("bot_id")
            connected = bool(health.get("connected"))
            authenticated = bool(health.get("authenticated"))
            tg_getme_ok = bool(bot_id and connected and authenticated)
            tg_getme_detail = (
                f"getMe ok: {bot_id}; получение обычных сообщений группы проверяется отдельно (/setprivacy, права бота)" if tg_getme_ok
                else f"transport not usable (connected={connected}, authenticated={authenticated})"
            )
        except Exception as e:  # noqa: BLE001
            tg_getme_detail = f"health() raised: {type(e).__name__}"
    checks.append({
        "id": "telegram.get_me",
        "kind": "active_external",
        "label": "Telegram Bot API отвечает (getMe)",
        "ok": tg_getme_ok,
        "detail": tg_getme_detail,
        "fix_step": "telegram_bot" if not tg_getme_ok else "",
    })

    # Telegram listener alive — RUNTIME CHECK. getMe success is
    # NOT enough: the supervisor task AND the adapter's polling
    # task must both be running (audit P0 #2 — a dead polling
    # loop idling behind a live listen_events() queue is not a
    # healthy listener).
    tg_listener_alive = False
    tg_listener_detail = "supervisor not started"
    try:
        sup = runtime._supervisors.get("telegram_listener")  # type: ignore[attr-defined]
        task = getattr(sup, "_task", None) if sup is not None else None
        supervisor_alive = task is not None and not task.done()
        adapter_is_fake = tg_adapter is not None and not getattr(
            tg_adapter, "is_real", lambda: False
        )()
        if supervisor_alive and tg_adapter is not None and not adapter_is_fake:
            tg_health = await runtime._tg.health()  # type: ignore[attr-defined]
            polling_flag = tg_health.get("polling")
            tg_listener_alive = polling_flag is not False
            tg_listener_detail = (
                "polling active" if tg_listener_alive
                else f"polling dead: {tg_health.get('polling_error') or 'task finished'}"
            )
        elif tg_adapter is not None and adapter_is_fake:
            # Test/development boundary: fakes have no polling task.
            # The receive path is proven by the integration evidence
            # — a fresh verified probe (HEALTHY), never a bare flag.
            from ..integration_status import _counts_as_live

            if _counts_as_live(tg_adapter, getattr(runtime, "runtime_mode", None)):
                snap_state = (
                    await runtime.integration_snapshot()
                ).get("telegram", {}).get("state")
                tg_listener_alive = snap_state == "HEALTHY"
                tg_listener_detail = (
                    "receive path verified (test boundary)"
                    if tg_listener_alive
                    else f"fake adapter evidence={snap_state}"
                )
            else:
                tg_listener_detail = "receive path not verifiable (not configured)"
        elif supervisor_alive:
            tg_listener_alive = True
            tg_listener_detail = "polling active"
        else:
            tg_listener_detail = "polling stopped"
    except Exception as e:  # noqa: BLE001
        tg_listener_detail = f"listener check raised: {type(e).__name__}"
    checks.append({
        "id": "telegram.listener_alive",
        "kind": "runtime",
        "label": "Telegram listener работает",
        "ok": tg_listener_alive,
        "detail": tg_listener_detail,
        "fix_step": "telegram_network" if not tg_listener_alive else "",
    })

    # ---- Telegram target — ACTIVE EXTERNAL + persistence ------
    # P0 #12: a target row alone is NOT sufficient. P1 #5: stored
    # validation facts (validated_at / missing_permissions=[]) go
    # STALE — rights can be revoked or Topics disabled after
    # pairing. The Test step performs a LIVE getChat +
    # getChatMember round-trip and compares against the current
    # requirements; the stored row is only the fallback when no
    # live adapter is available (offline unit wiring).
    from sqlalchemy import update as _sa_update

    from ..active_target import get_active_target
    from ..database.models import TelegramTarget as _TT
    target_ok = False
    target_detail = ""
    target_row = await get_active_target(runtime.db)
    if target_row is None:
        target_detail = "run /shmax_connect in your supergroup"
    else:
        live_adapter = runtime.telegram_adapter if runtime else None
        live_checked = False
        if live_adapter is not None and getattr(live_adapter, "is_real", lambda: False)():
            live_checked = True  # a live check WAS attempted — its
            # outcome is authoritative; stored fallback must not
            # resurrect a pass after a failed round-trip.

            # Surgical review: persistence must go through an
            # EXPLICIT UPDATE BY ID in the SAME session — mutating
            # a detached row and committing elsewhere persisted
            # nothing.
            async def _persist_target_facts(*, facts: dict) -> None:
                async with runtime.db.session() as s:  # type: ignore[attr-defined]
                    await s.execute(
                        _sa_update(_TT)
                        .where(_TT.id == target_row.id)
                        .values(**facts)
                    )
                    await s.commit()

            try:
                summary = await live_adapter.get_chat_summary(str(target_row.chat_id))
                perms = await live_adapter.get_bot_permissions(str(target_row.chat_id))
                missing = _validate_pairing_chat(summary, perms)
                if missing:
                    target_detail = f"права отозваны/изменились: {', '.join(missing)}"
                    # Failed validation persists the INVALID state —
                    # stale positive facts must not fool Finish.
                    await _persist_target_facts(
                        facts={
                            "is_forum": bool(summary.get("is_forum")),
                            "chat_type": summary.get("type"),
                            "bot_status": perms.get("status"),
                            "missing_permissions": json.dumps(
                                missing, ensure_ascii=False
                            ),
                            "validated_at": None,
                            "cached_at": _dt.datetime.now(_dt.timezone.utc),
                        }
                    )
                else:
                    # Current validation facts persisted.
                    await _persist_target_facts(
                        facts={
                            "is_forum": bool(summary.get("is_forum")),
                            "chat_type": summary.get("type"),
                            "bot_status": perms.get("status"),
                            "missing_permissions": "[]",
                            "validated_at": _dt.datetime.now(_dt.timezone.utc),
                            "cached_at": _dt.datetime.now(_dt.timezone.utc),
                        }
                    )
                    target_ok = True
                    target_detail = f"validated live: {summary.get('title') or target_row.chat_id}"
            except Exception as e:  # noqa: BLE001
                target_detail = (
                    f"live validation failed ({type(e).__name__}) — "
                    "проверьте, что бот всё ещё в группе"
                )
                # A failed live round-trip also invalidates the
                # stored facts: without a live confirmation we do
                # not KNOW the group is still valid.
                try:
                    await _persist_target_facts(
                        facts={
                            "missing_permissions": json.dumps(
                                [f"live validation failed: {type(e).__name__}"],
                                ensure_ascii=False,
                            ),
                            "validated_at": None,
                            "cached_at": _dt.datetime.now(_dt.timezone.utc),
                        }
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("persisting invalid target facts failed")
        if not live_checked:
            try:
                missing = list(json.loads(target_row.missing_permissions or "[]"))
            except (TypeError, ValueError):
                missing = ["validation state unreadable"]
            if target_row.validated_at is None:
                target_detail = "group saved but never validated against Telegram API"
            elif missing:
                target_detail = f"missing rights: {', '.join(missing)}"
            else:
                target_ok = True
                target_detail = f"validated: {target_row.title or target_row.chat_id}"
    checks.append({
        "id": "telegram.target_paired",
        "kind": "active_external",
        "label": "Telegram группа подключена и проверена",
        "ok": target_ok,
        "detail": target_detail,
        "fix_step": "telegram_group" if not target_ok else "",
    })

    # ---- Routes — PERSISTENCE CHECK ----------------------------
    # Routes must map the CURRENT validated target; a route to a
    # stale chat id is a failure, not a pass.
    from ..database.models import Route as _Route
    routes_ok = False
    routes_detail = ""
    async with runtime.db.session() as s:  # type: ignore[attr-defined]
        routes = (await s.execute(_select(_Route).where(_Route.enabled.is_(True)))).scalars().all()
    if target_row is None:
        routes_detail = "no paired target"
    else:
        valid = [
            r for r in routes
            if str(r.telegram_chat_id) == str(target_row.chat_id)
        ]
        if not valid:
            routes_ok = False
            routes_detail = "routes exist but none maps the paired group"
        elif target_row.is_forum and any(not r.telegram_thread_id for r in valid):
            # Mirror the readiness contract: forum routes need a topic.
            routes_ok = False
            routes_detail = (
                "forum route(s) without a topic (thread id) — recreate"
            )
        else:
            routes_ok = True
            routes_detail = (
                f"{len(valid)} route(s) to the paired group"
            )
    checks.append({
        "id": "routes.enabled",
        "kind": "persistence",
        "label": "Маршруты ведут в подключённую группу",
        "ok": routes_ok,
        "detail": routes_detail,
        "fix_step": "chats" if not routes_ok else "",
    })

    # ---- Approved users — PERSISTENCE CHECK --------------------
    # P0 #12: approval alone does not prove the ACL is usable —
    # the role must actually grant send_message.
    from ..database.models import TelegramUser as _TU
    from ..domain.enums import Role as _Role
    from ..auth.pairing import DEFAULT_PERMISSIONS as _PERMS
    users_ok = False
    users_detail = ""
    async with runtime.db.session() as s:  # type: ignore[attr-defined]
        approved_rows = (
            await s.execute(
                _select(_TU).where(_TU.approved.is_(True), _TU.blocked.is_(False))
            )
        ).scalars().all()
    usable = 0
    for u in approved_rows:
        try:
            role = _Role(u.role)
        except ValueError:
            continue
        if "send_message" in _PERMS.get(role, set()):
            usable += 1
    users_ok = usable >= 1
    users_detail = (
        f"{usable} approved user(s) may send"
        if users_ok
        else "no approved user with send_message role"
    )
    checks.append({
        "id": "users.approved",
        "kind": "persistence",
        "label": "Одобренные пользователи могут писать",
        "ok": users_ok,
        "detail": users_detail,
        "fix_step": "users" if not users_ok else "",
    })

    # ---- Workers — RUNTIME CHECK --------------------------------
    work_tasks = list(getattr(runtime, "_worker_tasks", []) or [])
    workers_alive = bool(work_tasks) and all(not t.done() for t in work_tasks)
    checks.append({
        "id": "bridge.workers_alive",
        "kind": "runtime",
        "label": "Обработчики очереди работают",
        "ok": workers_alive,
        "detail": f"{len(work_tasks)} worker(s)",
        "fix_step": "" if workers_alive else "diagnostics",
    })

    # ---- Queue — RUNTIME CHECK ---------------------------------
    # P0 #12: table existence is not an operational queue. Drive
    # the real claim machinery once: a bounded claim_batch round
    # trip proves the queue machinery actually works.
    queue_ok = False
    queue_detail = ""
    try:
        from ..queue.repository import DeliveryQueueRepository
        async with runtime.db.session() as s:  # type: ignore[attr-defined]
            counts = await DeliveryQueueRepository(s).counts_by_state()
            claimed = await DeliveryQueueRepository(s).claim_batch(
                limit=1, lease_seconds=5
            )
            # Undo the canary claim immediately — the probe must
            # not consume real traffic. reset_for_retry only
            # accepts failed rows, so restore the claimed row
            # directly to its pre-claim state.
            from sqlalchemy import update as _update

            from ..database.models import DeliveryQueueItem as _DQI
            for c in claimed:
                await s.execute(
                    _update(_DQI)
                    .where(_DQI.id == c.id)
                    .values(
                        status="pending",
                        claimed_at=None,
                        lease_until=None,
                        worker_id=None,
                    )
                )
            await s.commit()
        queue_ok = True
        queue_detail = (
            f"claim machinery ok; pending={counts.get('pending', 0)}, "
            f"processing={counts.get('processing', 0)}, "
            f"delivered={counts.get('delivered', 0)}, "
            f"failed={counts.get('failed', 0)}"
        )
    except Exception as e:  # noqa: BLE001
        queue_detail = f"queue machinery probe failed: {type(e).__name__}"
    checks.append({
        "id": "bridge.queue_operational",
        "kind": "runtime",
        "label": "Очередь доставки работает",
        "ok": queue_ok,
        "detail": queue_detail,
        "fix_step": "" if queue_ok else "diagnostics",
    })

    overall_ok = all(c["ok"] for c in checks)
    return {
        "ok": overall_ok,
        "checks": checks,
        "summary": (
            "All V1 readiness checks PASS."
            if overall_ok
            else f"{sum(1 for c in checks if not c['ok'])} check(s) failed."
        ),
    }
def _validate_pairing_chat(summary: dict, perms: dict) -> "list[str]":
    """Audit P0 #3 — derive the EXACT list of missing group
    requirements from live Telegram API facts. An empty list
    means the group is fully usable by SHMAX.

    The facts MUST come from ``get_chat_summary`` /
    ``get_bot_permissions`` (real Bot API state) — never from
    something the frontend or a manual chat-id entry supplied.
    """
    missing: list[str] = []
    chat_type = (summary.get("type") or "").lower()
    if chat_type != "supergroup":
        missing.append(
            "чат должен быть супергруппой (сейчас: "
            f"{chat_type or 'неизвестно'})"
        )
    if not summary.get("is_forum"):
        missing.append("включите «Темы» (Topics/Forum) в настройках группы")
    if not perms.get("is_member"):
        missing.append("добавьте бота в группу")
    elif not perms.get("can_send_messages"):
        missing.append("дайте боту право отправлять сообщения")
    if summary.get("is_forum") and perms.get("is_member"):
        if not perms.get("can_manage_topics"):
            missing.append(
                "дайте боту право управлять темами (Manage Topics)"
            )
    return missing


def _build_telegram_command_handler(
    *,
    users: "TelegramUsersRepository",
    pairing_codes: "PairingCodesRepository",
    secret_store: SecretStore,
    db: Database,
    adapter_getter: "Optional[Callable[[], Any]]" = None,
    limiter: "Optional[RateLimiter]" = None,
    on_paired: "Optional[Callable[[], Any]]" = None,
    member_sync=None,
) -> "Callable[[str, dict], Awaitable[None]]":
    """Build THE canonical slash-command handler.

    One handler, one code store, one receive path, one persisted
    target model (audit P0 #2). The Telegram adapter invokes it
    for every slash command — private chats get /start and
    /setname, the target supergroup consumes /shmax_connect CODE.

    ``adapter_getter`` returns the LIVE Telegram adapter used for
    P0 #3 group validation (getChat / getChatMember). When it is
    absent (unit tests) validation is skipped and the target is
    stored unvalidated — the wizard never treats an unvalidated
    target as ready.
    """

    async def _handler(command: str, payload: dict) -> None:
        user_id = payload.get("user_id")
        if not user_id:
            return
        display_name = payload.get("display_name")
        username = payload.get("username")
        text: str = (payload.get("text") or "").strip()
        # The adapter now passes the RAW Telegram chat.type string
        # ("private" / "group" / "supergroup") — never guess.
        chat_kind = payload.get("chat_kind")
        chat_id = payload.get("chat_id")
        thread_id = payload.get("thread_id")

        async def reply(message: str) -> None:
            respond = payload.get("respond")
            if respond is None:
                return
            try:
                await respond(message)
            except Exception:  # noqa: BLE001
                logger.exception("pairing respond() failed")

        parts = text.split()
        # Persist contact info on every command so the web UI
        # always shows the latest Telegram display name / @handle.
        await users.register_or_update(
            user_id=user_id,
            display_name=display_name,
            username=username,
        )

        if command == "/start" or text == "/start":
            # P1 #8: onboarding must respond, never silently
            # consume the command. Private chat only.
            if chat_kind not in (None, "private"):
                await reply(
                    "Команда /start работает только в личном чате с ботом."
                )
                return
            row = await users.get(user_id)
            if row is not None and row.approved and not row.blocked:
                await reply(
                    f"Привет, {display_name or 'друг'}! Ваш доступ к SHMAX "
                    "подтверждён — пишите в тему нужного чата."
                )
            elif row is not None and row.blocked:
                await reply(
                    "Ваш доступ к SHMAX заблокирован владельцем."
                )
            else:
                await reply(
                    "Вы зарегистрированы. Ожидайте подтверждения владельца SHMAX."
                )
            return

        if command == "/setname":
            if chat_kind not in (None, "private"):
                await reply(
                    "Команда /setname работает только в личном чате с ботом."
                )
                return
            if len(parts) < 2:
                await reply(
                    "Использование: /setname <псевдоним> — например, "
                    "/setname Александра"
                )
                return
            nickname = " ".join(parts[1:]).strip()
            nickname = nickname.replace("\n", " ").replace("\r", " ").strip()
            if not nickname:
                await reply("Псевдоним не может быть пустым.")
                return
            if len(nickname) > 64:
                nickname = nickname[:64].rstrip()
            try:
                await users.set_nickname(user_id=user_id, nickname=nickname)
            except ValueError:
                await reply("Подпись не должна содержать управляющие символы.")
                return
            await reply(f"Псевдоним сохранён: «{nickname}».")
            return

        if command == "/shmax_connect":
            # The group-pairing primitive. Guards:
            #   * must come from a GROUP chat (raw type
            #     "supergroup" / "group" — DM is rejected);
            #   * the code must exist, be unconsumed and unexpired
            #     (single canonical store: PairingCodesRepository);
            #   * the group must satisfy the P0 #3 checklist, else
            #     the operator gets the exact missing rights and
            #     the code stays valid for a corrected retry.
            if chat_kind not in ("supergroup", "group") or not chat_id:
                await reply(
                    "Команду /shmax_connect нужно отправить в группе, "
                    "которую вы хотите подключить к SHMAX."
                )
                return
            if len(parts) < 2:
                await reply(
                    "Использование: /shmax_connect КОД — код выдаёт "
                    "мастер настройки SHMAX."
                )
                return
            lim = limiter
            if lim is not None and lim.is_locked("pairing", str(chat_id)):
                remaining = int(lim.lockout_remaining("pairing", str(chat_id)))
                await reply(
                    "Слишком много неверных попыток. Подождите "
                    f"{max(remaining, 1)} с и попробуйте снова."
                )
                return
            code = parts[1].strip()

            # P0 #3: validate the chat against the REAL Telegram
            # API before consuming anything. Without a live adapter
            # (unit-test wiring only) the target is persisted as
            # explicitly UNVALIDATED and never counts as paired.
            missing: list[str] = []
            summary: dict = {}
            perms: dict = {}
            adapter = adapter_getter() if adapter_getter is not None else None
            if adapter is not None:
                try:
                    summary = await adapter.get_chat_summary(str(chat_id))
                    perms = await adapter.get_bot_permissions(str(chat_id))
                    missing = _validate_pairing_chat(summary, perms)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "pairing chat validation failed: %s", type(e).__name__
                    )
                    missing = [
                        "не удалось проверить группу через Telegram API — "
                        "убедитесь, что бот добавлен в группу, и повторите"
                    ]
            else:
                missing = ["группа не проверена через Telegram API"]
            if missing:
                if lim is not None:
                    lim.record_attempt("pairing", str(chat_id), success=False)
                await reply(
                    "Группа пока не готова:\n• "
                    + "\n• ".join(missing)
                    + "\nИсправьте и отправьте команду ещё раз — код остаётся действительным."
                )
                return

            try:
                claim = await pairing_codes.atomic_consume(
                    code=code,
                    user_id=user_id,
                    telegram_chat_id=str(chat_id),
                    telegram_thread_id=(
                        int(thread_id) if thread_id is not None else None
                    ),
                )
            except Exception:  # noqa: BLE001
                # PairingInvalid or a DB hiccup. Reply with a
                # generic message — never leak whether a code
                # exists (brute-force resistance).
                if lim is not None:
                    lim.record_attempt("pairing", str(chat_id), success=False)
                await reply(
                    "Код недействителен: проверьте код в мастере настройки "
                    "SHMAX и отправьте команду ещё раз."
                )
                return
            if lim is not None:
                lim.record_attempt("pairing", str(chat_id), success=True)
                lim.reset("pairing", str(chat_id))

            # Persist the CANONICAL TelegramTarget with the
            # validated facts, then (optionally) the route.
            from ..routing.repository import RoutesRepository
            from sqlalchemy import select as _sa_select

            from ..database.models import (
                MaxAccount,
                TelegramAccount,
            )

            group_title = (
                payload.get("chat_title")
                or summary.get("title")
                or str(chat_id)
            )
            async with db.session() as session:  # type: ignore[attr-defined]
                tg_row = (
                    await session.execute(
                        _sa_select(TelegramAccount).order_by(
                            TelegramAccount.id.asc()
                        )
                    )
                ).scalars().first()
                if tg_row is None:
                    tg_row = TelegramAccount(label="primary")
                    session.add(tg_row)
                    await session.flush()

                # A code may carry a MAX-chat binding. When it does
                # not (canonical V1 wizard flow) the route is
                # created later from the MAX-chats step.
                route_bound = False
                if claim.max_chat_id:
                    max_row = (
                        await session.execute(
                            _sa_select(MaxAccount).order_by(
                                MaxAccount.id.asc()
                            )
                        )
                    ).scalars().first()
                    if max_row is not None:
                        topic_id: Optional[str] = (
                            str(thread_id) if thread_id is not None else None
                        )
                        if topic_id is None and adapter is not None:
                            # Create a dedicated topic for the chat.
                            try:
                                topic_id = await adapter.create_topic(
                                    chat_id=str(chat_id),
                                    title=claim.max_chat_title
                                    or claim.max_chat_id,
                                )
                            except Exception:  # noqa: BLE001
                                logger.exception(
                                    "pairing create_topic failed"
                                )
                        await RoutesRepository(session).upsert(
                            max_account_id=max_row.id,
                            max_chat_id=claim.max_chat_id,
                            telegram_account_id=tg_row.id,
                            telegram_chat_id=str(chat_id),
                            telegram_thread_id=topic_id,
                            label=claim.max_chat_title,
                            enabled=True,
                        )
                        route_bound = True
                await session.commit()

            # P1 #5 (surgical): the target upsert, validation facts
            # and deactivation of every other target happen in ONE
            # serialized transaction — concurrent A/B pairings can
            # never leave zero active targets.
            from ..active_target import upsert_and_activate_target

            await upsert_and_activate_target(
                db,
                account_id=tg_row.id,
                chat_id=str(chat_id),
                title=group_title,
                is_forum=bool(summary.get("is_forum", True)),
                chat_type=summary.get("type") or "supergroup",
                bot_status=perms.get("status"),
            )

            # The target changed — refresh the integration probe so
            # the dashboard / Finish gate see the new state without
            # waiting for the next watchdog tick.
            if on_paired is not None:
                try:
                    on_paired()
                except Exception:  # noqa: BLE001
                    logger.exception("pairing on_paired hook failed")

            if member_sync is not None:
                member_sync.schedule()

            bound_note = (
                f"Связан чат MAX «{claim.max_chat_title or claim.max_chat_id}»."
                if route_bound
                else "Маршруты для чатов MAX создаются в мастере настройки."
            )
            await reply(
                f"✅ SHMAX подключён к группе «{group_title}». {bound_note}"
            )
            return

    from ..telegram_members.sync import observe_membership

    async def membership_listener(**kwargs):
        await observe_membership(db, **kwargs)

    async def message_listener(*, chat_id, user_id, username, display_name):
        from ..active_target import get_active_target
        target = await get_active_target(db)
        if target and str(target.chat_id) == str(chat_id):
            await users.register_or_update(user_id=user_id, username=username,
                                           display_name=display_name)

    _handler.message_listener = message_listener
    _handler.membership_listener = membership_listener
    return _handler


def create_app(
    settings: Settings | None = None,
    *,
    database: Database | None = None,
    secret_store: SecretStore | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    layout = PathLayout(settings.data_dir)
    layout.ensure()
    db = database or Database(DatabaseSettings(data_dir=settings.data_dir))
    secrets_dir = settings.data_dir / "secrets"
    secret_store = secret_store or SecretStore(secrets_dir)
    session_secret = (
        secret_store.get_secret("session_signing_key")
        if secret_store.has_secret("session_signing_key")
        else None
    )
    if session_secret is None:
        session_secret = secrets.token_urlsafe(32)
        secret_store.set_secret("session_signing_key", session_secret)
    signer = SessionSigner(session_secret)
    hasher = PasswordHasher()
    limiter = RateLimiter()
    host_helper = InProcessHostHelper()
    watchdog_store = WatchdogStateStore(settings.data_dir / "state" / "watchdogs.json")
    media_pipeline = MediaPipeline(settings.data_dir / "cache")
    from ..telegram_members.sync import MemberSyncService
    member_sync = MemberSyncService(db=db, secret_store=secret_store)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            await asyncio.to_thread(run_migrations_sync, db.settings.url)
        except Exception:
            logger.exception("alembic upgrade head failed at startup")
            raise

        # Construct the production AppRuntime as the canonical
        # lifecycle holder for adapters / workers / watchdogs /
        # listeners / outbox producer / outbox sender.
        from ..runtime import AppRuntime, RuntimeConfig
        from ..host.protocol import HostHelperProtocol

        # The directory bind exposes replacement sockets after daemon restarts.
        # Keep the real client even when temporarily unavailable so it can recover.
        socket = Path(os.environ.get("SHMAX_HOSTD_SOCKET", "/run/shmax/hostd.sock"))
        production_hostd: HostHelperProtocol = HostHelperProtocol(socket_path=socket)

        runtime = AppRuntime(
            database=db,
            secret_store=secret_store,
            layout=layout,
            config=RuntimeConfig(
                max_concurrent_workers=2,
                start_workers=True,
            ),
            # The app Settings own the runtime mode (TEST in the
            # test suite, PRODUCTION in deployment). Letting the
            # runtime re-resolve it from the environment desynced
            # the two and made a TEST-mode app boot with a real
            # PyMax adapter.
            runtime_mode=settings.runtime_mode,
        )
        # Wire optional PyMax debug logging. The flag is set by
        # SHMAX_DEBUG_MAX_PYMAX in the environment; when on, the
        # MAX auth state machine emits a structured event log
        # via the ``shmax.runtime.max_auth`` logger with masked
        # phone hints only.
        runtime.debug_max_pymax = bool(
            getattr(settings, "debug_max_pymax", False)
        )
        app.state.runtime = runtime
        app.state.host = production_hostd
        # Mirror the legacy state keys so existing API handlers
        # and tests continue to function. AppRuntime is the
        # canonical lifecycle owner; ``state.runtime`` is the
        # preferred access path going forward.
        app.state.db = db
        app.state.secrets = secret_store
        app.state.signer = signer
        app.state.hasher = hasher
        app.state.limiter = limiter
        app.state.watchdog_store = runtime.watchdog_store
        app.state.media_pipeline = runtime.media_pipeline
        app.state.paths = layout
        app.state.settings = settings
        try:
            await runtime.start()
        except Exception:
            logger.exception("AppRuntime.start() failed")
            await db.dispose()
            raise

        # Audit P0 #5: deterministically restore persisted
        # integrations after an ordinary restart. Telegram is
        # rebuilt from the persisted transport config (DB + secret
        # store) with the real probed selection; MAX re-verifies
        # the persisted session and re-arms its listener. A broken
        # integration never blocks the web UI — the report lands
        # in app.state.restore_report for diagnostics.
        from ..pairing.repository import PairingCodesRepository
        from ..startup_restore import restore_integrations
        from ..users.repository import TelegramUsersRepository
        from ..xray import state as xstate

        # APP-OWNED Xray manager (surgical review): created once on
        # app.state; startup bootstrap, API and shutdown share it.
        app.state.xray_manager = xstate.build_manager()
        runtime.xray_manager_ref = app.state.xray_manager

        restore_handler = _build_telegram_command_handler(
            member_sync=member_sync,
            users=TelegramUsersRepository(db),
            pairing_codes=PairingCodesRepository(db.session),
            secret_store=secret_store,
            db=db,
            adapter_getter=lambda: runtime.telegram_adapter,
            limiter=limiter,
            on_paired=lambda: asyncio.get_running_loop().create_task(
                runtime.probe_integration("telegram")
            ),
        )
        try:
            app.state.restore_report = await restore_integrations(
                runtime,
                db=db,
                secret_store=secret_store,
                session_dir=str(layout.root / "sessions" / "telegram"),
                on_command=restore_handler,
                auto_state_path=layout.root / "state" / "telegram_auto.json",
                xray_manager=app.state.xray_manager,
            )
        except Exception:
            logger.exception("integration restore failed")
            app.state.restore_report = {}
        # The runtime shutdown path stops the app-owned Xray child.
        runtime.xray_manager_ref = app.state.xray_manager
        member_sync.schedule(delay=10)
        app.state.started_at = _dt.datetime.now(_dt.timezone.utc)
        app.state.system_topic = None  # resolved on-demand by handlers

        # 24h auto update check (live acceptance #9/#15): fire-and-
        # forget; GitHub unavailability NEVER blocks the bridge.
        try:
            _update_service_task = asyncio.create_task(
                _update_service().auto_check()
            )
        except Exception:  # noqa: BLE001
            logger.exception("auto update check task failed to start")
            _update_service_task = None

        deployment_task = None
        deployment_id = os.environ.get("SHMAX_DEPLOYMENT_ID")
        if not deployment_id and (layout.state / "update-deployment-id").is_file():
            deployment_id = (layout.state / "update-deployment-id").read_text().strip()
        if deployment_id:
            from ..deployment_readiness import publish_readiness
            deployment_task = asyncio.create_task(publish_readiness(
                runtime, layout.state / "deployment-readiness.json", deployment_id
            ), name="shmax.deployment-readiness")
        try:
            yield
        finally:
            if deployment_task is not None:
                deployment_task.cancel()
                await asyncio.gather(deployment_task, return_exceptions=True)
            await member_sync.close()
            if _update_service_task is not None:
                _update_service_task.cancel()
            try:
                await runtime.stop()
            except Exception:
                logger.exception("runtime.stop() failed")
            try:
                await db.dispose()
            except Exception:
                logger.exception("db.dispose() failed")

    app = FastAPI(
        title=__service_name__,
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # -----------------------------------------------------------------
    # Security middleware: CSP, frame deny, no sniff, no referrer
    # -----------------------------------------------------------------
    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response: FastResponse = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
        )
        return response

    # -----------------------------------------------------------------
    # Dependencies
    # -----------------------------------------------------------------
    async def get_session() -> AsyncSession:
        async with db.session() as s:
            yield s

    async def require_admin(request: Request) -> str:
        """Verify the signed session and the live DB user state.

        The cookie carries an opaque session id. We resolve it
        against the ``admin_sessions`` table — a row that no
        longer exists (e.g. after logout) means the request is
        unauthenticated. We also confirm the user is not
        disabled and the session has not expired.

        Returns the username for downstream handlers.
        """
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "no session")
        sess = signer.deserialize(token)
        if sess is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad session")
        async with db.session() as s:
            row = await s.get(AdminSession, sess.session_id)
            if row is None:
                raise HTTPException(
                    status.HTTP_401_UNAUTHORIZED, "session revoked"
                )
            if row.expires_at <= _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=row.expires_at.tzinfo):
                raise HTTPException(
                    status.HTTP_401_UNAUTHORIZED, "session expired"
                )
            user = await s.get(AdminUser, row.user_id)
            if user is None or user.disabled:
                raise HTTPException(
                    status.HTTP_401_UNAUTHORIZED, "user disabled"
                )
            # Touch last_seen_at lazily (best-effort).
            try:
                row.last_seen_at = _dt.datetime.now(_dt.timezone.utc)
                await s.commit()
            except Exception:  # noqa: BLE001
                await s.rollback()
        request.state.session_user = user.username
        request.state.session_id = sess.session_id
        return user.username

    def require_csrf(request: Request) -> None:
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "no session")
        sess = signer.deserialize(token)
        if sess is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad session")
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            return
        supplied = request.headers.get(CSRF_HEADER) or request.headers.get("X-Shmax-CSRF") or request.headers.get("X-CSRF")
        if not supplied or supplied != sess.csrf_token:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "csrf")

    # -----------------------------------------------------------------
    # Health
    # -----------------------------------------------------------------
    @app.get("/health/live")
    def health_live() -> dict:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def health_ready(request: Request) -> dict:
        try:
            async with db.session() as s:
                await s.execute(__import__("sqlalchemy").text("SELECT 1"))
            db_ok = True
        except Exception:
            db_ok = False
        storage_ok = layout.root.is_dir() and os.access(layout.root, os.W_OK)
        runtime = getattr(request.app.state, "runtime", None)
        runtime_started = bool(runtime is not None and runtime.started)
        ready = db_ok and storage_ok and runtime_started
        return {
            "status": "ok" if ready else "maintenance",
            "database": db_ok,
            "storage": storage_ok,
            "runtime": runtime_started,
        }

    @app.get("/health", summary="liveness + readiness snapshot")
    def health(request: Request) -> dict:
        return {
            "status": "ok",
            "service": __service_name__,
            "version": __version__,
            "persistent": {
                "root": str(layout.root),
                "writable": True,
                "existing": [
                    str(p.relative_to(layout.root)) for p in layout.all_persistent()
                ],
                "missing": [],
            },
        }

    @app.get("/", response_class=HTMLResponse)
    def root() -> HTMLResponse:
        for directory in (Path("/app/frontend/dist"), Path(__file__).resolve().parents[3] / "frontend" / "dist"):
            index = directory / "index.html"
            if index.is_file():
                return HTMLResponse(index.read_text(encoding="utf-8"))
        from .index import INDEX_HTML
        return HTMLResponse(INDEX_HTML.format(service=__service_name__, version=__version__))

    # -----------------------------------------------------------------
    # Status / Doctor / Queue
    # -----------------------------------------------------------------
    @app.get("/api/status", response_model=StatusSnapshot, dependencies=[Depends(require_admin)])
    async def status_snapshot(request: Request) -> StatusSnapshot:
        async with db.session() as s:
            counts = await DeliveryQueueRepository(s).counts_by_state()
        storage = {
            "db_bytes": _dir_size(layout.root / "db"),
            "sessions_bytes": _dir_size(layout.root / "sessions"),
            "backups_bytes": _dir_size(layout.root / "backups"),
            "cache_bytes": _dir_size(layout.root / "cache"),
            "logs_bytes": _dir_size(layout.root / "logs"),
        }
        integrations = await request.app.state.runtime.integration_snapshot()
        watchdogs = {name: {**_watchdog_public(name, request.app.state.runtime.watchdog_store), **integrations[name]} for name in ("max", "telegram")}
        return StatusSnapshot(
            service=__service_name__,
            version=__version__,
            data_dir=str(layout.root),
            uptime_seconds=(
                _dt.datetime.now(_dt.timezone.utc) - request.app.state.started_at
            ).total_seconds(),
            max=integrations["max"],
            telegram=integrations["telegram"],
            bridge=integrations["bridge"],
            queue=counts,
            database="ok",
            storage=storage,
            watchdogs=watchdogs,
            update={"current_version": __version__, "available_version": None, "channel": "stable"},
        )

    @app.get("/api/wizard/readiness", dependencies=[Depends(require_admin), Depends(require_csrf)])
    async def wizard_readiness_endpoint(request: Request) -> dict:
        """Per-step wizard readiness for the Next buttons.

        Each step is gated on ACTUAL validated state — the bridge
        must be HEALTHY and the operator must have completed
        every preceding step. Stored configuration alone is not
        enough — the audit explicitly forbids declaring readiness
        from a saved report.
        """
        from ..wizard_readiness import wizard_readiness
        result = await wizard_readiness(request.app.state.runtime)
        return {
            name: {
                "ready": status.ready,
                "reason": status.reason,
                "blocking_check": status.blocking_check,
            }
            for name, status in result.items()
        }

    @app.get(
        "/api/wizard/test",
        dependencies=[Depends(require_admin), Depends(require_csrf)],
    )
    async def wizard_test_endpoint(request: Request) -> dict:
        """Active verification for the Test step (audit P0 #10).

        Runs a real, ordered list of probes against the running
        runtime. Each result includes ok / detail / fix-link. A
        failure here points the operator at the relevant setup
        step — never just a "FAILED" line.

        P0 #2 (surgical review): the result is PERSISTED so the
        Finish gate can require the latest mandatory Test to have
        actually succeeded — readiness computed from integration
        state alone cannot prove the operator ran the checks.
        """
        report = await _run_wizard_test_probes(request.app.state.runtime)
        from sqlalchemy.dialects.sqlite import insert as _sqlite_insert

        from ..database.models import Setting

        async with db.session() as s:
            payload = json.dumps(
                {
                    "ok": report["ok"],
                    "failed": [
                        c["id"] for c in report["checks"] if not c["ok"]
                    ],
                    "at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                },
                sort_keys=True,
            )
            await s.execute(
                _sqlite_insert(Setting)
                .values(key="wizard_test_latest", value=payload)
                .on_conflict_do_update(
                    index_elements=[Setting.key], set_={"value": payload}
                )
            )
            await s.commit()
        return report

    @app.get("/api/doctor", response_model=DoctorResponse, dependencies=[Depends(require_admin), Depends(require_csrf)])
    async def doctor_endpoint() -> DoctorResponse:
        doctor = Doctor(layout.root, db, secret_store)
        report = await doctor.run()
        return DoctorResponse(
            results=[
                {"name": r.name, "status": r.status.value, "detail": r.detail, "technical": r.technical}
                for r in report.results
            ]
        )

    @app.get("/api/queue", dependencies=[Depends(require_admin), Depends(require_csrf)])
    async def queue_endpoint() -> dict:
        async with db.session() as s:
            counts = await DeliveryQueueRepository(s).counts_by_state()
        return counts

    @app.post("/api/queue/{queue_id}/retry", dependencies=[Depends(require_admin), Depends(require_csrf)])
    async def retry_queue_item(queue_id: int) -> dict:
        from sqlalchemy import update
        from ..database.models import DeliveryQueueItem

        async with db.session() as s:
            await s.execute(
                update(DeliveryQueueItem)
                .where(DeliveryQueueItem.id == queue_id)
                .where(DeliveryQueueItem.status.in_(["failed", "retry"]))
                .values(status="pending", attempts=0, last_error=None, next_attempt_at=_dt.datetime.now(_dt.timezone.utc))
            )
            await s.commit()
        return {"ok": True, "queue_id": queue_id}

    @app.post("/api/queue/{queue_id}/purge", dependencies=[Depends(require_admin), Depends(require_csrf)])
    async def purge_queue_item(queue_id: int) -> dict:
        from sqlalchemy import delete
        from ..database.models import DeliveryQueueItem

        async with db.session() as s:
            await s.execute(delete(DeliveryQueueItem).where(DeliveryQueueItem.id == queue_id))
            await s.commit()
        return {"ok": True, "queue_id": queue_id}

    # -----------------------------------------------------------------
    # Routes
    # -----------------------------------------------------------------
    @app.get("/api/routes", dependencies=[Depends(require_admin), Depends(require_csrf)])
    async def routes_endpoint() -> list[dict]:
        async with db.session() as s:
            repo = RoutesRepository(s)
            rows = await repo.list_all()
        return [
            {
                "id": r.id,
                "max_account_id": r.max_account_id,
                "max_chat_id": r.max_chat_id,
                "telegram_account_id": r.telegram_account_id,
                "telegram_chat_id": r.telegram_chat_id,
                "telegram_thread_id": r.telegram_thread_id,
                "enabled": r.enabled,
                "label": r.label,
            }
            for r in rows
        ]

    @app.post("/api/routes", dependencies=[Depends(require_admin), Depends(require_csrf)])
    async def create_route(req: CreateRouteRequest) -> dict:
        async with db.session() as s:
            repo = RoutesRepository(s)
            rec = await repo.upsert(
                max_account_id=req.max_account_id,
                max_chat_id=req.max_chat_id,
                telegram_account_id=req.telegram_account_id,
                telegram_chat_id=req.telegram_chat_id,
                telegram_thread_id=req.telegram_thread_id,
                label=req.label,
                enabled=req.enabled,
            )
        return {"id": rec.id}

    @app.post(
        "/api/routes/from-chat",
        dependencies=[Depends(require_admin), Depends(require_csrf)],
    )
    async def create_route_from_chat(
        req: Annotated[RouteCreateFromChatRequest, Body()], request: Request
    ) -> dict:
        """Create a Telegram topic for the chosen MAX chat and
        persist the route in a single call.

        Topic creation is real: it goes through the live
        Telegram adapter. On failure we return 502; the UI must
        surface the error and not pretend success.
        """
        from sqlalchemy import select as _select
        from ..database.models import TelegramAccount as _TA, MaxAccount as _MA

        runtime = app.state.runtime
        if runtime is None or runtime.telegram_adapter is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "telegram adapter unavailable"
            )
        try:
            topic_id = await runtime.telegram_adapter.create_topic(
                chat_id=req.telegram_chat_id,
                title=req.max_chat_title or req.max_chat_id,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"create_topic failed: {type(exc).__name__}: {str(exc)[:200]}",
            )
        if not topic_id:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                "create_topic returned no id (forum topics disabled?)",
            )
        async with db.session() as s:
            tg_acc_id = req.telegram_account_id
            if tg_acc_id is None:
                tg_acc = (await s.execute(_select(_TA).limit(1))).scalars().first()
                if tg_acc is None:
                    tg_acc = _TA(label="primary")
                    s.add(tg_acc)
                    await s.flush()
                tg_acc_id = tg_acc.id
            max_acc_id = req.max_account_id
            if max_acc_id is None:
                ma = (await s.execute(_select(_MA).limit(1))).scalars().first()
                if ma is None:
                    ma = _MA(label="primary")
                    s.add(ma)
                    await s.flush()
                max_acc_id = ma.id
            repo = RoutesRepository(s)
            rec = await repo.upsert(
                max_account_id=max_acc_id,
                max_chat_id=req.max_chat_id,
                telegram_account_id=tg_acc_id,
                telegram_chat_id=req.telegram_chat_id,
                telegram_thread_id=topic_id,
                label=req.max_chat_title or req.max_chat_id,
                enabled=True,
            )
        return {
            "id": rec.id,
            "telegram_thread_id": topic_id,
            "telegram_chat_id": req.telegram_chat_id,
            "label": req.max_chat_title,
        }

    @app.post("/api/routes/{route_id}/toggle", dependencies=[Depends(require_admin), Depends(require_csrf)])
    async def toggle_route(route_id: int, req: ToggleRequest) -> dict:
        async with db.session() as s:
            repo = RoutesRepository(s)
            rec = await repo.set_enabled(route_id=route_id, enabled=req.enabled)
        return {"id": rec.id if rec else None, "enabled": req.enabled}

    @app.post(
        "/api/routes/{route_id}/recreate-topic",
        dependencies=[Depends(require_admin), Depends(require_csrf)],
    )
    async def recreate_topic(route_id: int, request: Request) -> dict:
        """Drop the existing Telegram topic and create a fresh
        one for this route, persisting the new ``thread_id``.
        """
        from sqlalchemy import select as _select
        from ..database.models import Route

        runtime = app.state.runtime
        async with db.session() as s:
            route = (
                await s.execute(_select(Route).where(Route.id == route_id))
            ).scalars().first()
            if route is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "route not found")
            if not route.telegram_chat_id:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, "route has no telegram_chat_id"
                )
            adapter = runtime.telegram_adapter if runtime else None
            if adapter is None:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE, "telegram adapter unavailable"
                )
            new_title = route.label or route.max_chat_id or "SHMAX topic"
            try:
                new_topic_id = await adapter.create_topic(
                    chat_id=route.telegram_chat_id,
                    title=new_title,
                )
            except Exception as e:  # noqa: BLE001
                raise HTTPException(
                    status.HTTP_502_BAD_GATEWAY,
                    f"create_topic failed: {type(e).__name__}: {str(e)[:200]}",
                )
            if not new_topic_id:
                raise HTTPException(
                    status.HTTP_502_BAD_GATEWAY,
                    "create_topic returned no id (forum topics disabled?)",
                )
            route.telegram_thread_id = new_topic_id
            await s.commit()
        return {
            "ok": True,
            "route_id": route_id,
            "telegram_chat_id": route.telegram_chat_id,
            "telegram_thread_id": new_topic_id,
            "title": new_title,
        }

    # -----------------------------------------------------------------
    # MAX auth state machine
    # -----------------------------------------------------------------



    def _auth_public(snap: object) -> dict:
        return {
            "state": getattr(snap, "state").value if hasattr(snap, "state") else str(snap),
            "phone_hint": getattr(snap, "phone_hint", None),
            "account_id": getattr(snap, "account_id", None),
            "display_name": getattr(snap, "display_name", None),
            "handle": getattr(snap, "handle", None),
            "last_error": getattr(snap, "last_error", None),
            "last_error_class": getattr(snap, "last_error_class", None),
            "next_action": getattr(snap, "next_action", "done"),
            "updated_at": getattr(snap, "updated_at", 0),
        }

    @app.get(
        "/api/max/auth/state",
        dependencies=[Depends(require_admin), Depends(require_csrf)],
    )
    async def max_auth_state(request: Request) -> dict:
        runtime = request.app.state.runtime
        if runtime.max_auth is None:
            return _auth_public(
                type("S", (), {"state": "not_started", "next_action": "start"})()
            )
        snap = await runtime.max_auth.refresh()
        return _auth_public(snap)

    @app.post(
        "/api/max/auth/start",
        dependencies=[Depends(require_admin), Depends(require_csrf)],
    )
    async def max_auth_start(req: Annotated[MaxAuthStartRequest, Body()], request: Request) -> dict:
        runtime = request.app.state.runtime
        if runtime.max_auth is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "max auth unavailable")
        snap = await runtime.max_auth.start(phone=req.phone)
        return _auth_public(snap)

    @app.post(
        "/api/max/auth/code",
        dependencies=[Depends(require_admin), Depends(require_csrf)],
    )
    async def max_auth_code(req: Annotated[MaxAuthCodeRequest, Body()], request: Request) -> dict:
        runtime = request.app.state.runtime
        if runtime.max_auth is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "max auth unavailable")
        snap = await runtime.max_auth.submit_code(code=req.code)
        return _auth_public(snap)

    @app.post(
        "/api/max/auth/password",
        dependencies=[Depends(require_admin), Depends(require_csrf)],
    )
    async def max_auth_password(req: Annotated[MaxAuthPasswordRequest, Body()], request: Request) -> dict:
        runtime = request.app.state.runtime
        if runtime.max_auth is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "max auth unavailable")
        snap = await runtime.max_auth.submit_password(password=req.password)
        return _auth_public(snap)

    @app.post(
        "/api/max/auth/resend",
        dependencies=[Depends(require_admin), Depends(require_csrf)],
    )
    async def max_auth_resend(request: Request) -> dict:
        runtime = request.app.state.runtime
        if runtime.max_auth is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "max auth unavailable")
        snap = await runtime.max_auth.resend_code()
        return _auth_public(snap)

    @app.post(
        "/api/max/auth/cancel",
        dependencies=[Depends(require_admin), Depends(require_csrf)],
    )
    async def max_auth_cancel(request: Request) -> dict:
        runtime = request.app.state.runtime
        if runtime.max_auth is None:
            return _auth_public(
                type("S", (), {"state": "not_started", "next_action": "start"})()
            )
        snap = await runtime.max_auth.cancel()
        return _auth_public(snap)

    @app.get("/api/max/profile", dependencies=[Depends(require_admin), Depends(require_csrf)])
    async def max_profile(request: Request) -> dict:
        runtime = request.app.state.runtime
        try:
            profile = await runtime.max_adapter.get_profile()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e)[:256])
        return {
            "user_id": profile.user_id,
            "display_name": profile.display_name,
            "handle": profile.handle,
        }

    @app.get("/api/max/chats", dependencies=[Depends(require_admin), Depends(require_csrf)])
    async def max_chats(request: Request) -> dict:
        runtime = request.app.state.runtime
        from ..adapters.max.errors import (
            MaxAuthError,
            MaxSessionError,
        )
        try:
            chats = await runtime.max_adapter.get_chats()
        except (MaxSessionError, MaxAuthError) as e:
            # Fresh install or no session — empty list with
            # configured=False is the honest answer.
            logger.info("max chats: %s (returning empty)", e.__class__.__name__)
            chats = []
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)[:256])
        auth_snap = runtime.max_auth.snapshot if runtime.max_auth else None
        configured = bool(auth_snap and auth_snap.state.value == "authenticated")
        return {
            "configured": configured,
            "chats": [
                {"chat_id": c.chat_id, "title": c.title, "kind": c.kind}
                for c in chats
            ],
        }

    @app.post("/api/max/probe", dependencies=[Depends(require_admin), Depends(require_csrf)])
    async def max_probe(request: Request) -> dict:
        snap = watchdog_store.load("max")
        runtime = request.app.state.runtime
        try:
            await runtime.probe_integration("max")
        except Exception:  # noqa: BLE001
            logger.exception("max probe failed")
        return _watchdog_public("max", watchdog_store) if snap else {"adapter": "max", "state": "unknown"}

    @app.post("/api/max/reconnect", dependencies=[Depends(require_admin), Depends(require_csrf)])
    async def max_reconnect(request: Request) -> dict:
        """Trigger a real reconnect through the live MAX adapter.

        BLOCKER 28 — the previous implementation only incremented
        a counter; the audit found the adapter never actually
        reconnected. We now go through ``runtime.max_watchdog``
        which has the live adapter reference and uses the
        PyMax ``relogin()`` / ``reconnect=True`` lifecycle.
        """
        runtime = request.app.state.runtime
        snap = watchdog_store.load("max") or _empty_snap("max")
        snap.reconnect_attempts = (snap.reconnect_attempts or 0) + 1
        watchdog_store.save(snap)
        # Ask the watchdog to actually drive the reconnect; this
        # honours the configured backoff and self-heal retries.
        if runtime is not None:
            try:
                await runtime.max_watchdog.reconnect_now()
            except Exception:  # noqa: BLE001
                logger.exception("max reconnect_now() failed")
        return {"ok": True, "reconnect_attempts": snap.reconnect_attempts}

    # -----------------------------------------------------------------
    # Telegram
    # -----------------------------------------------------------------



    def _mask_token(token: str | None) -> str:
        if not token:
            return ""
        if len(token) <= 8:
            return "*" * len(token)
        return f"{'*' * (len(token) - 4)}{token[-4:]}"

    def _telegram_token_holder() -> "TokenHolder":
        # The token holder is stored on app.state so all
        # handlers see the same value without going to the
        # secret store on every request.
        holder = getattr(app.state, "telegram_token", None)
        if holder is None:
            holder = TokenHolder()
            app.state.telegram_token = holder
        return holder

    @app.post(
        "/api/telegram/token",
        dependencies=[Depends(require_admin), Depends(require_csrf)],
    )
    async def telegram_save_token(req: Annotated[TelegramTokenRequest, Body()]) -> dict:
        """Persist the bot token to the SecretStore.

        The token never leaves the SecretStore; handlers that
        need it call ``secret_store.get_secret`` to read it
        back. Only the masked preview is returned to the UI.
        """
        token = (req.token or "").strip()
        if not token or ":" not in token:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid token format")
        secret_store.set_secret("telegram_bot_token", token)
        _telegram_token_holder().set(token)
        # Persist the bot identity if we already verified it;
        # the actual identity is recorded by ``/verify``.
        return {
            "ok": True,
            "saved": True,
            "preview": _mask_token(token),
        }

    @app.get(
        "/api/telegram/token/preview",
        dependencies=[Depends(require_admin), Depends(require_csrf)],
    )
    async def telegram_token_preview() -> dict:
        token = _telegram_token_holder().peek()
        if not token:
            try:
                token = secret_store.get_secret("telegram_bot_token")
            except Exception:  # noqa: BLE001
                # SecretStore raises ``SecretNotFound`` (or any
                # other transient store error) when the operator
                # has not yet saved a token. Surface a clean
                # "no token yet" rather than a 500.
                token = ""
        return {
            "saved": bool(token),
            "preview": _mask_token(token),
        }

    @app.post(
        "/api/telegram/verify",
        dependencies=[Depends(require_admin), Depends(require_csrf)],
    )
    async def telegram_verify(req: Annotated[TelegramTokenRequest, Body()]) -> dict:
        """Real Bot API ``getMe`` against the supplied (or
        stored) bot token.

        The handler always uses the
        :class:`TelegramBotApiAdapter` with the supplied
        transport configuration. The token is NEVER returned in
        the response; only the public bot identity is exposed.
        """
        from ..adapters.telegram.bot_api import TelegramBotApiAdapter
        from ..adapters.telegram.config import (
            ProxyConfig,
            TransportConfig,
            TransportMode,
        )
        from ..adapters.telegram.manager import (
            TelegramAutoStateStore,
            TelegramTransportManager,
        )
        from ..startup_restore import load_telegram_transport
        from ..xray import state as xstate

        token = (req.token or "").strip() or secret_store.get_secret("telegram_bot_token")
        if not token:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "no bot token saved")

        runtime = app.state.runtime
        manager = runtime.telegram_manager if runtime else None

        # Surgical P0: the FIRST getMe MUST go through the SAVED
        # network. Before any request: load the persisted transport,
        # ensure Xray is running when the network is Xray-managed,
        # and build a NON-POLLING probe adapter from those facts.
        # NO preliminary direct request, NO direct fallback.
        facts = await load_telegram_transport(db, secret_store)
        if manager is None and isinstance(facts, dict):
            proxy_f = facts.get("proxy") if isinstance(facts.get("proxy"), dict) else None
            if facts.get("mode") == "bot_api_socks5" and secret_store.has_secret(
                xstate.TRANSPORT_MODE_KEY
            ):
                # Xray-managed network: local SOCKS must be up first.
                started, detail = await xstate.start_from_cache(
                    app.state.xray_manager, secret_store
                )
                if not started:
                    raise HTTPException(
                        status.HTTP_502_BAD_GATEWAY,
                        f"Xray не запущен: {detail}",
                    )
            manager = TelegramTransportManager(
                TransportConfig(
                    mode=TransportMode(facts.get("mode", "auto")),
                    bot_token=token,
                    api_id=facts.get("api_id"),
                    api_hash=facts.get("api_hash"),
                    proxy=(
                        ProxyConfig(
                            scheme=proxy_f.get("scheme", "socks5"),
                            host=proxy_f.get("host"),
                            port=int(proxy_f.get("port") or 0),
                            username=proxy_f.get("username"),
                            password=proxy_f.get("password"),
                        )
                        if proxy_f
                        else None
                    ),
                    mtproxy=None,
                    session_dir=str(layout.root / "sessions" / "telegram"),
                ),
                TelegramAutoStateStore(
                    layout.root / "state" / "telegram_auto.json"
                ),
            )

        if manager is not None:
            proxy: Optional[ProxyConfig] = manager.config.proxy
            transport = TransportConfig(
                mode=manager.config.mode,
                bot_token=token,
                api_id=manager.config.api_id,
                api_hash=manager.config.api_hash,
                proxy=proxy,
                mtproxy=manager.config.mtproxy,
                session_dir=str(layout.root / "sessions" / "telegram"),
            )
        else:
            # Fresh install with NO network step at all: the only
            # possible transport is direct.
            transport = TransportConfig(
                mode=TransportMode.BOT_API_DIRECT,
                bot_token=token,
                session_dir=str(layout.root / "sessions" / "telegram"),
            )
        # Surgical review: verify must ONLY probe getMe — never
        # connect() (which starts the polling task / getUpdates
        # consumer). probe() uses a throwaway bot with no dispatcher.
        adapter = TelegramBotApiAdapter(transport)
        try:
            info = await adapter.probe()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"verify failed: {type(e).__name__}: {str(e)[:200]}",
            )
        # Persist the bot identity so the dashboard shows the
        # operator who their bot is.
        from sqlalchemy import select as _select
        from ..database.models import TelegramAccount
        async with db.session() as s:
            stmt = _select(TelegramAccount).limit(1)
            acc = (await s.execute(stmt)).scalars().first()
            if acc is None:
                acc = TelegramAccount(label="primary")
                s.add(acc)
            acc.bot_id = info.bot_id
            acc.bot_username = info.username
            acc.status = "verified"
            acc.last_error = None
            await s.commit()
        _telegram_token_holder().set(token)
        secret_store.set_secret("telegram_bot_token", token)

        # Limited repair #3: verify is STRICTLY non-polling —
        # probe() only, no connect(), no getUpdates consumer, no
        # install_telegram(), no persistent listener. Listener
        # activation moved to the pairing stage
        # (/api/telegram/group/pair-code), which runs AFTER verify
        # in the wizard and needs the live receiver for
        # /shmax_connect.
        return {
            "ok": True,
            "verified": True,
            "bot_id": info.bot_id,
            "username": info.username,
            "can_join_groups": info.can_join_groups,
            "privacy_warning": ("getMe не подтвердил отключение Privacy Mode. Проверьте /setprivacy → Disable в BotFather; права администратора группы также влияют на получение сообщений."
                                if info.can_read_all_group_messages is not True else None),
            "preview": _mask_token(token),
        }

    @app.post(
        "/api/telegram/system-topic",
        dependencies=[Depends(require_admin), Depends(require_csrf)],
    )
    async def telegram_system_topic(chat_id: str = Body(..., embed=True)) -> dict:
        """Create the ``🔧 SHMAX`` topic inside the operator's
        forum and persist a system route pointing to it.
        """
        from sqlalchemy import select as _select
        from ..database.models import TelegramAccount, TelegramTarget, Route

        runtime = app.state.runtime
        if runtime is None or runtime.telegram_adapter is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "telegram adapter unavailable")
        adapter = runtime.telegram_adapter
        try:
            topic_id = await adapter.create_topic(chat_id=chat_id, title="🔧 SHMAX")
        except Exception as e:  # noqa: BLE001
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"create_topic failed: {type(e).__name__}: {str(e)[:200]}",
            )
        if not topic_id:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                "create_topic returned no id (forum topics disabled?)",
            )
        # Persist target + system route.
        async with db.session() as s:
            stmt_acc = _select(TelegramAccount).limit(1)
            acc = (await s.execute(stmt_acc)).scalars().first()
            if acc is None:
                acc = TelegramAccount(label="primary")
                s.add(acc)
                await s.flush()
            stmt_tgt = _select(TelegramTarget).where(
                TelegramTarget.account_id == acc.id,
                TelegramTarget.chat_id == chat_id,
            )
            tgt = (await s.execute(stmt_tgt)).scalars().first()
            if tgt is None:
                tgt = TelegramTarget(
                    account_id=acc.id,
                    chat_id=chat_id,
                    title=chat_id,
                    is_forum=True,
                )
                s.add(tgt)
                await s.flush()
            # Drop existing system route(s) so we never end up
            # with two competing system routes.
            await s.execute(
                _select(Route).where(Route.label == "system")
            )
            existing_routes = (
                await s.execute(_select(Route).where(Route.label == "system"))
            ).scalars().all()
            for r in existing_routes:
                await s.delete(r)
            # The system route piggybacks on the operator's
            # MAX account (which the runtime guarantees exists).
            from ..database.models import MaxAccount
            max_acc = (await s.execute(_select(MaxAccount).limit(1))).scalars().first()
            if max_acc is None:
                max_acc = MaxAccount(label="primary")
                s.add(max_acc)
                await s.flush()
            s.add(
                Route(
                    max_account_id=max_acc.id,
                    max_chat_id="-system-",
                    telegram_account_id=acc.id,
                    telegram_chat_id=chat_id,
                    telegram_thread_id=topic_id,
                    enabled=True,
                    label="system",
                )
            )
            await s.commit()
        return {
            "ok": True,
            "chat_id": chat_id,
            "topic_id": topic_id,
            "title": "🔧 SHMAX",
        }

    @app.get(
        "/api/telegram/group",
        dependencies=[Depends(require_admin), Depends(require_csrf)],
    )
    async def telegram_group_info(request: Request) -> dict:
        """Return the operator's chosen Telegram target (group
        + validated facts + bot identity) for the UI to display.

        ``ready`` is True only when the target was validated
        against the real Telegram API and no permission is
        missing (audit P0 #3)."""
        from sqlalchemy import select as _select
        from ..database.models import TelegramAccount, TelegramTarget

        runtime = app.state.runtime
        manager = runtime.telegram_manager if runtime else None
        missing: list[str] = []
        validated = False
        from ..active_target import get_active_target

        async with db.session() as s:
            acc = (await s.execute(_select(TelegramAccount).limit(1))).scalars().first()
        target = await get_active_target(db)
        if target is not None:
            try:
                missing = list(json.loads(target.missing_permissions or "[]"))
            except (TypeError, ValueError):
                missing = []
            validated = (
                target.validated_at is not None and not missing
            )
        paired = bool(target is not None and validated)
        return {
            "paired": paired,
            "validated": validated,
            "missing_permissions": missing,
            "bot_id": acc.bot_id if acc else None,
            "bot_username": acc.bot_username if acc else None,
            "chat_id": target.chat_id if target else None,
            "title": target.title if target else None,
            "is_forum": bool(target.is_forum) if target else False,
            "chat_type": target.chat_type if target else None,
            "bot_status": target.bot_status if target else None,
            "transport_mode": manager.config.mode.value if manager else "auto",
        }

    @app.post(
        "/api/telegram/group/pair",
        dependencies=[Depends(require_admin), Depends(require_csrf)],
    )
    async def telegram_group_pair(req: Annotated[TelegramPairRequest, Body()], request: Request) -> dict:
        """Legacy operator-driven manual pairing endpoint.

        Kept for API compatibility, but the audit P0 #3 forbids
        trusting a manual chat id: the target is stored as
        explicitly UNVALIDATED (never `is_forum=True` from the
        request). The wizard does not use this path; the canonical
        pairing is ``/shmax_connect CODE`` which validates the
        chat through the real Telegram API.
        """
        from sqlalchemy import select as _select
        from ..database.models import TelegramAccount, TelegramTarget

        if not req.chat_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "chat_id required")
        chat_id = req.chat_id
        async with db.session() as s:
            acc = (await s.execute(_select(TelegramAccount).limit(1))).scalars().first()
            if acc is None:
                acc = TelegramAccount(label="primary")
                s.add(acc)
                await s.flush()
            stmt = _select(TelegramTarget).where(
                TelegramTarget.account_id == acc.id,
                TelegramTarget.chat_id == chat_id,
            )
            tgt = (await s.execute(stmt)).scalars().first()
            if tgt is None:
                tgt = TelegramTarget(
                    account_id=acc.id,
                    chat_id=chat_id,
                    title=chat_id,
                )
                s.add(tgt)
            # Honest state: unvalidated, remediation recorded.
            tgt.is_forum = False
            tgt.chat_type = None
            tgt.bot_status = None
            tgt.missing_permissions = json.dumps(
                ["группа не проверена через Telegram API — "
                 "отправьте /shmax_connect CODE в группе"]
            )
            tgt.validated_at = None
            tgt.title = chat_id
            tgt.cached_at = _dt.datetime.now(_dt.timezone.utc)
            await s.commit()
        return {"ok": True, "chat_id": chat_id, "validated": False}

    @app.post(
        "/api/telegram/group/pair-code",
        dependencies=[Depends(require_admin), Depends(require_csrf)],
    )
    async def telegram_pair_code() -> dict:
        """Issue a fresh pairing code for the Telegram group flow.

        Audit P0 #2: ONE code store. This endpoint now issues
        through the same :class:`PairingCodesRepository` the bot's
        ``/shmax_connect`` handler consumes from — the previous
        implementation wrote into the ``setup_tokens`` table via
        :class:`PairingCodeService`, a store the receiver never
        read, so wizard-issued codes could never pair anything.
        """
        from ..pairing.repository import PairingCodesRepository

        # Limited repair #3: listener activation lives HERE — the
        # pairing stage runs AFTER the strictly non-polling verify
        # and needs a live receiver for /shmax_connect. Built from
        # the PERSISTED transport + the VERIFIED token;
        # install_telegram keeps the exact-one-poller invariant.
        runtime = app.state.runtime
        if runtime is not None and secret_store.has_secret("telegram_bot_token"):
            from ..adapters.telegram.config import (
                ProxyConfig as _PC,
                TransportConfig,
                TransportMode,
            )
            from ..adapters.telegram.manager import (
                TelegramAutoStateStore,
                TelegramTransportManager,
            )
            from ..startup_restore import load_telegram_transport
            from ..users.repository import TelegramUsersRepository

            token = secret_store.get_secret("telegram_bot_token")
            current = runtime.telegram_manager
            if current is None or current.config.bot_token != token:
                facts = await load_telegram_transport(db, secret_store)
                if isinstance(facts, dict):
                    proxy_f = (
                        facts.get("proxy")
                        if isinstance(facts.get("proxy"), dict)
                        else None
                    )
                    cfg = TransportConfig(
                        mode=TransportMode(facts.get("mode", "auto")),
                        bot_token=token,
                        api_id=facts.get("api_id"),
                        api_hash=facts.get("api_hash"),
                        proxy=(
                            _PC(
                                scheme=proxy_f.get("scheme", "socks5"),
                                host=proxy_f.get("host"),
                                port=int(proxy_f.get("port") or 0),
                                username=proxy_f.get("username"),
                                password=proxy_f.get("password"),
                            )
                            if proxy_f
                            else None
                        ),
                        mtproxy=None,
                        session_dir=str(layout.root / "sessions" / "telegram"),
                    )
                else:
                    # Fresh install with NO network step: direct is
                    # the only possible transport (the same fallback
                    # verify's probe used).
                    cfg = TransportConfig(
                        mode=TransportMode.BOT_API_DIRECT,
                        bot_token=token,
                        session_dir=str(layout.root / "sessions" / "telegram"),
                    )
                new_manager = TelegramTransportManager(
                    cfg,
                    TelegramAutoStateStore(
                        layout.root / "state" / "telegram_auto.json"
                    ),
                )
                try:
                    new_adapter = await new_manager.select_probed(
                        override_mode=cfg.mode
                        if cfg.mode.value != "auto" else None
                    )
                    handler = _build_telegram_command_handler(
                        member_sync=member_sync,
                        users=TelegramUsersRepository(db),
                        pairing_codes=PairingCodesRepository(db.session),
                        secret_store=secret_store,
                        db=db,
                        adapter_getter=lambda: runtime.telegram_adapter,
                        limiter=limiter,
                        on_paired=lambda: asyncio.get_running_loop().create_task(
                            runtime.probe_integration("telegram")
                        ),
                    )
                    await runtime.install_telegram(
                        new_manager, new_adapter, on_command=handler
                    )
                    # Final repair #3: install starts the listener
                    # ASYNCHRONOUSLY (supervisor → connect() →
                    # polling task). A pairing code is only useful
                    # when /shmax_connect can actually be received —
                    # confirm the polling task is alive within a
                    # bounded timeout, otherwise fail (NOT 200) and
                    # roll the listener back so a retry starts
                    # cleanly.
                    if not await _await_listener_ready(runtime, new_adapter):
                        try:
                            sup = runtime._supervisors.get("telegram_listener")
                            if sup is not None:
                                await sup.stop()
                        except Exception:  # noqa: BLE001
                            logger.exception(
                                "pair-code: listener rollback stop failed"
                            )
                        try:
                            await new_adapter.disconnect()
                        except Exception:  # noqa: BLE001
                            pass
                        # A retry must start CLEANLY: drop the
                        # manager so the next pair-code reinstalls
                        # instead of seeing the right token and
                        # skipping activation entirely.
                        runtime._tg_manager = None
                        raise HTTPException(
                            status.HTTP_502_BAD_GATEWAY,
                            "Telegram listener не запустился — "
                            "устраните проблему и выпустите код повторно",
                        )
                except HTTPException:
                    raise
                except Exception as e:  # noqa: BLE001
                    raise HTTPException(
                        status.HTTP_502_BAD_GATEWAY,
                        f"transport setup failed: {type(e).__name__}: {str(e)[:200]}",
                    )

        async with db.session() as s:
            repo = PairingCodesRepository(db.session)
            code = await repo.issue()
        return {
            "code": code.code,
            "purpose": "group",
            "expires_at": _dt.datetime.fromtimestamp(
                code.expires_at, tz=_dt.timezone.utc
            ).isoformat(),
        }

    @app.get(
        "/api/telegram/users",
        dependencies=[Depends(require_admin), Depends(require_csrf)],
    )
    async def telegram_users(request: Request) -> dict:
        """Return discovered users, membership metadata and explicit SHMAX ACL.

        Discovery includes the bot participant list, membership events,
        pairing and visible messages. Telegram never supplies phone numbers.
        """
        from sqlalchemy import select as _select
        from ..database.models import TelegramUser

        async with db.session() as s:
            rows = (
                await s.execute(_select(TelegramUser).order_by(TelegramUser.first_seen_at.desc()))
            ).scalars().all()
        return {
            "users": [
                {
                    "user_id": r.user_id,
                    "display_name": r.display_name,
                    "username": r.username,
                    "nickname": r.nickname,
                    "role": r.role,
                    "approved": r.approved,
                    "blocked": r.blocked,
                    "membership": r.membership,
                    "is_admin": r.is_admin,
                    "source": r.source,
                    "first_seen_at": int(r.first_seen_at.timestamp()) if r.first_seen_at else None,
                    "last_seen_at": int(r.last_seen_at.timestamp()) if r.last_seen_at else None,
                }
                for r in rows
            ],
            "phone_available": False,
            "phone_note": "Telegram не передаёт номер телефона ботам.",
        }

    @app.get("/api/telegram/member-sync", dependencies=[Depends(require_admin), Depends(require_csrf)])
    async def member_sync_status() -> dict:
        return await member_sync.state()

    @app.put("/api/telegram/member-sync", dependencies=[Depends(require_admin), Depends(require_csrf)])
    async def member_sync_config(request: Request) -> dict:
        # Validate privately: FastAPI's default 422 includes rejected input,
        # which would echo an API hash. Never return the submitted secret.
        try:
            payload = await request.json()
            req = MemberSyncCredentials.model_validate(payload)
        except Exception:
            raise HTTPException(400, "Проверьте Telegram API ID и API Hash") from None
        if not req.api_hash and not secret_store.has_secret("tg_member_sync_api_hash"):
            raise HTTPException(400, "Укажите Telegram API Hash")
        member_sync.save_credentials(req.api_id, req.api_hash)
        member_sync.schedule()
        return await member_sync.state()

    @app.post("/api/telegram/member-sync", dependencies=[Depends(require_admin), Depends(require_csrf)])
    async def member_sync_run() -> dict:
        return (await member_sync.sync()).to_dict()

    @app.patch(
        "/api/telegram/users/{user_id}/approval",
        dependencies=[Depends(require_admin), Depends(require_csrf)],
    )
    async def telegram_user_set_approval(user_id: str, req: UserApprovalRequest) -> dict:
        """Set ``approved`` and ``role`` for a Telegram user.

        Operators control who can bridge from Telegram into MAX
        via this endpoint. The audit-mandated fail-closed ACL
        uses these values (audit P0 #7, P0 #8).
        """
        from ..users.repository import TelegramUsersRepository

        async with db.session() as session:
            repo = TelegramUsersRepository(db)
            row = await repo.set_approval(
                user_id=user_id,
                approved=req.approved,
                role=req.role,
            )
            if row is None:
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND,
                    f"unknown Telegram user {user_id}",
                )
        return {"ok": True, "user_id": row.user_id, "approved": row.approved, "role": row.role}

    @app.patch(
        "/api/telegram/users/{user_id}/blocked",
        dependencies=[Depends(require_admin), Depends(require_csrf)],
    )
    async def telegram_user_set_blocked(user_id: str, req: UserBlockedRequest) -> dict:
        """Toggle ``blocked`` for a Telegram user.

        Blocked users are denied at the audit-mandated ACL
        regardless of their role.
        """
        from ..users.repository import TelegramUsersRepository

        async with db.session() as session:
            repo = TelegramUsersRepository(db)
            row = await repo.set_blocked(user_id=user_id, blocked=req.blocked)
            if row is None:
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND,
                    f"unknown Telegram user {user_id}",
                )
        return {"ok": True, "user_id": row.user_id, "blocked": row.blocked}

    @app.patch(
        "/api/telegram/users/{user_id}/nickname",
        dependencies=[Depends(require_admin), Depends(require_csrf)],
    )
    async def telegram_user_set_nickname(user_id: str, req: UserNicknameRequest) -> dict:
        """Set operator-chosen nickname (persists across renames,
        overrides Telegram's own display_name in attribution)."""
        from ..users.repository import TelegramUsersRepository

        async with db.session() as session:
            repo = TelegramUsersRepository(db)
            row = await repo.set_nickname(user_id=user_id, nickname=req.nickname)
            if row is None:
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND,
                    f"unknown Telegram user {user_id}",
                )
        return {"ok": True, "user_id": row.user_id, "nickname": row.nickname}

    @app.get(
        "/api/telegram/transport",
        dependencies=[Depends(require_admin), Depends(require_csrf)],
    )
    async def telegram_transport_get(request: Request) -> dict:
        """Current transport state. Proxy credentials NEVER leave the
        SecretStore — only host/port/username and a has-password flag
        are returned so the wizard can redisplay the form safely."""

        def _proxy_fields(p) -> dict:
            return {
                "proxy_url": (
                    f"{p.scheme}://{p.host}:{p.port}" if p else None
                ),
                "proxy_host": p.host if p else None,
                "proxy_port": p.port if p else None,
                "proxy_username": p.username if p else None,
                "proxy_has_password": bool(p and p.password),
            }

        runtime = request.app.state.runtime
        manager = runtime.telegram_manager if runtime else None
        if manager is not None and manager.config is not None:
            fields = _proxy_fields(manager.config.proxy)
            mt = manager.config.mtproxy
            return {
                "mode": manager.config.mode.value,
                **fields,
                "api_id": manager.config.api_id,
                "api_hash_set": bool(manager.config.api_hash),
                "mtproxy_host": mt.host if mt else None,
                "mtproxy_port": mt.port if mt else None,
                "mtproxy_secret_set": bool(mt and mt.secret),
            }
        # Fall back to the persisted (credential-free) facts so the
        # wizard shows what will be restored on reboot.
        from ..startup_restore import load_telegram_transport

        facts = await load_telegram_transport(db, secret_store)
        if isinstance(facts, dict):
            proxy = facts.get("proxy") if isinstance(facts.get("proxy"), dict) else None
            mtproxy = facts.get("mtproxy") if isinstance(facts.get("mtproxy"), dict) else None
            fields = _proxy_fields(
                type("P", (), {
                    "scheme": proxy.get("scheme"),
                    "host": proxy.get("host"),
                    "port": proxy.get("port"),
                    "username": proxy.get("username"),
                    "password": proxy.get("password"),
                })() if proxy else None
            )
            return {
                "mode": facts.get("mode", "auto"),
                **fields,
                "api_id": facts.get("api_id"),
                "api_hash_set": bool(facts.get("api_hash")),
                "mtproxy_host": mtproxy.get("host") if mtproxy else None,
                "mtproxy_port": mtproxy.get("port") if mtproxy else None,
                "mtproxy_secret_set": bool(
                    mtproxy and mtproxy.get("secret")
                ),
            }
        return {"mode": "auto", **_proxy_fields(None), "api_id": None,
                "api_hash_set": False, "mtproxy_host": None,
                "mtproxy_port": None, "mtproxy_secret_set": False}

    @app.post(
        "/api/telegram/transport",
        dependencies=[Depends(require_admin), Depends(require_csrf)],
    )
    async def telegram_transport_set(req: Annotated[TelegramProxyRequest, Body()], request: Request) -> dict:
        from ..adapters.telegram.config import (
            MTProxyConfig,
            ProxyConfig,
            TransportConfig,
            TransportMode,
        )
        from ..adapters.telegram.manager import (
            TelegramTransportManager,
            TelegramAutoStateStore,
        )
        from ..pairing.repository import PairingCodesRepository
        from ..users.repository import TelegramUsersRepository

        # Normalise mode against the public enum.
        try:
            mode = TransportMode(req.mode)
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown mode: {req.mode}")

        proxy: Optional[ProxyConfig] = None
        if req.proxy_host and req.proxy_port:
            proxy = ProxyConfig(
                scheme="socks5" if "socks" in req.mode else "http",
                host=req.proxy_host,
                port=int(req.proxy_port),
                username=req.proxy_username or None,
                password=req.proxy_password or None,
            )
        elif req.proxy_url:
            proxy = _parse_proxy_url(req.proxy_url)
            if proxy is None:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid proxy URL")

        mtproxy: Optional[MTProxyConfig] = None
        if req.mtproxy_host and req.mtproxy_port and req.mtproxy_secret:
            mtproxy = MTProxyConfig(
                host=req.mtproxy_host,
                port=int(req.mtproxy_port),
                secret=req.mtproxy_secret,
            )

        api_id = req.api_id if mode in (
            TransportMode.MTPROTO_DIRECT,
            TransportMode.MTPROTO_SOCKS,
            TransportMode.MTPROTO_PROXY,
        ) else None
        api_hash = req.api_hash if api_id else None
        # Network bootstrap (surgical): the token may legitimately
        # not exist yet — the Network step runs before the Bot step.
        bot_token = (
            secret_store.get_secret("telegram_bot_token")
            if secret_store.has_secret("telegram_bot_token")
            else ""
        )
        cfg = TransportConfig(
            mode=mode,
            bot_token=bot_token,
            api_id=api_id,
            api_hash=api_hash,
            proxy=proxy,
            mtproxy=mtproxy,
            session_dir=str(layout.root / "sessions" / "telegram"),
        )
        store = TelegramAutoStateStore(layout.root / "state" / "telegram_auto.json")
        manager = TelegramTransportManager(cfg, store)
        runtime = app.state.runtime
        # Persist the transport: non-secret facts go to the DB
        # settings table, secrets (proxy password, api_hash,
        # MTProxy secret) go to the SecretStore. The previous
        # implementation wrote the full proxy URL — password
        # included — into the DB (audit P1 #6) and echoed it back
        # in this response.
        from ..startup_restore import persist_telegram_transport

        await persist_telegram_transport(
            db,
            secret_store,
            mode=mode,
            proxy=proxy,
            api_id=api_id,
            api_hash=api_hash,
            mtproxy=mtproxy,
        )
        # Reload the adapter into the runtime so subsequent
        # verifications and probes use the new config. P0 #8: the
        # production path uses the BOUNDED PROBED selection — each
        # candidate gets a connect timeout and a real getMe before
        # it is accepted; AUTO walks the ladder until one works.
        reachable: Optional[bool] = None
        reach_detail = ""
        if runtime is not None:
            token_present = secret_store.has_secret("telegram_bot_token")
            if req.skip_bot_probe and not token_present:
                # Network bootstrap: no token yet — test Telegram
                # HTTPS reachability ONLY. No getMe, no adapter, no
                # polling consumer (exact-one poller invariant kept).
                from ..xray.reachability import probe_telegram

                result = await probe_telegram(
                    mode=req.mode,
                    proxy_host=req.proxy_host,
                    proxy_port=req.proxy_port,
                    proxy_username=req.proxy_username,
                    proxy_password=req.proxy_password,
                )
                reachable = result.ok
                reach_detail = result.detail
                if not reachable:
                    return JSONResponse(
                        status_code=502,
                        content={
                            "ok": False,
                            "reachable": False,
                            "detail": reach_detail,
                            "mode": req.mode,
                        },
                    )
            else:
                try:
                    override = None if mode == TransportMode.AUTO else mode
                    new_adapter = await manager.select_probed(
                        override_mode=override
                    )
                    handler = _build_telegram_command_handler(
                        member_sync=member_sync,
                        users=TelegramUsersRepository(db),
                        pairing_codes=PairingCodesRepository(db.session),
                        secret_store=secret_store,
                        db=db,
                        adapter_getter=lambda: runtime.telegram_adapter,
                        limiter=limiter,
                        on_paired=lambda: asyncio.get_running_loop().create_task(
                            runtime.probe_integration("telegram")
                        ),
                    )
                    await runtime.install_telegram(
                        manager, new_adapter, on_command=handler
                    )
                except Exception as e:  # noqa: BLE001
                    raise HTTPException(
                        status.HTTP_400_BAD_REQUEST,
                        f"transport setup failed: {type(e).__name__}: {str(e)[:200]}",
                    )
        return {
            "ok": True,
            "reachable": reachable if reachable is not None else True,
            "detail": reach_detail,
            "mode": mode.value,
            "proxy_set": proxy is not None,
            "proxy_host": f"{proxy.scheme}://{proxy.host}:{proxy.port}" if proxy else None,
            "mtproxy_set": mtproxy is not None,
            "api_id_set": api_id is not None,
        }

    @app.post(
        "/api/telegram/transport/probe",
        dependencies=[Depends(require_admin), Depends(require_csrf)],
    )
    async def telegram_transport_probe(request: Request) -> dict:
        """Probe the configured Telegram transport by issuing
        a real ``getMe`` against the bot token.

        The probe uses a THROWAWAY adapter — never the live
        runtime adapter. The previous implementation connected
        and DISCONNECTED the installed adapter, which silently
        killed the running polling listener (the wizard's
        "Проверить соединение" button took Telegram down until
        the next transport save).
        """
        runtime = request.app.state.runtime
        if runtime is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "runtime unavailable")
        from ..adapters.telegram.bot_api import TelegramBotApiAdapter
        from ..adapters.telegram.config import TransportConfig, TransportMode

        manager = runtime.telegram_manager
        token = (
            secret_store.get_secret("telegram_bot_token")
            if secret_store.has_secret("telegram_bot_token")
            else ""
        )
        if manager is not None:
            probe_cfg = manager.config
        else:
            probe_cfg = TransportConfig(
                mode=TransportMode.AUTO,
                bot_token=token,
                session_dir=str(layout.root / "sessions" / "telegram"),
            )
        try:
            # P1 #1: probe() never starts the receive loop — the
            # previous connect/disconnect here briefly created a
            # SECOND getUpdates consumer next to the live listener.
            probe_adapter = TelegramBotApiAdapter(probe_cfg)
            info = await probe_adapter.probe()
        except Exception as e:  # noqa: BLE001
            return {
                "ok": False,
                "error_class": type(e).__name__,
                "detail": str(e)[:200],
            }
        return {
            "ok": True,
            "bot_id": info.bot_id,
            "username": info.username,
            "transport": manager.config.mode.value if manager else "auto",
        }

    # -----------------------------------------------------------------
    # Xray subscription (network bootstrap; surgical acceptance)
    # -----------------------------------------------------------------
    @app.post(
        "/api/telegram/xray/subscription",
        dependencies=[Depends(require_admin), Depends(require_csrf)],
    )
    async def xray_subscription(req: Annotated[XraySubscribeRequest, Body()], request: Request) -> dict:
        """Fetch + parse a VLESS/Trojan subscription.

        Secrets (URL, raw links, UUIDs/passwords) go to the
        SecretStore; the response carries SANITIZED metadata only.
        """
        from ..xray import state as xstate
        from ..xray.subscription import (
            SubscriptionFormatError,
            parse_subscription,
        )

        url = req.url.strip()
        if not url.lower().startswith(("http://", "https://")):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "некорректный URL подписки")
        try:
            body = await xstate.fetch_subscription(url)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"не удалось загрузить подписку: {type(e).__name__}",
            )
        try:
            parsed = parse_subscription(body)
        except SubscriptionFormatError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
        if not parsed.nodes:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "подписка пуста")

        xstate.store_subscription(secret_store, url, parsed.raw_links)
        await xstate.store_nodes_sanitized(db, [n.to_dict() for n in parsed.nodes])
        return {
            "ok": True,
            "nodes": [n.to_dict() for n in parsed.nodes],
        }

    @app.post(
        "/api/telegram/xray/select",
        dependencies=[Depends(require_admin), Depends(require_csrf)],
    )
    async def xray_select(req: Annotated[XraySelectRequest, Body()], request: Request) -> dict:
        """Select a node: persist it (encrypted), start/restart Xray
        with the ephemeral config, point Telegram SOCKS5 at the local
        Xray port, and probe Telegram through it. No direct fallback.
        """
        from ..xray import state as xstate
        from ..xray.reachability import probe_telegram
        from ..xray.runtime import XrayError
        from ..xray.subscription import build_xray_outbound, parse_subscription

        # NOTE: `status` (fastapi module) is shadowed below by the
        # XrayStatus instance — resolve HTTP codes via http_status.
        http_status = status

        links = xstate.load_subscription(secret_store)
        if links is None:
            raise HTTPException(http_status.HTTP_400_BAD_REQUEST, "подписка не загружена")
        raw_link = links.get(req.node_id)
        if raw_link is None:
            raise HTTPException(http_status.HTTP_404_NOT_FOUND, "узел не найден в подписке")
        parsed = parse_subscription(raw_link)
        node = next((n for n in parsed.nodes if n.node_id == req.node_id), None)
        if node is None or not node.supported:
            reason = node.reason if node else "узел не поддерживается"
            raise HTTPException(http_status.HTTP_400_BAD_REQUEST, reason)

        manager = xstate.get_manager(app.state)
        config = build_xray_outbound(
            node, raw_link, local_socks_port=manager.socks_port
        )
        # Persist the selection (encrypted SecretStore) BEFORE the
        # process attempt: even if Xray fails to start now, the
        # cached node lets a later startup/retry proceed without the
        # subscription server being online.
        xstate.store_selection(secret_store, req.node_id, raw_link)

        # Point Telegram at the local Xray SOCKS (persisted via the
        # existing SecretStore-backed transport path) BEFORE the
        # process attempt — the persisted mode survives a failed
        # Xray start, so restart restores through Xray, never direct.
        from ..adapters.telegram.config import ProxyConfig, TransportMode
        from ..startup_restore import persist_telegram_transport

        proxy = ProxyConfig(
            scheme="socks5",
            host="127.0.0.1",
            port=manager.socks_port,
        )
        token_present = secret_store.has_secret("telegram_bot_token")
        await persist_telegram_transport(
            db,
            secret_store,
            mode=TransportMode.BOT_API_SOCKS5,
            proxy=proxy,
            api_id=None,
            api_hash=None,
            mtproxy=None,
        )
        # Validate the generated config with the PINNED Xray binary
        # before launching: unsupported/invalid combos are rejected
        # here, never started. Binary unavailability is a separate
        # 502 (service problem), not a 400 node rejection.
        if not Path(manager.binary_path).exists():
            raise HTTPException(
                http_status.HTTP_502_BAD_GATEWAY,
                "Xray не запущен: xray binary not found",
            )
        config_ok, config_detail = await manager.test_config(config)
        if not config_ok:
            raise HTTPException(
                http_status.HTTP_400_BAD_REQUEST,
                f"узел отклонён Xray: {config_detail}",
            )
        try:
            xray_status_obj = await manager.restart(config)
        except XrayError as e:
            raise HTTPException(http_status.HTTP_502_BAD_GATEWAY, f"Xray не запущен: {e}")

        runtime = app.state.runtime
        # getMe through the local SOCKS ONLY when a token exists;
        # otherwise a pure reachability probe. NO direct fallback.
        result = await probe_telegram(
            mode="bot_api_socks5",
            proxy_host="127.0.0.1",
            proxy_port=manager.socks_port,
            timeout=10.0,
        )
        if result.ok and token_present and runtime is not None:
            cfg = TransportConfig(
                mode=TransportMode.BOT_API_SOCKS5,
                bot_token=secret_store.get_secret("telegram_bot_token"),
                proxy=proxy,
                session_dir=str(layout.root / "sessions" / "telegram"),
            )
            from ..adapters.telegram.manager import (
                TelegramAutoStateStore,
                TelegramTransportManager,
            )

            t_manager = TelegramTransportManager(
                cfg,
                TelegramAutoStateStore(layout.root / "state" / "telegram_auto.json"),
            )
            try:
                override = TransportMode.BOT_API_SOCKS5
                new_adapter = await t_manager.select_probed(override_mode=override)
                handler = _build_telegram_command_handler(
                    member_sync=member_sync,
                    users=TelegramUsersRepository(db),
                    pairing_codes=PairingCodesRepository(db.session),
                    secret_store=secret_store,
                    db=db,
                    adapter_getter=lambda: runtime.telegram_adapter,
                    limiter=limiter,
                    on_paired=lambda: asyncio.get_running_loop().create_task(
                        runtime.probe_integration("telegram")
                    ),
                )
                await runtime.install_telegram(
                    t_manager, new_adapter, on_command=handler
                )
            except Exception as e:  # noqa: BLE001
                return {
                    "ok": True,
                    "xray": xray_status_obj.to_dict(),
                    "reachable": True,
                    "detail": "Telegram доступен, но бот ещё не проверен",
                    "bot_error": f"{type(e).__name__}: {e}"[:200],
                }
        return {
            "ok": True,
            "xray": xray_status_obj.to_dict(),
            "reachable": result.ok,
            "detail": result.detail,
        }

    @app.get(
        "/api/telegram/xray/status",
        dependencies=[Depends(require_admin), Depends(require_csrf)],
    )
    async def xray_status(request: Request) -> dict:
        from ..xray import state as xstate

        manager = xstate.get_manager(app.state)
        status = await manager.health()
        runtime = getattr(app.state, "runtime", None)
        nodes = await xstate.load_nodes_sanitized(db)
        selection = xstate.load_selected(secret_store)
        disappeared = xstate.selected_node_disappeared(secret_store)
        return {
            **status.to_dict(),
            "executable_installed": os.path.isfile(manager.binary_path) and os.access(manager.binary_path, os.X_OK),
            "process_running": manager.is_running,
            "socks_listening": status.state == "running",
            "telegram_reachable": _xray_telegram_reachable(runtime, manager),
            "nodes": nodes,
            "selected_node_id": selection[0] if selection else None,
            "subscription_loaded": xstate.load_subscription(secret_store) is not None,
            "subscription_url_set": bool(xstate.subscription_url(secret_store)),
            # Selected node missing from refreshed subscription —
            # actionable, never auto-replaced.
            "selected_node_disappeared": disappeared,
        }

    @app.get("/api/telegram/chats", dependencies=[Depends(require_admin), Depends(require_csrf)])
    async def telegram_chats(request: Request) -> dict:
        """Return the operator's paired Telegram target as a
        list. Telegram Bot API doesn't expose arbitrary chat
        discovery; we return whatever the runtime has
        registered (system + paired).
        """
        from sqlalchemy import select as _select
        from ..database.models import TelegramAccount, TelegramTarget

        runtime = app.state.runtime
        configured = bool(runtime and runtime.telegram_adapter)
        async with db.session() as s:
            acc = (await s.execute(_select(TelegramAccount).limit(1))).scalars().first()
            chats: list[dict] = []
            if acc is not None:
                tgts = (
                    await s.execute(
                        _select(TelegramTarget).where(TelegramTarget.account_id == acc.id)
                    )
                ).scalars().all()
                chats = [
                    {
                        "chat_id": t.chat_id,
                        "title": t.title,
                        "is_forum": t.is_forum,
                    }
                    for t in tgts
                ]
        return {"configured": configured, "chats": chats}

    # -----------------------------------------------------------------
    # Backups
    # -----------------------------------------------------------------
    from .portable import install_routes as install_portable_routes
    install_portable_routes(app, db=db, layout=layout, settings=settings,
                            require_admin=require_admin, require_csrf=require_csrf, limiter=limiter)

    @app.get("/api/backups", dependencies=[Depends(require_admin), Depends(require_csrf)])
    async def list_backups_endpoint() -> list[dict]:
        return list_backups(layout.root)

    @app.post("/api/backups", dependencies=[Depends(require_admin), Depends(require_csrf)])
    async def create_backup_endpoint(req: BackupRequest) -> dict:
        bdir = layout.root / "backups"
        bdir.mkdir(parents=True, exist_ok=True)
        out = bdir / f"shmax-{int(_dt.datetime.now().timestamp())}.shmax"
        if req.passphrase:
            manifest = create_encrypted_backup(
                data_dir=layout.root,
                out_path=out,
                passphrase=req.passphrase,
                shmax_version=__version__,
            )
        else:
            manifest = create_backup(
                data_dir=layout.root,
                out_path=out,
                shmax_version=__version__,
            )
        return {
            "name": out.name,
            "path": str(out),
            "entries": len(manifest.entries),
            "encrypted": bool(req.passphrase),
        }

    @app.post(
        "/api/backups/{name}/restore",
        dependencies=[Depends(require_admin), Depends(require_csrf)],
    )
    async def restore_backup_endpoint(name: str, req: RestoreRequest) -> dict:
        from ..config import RuntimeMode
        if settings.runtime_mode == RuntimeMode.PRODUCTION:
            raise HTTPException(409, "Восстановите переносимую копию через интерфейс владельца; локальные копии восстанавливаются через CLI при остановленном SHMAX.")
        # Live acceptance #5: traversal-safe name resolution.
        try:
            archive = safe_backup_name(name, layout.root / "backups")
        except ValueError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
        # Validate up-front (cheap) before touching anything.
        try:
            ok, errors = validate_backup(archive)
        except Exception as e:  # noqa: BLE001 — malformed archives
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"битая копия: {type(e).__name__}",
            )
        if not ok:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "; ".join(errors))
        # Production path: a single transactional coordinator
        # owns the manifest gate, the safety snapshot, the
        # runtime quiesce/refresh hooks, and the rollback.
        coordinator = TransactionalRestore(
            data_dir=layout.root,
            backups_dir=layout.root / "backups",
            shmax_version=__version__,
        )
        # The runtime (if any) must quiesce before we mutate
        # data_dir and restart afterwards so the API can talk to
        # the freshly-restored DB / session signer / secret store.
        runtime = getattr(app.state, "runtime", None)
        was_started = False
        if runtime is not None and runtime.started:

            async def _before() -> None:
                await runtime.stop()

            async def _after() -> None:
                await runtime.start()

            coordinator.on_before_restore(_before)
            coordinator.on_after_restore(_after)
            was_started = True
        try:
            outcome = await asyncio.to_thread(
                coordinator.run, archive, passphrase=req.passphrase
            )
        except RestoreError as e:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(e))
        return {
            "ok": True,
            "format": outcome.manifest.format,
            "entries": len(outcome.manifest.entries),
            "safety_backup": str(outcome.safety_backup_path),
            "runtime_restarted": was_started,
        }

    @app.get(
        "/api/backups/{name}/download",
        dependencies=[Depends(require_admin), Depends(require_csrf)],
    )
    async def download_backup(name: str) -> FastResponse:
        # Live acceptance #5: traversal-safe name resolution.
        try:
            archive = safe_backup_name(name, layout.root / "backups")
        except ValueError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
        data = archive.read_bytes()
        return FastResponse(
            content=data,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    # -----------------------------------------------------------------
    # Update (live acceptance #6-#12): real GitHub Releases
    # discovery; install/rollback delegated to shmax-hostd; the web
    # app never touches Docker or /opt/shmax.
    # -----------------------------------------------------------------
    def _update_service() -> "UpdateService":
        from ..update.service import UpdateService

        return UpdateService(
            current_version=__version__,
            secret_store=secret_store,
            db=db,
            data_dir=layout.root,
        )

    @app.get("/api/update/status", dependencies=[Depends(require_admin), Depends(require_csrf)])
    async def update_status() -> dict:
        status = _update_service().status()
        # Live acceptance A8: the UI must never offer a fake Apply —
        # the button is honest only when the host updater is real.
        from ..host.protocol import InProcessHostHelper

        helper = app.state.host
        status["hostd_available"] = not isinstance(
            helper, InProcessHostHelper
        )
        return status

    @app.post("/api/update/check", dependencies=[Depends(require_admin), Depends(require_csrf)])
    async def update_check() -> dict:
        try:
            return await _update_service().check()
        except Exception as e:  # noqa: BLE001 — sanitized failure
            logger.warning("manual update check failed: %s", type(e).__name__)
            return {"status": "network_error", "current_version": __version__}

    @app.post("/api/update/apply", dependencies=[Depends(require_admin), Depends(require_csrf)])
    async def update_apply(channel: str = Body(default="stable", embed=True)) -> dict:
        from ..update.service import SUPPORTED_CHANNELS

        if channel not in SUPPORTED_CHANNELS:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid channel")
        helper = app.state.host
        # Hostd-delegated: backup → download → verify → stage →
        # install → healthcheck → rollback-on-failure all live
        # host-side (restricted op set, no arbitrary commands).
        r = await helper.update(channel=channel)
        if not r.ok:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"host updater: {r.detail or 'unavailable'}",
            )
        return {"ok": True, "detail": r.detail or "update started host-side"}

    @app.post("/api/update/rollback", dependencies=[Depends(require_admin), Depends(require_csrf)])
    async def update_rollback() -> dict:
        r = await app.state.host.rollback()
        if not r.ok:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"host updater: {r.detail or 'unavailable'}",
            )
        return {"ok": True, "detail": r.detail or "rollback started host-side"}

    @app.post("/api/update/channel", dependencies=[Depends(require_admin), Depends(require_csrf)])
    async def update_channel(channel: str = Body(..., embed=True)) -> dict:
        if channel not in {"stable", "beta", "disabled"}:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid channel")
        if channel != "disabled":
            _update_service().set_channel(channel)
        return {"ok": True, "channel": channel}

    # -----------------------------------------------------------------
    # Watchdog
    # -----------------------------------------------------------------
    @app.get("/api/watchdogs/{adapter}", dependencies=[Depends(require_admin)])
    async def watchdog_get(adapter: str) -> WatchdogResponse:
        if adapter not in ("max", "telegram"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown adapter")
        integrations = await app.state.runtime.integration_snapshot()
        return {**_watchdog_dto(adapter, app.state.runtime.watchdog_store).model_dump(), **integrations[adapter]}

    @app.get("/api/watchdogs/max", response_model=WatchdogResponse, include_in_schema=False)
    async def watchdog_max() -> WatchdogResponse:
        return _watchdog_dto("max", watchdog_store)

    @app.get("/api/watchdogs/telegram", response_model=WatchdogResponse, include_in_schema=False)
    async def watchdog_telegram() -> WatchdogResponse:
        return _watchdog_dto("telegram", watchdog_store)

    @app.post("/api/watchdogs/{adapter}/probe", dependencies=[Depends(require_admin), Depends(require_csrf)])
    async def watchdog_probe(adapter: str) -> dict:
        if adapter not in ("max", "telegram"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown adapter")
        await app.state.runtime.probe_integration(adapter)
        integrations = await app.state.runtime.integration_snapshot()
        return {**_watchdog_dto(adapter, app.state.runtime.watchdog_store).model_dump(), **integrations[adapter]}

    @app.post(
        "/api/watchdogs/{adapter}/self-heal",
        dependencies=[Depends(require_admin), Depends(require_csrf)],
    )
    async def watchdog_self_heal(adapter: str) -> dict:
        if adapter not in ("max", "telegram"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown adapter")
        snap = watchdog_store.load(adapter) or _empty_snap(adapter)
        snap.reconnect_attempts = (snap.reconnect_attempts or 0) + 1
        snap.last_probe_at = int(_dt.datetime.now().timestamp())
        watchdog_store.save(snap)
        return {"ok": True, "adapter": adapter, "reconnect_attempts": snap.reconnect_attempts}

    @app.post(
        "/api/watchdogs/max/relogin",
        dependencies=[Depends(require_admin), Depends(require_csrf)],
    )
    async def watchdog_max_relogin() -> dict:
        # MAX relogin: invalidate the cached session, force the
        # next adapter.connect() to re-authenticate. Real flow
        # (SMS/QR) is gated on the live MAX account; this
        # endpoint clears the *server-side* state so the
        # operator can re-initiate the flow from the UI.
        snap = watchdog_store.load("max") or _empty_snap("max")
        snap.state = "auth_required"
        snap.last_error_class = "ReLoginRequested"
        snap.last_error_detail = "session invalidated by operator"
        snap.last_probe_at = int(_dt.datetime.now().timestamp())
        watchdog_store.save(snap)
        return {"ok": True, "detail": "max session invalidated; re-login required"}

    # -----------------------------------------------------------------
    # Pairing
    # -----------------------------------------------------------------
    @app.post("/api/pairing/{purpose}", dependencies=[Depends(require_admin), Depends(require_csrf)])
    async def pairing_issue(purpose: str, req: PairingRequest) -> dict:
        if purpose not in ("group", "user"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "purpose must be group|user")
        ttl = _dt.timedelta(seconds=req.ttl_seconds)
        async with db.session() as s:
            svc = PairingCodeService(s)
            code = await svc.issue(purpose=purpose, ttl=ttl)
        return {"code": code.code, "purpose": code.purpose, "expires_at": code.expires_at.isoformat()}

    @app.post("/api/pairing/consume", dependencies=[Depends(require_admin), Depends(require_csrf)])
    async def pairing_consume(req: PairingConsumeRequest) -> dict:
        async with db.session() as s:
            svc = PairingCodeService(s)
            valid = await svc.validate(req.code, purpose=req.purpose)
            if not valid:
                return {"ok": False, "detail": "invalid or expired"}
            consumed = await svc.consume(req.code, purpose=req.purpose)
            return {"ok": consumed, "purpose": req.purpose}

    @app.post(
        "/api/pairing/group/issue",
        dependencies=[Depends(require_admin), Depends(require_csrf)],
    )
    async def issue_group_pairing_code(req: GroupPairingIssueRequest) -> dict:
        """Issue a one-time code (optionally bound to a MAX chat
        id). The operator sends it via ``/shmax_connect CODE`` from
        the target Telegram supergroup; the bot consumes it from
        the same store and persists the canonical target. P0 #2."""
        from ..pairing.repository import PairingCodesRepository

        async with db.session() as session:
            repo = PairingCodesRepository(db.session)
            code = await repo.issue(
                max_chat_id=req.max_chat_id or "",
                max_chat_title=req.max_chat_title,
                telegram_chat_hint=req.telegram_chat_hint,
                ttl_seconds=req.ttl_seconds,
            )
        return {
            "code": code.code,
            "expires_at": code.expires_at,
            "max_chat_id": code.max_chat_id,
            "max_chat_title": code.max_chat_title,
            "telegram_chat_hint": code.telegram_chat_hint,
        }

    @app.get(
        "/api/pairing/group/active",
        dependencies=[Depends(require_admin)],
    )
    async def list_active_group_pairing_codes() -> dict:
        """List active pairing codes so the wizard can show
        which codes are still valid."""
        from ..pairing.repository import PairingCodesRepository

        async with db.session() as session:
            repo = PairingCodesRepository(lambda: session)
            codes = await repo.list_active()
        return {
            "codes": [
                {
                    "code": c.code,
                    "expires_at": c.expires_at,
                    "max_chat_id": c.max_chat_id,
                    "max_chat_title": c.max_chat_title,
                    "telegram_chat_hint": c.telegram_chat_hint,
                }
                for c in codes
            ]
        }

    @app.delete(
        "/api/pairing/group/{code}",
        dependencies=[Depends(require_admin), Depends(require_csrf)],
    )
    async def revoke_group_pairing_code(code: str) -> dict:
        """Revoke a pairing code before it has been consumed.
        Audit P0 #6."""
        from ..pairing.repository import PairingCodesRepository

        async with db.session() as session:
            repo = PairingCodesRepository(lambda: session)
            revoked = await repo.revoke(code=code)
        return {"ok": revoked}

    # -----------------------------------------------------------------
    # Storage manager
    # -----------------------------------------------------------------
    @app.get("/api/storage", response_model=StorageResponse, dependencies=[Depends(require_admin)])
    async def storage() -> StorageResponse:
        return StorageResponse(
            db_bytes=_dir_size(layout.root / "db"),
            sessions_bytes=_dir_size(layout.root / "sessions"),
            backups_bytes=_dir_size(layout.root / "backups"),
            cache_bytes=_dir_size(layout.root / "cache"),
            logs_bytes=_dir_size(layout.root / "logs"),
        )

    @app.post("/api/storage/clear-cache", dependencies=[Depends(require_admin), Depends(require_csrf)])
    async def clear_cache() -> dict:
        removed = media_pipeline.clear_cache()
        return {"ok": True, "removed": removed}

    # -----------------------------------------------------------------
    # Support bundle
    # -----------------------------------------------------------------
    @app.post("/api/support-bundle", dependencies=[Depends(require_admin), Depends(require_csrf)])
    async def support_bundle() -> dict:
        out_dir = layout.root / "backups"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"support-{int(_dt.datetime.now().timestamp())}.tar.gz"
        secret_names = secret_store.names()
        async with db.session() as s:
            counts = await DeliveryQueueRepository(s).counts_by_state()
        doctor = Doctor(layout.root, db, secret_store)
        report = await doctor.run()
        bundle = collect_support_bundle(
            data_dir=layout.root,
            db=db,
            secret_store=secret_store,
            doctor_results=[
                {"name": r.name, "status": r.status.value, "detail": r.detail}
                for r in report.results
            ],
            watchdog_snapshots={
                "max": _watchdog_public("max", watchdog_store),
                "telegram": _watchdog_public("telegram", watchdog_store),
            },
            queue_counts=counts,
            schema_revision="",
            log_tail_lines=200,
        )
        bundle["secrets"] = secret_names
        log_path = layout.root / "logs" / "shmax.log"
        write_support_bundle(bundle, log_path, out)
        return {"name": out.name, "path": str(out), "size": out.stat().st_size}

    # -----------------------------------------------------------------
    # Recent activity (metadata only)
    # -----------------------------------------------------------------
    @app.get("/api/recent-activity", dependencies=[Depends(require_admin), Depends(require_csrf)])
    async def recent_activity() -> dict:
        from sqlalchemy import desc
        from ..database.models import AuditLog

        async with db.session() as s:
            rows = (
                await s.execute(
                    select(AuditLog).order_by(desc(AuditLog.created_at)).limit(50)
                )
            ).scalars().all()
        return {
            "items": [
                {
                    "actor": r.actor,
                    "event": r.event,
                    "target": r.target,
                    "created_at": int(r.created_at.timestamp()),
                }
                for r in rows
            ]
        }

    # -----------------------------------------------------------------
    # Danger Zone
    # -----------------------------------------------------------------
    @app.post("/api/danger-zone/purge", dependencies=[Depends(require_admin), Depends(require_csrf)])
    async def danger_zone_purge(req: DangerZoneRequest) -> dict:
        if req.confirmation.strip() != "SHMAX":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "confirmation mismatch")
        # Verify password (in addition to session) before purging.
        async with db.session() as s:
            row = (
                await s.execute(select(AdminUser).where(AdminUser.username == req.username))
            ).scalar_one_or_none()
        if row is None or not hasher.verify(req.password, row.password_hash):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad credentials")
        # In production this calls host_helper.purge() through the
        # restricted hostd protocol. The web container itself
        # never deletes data.
        return {
            "ok": False,
            "detail": "host helper unavailable; delegate to shmax-hostd",
            "purge": req.purge,
        }

    # -----------------------------------------------------------------
    # Setup
    # -----------------------------------------------------------------
    @app.post("/api/setup/token")
    async def create_setup_token(request: Request, response: Response) -> dict:
        """Mint a setup token for the Web UI bootstrap flow.

        This endpoint is **trusted-source-only**. The token
        must originate from the host installer or
        ``shmax setup-link`` CLI; a LAN client MUST NOT be
        able to call this directly.

        Trust is established by either:

        1. ``X-Shmax-Setup-Secret`` header equal to a value
           written by the installer to
           ``$SHMAX_DATA_DIR/state/setup_secret``. The
           installer prints the URL with ``?secret=...`` so
           the LAN client does not see the secret.
        2. A localhost-only request (``request.client.host``
           resolves to ``127.0.0.1`` / ``::1``), so the
           installer can call this from the host shell.

        Without either, the endpoint returns 403.
        """
        async with db.session() as s:
            existing = (await s.execute(select(AdminUser))).first()
            if existing is not None:
                raise HTTPException(status.HTTP_409_CONFLICT, "admin already exists")

        client_host = (request.client.host if request.client else "") or ""
        is_loopback = client_host in {"127.0.0.1", "::1", "localhost", ""}
        provided = request.headers.get("X-Shmax-Setup-Secret", "")
        secret_path = settings.data_dir / "state" / "setup_secret"
        if is_loopback:
            pass
        elif secret_path.is_file():
            expected = secret_path.read_text(encoding="utf-8").strip()
            if not expected or not secrets.compare_digest(provided, expected):
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    "setup token mint requires X-Shmax-Setup-Secret",
                )
        else:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "setup token mint is restricted to localhost and installer",
            )
        async with db.session() as s:
            svc = SetupTokenService(s)
            info = await svc.create(ttl=_dt.timedelta(hours=12))
        return {"token": info.token, "expires_at": info.expires_at.isoformat()}

    def set_session_cookies(response: Response, sess) -> None:
        response.set_cookie(SESSION_COOKIE, signer.serialize(sess), httponly=True,
                            samesite="lax", max_age=8 * 3600, path="/")
        response.set_cookie(CSRF_COOKIE, sess.csrf_token, httponly=False,
                            samesite="lax", max_age=8 * 3600, path="/")

    @app.post("/api/setup/admin")
    async def create_first_admin(req: SetupAdminRequest, response: Response, request: Request) -> dict:
        if limiter.is_locked("setup_admin", req.username):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate-limited")
        limiter.record_attempt("setup_admin", req.username, success=False)
        if not req.username.strip() or len(req.password) < 8:
            raise HTTPException(400, "username and password of at least 8 characters required")
        async with db.session() as s:
            # Serialize bootstrap attempts: owner, token and session commit together.
            await s.execute(text("BEGIN IMMEDIATE"))
            if (await s.execute(select(AdminUser))).first() is not None:
                raise HTTPException(409, "admin already exists")
            svc = SetupTokenService(s)
            if not await svc.validate(req.token):
                raise HTTPException(400, "bad setup token")
            owner = AdminUser(username=req.username.strip(), password_hash=hasher.hash(req.password), role="owner")
            s.add(owner)
            await s.flush()
            if not await svc.consume(req.token, commit=False):
                raise HTTPException(400, "bad setup token")
            sess = signer.issue(username=owner.username, role="owner")
            s.add(AdminSession(id=sess.session_id, user_id=owner.id, csrf_token=sess.csrf_token,
                              expires_at=_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=8)))
            s.add(Setting(key="setup_completed", value="false"))
            await s.commit()
        limiter.reset("setup_admin", req.username)
        set_session_cookies(response, sess)
        return {"ok": True, "username": owner.username, "csrf_token": sess.csrf_token, "setup_complete": False}

    @app.post("/api/setup/dismiss", dependencies=[Depends(require_admin), Depends(require_csrf)])
    async def dismiss_setup() -> dict:
        """Dismiss onboarding without completing setup or changing runtime/configuration."""
        async with db.session() as s:
            row = await s.get(Setting, "setup_dismissed")
            if row is None:
                s.add(Setting(key="setup_dismissed", value="true"))
            else:
                row.value = "true"
            await s.commit()
        return {"ok": True, "setup_dismissed": True}

    @app.post("/api/setup/complete", dependencies=[Depends(require_admin), Depends(require_csrf)])
    async def complete_setup(acknowledge_unconfigured: bool = Body(False, embed=True)) -> dict:
        """Finish the setup — SERVER-SIDE enforced (audit P0 #5).

        Without the explicit acknowledgement this endpoint applies
        the FULL mandatory readiness contract (the same one the
        wizard Finish gate renders): MAX authenticated, Telegram
        healthy, a validated target, a live route, an approved
        usable user and the bridge HEALTHY. A crafted API call
        cannot bypass the UI gates.

        ``acknowledge_unconfigured=True`` preserves the intentional
        V1 override (finish now, fix later) and records it.
        """
        runtime = app.state.runtime
        from ..wizard_readiness import wizard_readiness

        # Surgical review P0: Finish must not trust a stale
        # successful Test. Without the explicit override, perform
        # the CURRENT mandatory active validation right here — the
        # probe suite live-revalidates the Telegram target rights
        # (getChat/getChatMember, persisting fresh/invalid facts),
        # routes, users, workers, queue and the MAX/TG active
        # probes. Permission checks performed: ALL of them. The
        # fresh result is then consumed by readiness (it re-reads
        # the persisted facts the probes just wrote).
        report = await _run_wizard_test_probes(runtime)
        readiness = await wizard_readiness(runtime)
        finish = readiness.get("finish")
        if (not report.get("ok") or finish is None or not finish.ready) and not acknowledge_unconfigured:
            missing = [
                {"step": k, "reason": v.reason}
                for k, v in readiness.items()
                if k != "finish" and not v.ready
            ]
            failed_checks = [
                {
                    "id": c.get("id"),
                    "detail": c.get("detail"),
                    "fix_step": c.get("fix_step"),
                }
                for c in report.get("checks", [])
                if not c.get("ok")
            ]
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "readiness requirements are not met",
                    "missing": missing,
                    "failed_checks": failed_checks,
                    "finish_reason": finish.reason if finish else "readiness unavailable",
                },
            )
        async with db.session() as s:
            row = await s.get(Setting, "setup_completed")
            if row is None:
                s.add(Setting(key="setup_completed", value="true"))
            else:
                row.value = "true"
            if acknowledge_unconfigured and (finish is None or not finish.ready):
                s.add(
                    Setting(
                        key="setup_acknowledged_unconfigured",
                        value=_dt.datetime.now(_dt.timezone.utc).isoformat(),
                    )
                )
            await s.commit()
        return {"ok": True, "setup_complete": True}

    # -----------------------------------------------------------------
    # Auth
    # -----------------------------------------------------------------
    @app.post("/api/auth/login")
    async def login(req: LoginRequest, response: Response, request: Request) -> dict:
        if limiter.is_locked("login", req.username):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate-limited")
        async with db.session() as s:
            row = (
                await s.execute(select(AdminUser).where(AdminUser.username == req.username))
            ).scalar_one_or_none()
        if row is None or row.disabled or not hasher.verify(req.password, row.password_hash):
            limiter.record_attempt("login", req.username, success=False)
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad credentials")
        limiter.record_attempt("login", req.username, success=True)
        sess = signer.issue(username=row.username, role=row.role)
        token = signer.serialize(sess)
        # Materialise the session server-side so logout can
        # revoke it.
        async with db.session() as s:
            from ..database.models import AdminSession

            s.add(
                AdminSession(
                    id=sess.session_id,
                    user_id=row.id,
                    csrf_token=sess.csrf_token,
                    created_at=_dt.datetime.now(_dt.timezone.utc),
                    expires_at=_dt.datetime.now(_dt.timezone.utc)
                    + _dt.timedelta(hours=8),
                    user_agent=(request.headers.get("user-agent") or "")[:256] or None,
                    ip=(request.client.host if request.client else None),
                )
            )
            row.last_login_at = _dt.datetime.now(_dt.timezone.utc)
            await s.commit()
        response.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            samesite="lax",
            max_age=8 * 3600,
            path="/",
        )
        response.set_cookie(
            CSRF_COOKIE,
            sess.csrf_token,
            httponly=False,
            samesite="lax",
            max_age=8 * 3600,
            path="/",
        )
        return {"ok": True, "username": row.username, "role": row.role, "csrf": sess.csrf_token, "csrf_token": sess.csrf_token, "expires_at": sess.issued_at + 8 * 3600}

    @app.post("/api/auth/logout")
    async def logout(request: Request, response: Response) -> dict:
        """Revoke the current session server-side.

        We delete the matching ``admin_sessions`` row so a
        stolen cookie cannot outlive the operator's logout.
        """
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            sess = signer.deserialize(token)
            if sess is not None:
                async with db.session() as s:
                    from ..database.models import AdminSession

                    row = await s.get(AdminSession, sess.session_id)
                    if row is not None:
                        await s.delete(row)
                        await s.commit()
        response.delete_cookie(SESSION_COOKIE, path="/")
        response.delete_cookie(CSRF_COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/auth/session")
    async def session_status(request: Request) -> dict:
        async with db.session() as s:
            admin_exists = (await s.execute(select(AdminUser.id).limit(1))).first() is not None
            completed = await s.get(Setting, "setup_completed")
            dismissed = await s.get(Setting, "setup_dismissed")
        authenticated = False
        username = csrf = expires = None
        try:
            username = await require_admin(request)
            sess = signer.deserialize(request.cookies[SESSION_COOKIE])
            authenticated = True
            csrf = sess.csrf_token
            expires = sess.issued_at + 8 * 3600
        except HTTPException:
            pass
        return {"authenticated": authenticated, "admin_exists": admin_exists,
                "mode": "BOOTSTRAP" if not admin_exists else ("AUTHENTICATED" if authenticated else "AUTH_REQUIRED"),
                "setup_complete": bool(completed and completed.value == "true"),
                "setup_dismissed": bool(authenticated and dismissed and dismissed.value == "true"),
                "username": username, "csrf_token": csrf, "expires_at": expires}

    # -----------------------------------------------------------------
    # Logs
    # -----------------------------------------------------------------
    @app.get("/api/logs", dependencies=[Depends(require_admin), Depends(require_csrf)])
    async def logs_endpoint(tail: int = 200) -> dict:
        path = layout.root / "logs" / "shmax.log"
        if not path.is_file():
            return {"lines": []}
        text = path.read_text(errors="replace").splitlines()
        return {"lines": text[-tail:]}

    # -----------------------------------------------------------------
    # Routers
    # -----------------------------------------------------------------
    app.include_router(_require_routers_for_health())

    # -----------------------------------------------------------------
    # SPA fallback
    # -----------------------------------------------------------------
    # After FastAPI runs all the explicit routes, any GET that
    # hasn't matched falls through to ``/setup``, ``/login``,
    # ``/dashboard`` etc. — the SPA shell. ``/api/*`` and
    # ``/health/*`` are matched above and never reach here.
    from fastapi.staticfiles import StaticFiles

    static_dir = next(
        (p for p in static_dir_candidates() if p.is_dir()), None
    )
    if static_dir is not None:
        assets_dir = static_dir / "assets"
        index_html = static_dir / "index.html"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

        @app.get("/", include_in_schema=False)
        @app.get("/setup", include_in_schema=False)
        @app.get("/login", include_in_schema=False)
        @app.get("/dashboard", include_in_schema=False)
        @app.get("/max", include_in_schema=False)
        @app.get("/telegram", include_in_schema=False)
        @app.get("/chats", include_in_schema=False)
        @app.get("/users", include_in_schema=False)
        @app.get("/diagnostics", include_in_schema=False)
        @app.get("/logs", include_in_schema=False)
        @app.get("/backups", include_in_schema=False)
        @app.get("/updates", include_in_schema=False)
        @app.get("/settings", include_in_schema=False)
        async def spa_fallback() -> HTMLResponse:
            return HTMLResponse(index_html.read_text(encoding="utf-8"))

        # Nested SPA routes — anything that doesn't look like an
        # API or asset request falls back to the SPA shell so
        # ``/chats/123``, ``/backups/pre-restore-1700000000``
        # etc. work without enumerating every possible deep link.
        # ``/api/*``, ``/health/*``, and ``/assets/*`` are matched
        # above; this catch-all runs only for the rest.
        from fastapi.exceptions import HTTPException as _HTTPException

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa_nested(full_path: str) -> Any:
            # Block any path that should have been matched by an
            # explicit route (defence in depth — 404 is more
            # informative than the SPA shell for missing APIs).
            if full_path.startswith(("api/", "health/", "assets/", "docs", "openapi", "redoc")):
                raise _HTTPException(status_code=404, detail="not found")
            # Static files (favicon, mascot PNG, manifest, etc.)
            # take priority over the SPA shell. Live acceptance
            # A12: BINARY assets must be served as files with the
            # correct content type — the previous implementation
            # ran every static file through HTMLResponse(read_text)
            # which corrupted PNGs into broken images in
            # production.
            candidate = (static_dir / full_path).resolve()
            try:
                candidate.relative_to(static_dir.resolve())
            except ValueError:
                raise _HTTPException(status_code=404, detail="not found")
            if candidate.is_file() and not candidate.is_symlink():
                return FastResponse(
                    content=candidate.read_bytes(),
                    media_type=_guess_media_type(candidate.suffix.lower()),
                )
            return HTMLResponse(index_html.read_text(encoding="utf-8"))

    return app


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def static_dir_candidates() -> list:
    """SPA static roots, first match wins. Module-level so tests can
    stage a fake dist WITHOUT touching the real frontend/dist (live
    A12 regression: a test once polluted the dev dist with a stub
    index.html and broke the whole e2e suite)."""
    return [
        # Production install — bundled inside the runtime image.
        Path("/app/frontend/dist"),
        # Dev workflow — Vite emits to frontend/dist relative to repo root.
        Path(__file__).resolve().parents[3] / "frontend" / "dist",
        # Bare frontend dir (dev SPA shell served from disk).
        Path(__file__).resolve().parents[3] / "frontend",
    ]


def _guess_media_type(suffix: str) -> str:
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
        ".webp": "image/webp",
        ".css": "text/css",
        ".js": "application/javascript",
        ".json": "application/json",
        ".txt": "text/plain",
        ".woff": "font/woff",
        ".woff2": "font/woff2",
    }.get(suffix, "application/octet-stream")


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for f in path.rglob("*"):
        if f.is_file():
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total


def _empty_snap(adapter: str):
    from ..watchdog.base import AdapterHealthState, WatchdogSnapshot

    return WatchdogSnapshot(adapter=adapter, state=AdapterHealthState.HEALTHY)


class TokenHolder:
    """In-memory holder for the Telegram bot token.

    The token is also persisted in :class:`SecretStore`. This
    holder exists so per-request handlers do not hit the
    secret-store disk every time they need to mask the token
    for the UI. ``peek()`` returns the token without consuming
    it; ``set()`` replaces the in-memory copy.
    """

    def __init__(self) -> None:
        self._token: Optional[str] = None

    def set(self, token: str) -> None:
        self._token = token

    def peek(self) -> Optional[str]:
        return self._token


def _parse_proxy_url(url: str):
    """Parse a SOCKS5 or HTTP(S) proxy URL into a
    :class:`ProxyConfig`.

    Accepted schemes:
    - ``socks5://[user:pass@]host:port``
    - ``http://[user:pass@]host:port``
    - ``https://[user:pass@]host:port`` (only when the
      underlying transport supports CONNECT through TLS).

    Returns ``None`` for unknown schemes or malformed URLs.
    Passwords are URL-decoded.
    """
    from urllib.parse import unquote, urlparse

    from ..adapters.telegram.config import ProxyConfig

    url = (url or "").strip()
    if not url:
        return None
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("socks5", "http", "https"):
        return None
    if not parsed.hostname or not parsed.port:
        return None
    username = unquote(parsed.username) if parsed.username else None
    password = unquote(parsed.password) if parsed.password else None
    return ProxyConfig(
        scheme=scheme,
        host=parsed.hostname,
        port=int(parsed.port),
        username=username,
        password=password,
    )


def _watchdog_public(adapter: str, store: WatchdogStateStore) -> dict:
    snap = store.load(adapter)
    if snap is None:
        return {"adapter": adapter, "state": "unknown"}
    return _watchdog_dto(adapter, store).model_dump()


def _watchdog_dto(adapter: str, store: WatchdogStateStore) -> WatchdogResponse:
    snap = store.load(adapter)
    if snap is None:
        return WatchdogResponse(
            adapter=adapter,
            state="unknown",
            last_success_at=None,
            last_probe_at=None,
            down_since=None,
            consecutive_failures=0,
            last_error_class=None,
            last_error_detail=None,
        )
    return WatchdogResponse(
        adapter=adapter,
        state=snap.state.value,
        last_success_at=snap.last_success_at,
        last_probe_at=snap.last_probe_at,
        down_since=snap.down_since,
        consecutive_failures=snap.consecutive_failures,
        last_error_class=snap.last_error_class,
        last_error_detail=snap.last_error_detail,
    )


def _require_routers_for_health():
    """Hook reserved for future modular routers (Stage 2)."""
    from fastapi import APIRouter

    return APIRouter()


# Build the default-configured app lazily so importing this
# module (for tests that pass their own ``settings=...``)
# does not require SHMAX_DATA_DIR to be writable.
def _default_app():
    return create_app()


# ``uvicorn shmax.main:app`` resolves to a property that
# builds the default app on first access.
class _LazyApp:
    def __getattr__(self, name):
        return getattr(_default_app(), name)


app = _LazyApp()  # type: ignore[assignment]


def _xray_telegram_reachable(runtime, manager):
    if runtime is None:
        return None
    cfg = getattr(runtime.telegram_adapter, "_config", None)
    proxy = getattr(cfg, "proxy", None)
    if proxy is None or proxy.host not in ("127.0.0.1", "localhost", "::1") or proxy.port != manager.socks_port:
        return None
    probe = runtime._integration_probes.get("telegram")
    if not probe or probe.get("adapter") is not runtime.telegram_adapter:
        return None
    import time
    if time.monotonic() - probe["at"] > 120:
        return None
    health = probe.get("health", {})
    return bool(health.get("connected") and health.get("authenticated"))
