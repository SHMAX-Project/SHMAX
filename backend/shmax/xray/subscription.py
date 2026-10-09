"""Xray subscription parsing (surgical acceptance, items 5–6).

Supports V1 subscription bodies:

* plain newline-separated ``vless://`` / ``trojan://`` links;
* the whole body Base64-encoded, decoding to the same links.

Every link is parsed into a SANITIZED node record — raw URI/UUID /
password never leave this module except into the SecretStore caller.
Unsupported combinations are marked ``supported=False`` with a
human reason — never silently converted into broken config.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

SUPPORTED_MESSAGE = "Формат подписки пока не поддерживается"


class SubscriptionFormatError(ValueError):
    """The whole subscription body is not a supported V1 format."""


@dataclass(frozen=True)
class XrayNode:
    """SANITIZED node metadata — safe to return to the frontend."""

    node_id: str            # stable short hash of the raw link
    name: str               # URI fragment (human node name)
    protocol: str           # "vless" | "trojan"
    transport: str          # "tcp" | "ws" | "grpc"
    security: str           # "tls" | "reality" | "none"
    address: str
    port: int
    supported: bool
    reason: str = ""        # why unsupported, human-readable (RU)
    # Non-secret details safe for the UI:
    sni: str = ""
    fingerprint: str = ""
    flow: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.node_id,
            "name": self.name,
            "protocol": self.protocol,
            "transport": self.transport,
            "security": self.security,
            "address": self.address,
            "port": self.port,
            "supported": self.supported,
            "reason": self.reason,
            "sni": self.sni,
            "fingerprint": self.fingerprint,
            "flow": self.flow,
        }


@dataclass(frozen=True)
class ParsedSubscription:
    nodes: list[XrayNode]
    # raw link per node_id — SecretStore caller persists it encrypted.
    raw_links: dict[str, str] = field(default_factory=dict)


def decode_subscription_body(body: str) -> list[str]:
    """Return the list of raw share links from a subscription body.

    Plain newline-separated links pass through; a Base64 body is
    decoded first. Anything else (YAML/JSON/VMess-only/HTML) raises
    :class:`SubscriptionFormatError`.
    """
    body = (body or "").strip()
    if not body:
        raise SubscriptionFormatError(SUPPORTED_MESSAGE)

    links = [
        line.strip()
        for line in body.splitlines()
        if line.strip()
    ]
    if links and all(
        _looks_supported(link) for link in links
    ) and any(link.startswith(("vless://", "trojan://")) for link in links):
        return links

    # Try Base64 (whole body).
    try:
        decoded = base64.b64decode(body, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise SubscriptionFormatError(SUPPORTED_MESSAGE) from None
    links = [
        line.strip()
        for line in decoded.splitlines()
        if line.strip()
    ]
    if not links or not any(
        link.startswith(("vless://", "trojan://")) for link in links
    ):
        raise SubscriptionFormatError(SUPPORTED_MESSAGE)
    return links


def _looks_supported(link: str) -> bool:
    return link.startswith(("vless://", "trojan://"))


def parse_subscription(body: str) -> ParsedSubscription:
    """Parse a subscription body into sanitized nodes.

    Individual unsupported links are kept with ``supported=False`` +
    reason; they are visible in the UI but not selectable.
    """
    import hashlib

    links = decode_subscription_body(body)
    nodes: list[XrayNode] = []
    raw_links: dict[str, str] = {}
    for link in links:
        node_id = hashlib.sha256(link.encode("utf-8")).hexdigest()[:16]
        raw_links[node_id] = link
        if link.startswith("vless://"):
            nodes.append(_parse_vless(link, node_id))
        elif link.startswith("trojan://"):
            nodes.append(_parse_trojan(link, node_id))
        else:
            nodes.append(
                XrayNode(
                    node_id=node_id, name="неизвестный узел",
                    protocol="unknown", transport="unknown",
                    security="unknown", address="", port=0,
                    supported=False,
                    reason="Протокол пока не поддерживается",
                )
            )
    return ParsedSubscription(nodes=nodes, raw_links=raw_links)


def _safe_host(address: str) -> str:
    """Address safe to display: bare IPs/hostnames pass through,
    anything weird is masked."""
    address = address.strip()
    try:
        ipaddress.ip_address(address)
        return address
    except ValueError:
        pass
    if address and all(
        ch.isalnum() or ch in {".", "-", "_"} for ch in address
    ):
        return address
    return "(скрыто)"


def _unsupported(name, protocol, transport, security, address, port, reason):
    return XrayNode(
        node_id="", name=name, protocol=protocol, transport=transport,
        security=security, address=_safe_host(address), port=port,
        supported=False, reason=reason,
    )


def _is_valid_reality_pbk(pbk: str) -> bool:
    """X25519 public key: 43 base64url characters (no padding)."""
    import re as _re

    return bool(_re.fullmatch(r"[A-Za-z0-9_-]{43}", pbk or ""))


def _parse_vless(link: str, node_id: str) -> XrayNode:
    try:
        parsed = urllib.parse.urlsplit(link)
        query = urllib.parse.parse_qs(parsed.query)
        name = urllib.parse.unquote(parsed.fragment) or "VLESS узел"
        uuid = parsed.username or ""
        address = parsed.hostname or ""
        port = parsed.port or 0
    except ValueError:
        return _unsupported("VLESS узел", "vless", "?", "?", "", 0,
                            "Некорректная ссылка узла")

    q = {k: v[0] for k, v in query.items()}
    transport = (q.get("type") or "tcp").lower()
    security = (q.get("security") or "none").lower()
    flow = q.get("flow") or ""
    sni = q.get("sni") or ""
    fingerprint = q.get("fp") or ""
    path = q.get("path") or ""
    service_name = q.get("serviceName") or ""
    header_type = (q.get("headerType") or "").lower()

    base = dict(
        node_id=node_id, name=name, protocol="vless",
        transport=transport, security=security,
        address=_safe_host(address), port=port,
        sni=sni, fingerprint=fingerprint, flow=flow,
    )

    if not uuid or not address or not port:
        return XrayNode(**base, supported=False, reason="Некорректная ссылка узла")

    if transport not in ("tcp", "ws", "grpc") or header_type not in ("", "none"):
        return XrayNode(**base, supported=False,
                        reason=f"Транспорт «{transport}» пока не поддерживается")
    if security == "tls":
        return XrayNode(**base, supported=True)
    if security == "reality":
        # Surgical review: validate against pinned Xray 25.3.6
        # compatibility. Reality runs over TCP and gRPC; WS+Reality
        # is rejected by its config test. Public key must be a 43-char
        # base64url X25519 key; Vision is the only supported flow.
        if transport not in ("tcp", "grpc"):
            return XrayNode(
                **base, supported=False,
                reason=f"Reality с транспортом «{transport}» "
                "не поддерживается Xray 25.3.6",
            )
        if not _is_valid_reality_pbk(q.get("pbk") or ""):
            return XrayNode(**base, supported=False,
                            reason="Некорректный Reality public key (pbk)")
        # Semantic compatibility (surgical review): Vision flow is
        # only valid on TCP/RAW — a VLESS outbound does not support
        # Vision with gRPC, even when Xray's config test accepts it.
        if flow == "xtls-rprx-vision" and transport != "tcp":
            return XrayNode(**base, supported=False,
                            reason="Vision flow несовместим с gRPC")
        if flow and flow != "xtls-rprx-vision":
            return XrayNode(**base, supported=False,
                            reason=f"Flow «{flow}» не поддерживается")
        return XrayNode(**base, supported=True)
    if security in ("none", ""):
        return XrayNode(**base, supported=False,
                        reason="VLESS без TLS/Reality не поддерживается")
    return XrayNode(**base, supported=False,
                    reason=f"Безопасность «{security}» пока не поддерживается")


def _parse_trojan(link: str, node_id: str) -> XrayNode:
    try:
        parsed = urllib.parse.urlsplit(link)
        query = urllib.parse.parse_qs(parsed.query)
        name = urllib.parse.unquote(parsed.fragment) or "Trojan узел"
        password = parsed.username or ""
        address = parsed.hostname or ""
        port = parsed.port or 0
    except ValueError:
        return _unsupported("Trojan узел", "trojan", "?", "?", "", 0,
                            "Некорректная ссылка узла")

    q = {k: v[0] for k, v in query.items()}
    transport = (q.get("type") or "tcp").lower()
    security = (q.get("security") or "tls").lower()  # trojan is TLS-native
    sni = q.get("sni") or ""
    fingerprint = q.get("fp") or ""
    header_type = (q.get("headerType") or "").lower()

    base = dict(
        node_id=node_id, name=name, protocol="trojan",
        transport=transport, security=security,
        address=_safe_host(address), port=port,
        sni=sni, fingerprint=fingerprint, flow="",
    )

    if not password or not address or not port:
        return XrayNode(**base, supported=False, reason="Некорректная ссылка узла")
    if transport not in ("tcp", "ws", "grpc") or header_type not in ("", "none"):
        return XrayNode(**base, supported=False,
                        reason=f"Транспорт «{transport}» пока не поддерживается")
    if security in ("tls", ""):
        return XrayNode(**base, supported=True)
    if security == "none":
        return XrayNode(**base, supported=False,
                        reason="Trojan без TLS не поддерживается")
    return XrayNode(**base, supported=False,
                    reason=f"Безопасность «{security}» пока не поддерживается")


# ----------------------------------------------------------------------
# Runtime Xray config generation (ephemeral file content). Secrets
# are embedded here by the caller right before writing 0600 file.
# ----------------------------------------------------------------------
def build_xray_outbound(node: XrayNode, raw_link: str,
                        *, local_socks_port: int) -> dict:
    """Build the full Xray config dict for ONE node + local SOCKS inbound.

    The caller writes it to the ephemeral 0600 config file.
    """
    parsed = urllib.parse.urlsplit(raw_link)
    query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
    transport = node.transport
    security = node.security

    stream: dict = {"network": transport, "security": security}
    tls_name = "realitySettings" if security == "reality" else "tlsSettings"
    if security in ("tls", "reality"):
        settings: dict = {"serverName": query.get("sni") or node.address}
        fp = query.get("fp") or "chrome"
        settings["fingerprint"] = fp
        settings["allowInsecure"] = False
        if security == "reality":
            settings["publicKey"] = query.get("pbk") or ""
            settings["shortId"] = query.get("sid") or ""
            if query.get("spx"):
                settings["spiderX"] = query.get("spx")
        stream[tls_name] = settings

    if transport == "ws":
        ws: dict = {"path": query.get("path") or "/"}
        if query.get("host"):
            ws["headers"] = {"Host": query["host"]}
        stream["wsSettings"] = ws
    elif transport == "grpc":
        stream["grpcSettings"] = {
            "serviceName": query.get("serviceName") or "",
            "multiMode": False,
        }

    if node.protocol == "vless":
        outbound = {
            "protocol": "vless",
            "settings": {
                "vnext": [{
                    "address": node.address,
                    "port": node.port,
                    "users": [{
                        "id": urllib.parse.unquote(parsed.username or ""),
                        "encryption": "none",
                        "flow": query.get("flow") or "",
                    }],
                }]
            },
            "streamSettings": stream,
        }
    else:  # trojan
        outbound = {
            "protocol": "trojan",
            "settings": {
                "servers": [{
                    "address": node.address,
                    "port": node.port,
                    "password": urllib.parse.unquote(parsed.username or ""),
                }]
            },
            "streamSettings": stream,
        }

    return {
        "log": {"loglevel": "warning"},
        "inbounds": [{
            "tag": "socks-in",
            "protocol": "socks",
            "listen": "127.0.0.1",
            "port": local_socks_port,
            "settings": {"auth": "noauth", "udp": False},
        }],
        "outbounds": [
            {**outbound, "tag": "proxy"},
            {"tag": "direct", "protocol": "freedom"},
        ],
        "routing": {
            "rules": [{
                "type": "field",
                "outboundTag": "proxy",
                "domain": ["domain:api.telegram.org", "domain:telegram.org"],
            }]
        },
    }


__all__ = [
    "XrayNode",
    "ParsedSubscription",
    "SubscriptionFormatError",
    "SUPPORTED_MESSAGE",
    "decode_subscription_body",
    "parse_subscription",
    "build_xray_outbound",
]
