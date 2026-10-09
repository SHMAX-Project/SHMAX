"""Surgical acceptance — Xray subscription parsing / sanitization."""

from __future__ import annotations

import base64

import pytest

from shmax.xray.subscription import (
    SUPPORTED_MESSAGE,
    SubscriptionFormatError,
    build_xray_outbound,
    decode_subscription_body,
    parse_subscription,
)

VLESS_TLS = (
    "vless://uuid-1@de1.example.com:443"
    "?type=tcp&security=tls&sni=example.com&fp=chrome"
    "#Germany%20VLESS%20TLS"
)
VLESS_REALITY = (
    "vless://uuid-2@fi1.example.com:443"
    "?type=tcp&security=reality&pbk=pbk8KQSTMHb1by1a9JHx_8bJc3gkMfO2LpVQ0FtN0Lm&sid=abcd"
    "&sni=www.microsoft.com&fp=chrome&flow=xtls-rprx-vision"
    "#Finland%20Reality"
)
VLESS_WS_TLS = (
    "vless://uuid-3@nl1.example.com:443"
    "?type=ws&security=tls&path=%2Fws&host=cdn.example.com"
    "#Netherlands%20WS"
)
VLESS_GRPC_TLS = (
    "vless://uuid-4@de3.example.com:443"
    "?type=grpc&security=tls&serviceName=grpc-svc"
    "#Germany%20gRPC"
)
TROJAN_TCP = (
    "trojan://pass-1@tr1.example.com:443"
    "?type=tcp&security=tls&sni=tr.example.com"
    "#Trojan%20TCP"
)
TROJAN_WS = (
    "trojan://pass-2@tr2.example.com:443"
    "?type=ws&security=tls&path=%2Ftrojan#Trojan%20WS"
)
TROJAN_GRPC = (
    "trojan://pass-3@tr3.example.com:443"
    "?type=grpc&security=tls&serviceName=tr-grpc#Trojan%20gRPC"
)
VLESS_PLAIN = "vless://uuid-5@in1.example.com:80?type=tcp&security=none#Plain"
VLESS_KCP = (
    "vless://uuid-6@kcp.example.com:443?type=kcp&security=tls#KCP"
)
VMESS = "vmess://eyJ2IjoiMiJ9"
_PBK = "pbk8KQSTMHb1by1a9JHx_8bJc3gkMfO2LpVQ0FtN0Lm"


def test_plaintext_subscription_parses_supported_stack():
    body = "\n".join([VLESS_TLS, VLESS_REALITY, VLESS_WS_TLS, VLESS_GRPC_TLS])
    parsed = parse_subscription(body)
    by_proto = {(n.protocol, n.transport, n.security): n for n in parsed.nodes}
    assert ("vless", "tcp", "tls") in by_proto
    assert ("vless", "tcp", "reality") in by_proto
    assert ("vless", "ws", "tls") in by_proto
    assert ("vless", "grpc", "tls") in by_proto
    reality = by_proto[("vless", "tcp", "reality")]
    assert reality.supported is True
    assert reality.flow == "xtls-rprx-vision"
    assert reality.name == "Finland Reality"
    # sanitized: raw UUID never appears in metadata
    assert "uuid-2" not in reality.to_dict().__str__()


def test_trojan_stack_supported():
    body = "\n".join([TROJAN_TCP, TROJAN_WS, TROJAN_GRPC])
    parsed = parse_subscription(body)
    assert all(n.supported for n in parsed.nodes)
    assert {n.transport for n in parsed.nodes} == {"tcp", "ws", "grpc"}
    # sanitized: trojan password never appears in metadata
    for n in parsed.nodes:
        assert "pass-" not in str(n.to_dict())


def test_base64_subscription_decodes():
    body = base64.b64encode(
        "\n".join([VLESS_TLS, TROJAN_TCP]).encode()
    ).decode()
    parsed = parse_subscription(body)
    assert len(parsed.nodes) == 2
    assert parsed.nodes[0].protocol == "vless"
    assert parsed.nodes[1].protocol == "trojan"


def test_malformed_subscription_raises_clear_error():
    for bad in ("", "not-a-subscription", '{"yaml": true}', VMESS,
                "random text\nmore text"):
        with pytest.raises(SubscriptionFormatError) as ei:
            parse_subscription(bad)
        assert SUPPORTED_MESSAGE in str(ei.value)


def test_unsupported_nodes_marked_not_converted():
    body = "\n".join([VLESS_PLAIN, VLESS_KCP, VLESS_TLS])
    parsed = parse_subscription(body)
    plain = next(n for n in parsed.nodes if n.security == "none")
    kcp = next(n for n in parsed.nodes if n.transport == "kcp")
    tls = next(n for n in parsed.nodes
               if n.security == "tls" and n.transport == "tcp")
    assert plain.supported is False
    assert "TLS/Reality" in plain.reason
    assert kcp.supported is False
    assert "не поддерживается" in kcp.reason
    assert tls.supported is True


def test_node_name_from_uri_fragment():
    parsed = parse_subscription(VLESS_REALITY)
    assert parsed.nodes[0].name == "Finland Reality"


def test_secrets_never_returned_in_sanitized_metadata():
    body = "\n".join([VLESS_REALITY, TROJAN_TCP])
    parsed = parse_subscription(body)
    blob = "".join(str(n.to_dict()) for n in parsed.nodes)
    assert "uuid-2" not in blob
    assert "pass-1" not in blob
    assert "pbk=pbk8KQSTMHb1by1a9JHx_8bJc3gkMfO2LpVQ0FtN0Lm" not in blob


def test_build_xray_outbound_vless_reality():
    body = VLESS_REALITY
    parsed = parse_subscription(body)
    node = parsed.nodes[0]
    raw = parsed.raw_links[node.node_id]
    cfg = build_xray_outbound(node, raw, local_socks_port=10808)
    # Local SOCKS inbound only, on loopback.
    assert cfg["inbounds"][0]["listen"] == "127.0.0.1"
    assert cfg["inbounds"][0]["port"] == 10808
    outbound = cfg["outbounds"][0]
    assert outbound["protocol"] == "vless"
    vnext = outbound["settings"]["vnext"][0]
    assert vnext["address"] == "fi1.example.com"
    assert vnext["users"][0]["flow"] == "xtls-rprx-vision"
    stream = outbound["streamSettings"]
    reality = stream["realitySettings"]
    assert reality["publicKey"] == "pbk8KQSTMHb1by1a9JHx_8bJc3gkMfO2LpVQ0FtN0Lm"
    assert reality["shortId"] == "abcd"
    assert stream["security"] == "reality"


def test_build_xray_outbound_trojan_ws():
    parsed = parse_subscription(TROJAN_WS)
    node = parsed.nodes[0]
    cfg = build_xray_outbound(
        node, parsed.raw_links[node.node_id], local_socks_port=10809
    )
    outbound = cfg["outbounds"][0]
    assert outbound["protocol"] == "trojan"
    assert outbound["settings"]["servers"][0]["password"] == "pass-2"
    assert outbound["streamSettings"]["wsSettings"]["path"] == "/trojan"


def test_base64_decode_rejects_binary_garbage():
    with pytest.raises(SubscriptionFormatError):
        decode_subscription_body(base64.b64encode(b"\x00\x01\x02").decode())


# ----------------------------------------------------------------------
# Surgical review — Reality/Xray 25.3.6 validation
# ----------------------------------------------------------------------
def test_reality_ws_rejected():
    body = (
        "vless://uuid-ws@x.example.com:443"
        "?type=ws&security=reality&pbk=pbk8KQSTMHb1by1a9JHx_8bJc3gkMfO2LpVQ0FtN0Lm"
        "&sni=x.com#WSReality"
    )
    n = parse_subscription(body).nodes[0]
    assert n.supported is False
    assert "не поддерживается" in n.reason


def test_reality_bad_pbk_rejected():
    body = (
        "vless://uuid-b@x.example.com:443"
        "?type=tcp&security=reality&pbk=short"
        "&sni=x.com#BadPBK"
    )
    n = parse_subscription(body).nodes[0]
    assert n.supported is False
    assert "public key" in n.reason


def test_reality_grpc_supported():
    body = (
        "vless://uuid-g@x.example.com:443"
        "?type=grpc&security=reality&pbk=pbk8KQSTMHb1by1a9JHx_8bJc3gkMfO2LpVQ0FtN0Lm"
        "&serviceName=svc#GrpcReality"
    )
    n = parse_subscription(body).nodes[0]
    assert n.supported is True


# ----------------------------------------------------------------------
# Surgical review — Vision/transport semantic compatibility
# ----------------------------------------------------------------------
def test_tcp_reality_vision_supported():
    n = parse_subscription(
        "vless://u@x.example.com:443?type=tcp&security=reality"
        "&pbk=" + _PBK + "&flow=xtls-rprx-vision&sni=x.com#TCPVision"
    ).nodes[0]
    assert n.supported is True


def test_grpc_reality_without_flow_supported():
    n = parse_subscription(
        "vless://u@x.example.com:443?type=grpc&security=reality"
        "&pbk=" + _PBK + "&serviceName=svc#GrpcNoFlow"
    ).nodes[0]
    assert n.supported is True
    assert n.flow == ""


def test_grpc_reality_vision_unsupported():
    n = parse_subscription(
        "vless://u@x.example.com:443?type=grpc&security=reality"
        "&pbk=" + _PBK + "&flow=xtls-rprx-vision&serviceName=svc#GrpcVision"
    ).nodes[0]
    assert n.supported is False
    assert "Vision" in n.reason
