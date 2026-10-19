"""Deterministic startup restoration of configured integrations.

Audit P0 #5: after an ordinary reboot SHMAX must come back with
its integrations intact — no owner interaction, no re-auth while
sessions remain valid, and no fake HEALTHY when they do not.

Two flows:

* **Telegram** — the persisted transport configuration (DB
  ``settings`` row + SecretStore secrets) is rebuilt into a
  :class:`TransportConfig`, the adapter is chosen with the real
  bounded probe ladder (:meth:`select_probed`), installed and
  started via :meth:`AppRuntime.install_telegram`.
* **MAX** — the persisted auth snapshot decides: AUTHENTICATED
  snapshots are re-verified against the live profile and the
  listener is started. Expired sessions surface honestly as
  NOT_STARTED / FAILED — never as HEALTHY.

Transport persistence format
============================

Non-secret transport facts live in the DB ``settings`` table
under ``telegram_transport``::

    {"mode": "...", "proxy": {"scheme", "host", "port",
     "username"} | null, "api_id": int | null,
     "mtproxy": {"host", "port"} | null}

Secrets (proxy password, api_hash, MTProxy secret) live ONLY in
the :class:`SecretStore` under ``telegram_proxy_password``,
``telegram_api_hash`` and ``telegram_mtproxy_secret`` (audit
P1 #6 — the previous implementation wrote the full proxy URL,
password included, into the DB row).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from .adapters.telegram.config import (
    MTProxyConfig,
    ProxyConfig,
    TransportConfig,
    TransportMode,
)
from .adapters.telegram.manager import (
    TelegramAutoStateStore,
    TelegramTransportManager,
)
from .database import Database
from .security import SecretStore


logger = logging.getLogger(__name__)

TRANSPORT_SETTING_KEY = "telegram_transport"
PROXY_PASSWORD_SECRET = "telegram_proxy_password"
API_HASH_SECRET = "telegram_api_hash"
MTPROXY_SECRET_KEY = "telegram_mtproxy_secret"


async def persist_telegram_transport(
    db: Database,
    secret_store: SecretStore,
    *,
    mode: TransportMode,
    proxy: Optional[ProxyConfig],
    api_id: Optional[int],
    api_hash: Optional[str],
    mtproxy: Optional[MTProxyConfig],
) -> None:
    """Persist transport config: non-secrets in the DB, secrets
    in the SecretStore (audit P1 #6)."""
    from sqlalchemy import delete as _delete
    from sqlalchemy.dialects.sqlite import insert as _sqlite_insert

    from .database.models import Setting

    row = {
        "mode": mode.value,
        "proxy": (
            {
                "scheme": proxy.scheme,
                "host": proxy.host,
                "port": proxy.port,
                "username": proxy.username,
            }
            if proxy is not None
            else None
        ),
        "api_id": api_id,
        "mtproxy": (
            {"host": mtproxy.host, "port": mtproxy.port}
            if mtproxy is not None
            else None
        ),
    }
    async with db.session() as s:
        await s.execute(
            _sqlite_insert(Setting)
            .values(key=TRANSPORT_SETTING_KEY, value=json.dumps(row, sort_keys=True))
            .on_conflict_do_update(
                index_elements=[Setting.key],
                set_={"value": json.dumps(row, sort_keys=True)},
            )
        )
        await s.commit()

    # Secrets — set the ones present, clear stale ones so a mode
    # switch never leaves an old password behind.
    if proxy is not None and proxy.password:
        secret_store.set_secret(PROXY_PASSWORD_SECRET, proxy.password)
    else:
        secret_store.delete_secret(PROXY_PASSWORD_SECRET)
    if api_hash:
        secret_store.set_secret(API_HASH_SECRET, api_hash)
    else:
        secret_store.delete_secret(API_HASH_SECRET)
    if mtproxy is not None and mtproxy.secret:
        secret_store.set_secret(MTPROXY_SECRET_KEY, mtproxy.secret)
    else:
        secret_store.delete_secret(MTPROXY_SECRET_KEY)


async def load_telegram_transport(
    db: Database,
    secret_store: SecretStore,
) -> Optional[dict[str, Any]]:
    """Load the persisted transport facts (DB row + secrets).

    Returns ``None`` when nothing was persisted yet."""
    from sqlalchemy import select as _select

    from .database.models import Setting

    async with db.session() as s:
        rec = (
            await s.execute(
                _select(Setting).where(Setting.key == TRANSPORT_SETTING_KEY)
            )
        ).scalars().first()
    if rec is None:
        return None
    try:
        facts = json.loads(rec.value)
    except (TypeError, ValueError):
        return None
    if not isinstance(facts, dict):
        return None
    # Re-attach the secrets from the SecretStore.
    if isinstance(facts.get("proxy"), dict) and secret_store.has_secret(
        PROXY_PASSWORD_SECRET
    ):
        facts["proxy"]["password"] = secret_store.get_secret(
            PROXY_PASSWORD_SECRET
        )
    if secret_store.has_secret(API_HASH_SECRET):
        facts["api_hash"] = secret_store.get_secret(API_HASH_SECRET)
    if isinstance(facts.get("mtproxy"), dict) and secret_store.has_secret(
        MTPROXY_SECRET_KEY
    ):
        facts["mtproxy"]["secret"] = secret_store.get_secret(
            MTPROXY_SECRET_KEY
        )
    return facts


def _build_transport_config(facts: dict[str, Any], bot_token: str, session_dir: str) -> TransportConfig:
    mode = TransportMode(facts.get("mode", "auto"))
    proxy: Optional[ProxyConfig] = None
    if isinstance(facts.get("proxy"), dict):
        p = facts["proxy"]
        try:
            proxy = ProxyConfig(
                scheme=str(p.get("scheme", "socks5")),
                host=str(p.get("host", "")),
                port=int(p.get("port", 0)),
                username=p.get("username"),
                password=p.get("password"),
            )
        except (TypeError, ValueError):
            proxy = None
    mtproxy: Optional[MTProxyConfig] = None
    if isinstance(facts.get("mtproxy"), dict):
        m = facts["mtproxy"]
        if m.get("secret"):
            try:
                mtproxy = MTProxyConfig(
                    host=str(m.get("host", "")),
                    port=int(m.get("port", 0)),
                    secret=str(m.get("secret")),
                )
            except (TypeError, ValueError):
                mtproxy = None
    api_id = facts.get("api_id")
    try:
        api_id = int(api_id) if api_id is not None else None
    except (TypeError, ValueError):
        api_id = None
    return TransportConfig(
        mode=mode,
        bot_token=bot_token,
        api_id=api_id,
        api_hash=facts.get("api_hash"),
        proxy=proxy,
        mtproxy=mtproxy,
        session_dir=session_dir,
    )


async def restore_telegram(
    runtime,
    *,
    db: Database,
    secret_store: SecretStore,
    session_dir: str,
    on_command=None,
    auto_state_path=None,
) -> dict:
    """Rebuild the Telegram integration after a restart.

    Returns ``{"restored": bool, "state": str, "detail": str}``.
    Never raises — a broken Telegram config must not prevent the
    web UI from coming up; the dashboard reports DOWN / ERROR.
    """
    result = {"restored": False, "state": "NOT_CONFIGURED", "detail": ""}
    try:
        token = (
            secret_store.get_secret("telegram_bot_token")
            if secret_store.has_secret("telegram_bot_token")
            else None
        )
        if not token:
            result["detail"] = "no bot token saved"
            return result
        facts = await load_telegram_transport(db, secret_store)
        if facts is None:
            result["detail"] = "no transport configuration saved"
            return result
        cfg = _build_transport_config(facts, token, session_dir)
        store = (
            TelegramAutoStateStore(auto_state_path)
            if auto_state_path is not None
            else None
        )
        manager = TelegramTransportManager(cfg, store)
        # P0 #8: the restore path uses the REAL probed selection —
        # AUTO candidates are probed with a timeout and the first
        # working one wins; explicit modes are verified too.
        override = None if cfg.mode == TransportMode.AUTO else cfg.mode
        from .telegram_recovery import proxy_accepting
        previous = runtime.telegram_adapter
        if override is not None:
            manager._build_for_mode(override)  # Validate before TCP readiness; invalid config is permanent.
        if cfg.proxy is not None and cfg.mode in (TransportMode.BOT_API_SOCKS5, TransportMode.BOT_API_HTTP, TransportMode.MTPROTO_SOCKS):
            await proxy_accepting(cfg.proxy)
        adapter = await manager.select_probed(override_mode=override)
        installed = await runtime.install_telegram(
            manager, adapter, on_command=on_command, expected_adapter=previous
        )
        if installed is False:
            return {"restored": False, "state": "DEGRADED", "detail": "configuration_changed"}
        if adapter.is_real():
            async def listener_ready():
                while True:
                    failure = runtime._telegram_failure
                    if failure:
                        return False
                    health = await asyncio.wait_for(adapter.health(), timeout=5)
                    if health.get("connected") and health.get("authenticated") and health.get("polling") is not False:
                        return True
                    await asyncio.sleep(0.05)
            ready = await asyncio.wait_for(listener_ready(), timeout=15)
            if not ready:
                permanent, category = runtime._telegram_failure or (False, "listener_not_ready")
                return {"restored": False, "state": "AUTH_REQUIRED" if permanent else "DEGRADED", "detail": category}
        result["restored"] = True
        result["state"] = "HEALTHY"
        result["detail"] = cfg.mode.value
        return result
    except Exception as e:  # noqa: BLE001
        from .telegram_recovery import failure_kind
        permanent, category = failure_kind(e)
        logger.warning("telegram startup restore failed category=%s permanent=%s", category, permanent)
        result["restored"] = False
        result["state"] = "AUTH_REQUIRED" if permanent else "DEGRADED"
        result["detail"] = category
        return result


# Audit P0 #4: the restore connect must be bounded — a dead MAX
# endpoint must never block the whole web application startup.
RESTORE_CONNECT_TIMEOUT = 10.0
# Surgical review P1 #3: the cleanup itself must be bounded too —
# a connect timeout followed by a hanging disconnect still blocks
# startup forever.
RESTORE_DISCONNECT_TIMEOUT = 5.0


async def _bounded_disconnect(adapter) -> None:
    """Disconnect the adapter with a STRICT timeout.

    A hanging ``disconnect()`` must never extend the restore past
    its bound. CancelledError (of the surrounding restore task) is
    re-raised; any cleanup failure is logged and swallowed — the
    restore outcome already reflects the failure.
    """
    try:
        await asyncio.wait_for(
            adapter.disconnect(), timeout=RESTORE_DISCONNECT_TIMEOUT
        )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        logger.warning(
            "max restore: disconnect timed out after %.1fs — "
            "continuing without cleanup",
            RESTORE_DISCONNECT_TIMEOUT,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "max restore: disconnect failed safely: %s", type(e).__name__
        )


def _publish_max_watchdog_state(runtime, state: str, error_class: str, detail: str) -> None:
    """Record the restore outcome so the dashboard shows an honest
    state instead of a stale snapshot."""
    try:
        from .watchdog.base import AdapterHealthState, WatchdogSnapshot

        mapping = {
            "HEALTHY": AdapterHealthState.HEALTHY,
            "DEGRADED": AdapterHealthState.DEGRADED,
            "AUTH_REQUIRED": AdapterHealthState.AUTH_REQUIRED,
            "DOWN": AdapterHealthState.DOWN,
            "ERROR": AdapterHealthState.DOWN,
        }
        import time as _time

        runtime.watchdog_store.save(
            WatchdogSnapshot(
                adapter="max",
                state=mapping.get(state, AdapterHealthState.DOWN),
                last_probe_at=int(_time.time()),
                last_error_class=error_class,
                last_error_detail=detail[:200],
            )
        )
    except Exception:  # noqa: BLE001
        logger.exception("restore: publishing max watchdog state failed")


async def restore_max(runtime) -> dict:
    """Re-arm the MAX integration after a restart.

    If the persisted auth snapshot says AUTHENTICATED the adapter
    is connected (PyMax connects with the saved session and does
    NOT re-run the SMS flow), the profile is re-verified against
    the live account, and the listener starts — no owner
    interaction needed. Every failure path is honest and bounded:

    * hanging connect → bounded wait, then DEGRADED (web UI still
      starts; audit P0 #4);
    * expired / invalid session → AUTH_REQUIRED;
    * verified reconnect → listener armed, HEALTHY.

    The listener starts ONLY after a verified reconnect.
    """
    result = {"restored": False, "state": "NOT_STARTED", "detail": ""}
    try:
        import asyncio

        from .adapters.max.errors import MaxAuthError, MaxSessionError
        from .auth.max.service import MaxAuthState

        svc = runtime.max_auth
        if svc is None:
            result["detail"] = "max auth service unavailable"
            return result
        snap = svc.snapshot
        if snap.state != MaxAuthState.AUTHENTICATED:
            result["state"] = snap.state.value
            result["detail"] = "auth not completed — owner re-auth required"
            return result
        adapter = runtime.max_adapter
        profile = None
        # P1 #2 (surgical): OWNERSHIP — every unsuccessful restore
        # path disconnects the adapter. The timeout used to leave a
        # half-open client alive (disconnect calls = 0).
        try:
            # Idempotent; with a valid persisted session this
            # performs a plain reconnect (no request_code). BOUNDED:
            # a hanging TCP/TLS attempt must not block app startup.
            await asyncio.wait_for(
                adapter.connect(), timeout=RESTORE_CONNECT_TIMEOUT
            )
            profile = await asyncio.wait_for(
                adapter.get_profile(), timeout=RESTORE_CONNECT_TIMEOUT
            )
        except asyncio.CancelledError:
            # The restore task itself was cancelled — unwind the
            # connect and re-raise.
            await _bounded_disconnect(adapter)
            raise
        except asyncio.TimeoutError:
            logger.warning("max restore: connect timed out (bounded)")
            await _bounded_disconnect(adapter)
            result["state"] = "DEGRADED"
            result["detail"] = "MAX did not answer in time — will retry in background"
            _publish_max_watchdog_state(
                runtime, "DEGRADED", "RestoreTimeout", result["detail"]
            )
            return result
        except (MaxSessionError, MaxAuthError) as e:
            logger.warning(
                "max restore: session rejected: %s", type(e).__name__
            )
            await _bounded_disconnect(adapter)
            result["state"] = "AUTH_REQUIRED"
            result["detail"] = "saved session is invalid or expired — re-auth required"
            _publish_max_watchdog_state(
                runtime, "AUTH_REQUIRED", type(e).__name__, result["detail"]
            )
            return result
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "max restore: unreachable: %s", type(e).__name__
            )
            await _bounded_disconnect(adapter)
            result["state"] = "DEGRADED"
            result["detail"] = f"MAX unreachable: {type(e).__name__}"
            _publish_max_watchdog_state(
                runtime, "DEGRADED", type(e).__name__, result["detail"]
            )
            return result
        if profile is None or not getattr(profile, "user_id", ""):
            # Profile verification failure: disconnect too.
            await _bounded_disconnect(adapter)
        if profile is not None and getattr(profile, "user_id", ""):
            listener_armed = await runtime.start_max_listener()
            if not listener_armed:
                result["state"] = "ERROR"
                result["detail"] = "listener supervisor unavailable"
                _publish_max_watchdog_state(
                    runtime, "ERROR", "NoSupervisor", result["detail"]
                )
                return result
            result["restored"] = True
            result["state"] = "HEALTHY"
            result["detail"] = "session verified; listener started"
            return result
        # Session expired / unusable: publish the honest state so
        # the wizard sends the owner back through MAX auth instead
        # of showing a fake healthy bridge.
        snap = await svc.refresh()
        result["state"] = "AUTH_REQUIRED"
        result["detail"] = "saved session is invalid or expired — re-auth required"
        _publish_max_watchdog_state(
            runtime, "AUTH_REQUIRED", "SessionInvalid", result["detail"]
        )
        return result
    except Exception as e:  # noqa: BLE001
        logger.exception("max startup restore failed")
        result["restored"] = False
        result["state"] = "ERROR"
        result["detail"] = f"{type(e).__name__}: {e}"[:300]
        _publish_max_watchdog_state(
            runtime, "ERROR", type(e).__name__, result["detail"]
        )
        return result


async def bootstrap_xray(
    secret_store: SecretStore, *, xray_manager=None
) -> dict:
    """Start Xray from the cached selected node (bounded).

    Uses the APP-OWNED manager when supplied. Returns
    {"started": bool, "detail": str}. Never raises.
    """
    from .xray import state as xstate
    from .xray.runtime import XrayError

    if not secret_store.has_secret(xstate.TRANSPORT_MODE_KEY):
        return {"started": False, "detail": "xray not configured"}
    if xray_manager is None:
        # Fallback for direct callers without app state.
        from .xray.state import build_manager

        xray_manager = build_manager()
    started, detail = await xstate.start_from_cache(xray_manager, secret_store)
    if not started:
        logger.warning("xray restore: %s — Telegram will be DEGRADED", detail)
    return {"started": started, "detail": detail}


async def restore_integrations(
    runtime,
    *,
    db: Database,
    secret_store: SecretStore,
    session_dir: str,
    on_command=None,
    auto_state_path=None,
    xray_manager=None,
) -> dict:
    """Restore both integrations. Called once from the app
    lifespan right after ``runtime.start()``.

    ``xray_manager`` is the APP-OWNED manager — startup bootstrap
    must use the same instance the API uses (surgical review)."""
    # Surgical acceptance: Xray must be UP before the Telegram
    # listener restores through its local SOCKS. Failure never blocks
    # the web app — Telegram becomes DEGRADED, never direct.
    xray_bootstrap = await bootstrap_xray(secret_store, xray_manager=xray_manager)
    tg = await restore_telegram(
        runtime,
        db=db,
        secret_store=secret_store,
        session_dir=session_dir,
        on_command=on_command,
        auto_state_path=auto_state_path,
    )
    mx = await restore_max(runtime)
    if getattr(runtime,"max_recovery",None) is not None:
        runtime.max_recovery.start()
    from .telegram_recovery import TelegramRecovery
    proxy_failures = 0
    async def prepare_proxy():
        nonlocal proxy_failures
        if xray_manager is None or not secret_store.has_secret("xray_managed"):
            return
        if not xray_manager.is_running:
            proxy_failures = 0
            await bootstrap_xray(secret_store, xray_manager=xray_manager)
            return
        health = await xray_manager.health()
        if health.state == "running":
            proxy_failures = 0
            return  # Never restart a listening proxy because Telegram getMe failed.
        proxy_failures += 1
        if proxy_failures >= 3:
            from .xray.state import selected_node_config
            config = selected_node_config(secret_store, local_socks_port=xray_manager.socks_port)
            if config is not None:
                await xray_manager.restart(config)
            proxy_failures = 0
    async def retry_telegram():
        return await restore_telegram(
            runtime, db=db, secret_store=secret_store, session_dir=session_dir,
            on_command=on_command, auto_state_path=auto_state_path,
        )
    if runtime.telegram_recovery is None:
        runtime.telegram_recovery = TelegramRecovery(runtime, restore=retry_telegram, prepare=prepare_proxy)
    runtime.telegram_recovery.start()
    return {
        "telegram": tg,
        "max": mx,
        "xray": xray_bootstrap,
    }


__all__ = [
    "persist_telegram_transport",
    "load_telegram_transport",
    "restore_telegram",
    "restore_max",
    "restore_integrations",
    "TRANSPORT_SETTING_KEY",
]
