"""Xray application-level state (surgical acceptance).

Bridges the XrayRuntimeManager, the SecretStore-persisted
subscription/selection and the Telegram transport into one place.
Secrets (subscription URL, raw links, selected node link) live ONLY
in the SecretStore; SQLite carries sanitized node metadata only.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import urlsplit
from dataclasses import dataclass
from typing import Any, Optional

from ..security import SecretStore
from .runtime import XrayError, XrayRuntimeManager
from .subscription import build_xray_outbound, parse_subscription

logger = logging.getLogger(__name__)

SECRET_SUB_URL = "xray_subscription_url"
SECRET_RAW_LINKS = "xray_raw_links"          # JSON {node_id: raw link}
SECRET_SELECTED_LINK = "xray_selected_link"  # raw link of selection
SECRET_SELECTED_ID = "xray_selected_node_id"
SETTING_NODES = "xray_nodes"                 # sanitized JSON list (SQLite)
TRANSPORT_MODE_KEY = "xray_managed"          # SecretStore marker


@dataclass
class XrayAppState:
    manager: XrayRuntimeManager
    secret_store: SecretStore


def build_manager() -> XrayRuntimeManager:
    """Build the manager from environment (binary path + SOCKS port)."""
    import os

    binary = os.environ.get("SHMAX_XRAY_BINARY", "/usr/local/bin/xray")
    try:
        port = int(os.environ.get("SHMAX_XRAY_SOCKS_PORT", "10808"))
    except ValueError:
        port = 10808
    return XrayRuntimeManager(binary_path=binary, socks_port=port)


def get_manager(app_state: Any) -> XrayRuntimeManager:
    """The APP-OWNED manager singleton (surgical review): created once
    on app state and reused by startup bootstrap, API, health and
    shutdown — never a private/temporary instance."""
    manager = getattr(app_state, "xray_manager", None)
    if manager is None:
        manager = build_manager()
        app_state.xray_manager = manager
    return manager


def store_subscription(
    secret_store: SecretStore, url: str, raw_links: dict[str, str]
) -> None:
    secret_store.set_secret(SECRET_SUB_URL, url)
    secret_store.set_secret(SECRET_RAW_LINKS, json.dumps(raw_links))


def load_subscription(secret_store: SecretStore) -> Optional[dict[str, str]]:
    if not secret_store.has_secret(SECRET_RAW_LINKS):
        return None
    try:
        return json.loads(secret_store.get_secret(SECRET_RAW_LINKS))
    except (TypeError, ValueError):
        return None


def subscription_url(secret_store: SecretStore) -> str:
    if secret_store.has_secret(SECRET_SUB_URL):
        return secret_store.get_secret(SECRET_SUB_URL)
    return ""


async def store_nodes_sanitized(db, nodes: list[dict]) -> None:
    """Sanitized node metadata only — no secrets in SQLite."""
    import json as _json

    from sqlalchemy.dialects.sqlite import insert as _insert

    from ..database.models import Setting

    value = _json.dumps(nodes, ensure_ascii=False)
    async with db.session() as s:
        await s.execute(
            _insert(Setting)
            .values(key=SETTING_NODES, value=value)
            .on_conflict_do_update(
                index_elements=[Setting.key], set_={"value": value}
            )
        )
        await s.commit()


async def load_nodes_sanitized(db) -> list[dict]:
    import json as _json

    from sqlalchemy import select as _select

    from ..database.models import Setting

    async with db.session() as s:
        row = (
            await s.execute(
                _select(Setting).where(Setting.key == SETTING_NODES)
            )
        ).scalars().first()
        try:
            return _json.loads(row.value or "[]") if row is not None else []
        except (TypeError, ValueError):
            return []


def store_selection(secret_store: SecretStore, node_id: str, raw_link: str) -> None:
    secret_store.set_secret(SECRET_SELECTED_ID, node_id)
    secret_store.set_secret(SECRET_SELECTED_LINK, raw_link)
    secret_store.set_secret(TRANSPORT_MODE_KEY, "1")


def load_selected(secret_store: SecretStore) -> Optional[tuple[str, str]]:
    if not (
        secret_store.has_secret(SECRET_SELECTED_ID)
        and secret_store.has_secret(SECRET_SELECTED_LINK)
    ):
        return None
    return (
        secret_store.get_secret(SECRET_SELECTED_ID),
        secret_store.get_secret(SECRET_SELECTED_LINK),
    )


def clear_selection(secret_store: SecretStore) -> None:
    secret_store.delete_secret(SECRET_SELECTED_ID)
    secret_store.delete_secret(SECRET_SELECTED_LINK)
    secret_store.delete_secret(TRANSPORT_MODE_KEY)


def selected_node_config(
    secret_store: SecretStore, *, local_socks_port: int
) -> Optional[dict]:
    """Rebuild the runtime Xray config from the CACHED selected node —
    reboot does not depend on the subscription server being online."""
    selection = load_selected(secret_store)
    if selection is None:
        return None
    node_id, raw_link = selection
    try:
        parsed = parse_subscription(raw_link)
    except Exception:  # noqa: BLE001
        return None
    node = next((n for n in parsed.nodes if n.node_id == node_id), None)
    if node is None or not node.supported:
        return None
    return build_xray_outbound(
        node, raw_link, local_socks_port=local_socks_port
    )


def selected_node_disappeared(secret_store: SecretStore) -> bool:
    """True when the CACHED selected node is gone from the CURRENT
    subscription. Caller surfaces an actionable state and requires
    manual re-selection — never silently picks another node."""
    selection = load_selected(secret_store)
    links = load_subscription(secret_store)
    if selection is None or links is None:
        return False
    return selection[0] not in links


async def start_from_cache(
    manager: XrayRuntimeManager, secret_store: SecretStore
) -> tuple[bool, str]:
    """Startup bootstrap: start Xray from the cached selected node.

    Returns (started, detail). Bounded inside the manager.
    """
    if manager.is_running:
        # Already up (e.g. the Bot-step verify ran after the Network
        # step's select) — that IS the desired state.
        return True, "xray already running"
    config = selected_node_config(secret_store, local_socks_port=manager.socks_port)
    if config is None:
        return False, "нет кэшированного узла"
    try:
        status = await manager.start(config)
        return True, f"xray running (pid={status.pid})"
    except XrayError as e:
        logger.warning("xray startup bootstrap failed: %s", e)
        return False, str(e)


MAX_SUBSCRIPTION_BYTES = 2 * 1024 * 1024  # 2 MiB hard limit (decoded)


async def fetch_subscription(url: str, *, timeout: float = 20.0) -> str:
    """Fetch the subscription body over HTTP(S) with a HARD size
    limit: 2 MiB of DECODED/decompressed bytes, streamed and counted
    (Content-Length alone is not trusted). Non-http(s) schemes are
    rejected. Errors are sanitized — the URL/body never appear in
    the exception text or logs."""
    import aiohttp

    scheme = urlsplit(url.strip()).scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError("некорректная схема подписки")

    received = 0
    parts: list[bytes] = []
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                declared = resp.headers.get("Content-Length")
                if declared and int(declared) > MAX_SUBSCRIPTION_BYTES:
                    raise ValueError("подписка слишком большая (лимит 2 МиБ)")
                stream = getattr(resp, "content", None)
                if stream is None:
                    # Test double without a stream: single-shot text.
                    data = (await resp.text()).encode("utf-8")
                    received += len(data)
                    if received > MAX_SUBSCRIPTION_BYTES:
                        raise ValueError(
                            "подписка слишком большая (лимит 2 МиБ)"
                        )
                    parts.append(data)
                else:
                    # Count each DECODED/decompressed chunk EXACTLY
                    # ONCE — never add the assembled length again
                    # (surgical review: double-counting rejected
                    # real ~1.5 MiB responses).
                    iterator = (
                        stream.iter_any()
                        if hasattr(stream, "iter_any")
                        else stream
                    )
                    async for chunk in iterator:
                        received += len(chunk)
                        if received > MAX_SUBSCRIPTION_BYTES:
                            raise ValueError(
                                "подписка слишком большая (лимит 2 МиБ)"
                            )
                        parts.append(chunk)
    except ValueError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ValueError(
            f"не удалось загрузить подписку ({type(e).__name__})"
        ) from None
    return b"".join(parts).decode("utf-8", errors="replace")


__all__ = [
    "XrayAppState",
    "get_manager",
    "store_subscription",
    "load_subscription",
    "subscription_url",
    "store_nodes_sanitized",
    "load_nodes_sanitized",
    "store_selection",
    "load_selected",
    "clear_selection",
    "selected_node_config",
    "selected_node_disappeared",
    "start_from_cache",
    "fetch_subscription",
    "SECRET_SUB_URL",
    "TRANSPORT_MODE_KEY",
]
