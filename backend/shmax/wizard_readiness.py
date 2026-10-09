"""Wizard readiness gates.

Each Next button must be tied to ACTUAL validated state — not
mocks, not summary flags, not stored config alone. This module
answers the per-step question: "Is the operator's previous run
genuinely successful, or do we have to send them back?"

Used by the API layer to render the wizard's Next-disabled
states and by the Finish step to refuse to declare readiness.
"""

from __future__ import annotations

import json

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class StepStatus:
    """A single wizard step's readiness.

    ``ready`` is the only field the UI cares about for the Next
    button. ``reason`` is a short Russian / English hint for
    the operator, never a stack trace.
    """

    step: str
    ready: bool
    reason: str
    blocking_check: Optional[str] = None


def _needs(check: str, *, ok: bool, reason: str) -> str:
    """Internal helper — kept for readability."""
    if ok:
        return ""
    return f"{check}: {reason}"


async def wizard_readiness(runtime) -> dict[str, StepStatus]:
    """Compute per-step readiness against the actual runtime state.

    The result is a dict keyed by the canonical step id:
        max, telegram_bot, telegram_network, telegram_group,
        chats, users, finish.

    Each step's status is computed from the live adapter state,
    not from stored configuration alone — the audit explicitly
    forbids declaring readiness from a saved report.
    """
    # Pull the integration snapshot once so we don't redo the
    # same probes twice for each step.
    snap = await runtime.integration_snapshot()
    max_snap = snap.get("max", {})
    tg_snap = snap.get("telegram", {})

    max_state = max_snap.get("state", "NOT_CONFIGURED")
    tg_state = tg_snap.get("state", "NOT_CONFIGURED")

    # MAX step. NOTE: the canonical integration snapshot never
    # reports "AUTHENTICATED" — its healthy state is "HEALTHY"
    # (evidence-based). The old code only accepted the
    # non-existent "AUTHENTICATED" value, so a fully healthy MAX
    # fell into the not-configured else-branch and the Finish
    # gate could never open (audit P0 #13).
    if max_state in ("HEALTHY", "AUTHENTICATED"):
        max_step = StepStatus(
            step="max",
            ready=True,
            reason="MAX authenticated.",
        )
    elif max_state in ("CONFIGURED", "CONNECTING"):
        max_step = StepStatus(
            step="max",
            ready=False,
            reason=(
                "MAX configured but not authenticated yet — "
                "complete the phone → OTP round-trip."
            ),
            blocking_check="max.authenticated",
        )
    elif max_state == "AUTH_REQUIRED":
        max_step = StepStatus(
            step="max",
            ready=False,
            reason=(
                "MAX session revoked. Re-authenticate before "
                "continuing."
            ),
            blocking_check="max.authenticated",
        )
    elif max_state in ("DOWN", "DEGRADED", "ERROR"):
        max_step = StepStatus(
            step="max",
            ready=False,
            reason=(
                f"MAX adapter reports {max_state}. The wizard "
                "blocks downstream steps until MAX is healthy."
            ),
            blocking_check="max.healthy",
        )
    else:
        max_step = StepStatus(
            step="max",
            ready=False,
            reason=(
                "MAX not configured. Enter a real phone number "
                "and complete OTP authentication."
            ),
            blocking_check="max.configured",
        )

    # Telegram bot step — must have a token that getMe verified
    if tg_state in ("HEALTHY", "DEGRADED"):
        tg_bot_step = StepStatus(
            step="telegram_bot",
            ready=True,
            reason="Telegram bot verified and reachable.",
        )
    elif tg_state == "CONFIGURED":
        tg_bot_step = StepStatus(
            step="telegram_bot",
            ready=False,
            reason=(
                "Telegram bot configured but not verified — re-check "
                "the token and let the bot reach api.telegram.org."
            ),
            blocking_check="telegram.verified",
        )
    elif tg_state in ("DOWN", "ERROR"):
        tg_bot_step = StepStatus(
            step="telegram_bot",
            ready=False,
            reason=(
                f"Telegram bot reports {tg_state}. Verify the token "
                "and that the bot is not in conflict with another "
                "polling consumer."
            ),
            blocking_check="telegram.healthy",
        )
    else:
        tg_bot_step = StepStatus(
            step="telegram_bot",
            ready=False,
            reason=(
                "Telegram bot not configured. Save a real BotFather "
                "token and verify it."
            ),
            blocking_check="telegram.configured",
        )

    # Telegram network — requires a working transport. We
    # piggy-back on the bot state because the runtime reports a
    # single Telegram state across bot and network.
    tg_network_step = StepStatus(
        step="telegram_network",
        ready=tg_bot_step.ready,
        reason=(
            tg_bot_step.reason
            if not tg_bot_step.ready
            else "Telegram transport reachable."
        ),
        blocking_check=tg_bot_step.blocking_check,
    )

    # Telegram group — requires a paired TelegramTarget row that
    # was VALIDATED against the real Telegram API with no missing
    # permissions (audit P0 #3 / P0 #13). A raw row alone is not
    # readiness.
    from sqlalchemy import select
    from .database.engine import Database
    from .database.models import TelegramTarget

    import json as _json

    target_ok = False
    target_reason = ""
    from .active_target import get_active_target

    target = await get_active_target(runtime.db)
    if target is None:
        target_ok = False
        target_reason = (
            "Pair the operator's Telegram supergroup via "
            "/shmax_connect <code> in the wizard."
        )
    else:
        try:
            missing = list(_json.loads(target.missing_permissions or "[]"))
        except (TypeError, ValueError):
            missing = ["validation state unreadable"]
        if missing:
            # Authoritative invalid state (revoked rights / failed
            # live check) — reported BEFORE the generic "never
            # validated" message so the operator sees WHAT broke.
            target_ok = False
            target_reason = (
                "Group is missing required bot rights: "
                + ", ".join(missing)
                + "."
            )
        elif target.validated_at is None:
            target_ok = False
            target_reason = (
                "Group saved but not validated against the Telegram "
                "API yet — resend /shmax_connect."
            )
        else:
            target_ok = True
            target_reason = (
                "Group paired and validated: "
                f"{target.title or target.chat_id}."
            )

    tg_ok = target_ok
    tg_group_step = StepStatus(
        step="telegram_group",
        ready=tg_ok,
        reason=target_reason,
        blocking_check=(
            None if target_ok else "telegram.target_paired"
        ),
    )

    # MAX chats — at least one enabled route that actually maps to
    # the ACTIVE target; a forum target additionally requires a
    # thread id on the route (audit P0 #2 surgical: an arbitrary
    # enabled route pointing elsewhere, or a topic-less forum
    # route, must not satisfy readiness).
    from .database.models import Route

    from .active_target import get_active_target

    active = await get_active_target(runtime.db)
    routes_ok = False
    route_reason = ""
    async with runtime.db.session() as s:  # type: ignore[attr-defined]
        from sqlalchemy import select as _select
        enabled_routes = (
            await s.execute(_select(Route).where(Route.enabled.is_(True)))
        ).scalars().all()
    if active is None:
        route_reason = "Pair the Telegram group first — no active target."
    else:
        mapped = [
            r for r in enabled_routes
            if str(r.telegram_chat_id) == str(active.chat_id)
        ]
        if not mapped:
            route_reason = (
                "No enabled route maps the active Telegram group."
            )
        else:
            if active.is_forum and any(not r.telegram_thread_id for r in mapped):
                route_reason = (
                    "Forum routes must carry a topic (thread id) — "
                    "recreate the topic for the broken route."
                )
            else:
                routes_ok = True
                route_reason = f"{len(mapped)} valid route(s) to the active group."
    chats_step = StepStatus(
        step="chats",
        ready=routes_ok,
        reason=route_reason,
        blocking_check=(
            None if routes_ok else "routes.valid_to_active_target>=1"
        ),
    )

    # Users — owner must exist and at least one Telegram user who
    # is approved, NOT blocked, and whose role actually grants
    # send_message (audit P0 #2 surgical: a blocked or read_only
    # "approved" user must not satisfy readiness).
    from .database.models import AdminUser, TelegramUser

    from .domain.enums import Role as _Role
    from .auth.pairing import DEFAULT_PERMISSIONS as _PERMS

    owner_ok = False
    usable_user_ok = False
    async with runtime.db.session() as s:  # type: ignore[attr-defined]
        owner = (
            await s.execute(select(AdminUser).limit(1))
        ).scalars().first()
        owner_ok = owner is not None
        rows = (
            await s.execute(select(TelegramUser).where(TelegramUser.approved.is_(True)))
        ).scalars().all()
    for u in rows:
        if u.blocked:
            continue
        try:
            role = _Role(u.role)
        except ValueError:
            continue
        if "send_message" in _PERMS.get(role, set()):
            usable_user_ok = True
            break

    users_step = StepStatus(
        step="users",
        ready=owner_ok and usable_user_ok,
        reason=(
            f"Owner {'present' if owner_ok else 'missing'}; "
            f"approved user allowed to send "
            f"{'yes' if usable_user_ok else 'no'}."
        ),
        blocking_check=(
            None
            if (owner_ok and usable_user_ok)
            else ("admin.exists" if not owner_ok else "telegram.approved_count>=1")
        ),
    )

    # Finish — every preceding step must be ready AND the bridge
    # state must be HEALTHY with workers running AND the latest
    # mandatory Test must have SUCCEEDED AND the mandatory active
    # probes must CURRENTLY pass (audit P0 #2 surgical).
    bridge_state = snap.get("bridge", {}).get("state", "NOT_CONFIGURED")

    test_ok = False
    test_reason = ""
    from .database.models import Setting as _Setting

    async with runtime.db.session() as s:  # type: ignore[attr-defined]
        test_row = (
            await s.execute(
                select(_Setting).where(_Setting.key == "wizard_test_latest")
            )
        ).scalars().first()
    if test_row is None:
        test_reason = "Run the mandatory Test — no result recorded."
    else:
        try:
            test_data = json.loads(test_row.value)
        except (TypeError, ValueError):
            test_data = {}
        if not test_data.get("ok"):
            failed = ", ".join(test_data.get("failed", [])) or "unknown"
            test_reason = f"Latest Test FAILED ({failed}) — re-run it."
        else:
            test_ok = True
            test_reason = "Latest Test passed."

    probes_ok = False
    probes_reason = ""
    max_state_v = max_snap.get("state", "NOT_CONFIGURED")
    tg_state_v = tg_snap.get("state", "NOT_CONFIGURED")
    if max_state_v == "HEALTHY" and tg_state_v == "HEALTHY":
        probes_ok = True
        probes_reason = "Active probes pass."
    else:
        probes_reason = (
            f"Active probes: MAX={max_state_v}, Telegram={tg_state_v}."
        )

    finish_ready = (
        max_step.ready
        and tg_bot_step.ready
        and tg_network_step.ready
        and tg_group_step.ready
        and chats_step.ready
        and users_step.ready
        and bridge_state == "HEALTHY"
        and test_ok
        and probes_ok
    )
    if finish_ready:
        finish_reason = "All V1 prerequisites satisfied."
    else:
        missing = [
            s.step
            for s in (
                max_step,
                tg_bot_step,
                tg_network_step,
                tg_group_step,
                chats_step,
                users_step,
            )
            if not s.ready
        ]
        blockers = []
        if missing:
            blockers.append("steps: " + ", ".join(missing))
        if bridge_state != "HEALTHY":
            blockers.append(f"bridge={bridge_state}")
        if not test_ok:
            blockers.append(test_reason)
        if not probes_ok:
            blockers.append(probes_reason)
        finish_reason = "Not ready: " + "; ".join(blockers) + "."
    finish_step = StepStatus(
        step="finish",
        ready=finish_ready,
        reason=finish_reason,
        blocking_check=(
            None
            if finish_ready
            else (
                "wizard.test"
                if not test_ok
                else ("bridge.healthy" if bridge_state != "HEALTHY" else "wizard.steps")
            )
        ),
    )

    return {
        s.step: s
        for s in (
            max_step,
            tg_bot_step,
            tg_network_step,
            tg_group_step,
            chats_step,
            users_step,
            finish_step,
        )
    }


__all__ = ["StepStatus", "wizard_readiness"]