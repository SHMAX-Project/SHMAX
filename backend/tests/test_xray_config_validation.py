"""Surgical review — generated Xray configs validated by the PINNED
Xray 25.3.6 binary (config-test mode).

Uses the real Xray binary when available:
  * env SHMAX_XRAY_TEST_BINARY, or
  * the downloaded macOS/linux artifact under /tmp/xray-macos/xray,
    /tmp/xray-verify/xray, etc.
Skips (with reason) when no binary is available offline.

Pinned:
  valid TCP Reality/Vision  => config test PASSES
  valid gRPC Reality        => config test PASSES
  WS + Reality              => config test REJECTED
  invalid pbk               => config test REJECTED
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from shmax.xray.runtime import XrayRuntimeManager
from shmax.xray.subscription import build_xray_outbound, parse_subscription

_CANDIDATE_BINARIES = [
    os.environ.get("SHMAX_XRAY_TEST_BINARY", ""),
    "/tmp/xray-macos/xray",
    "/tmp/xray-verify/xray",
    "/usr/local/bin/xray",
]

_PBK = "pbk8KQSTMHb1by1a9JHx_8bJc3gkMfO2LpVQ0FtN0Lm"


def _find_binary() -> str | None:
    for cand in _CANDIDATE_BINARIES:
        if cand and Path(cand).exists():
            return cand
    return None


@pytest.fixture(scope="module")
def xray_binary() -> str | None:
    return _find_binary()


def _link(transport: str, security: str, pbk: str = _PBK) -> str:
    base = (
        f"vless://uuid-x@x.example.com:443?type={transport}"
        f"&security={security}&sni=x.com"
    )
    if security == "reality":
        base += f"&pbk={pbk}&fp=chrome&flow=xtls-rprx-vision"
    if transport == "ws":
        base += "&path=%2Fws"
    if transport == "grpc":
        base += "&serviceName=svc"
    return f"{base}#Node"


def _config_for(link: str, port: int = 10990) -> dict:
    parsed = parse_subscription(link)
    node = parsed.nodes[0]
    return build_xray_outbound(
        node, parsed.raw_links[node.node_id], local_socks_port=port
    )


@pytest.mark.skipif(_find_binary() is None, reason="Xray binary unavailable")
def test_valid_tcp_reality_vision_config_accepted(xray_binary):
    mgr = XrayRuntimeManager(binary_path=xray_binary)
    ok, detail = asyncio_run_test(mgr, _config_for(_link("tcp", "reality")))
    assert ok, detail


@pytest.mark.skipif(_find_binary() is None, reason="Xray binary unavailable")
def test_valid_grpc_reality_config_accepted(xray_binary):
    mgr = XrayRuntimeManager(binary_path=xray_binary)
    ok, detail = asyncio_run_test(mgr, _config_for(_link("grpc", "reality")))
    assert ok, detail


@pytest.mark.skipif(_find_binary() is None, reason="Xray binary unavailable")
def test_ws_reality_config_rejected(xray_binary):
    mgr = XrayRuntimeManager(binary_path=xray_binary)
    ok, detail = asyncio_run_test(mgr, _config_for(_link("ws", "reality")))
    assert not ok, "WS+Reality must be rejected by Xray 25.3.6"


@pytest.mark.skipif(_find_binary() is None, reason="Xray binary unavailable")
def test_invalid_pbk_config_rejected(xray_binary):
    mgr = XrayRuntimeManager(binary_path=xray_binary)
    ok, detail = asyncio_run_test(
        mgr, _config_for(_link("tcp", "reality", pbk="short"))
    )
    assert not ok, "invalid Reality pbk must be rejected"


@pytest.mark.skipif(_find_binary() is None, reason="Xray binary unavailable")
def test_invalid_generated_config_rejected(xray_binary):
    mgr = XrayRuntimeManager(binary_path=xray_binary)
    bad = {
        "inbounds": [{"tag": "s", "protocol": "socks", "listen": "127.0.0.1",
                      "port": 10991, "settings": {"auth": "noauth", "udp": False}}],
        "outbounds": [{"tag": "proxy", "protocol": "unknown-proto",
                       "settings": {}}],
    }
    ok, _ = asyncio_run_test(mgr, bad)
    assert not ok


def asyncio_run_test(mgr: XrayRuntimeManager, config: dict) -> tuple[bool, str]:
    import asyncio

    return asyncio.run(mgr.test_config(config))


@pytest.mark.skipif(_find_binary() is None, reason='Xray binary unavailable')
def test_max_xray_independent_http_inbound_accepted(xray_binary):
    from shmax.max_network import with_http
    manager=XrayRuntimeManager(binary_path=xray_binary)
    config=with_http(_config_for(_link('tcp','reality'),port=10990),10990)
    ok,detail=asyncio_run_test(manager,config)
    assert ok,detail
    assert all(i['listen']=='127.0.0.1' for i in config['inbounds'])


@pytest.mark.skipif(_find_binary() is None, reason='Xray binary unavailable')
def test_max_socks_bridge_for_sdk_uploads_accepted(xray_binary):
    from shmax.max_network import socks_bridge
    manager=XrayRuntimeManager(binary_path=xray_binary)
    config=socks_bridge({'mode':'socks5','host':'127.0.0.1','port':10991,'username':'test-user','password':'synthetic-test-only'},10990)
    ok,detail=asyncio_run_test(manager,config)
    assert ok,detail
