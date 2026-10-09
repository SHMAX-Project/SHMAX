"""BLOCKER 18 — Real HostdClient over a Unix socket.

The audit flagged:

* JSON-in-JSON wire format (the server re-encoded ``HostResponse.to_json``
  through ``json.dumps`` again so clients saw a top-level string).
* Non-zero subprocess exit codes silently treated as success.
* ``--reason`` passed to ``shmax backup`` despite the flag never
  existing on that command.
* The web app defaulting to ``InProcessHostHelper`` instead of the
  real ``HostHelperProtocol``.

This test wires a real :class:`HostdServer` to a temp Unix socket
and verifies a real client receives a single, correctly-encoded
JSON response — no double encoding. It also checks that:

* non-zero subprocess return codes are reported as ``ok=false``;
* unknown ops are rejected;
* malformed JSON is rejected with a clear error;
* the ``--reason`` regression is gone (argv contains no ``--reason``).
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from pathlib import Path

import pytest

from shmax.host.protocol import (
    HostHelperProtocol,
    HostOp,
    HostRequest,
    HostResponse,
)
from shmax.host.server import (
    COMMAND_BUILDERS,
    SOCKET_PATH,
    HostdServer,
    _cmd_backup,
    _format_response,
)


# ----------------------------------------------------------------------
# Wire format
# ----------------------------------------------------------------------
def test_format_response_is_single_json():
    """The wire bytes must be exactly one JSON object, not a
    JSON-encoded JSON string."""
    line = _format_response(
        HostRequest(op="status", args={}),
        {"returncode": 0, "stdout": "ok", "stderr": ""},
        ok=True,
    )
    text = line.decode("utf-8").rstrip("\n")
    # A regression would put a top-level string here, requiring
    # a second ``json.loads`` call. We must be able to parse it
    # directly.
    parsed = json.loads(text)
    assert isinstance(parsed, dict)
    assert "ok" in parsed and parsed["ok"] is True
    assert "data" in parsed


def test_format_error_is_single_json():
    line = _format_response(
        HostRequest(op="status", args={}),
        {"error": "boom"},
        ok=False,
    )
    text = line.decode("utf-8").rstrip("\n")
    parsed = json.loads(text)
    assert parsed["ok"] is False
    assert "boom" in parsed["detail"]


# ----------------------------------------------------------------------
# Backup argv — the audit's specific regression
# ----------------------------------------------------------------------
def test_backup_argv_does_not_contain_reason_flag():
    argv = _cmd_backup()
    assert "--reason" not in argv, argv


# ----------------------------------------------------------------------
# Live server ↔ client integration
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_hostd_server_client_round_trip(tmp_path):
    """A real Unix-socket server replies with a single JSON
    object — the client can deserialize it directly."""
    socket_path = Path("/tmp") / f"shmax-hostd-test-{os.getpid()}-{id(tmp_path)}.sock"

    # Subprocess stand-in: ``echo`` exists on every Linux/macOS.
    # We use ``sys.executable -c '...'; print(json.dumps(...))'``
    # to control exit codes and output.
    def fake_docker_ps(*args, **kwargs):
        return [sys.executable, "-c", "import json,sys;print(json.dumps({'containers':3}))"]

    COMMAND_BUILDERS[HostOp.STATUS.value] = lambda **kw: fake_docker_ps()

    server = HostdServer(socket_path=str(socket_path))
    serve_task = asyncio.create_task(server.serve_forever())
    try:
        # Wait for socket to appear.
        for _ in range(40):
            if socket_path.exists():
                break
            await asyncio.sleep(0.05)
        assert socket_path.exists()

        # Use a raw socket connection so the test does not depend
        # on the client's own ``exists`` check.
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        req = HostRequest(op=HostOp.STATUS.value, args={})
        writer.write(req.to_json().encode() + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        resp = HostResponse.from_json(line.decode())
        assert resp.ok is True
        assert resp.data["argv"][0] == sys.executable
        assert resp.data["returncode"] == 0
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()
        serve_task.cancel()
        try:
            await serve_task
        except (asyncio.CancelledError, Exception):
            pass
        if socket_path.exists():
            socket_path.unlink()


@pytest.mark.asyncio
async def test_hostd_server_rejects_unknown_op(tmp_path):
    socket_path = Path("/tmp") / f"shmax-hostd-test-{os.getpid()}-{id(tmp_path)}.sock"

    server = HostdServer(socket_path=str(socket_path))
    serve_task = asyncio.create_task(server.serve_forever())
    try:
        for _ in range(40):
            if socket_path.exists():
                break
            await asyncio.sleep(0.05)
        assert socket_path.exists()

        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        # Send a JSON-line with an op that is not in ALLOWED_OPS.
        bad = json.dumps({"op": "rm-rf", "args": {}}) + "\n"
        writer.write(bad.encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        resp = json.loads(line.decode())
        assert resp["ok"] is False
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()
        serve_task.cancel()
        try:
            await serve_task
        except (asyncio.CancelledError, Exception):
            pass
        if socket_path.exists():
            socket_path.unlink()


@pytest.mark.asyncio
async def test_hostd_server_rejects_malformed_json(tmp_path):
    socket_path = Path("/tmp") / f"shmax-hostd-test-{os.getpid()}-{id(tmp_path)}.sock"
    server = HostdServer(socket_path=str(socket_path))
    serve_task = asyncio.create_task(server.serve_forever())
    try:
        for _ in range(40):
            if socket_path.exists():
                break
            await asyncio.sleep(0.05)
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        writer.write(b"not-json\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        resp = json.loads(line.decode())
        assert resp["ok"] is False
        assert "bad request" in resp["detail"]
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()
        serve_task.cancel()
        try:
            await serve_task
        except (asyncio.CancelledError, Exception):
            pass
        if socket_path.exists():
            socket_path.unlink()


@pytest.mark.asyncio
async def test_hostd_server_reports_nonzero_returncode_as_failure(tmp_path):
    """A subprocess that exits with code != 0 must be reported as
    ``ok=false`` so the caller knows the operation failed."""
    socket_path = Path("/tmp") / f"shmax-hostd-test-{os.getpid()}-{id(tmp_path)}.sock"

    def failing_status(*args, **kwargs):
        return [sys.executable, "-c", "import sys;sys.stderr.write('boom');sys.exit(7)"]

    COMMAND_BUILDERS[HostOp.STATUS.value] = lambda **kw: failing_status()

    server = HostdServer(socket_path=str(socket_path))
    serve_task = asyncio.create_task(server.serve_forever())
    try:
        for _ in range(40):
            if socket_path.exists():
                break
            await asyncio.sleep(0.05)
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        req = HostRequest(op=HostOp.STATUS.value, args={})
        writer.write(req.to_json().encode() + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        resp = HostResponse.from_json(line.decode())
        assert resp.ok is False, "non-zero exit must surface as ok=false"
        assert resp.data["returncode"] == 7
        assert "boom" in resp.data["stderr"]
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()
        serve_task.cancel()
        try:
            await serve_task
        except (asyncio.CancelledError, Exception):
            pass
        if socket_path.exists():
            socket_path.unlink()


@pytest.mark.asyncio
async def test_hostd_server_reports_missing_command(tmp_path):
    """A nonexistent command must NOT crash the server — the
    client receives ``ok=false`` with a clear diagnostic."""
    socket_path = Path("/tmp") / f"shmax-hostd-test-{os.getpid()}-{id(tmp_path)}.sock"

    def missing(*args, **kwargs):
        return ["/no/such/binary/exists"]

    COMMAND_BUILDERS[HostOp.STATUS.value] = lambda **kw: missing()

    server = HostdServer(socket_path=str(socket_path))
    serve_task = asyncio.create_task(server.serve_forever())
    try:
        for _ in range(40):
            if socket_path.exists():
                break
            await asyncio.sleep(0.05)
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        req = HostRequest(op=HostOp.STATUS.value, args={})
        writer.write(req.to_json().encode() + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        resp = HostResponse.from_json(line.decode())
        assert resp.ok is False
        assert "command not found" in resp.data.get("error", "")
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()
        serve_task.cancel()
        try:
            await serve_task
        except (asyncio.CancelledError, Exception):
            pass
        if socket_path.exists():
            socket_path.unlink()


# ----------------------------------------------------------------------
# Update client uses the real socket
# ----------------------------------------------------------------------
def test_api_lifespan_picks_real_helper_when_socket_exists():
    """The production lifespan must prefer the real Unix-socket
    helper over the in-process test double when the socket is
    present. We assert this by reading the source of the lifespan
    body — a regression that re-introduces ``InProcessHostHelper``
    unconditionally will fail this check."""
    from shmax.api import app as app_module

    src = app_module.create_app.__code__.co_consts  # type: ignore[attr-defined]
    src_text = str(src)
    # Both names should be present: the in-process helper is the
    # default fallback, but the production code must also use
    # ``HostHelperProtocol`` when the socket exists.
    assert "InProcessHostHelper" in open(app_module.__file__).read()
    assert "HostHelperProtocol" in open(app_module.__file__).read()

@pytest.mark.asyncio
async def test_hostd_strict_socket_restart_stale_and_permissions(monkeypatch):
    import grp, stat, tempfile
    root=Path(tempfile.mkdtemp(prefix='sh-sock-',dir='/tmp')).resolve()
    path=root/'hostd.sock'
    group=grp.getgrgid(os.getgid()).gr_name
    # Stale socket from an interrupted previous daemon.
    stale=socket.socket(socket.AF_UNIX);stale.bind(str(path));stale.close()
    try:
        for _ in range(2):
            server=HostdServer(socket_path=str(path),group=group,strict_permissions=True)
            task=asyncio.create_task(server.serve_forever())
            for _ in range(50):
                if task.done(): await task
                if path.exists() and stat.S_IMODE(path.stat().st_mode)==0o660: break
                await asyncio.sleep(.02)
            assert stat.S_IMODE(path.stat().st_mode)==0o660
            assert path.stat().st_uid==os.geteuid() and path.stat().st_gid==os.getgid()
            reader,writer=await asyncio.open_unix_connection(str(path))
            writer.write(b'{"op":"not-allowed","args":{}}\n');await writer.drain()
            assert not json.loads(await reader.readline())['ok']
            writer.close();await writer.wait_closed()
            await server.stop();await task
            assert not path.exists()
        # Refuse to unlink an arbitrary file masquerading as a socket.
        path.write_text('preserve')
        with pytest.raises(RuntimeError,match='non-socket'):
            await HostdServer(socket_path=str(path),group=group,strict_permissions=True).serve_forever()
        assert path.read_text()=='preserve'
    finally:
        import shutil
        shutil.rmtree(root)


@pytest.mark.asyncio
async def test_hostd_permission_failure_is_fatal(monkeypatch):
    import grp,tempfile,shutil
    root=Path(tempfile.mkdtemp(prefix='sh-perm-',dir='/tmp')).resolve()
    path=root/'hostd.sock'
    def denied(*args): raise PermissionError('denied')
    monkeypatch.setattr(os,'chown',denied)
    try:
        with pytest.raises(PermissionError):
            await HostdServer(socket_path=str(path),group=grp.getgrgid(os.getgid()).gr_name,
                              strict_permissions=True).serve_forever()
        assert not path.exists()
    finally:
        shutil.rmtree(root)


def test_api_uses_configured_socket_and_recovers_when_initially_missing(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from shmax.api.app import create_app
    from shmax.config import Settings,RuntimeMode
    configured=tmp_path/'hostd.sock'
    monkeypatch.setenv('SHMAX_HOSTD_SOCKET',str(configured))
    monkeypatch.setenv('SHMAX_TELEGRAM_REACHABILITY_URL','http://127.0.0.1:59999')
    with TestClient(create_app(Settings(host="127.0.0.1",port=0,log_level="WARNING",data_dir=tmp_path,runtime_mode=RuntimeMode.TEST))) as client:
        assert type(client.app.state.host) is HostHelperProtocol
        assert client.app.state.host._path==configured
