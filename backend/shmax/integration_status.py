"""Canonical UI state. Fake adapters and stale probes are never live evidence
— EXCEPT in test/development runtime modes, where the fake IS the sanctioned
external boundary and a fresh verified probe counts as honest evidence."""
from __future__ import annotations
import os
import time
from pathlib import Path
from sqlalchemy import select
from .database.models import MaxAccount, TelegramAccount, TelegramTarget, Route
from .adapters.max.fake import FakeMaxAdapter
from .adapters.telegram.fake import FakeTelegramAdapter
from .adapters.telegram.config import TransportMode

STATES = {"NOT_CONFIGURED", "CONFIGURED", "CONNECTING", "HEALTHY", "DEGRADED", "DOWN", "AUTH_REQUIRED", "ERROR", "RECONNECTING", "VERIFICATION_UNAVAILABLE"}


def live_adapter(adapter) -> bool:
    return adapter is not None and not isinstance(adapter, (FakeMaxAdapter, FakeTelegramAdapter))


def _counts_as_live(adapter, runtime_mode=None) -> bool:
    """Production rule: fakes are never live evidence. In the
    test/development runtime modes the fake is the sanctioned
    external boundary — the active probe below still verifies it,
    so the snapshot stays honest without lying about production."""
    if live_adapter(adapter):
        return True
    if runtime_mode is not None and getattr(runtime_mode, "value", None) in ("test", "development"):
        return adapter is not None
    return False


def evidence_state(configured, adapter, probe, snap, now, runtime_mode=None):
    if not configured:
        return "NOT_CONFIGURED"
    if not _counts_as_live(adapter, runtime_mode):
        return "CONFIGURED"
    if not probe or probe.get("adapter") is not adapter:
        return "CONNECTING"
    if now - probe["at"] > 2 * 60:
        return "DEGRADED"
    if probe.get("error"):
        if probe.get("error_class")=="auth_rejected":return "AUTH_REQUIRED"
        if probe.get("error_class")=="verification_unavailable":return "VERIFICATION_UNAVAILABLE"
        return "ERROR"
    health = probe["health"]
    if not health.get("authenticated"):
        return "AUTH_REQUIRED"
    if not health.get("connected"):
        return "DOWN"
    # Audit P0 #2: getMe reachable but the polling task dead means
    # no updates are being received — degraded, never HEALTHY.
    if health.get("polling") is False:
        return "DEGRADED"
    if not probe.get("verified"):
        return "CONFIGURED"
    if snap and str(snap.state.value).upper() in {"DOWN", "DEGRADED", "AUTH_REQUIRED"}:
        return str(snap.state.value).upper()
    return "HEALTHY"


async def snapshot(runtime):
    async with runtime.db.session() as s:
        max_account = (await s.execute(select(MaxAccount).limit(1))).scalars().first()
        tg_account = (await s.execute(select(TelegramAccount).limit(1))).scalars().first()
        target = (await s.execute(select(TelegramTarget).limit(1))).scalars().first()
        route = (await s.execute(select(Route).where(Route.enabled.is_(True)).limit(1))).scalars().first()
    def secret(name, env):
        return runtime.secrets.get_secret(name) if runtime.secrets.has_secret(name) else os.environ.get(env)
    max_cfg = getattr(runtime.max_adapter, "_config", None)
    session_paths = list((runtime.layout.root / "sessions" / "max").glob("*.db"))
    phone = (max_account.phone if max_account else None) or getattr(max_cfg, "phone", None) or secret("max_phone", "SHMAX_MAX_PHONE")
    max_configured = bool(max_account and (phone or any(p.is_file() and p.stat().st_size for p in session_paths)))
    tg_cfg = getattr(runtime.telegram_adapter, "_config", None)
    recovery = getattr(runtime, "telegram_recovery", None)
    saved_transport = None
    if recovery is not None:
        from .startup_restore import load_telegram_transport, _build_transport_config
        saved_transport = await load_telegram_transport(runtime.db, runtime.secrets)
        if tg_cfg is None and saved_transport is not None:
            try:
                token_for_config = secret("telegram_bot_token", "SHMAX_TG_BOT_TOKEN")
                tg_cfg = _build_transport_config(saved_transport, token_for_config, "")
            except (ValueError, TypeError):
                pass
    token = getattr(tg_cfg, "bot_token", None) or secret("telegram_bot_token", "SHMAX_TG_BOT_TOKEN")
    mode = getattr(tg_cfg, "mode", None) or os.environ.get("SHMAX_TG_TRANSPORT", "auto")
    mode = getattr(mode, "value", mode)
    valid_transport = mode in {m.value for m in TransportMode}
    if mode in {"mtproto_direct", "mtproto_socks", "mtproto_proxy"}:
        valid_transport = valid_transport and bool(getattr(tg_cfg, "api_id", None) and getattr(tg_cfg, "api_hash", None))
    if mode in {"bot_api_http", "bot_api_socks5", "mtproto_socks"}:
        valid_transport = valid_transport and bool(getattr(tg_cfg, "proxy", None))
    if mode == "mtproto_proxy":
        valid_transport = valid_transport and bool(getattr(tg_cfg, "mtproxy", None))
    tg_configured = bool(tg_account and token and (target or route) and valid_transport)
    now = time.monotonic()
    runtime_mode = getattr(runtime, "runtime_mode", None)
    result = {}
    for name, configured, adapter, watchdog in (("max", max_configured, runtime.max_adapter, runtime.max_watchdog), ("telegram", tg_configured, runtime.telegram_adapter, runtime.tg_watchdog)):
        probe = runtime._integration_probes.get(name)
        state = evidence_state(configured, adapter, probe, None if name=="max" and getattr(runtime,"max_recovery",None) is not None else watchdog.snapshot, now, runtime_mode)
        if name == "telegram" and recovery is not None and configured:
            if recovery.status["state"] not in {"HEALTHY", "NOT_CONFIGURED"}:
                state = recovery.status["state"]
        health = probe.get("health", {}) if probe and probe.get("adapter") is adapter else {}
        result[name] = {"configured": configured, "state": state, "status": state.lower(),
                        "authenticated": bool(health.get("authenticated") and _counts_as_live(adapter, runtime_mode)),
                        "connected": bool(health.get("connected") and _counts_as_live(adapter, runtime_mode)),
                        "account_id": health.get("account_id") if name == "max" else None,
                        "bot_id": health.get("bot_id") if name == "telegram" else None,
                        "last_probe_at": probe.get("wall_at") if probe else None}
    mx_recovery=getattr(runtime,"max_recovery",None)
    if mx_recovery is not None and max_configured:
        result["max"]["recovery"]=dict(mx_recovery.status)
        if mx_recovery.status["state"]!="HEALTHY":
            result["max"]["state"]=mx_recovery.status["state"]
            result["max"]["status"]=mx_recovery.status["state"].lower()
        elif not mx_recovery.listener_alive():
            result["max"].update(state="DEGRADED",status="degraded")
    if recovery is not None:
        result["telegram"]["recovery"] = dict(recovery.status)
    workers = bool(runtime.started and runtime._worker_tasks and all(not task.done() for task in runtime._worker_tasks))
    states = [result[name]["state"] for name in ("max", "telegram")]
    bridge = "NOT_CONFIGURED" if "NOT_CONFIGURED" in states else ("HEALTHY" if states == ["HEALTHY", "HEALTHY"] and workers else "DEGRADED")
    result["bridge"] = {"state": bridge, "status": bridge.lower(), "worker_running": workers}
    return result
