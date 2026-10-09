"""Surgical acceptance — XrayRuntimeManager lifecycle.

Uses a stub binary (a tiny Python script acting as a SOCKS5 server)
instead of the real Xray — the manager contract (bounded
start/stop, exactly one process, no shell=True, crash health,
restart) is what's pinned here.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import textwrap
from pathlib import Path

import pytest

from shmax.xray.runtime import XrayError, XrayRuntimeManager


def _write_stub_binary(dir_path: Path, behavior: str = "socks") -> Path:
    """A stub 'xray' executable.

    behavior:
      socks  — parse -c config, listen SOCKS port, accept & hold.
      crash  — exit(1) immediately.
    """
    body = textwrap.dedent(f"""
        #!/usr/bin/env python3
        import json, socket, sys, time
        behavior = {behavior!r}
        if behavior == "crash":
            print("stub crash", flush=True)
            sys.exit(1)
        # locate config path after -c
        cfg_path = sys.argv[sys.argv.index("-c") + 1]
        with open(cfg_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        port = cfg["inbounds"][0]["port"]
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(8)
        print("READY", flush=True)
        while True:
            conn, _ = srv.accept()
            # SOCKS5 greeting: accept no-auth, answer success on
            # CONNECT, then hold the connection open.
            try:
                data = conn.recv(1024)
                if data and data[0] == 5:
                    conn.sendall(b"\\x05\\x00")
                    req = conn.recv(1024)
                    if len(req) >= 10 and req[1] == 1:
                        conn.sendall(b"\\x05\\x00\\x00\\x01" + b"\\x00" * 6)
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
            except OSError:
                pass
            finally:
                conn.close()
    """)
    target = dir_path / "stub-xray"
    # The shebang MUST be on line 1 — dedent leaves a leading
    # newline that would break exec.
    target.write_text(body.lstrip("\n"))
    target.chmod(target.stat().st_mode | stat.S_IXUSR)
    return target


def _minimal_config(port: int) -> dict:
    return {
        "inbounds": [{
            "tag": "socks-in", "protocol": "socks",
            "listen": "127.0.0.1", "port": port,
            "settings": {"auth": "noauth", "udp": False},
        }],
        "outbounds": [{"tag": "proxy", "protocol": "freedom"}],
    }


@pytest.fixture
def runtime_dir(tmp_path):
    return tmp_path / "xray-run"


@pytest.mark.asyncio
async def test_start_and_health(tmp_path, runtime_dir):
    binary = _write_stub_binary(tmp_path, "socks")
    mgr = XrayRuntimeManager(
        binary_path=str(binary), config_dir=runtime_dir, socks_port=10811
    )
    status = await mgr.start(_minimal_config(10811))
    assert status.state == "running"
    health = await mgr.health()
    assert health.state == "running"
    # exactly one process
    assert mgr._process is not None and mgr._process.returncode is None
    await mgr.stop()
    assert mgr.is_running is False


@pytest.mark.asyncio
async def test_config_file_ephemeral_0600(tmp_path, runtime_dir):
    binary = _write_stub_binary(tmp_path, "socks")
    mgr = XrayRuntimeManager(
        binary_path=str(binary), config_dir=runtime_dir, socks_port=10812
    )
    await mgr.start(_minimal_config(10812))
    cfg_path = mgr._config_path
    assert cfg_path is not None and cfg_path.exists()
    mode = stat.S_IMODE(cfg_path.stat().st_mode)
    assert mode == 0o600, f"config must be 0600, got {oct(mode)}"
    await mgr.stop()


@pytest.mark.asyncio
async def test_crash_during_start_raises_bounded(tmp_path, runtime_dir):
    binary = _write_stub_binary(tmp_path, "crash")
    mgr = XrayRuntimeManager(
        binary_path=str(binary), config_dir=runtime_dir, socks_port=10813
    )
    with pytest.raises(XrayError):
        await mgr.start(_minimal_config(10813))
    assert mgr.is_running is False


@pytest.mark.asyncio
async def test_crash_after_start_reflects_in_health(tmp_path, runtime_dir):
    binary = _write_stub_binary(tmp_path, "socks")
    mgr = XrayRuntimeManager(
        binary_path=str(binary), config_dir=runtime_dir, socks_port=10814
    )
    await mgr.start(_minimal_config(10814))
    # simulate crash
    mgr._process.kill()
    await mgr._process.wait()
    health = await mgr.health()
    assert health.state != "running"
    await mgr.stop()


@pytest.mark.asyncio
async def test_restart_stops_previous_process(tmp_path, runtime_dir):
    binary = _write_stub_binary(tmp_path, "socks")
    mgr = XrayRuntimeManager(
        binary_path=str(binary), config_dir=runtime_dir, socks_port=10815
    )
    await mgr.start(_minimal_config(10815))
    first_pid = mgr._process.pid
    await mgr.restart(_minimal_config(10815))
    assert mgr._process.pid != first_pid
    assert mgr.is_running
    # exactly one process: previous pid must be dead
    with pytest.raises(ProcessLookupError):
        import os

        os.kill(first_pid, 0)
    await mgr.stop()


@pytest.mark.asyncio
async def test_double_start_raises_not_spawns(tmp_path, runtime_dir):
    binary = _write_stub_binary(tmp_path, "socks")
    mgr = XrayRuntimeManager(
        binary_path=str(binary), config_dir=runtime_dir, socks_port=10816
    )
    await mgr.start(_minimal_config(10816))
    with pytest.raises(XrayError):
        await mgr.start(_minimal_config(10816))
    # still exactly one child
    assert mgr.is_running
    await mgr.stop()
    assert mgr.is_running is False


@pytest.mark.asyncio
async def test_no_shell_true_in_spawn(tmp_path, runtime_dir):
    """The manager must spawn via argv list (create_subprocess_exec),
    never a shell string."""
    repo_source = Path(__file__).resolve().parents[1] / "shmax" / "xray" / "runtime.py"
    # Strip docstrings/comments before scanning: the contract is
    # about actual spawn calls.
    import ast

    tree = ast.parse(repo_source.read_text())
    text = ast.get_source_segment(repo_source.read_text(), tree)  # keep raw
    code_lines = [
        line for line in repo_source.read_text().splitlines()
        if not line.strip().startswith(("#", '"', "'"))
    ]
    code = "\n".join(code_lines)
    assert "shell=True" not in code
    assert "create_subprocess_exec" in code
    assert "os.system" not in code


@pytest.mark.asyncio
async def test_stop_is_idempotent(tmp_path, runtime_dir):
    binary = _write_stub_binary(tmp_path, "socks")
    mgr = XrayRuntimeManager(
        binary_path=str(binary), config_dir=runtime_dir, socks_port=10817
    )
    await mgr.stop()  # not running — safe
    await mgr.start(_minimal_config(10817))
    await mgr.stop()
    await mgr.stop()  # already stopped — safe
    assert mgr.status().state in ("stopped", "not_configured")


# ----------------------------------------------------------------------
# Surgical review — cancellation cleanup + stop ownership
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_start_cancelled_during_readiness_reaps_before_raise(
    tmp_path, runtime_dir
):
    """Cancel arrives EXACTLY while _wait_socks_ready polls (child
    alive, SOCKS port never opens): the inline bounded cleanup owns
    the handle until the child is dead AND reaped, so by the time
    CancelledError surfaces the returncode is already set — no
    grace window, no background reaper."""
    body = textwrap.dedent("""
        #!/usr/bin/env python3
        import sys, time
        # Stay alive; never open the SOCKS port — readiness polls
        # forever.
        cfg_path = sys.argv[sys.argv.index("-c") + 1]
        open(cfg_path, "rb").read()
        while True:
            time.sleep(0.1)
    """)
    binary = tmp_path / "stub-xray-nolisten"
    binary.write_text(body.lstrip("\n"))
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)

    mgr = XrayRuntimeManager(
        binary_path=str(binary), config_dir=runtime_dir, socks_port=10822
    )

    async def _do_start():
        await mgr.start(_minimal_config(10822))

    task = asyncio.ensure_future(_do_start())
    child = None
    in_readiness = False
    for _ in range(200):
        await asyncio.sleep(0.05)
        if mgr._process is not None and mgr._process.returncode is None:
            child = mgr._process
            # Confirm we are IN the readiness phase: the SOCKS port
            # still refuses connections (never opened).
            try:
                await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", 10822), timeout=0.2
                )
            except (OSError, asyncio.TimeoutError):
                in_readiness = True
                break
    assert child is not None and in_readiness, "never reached readiness phase"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Cleanup completed BEFORE the cancellation surfaced: the child
    # is already reaped — returncode set with zero grace period.
    assert child.returncode is not None, (
        "cancel surfaced before the child was reaped"
    )
    assert mgr._process is None
    assert mgr.is_running is False


@pytest.mark.asyncio
async def test_start_cancelled_kills_child(tmp_path, runtime_dir):
    """start() cancelled during SOCKS readiness → zero live child,
    clean manager state, CancelledError re-raised."""
    binary = _write_stub_binary(tmp_path, "socks")
    mgr = XrayRuntimeManager(
        binary_path=str(binary), config_dir=runtime_dir, socks_port=10818
    )

    async def _do_start():
        await mgr.start(_minimal_config(10818))

    task = asyncio.ensure_future(_do_start())
    # Wait until the child is actually spawned, then cancel.
    for _ in range(100):
        await asyncio.sleep(0.05)
        if mgr._process is not None:
            break
    assert mgr._process is not None, "child never spawned"
    child = mgr._process
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Zero live children.
    for _ in range(20):
        await asyncio.sleep(0.05)
        if child.returncode is not None:
            break
    assert child.returncode is not None, "child not killed after cancel"
    assert mgr.is_running is False


@pytest.mark.asyncio
async def test_test_config_cancelled_kills_child(tmp_path):
    """test_config() cancelled AFTER CONFIRMED spawn (the child
    publishes its PID) → the pid is dead and reaped BEFORE the
    CancelledError surfaces; CancelledError re-raised."""
    pid_file = tmp_path / "test-config-child.pid"
    body = textwrap.dedent(f"""
        #!/usr/bin/env python3
        import os, sys, time
        pid_path = {str(pid_file)!r}
        with open(pid_path, "w") as fh:
            fh.write(str(os.getpid()))
        # -test mode that never finishes: only cancel/timeout ends it.
        cfg_path = sys.argv[sys.argv.index("-c") + 1]
        open(cfg_path, "rb").read()
        while True:
            time.sleep(0.1)
    """)
    binary = tmp_path / "stub-xray-test-hang"
    binary.write_text(body.lstrip("\n"))
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)

    mgr = XrayRuntimeManager(
        binary_path=str(binary), config_dir=tmp_path / "xray-run", socks_port=10819
    )
    cfg = _minimal_config(10819)

    async def _do_test():
        await mgr.test_config(cfg)

    task = asyncio.ensure_future(_do_test())
    pid = None
    for _ in range(200):
        await asyncio.sleep(0.05)
        if pid_file.exists():
            pid = int(pid_file.read_text().strip())
            break
    assert pid is not None, "test-config child never spawned"
    os.kill(pid, 0)  # alive at cancel time
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The child is dead AND reaped before the cancel surfaced.
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert pid_file.exists()


@pytest.mark.asyncio
async def test_test_config_timeout_reaps_before_return(tmp_path):
    """test_config() timeout: the hung child is killed AND reaped
    BEFORE the (False, timed out) result is returned."""
    pid_file = tmp_path / "test-config-timeout.pid"
    body = textwrap.dedent(f"""
        #!/usr/bin/env python3
        import os, sys, time
        with open({str(pid_file)!r}, "w") as fh:
            fh.write(str(os.getpid()))
        cfg_path = sys.argv[sys.argv.index("-c") + 1]
        open(cfg_path, "rb").read()
        while True:
            time.sleep(0.1)
    """)
    binary = tmp_path / "stub-xray-test-hang2"
    binary.write_text(body.lstrip("\n"))
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)

    mgr = XrayRuntimeManager(
        binary_path=str(binary), config_dir=tmp_path / "xray-run", socks_port=10823
    )
    ok, detail = await mgr.test_config(
        _minimal_config(10823), timeout=0.5
    )
    assert ok is False and "timed out" in detail
    pid = int(pid_file.read_text().strip())
    # Reaped before the result surfaced.
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_stop_repeated_cancellation_reaps_before_raise(
    tmp_path, runtime_dir
):
    """Regression (final repair #1): child ignores SIGTERM; the stop
    cleanup is cancelled REPEATEDLY; the CancelledError may surface
    only AFTER the child is confirmed reaped — returncode set and
    the process gone from the OS before the caller sees anything."""
    import os as _os

    body = textwrap.dedent(f"""
        #!/usr/bin/env python3
        import json, signal, socket, sys
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        with open({str(tmp_path / 'rep.pid')!r}, "w") as fh:
            fh.write(str(__import__('os').getpid()))
        cfg_path = sys.argv[sys.argv.index("-c") + 1]
        with open(cfg_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        port = cfg["inbounds"][0]["port"]
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(8)
        while True:
            srv.accept()
    """)
    stub = runtime_dir / "stub-xray-termignore-repeat"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    stub.write_text(body.lstrip("\n"))
    stub.chmod(0o755)

    mgr = XrayRuntimeManager(
        binary_path=str(stub), config_dir=runtime_dir, socks_port=10824
    )
    await mgr.start(_minimal_config(10824))
    child = mgr._process
    pid = int((tmp_path / "rep.pid").read_text())
    assert child is not None

    async def _do_stop():
        await mgr.stop()

    task = asyncio.ensure_future(_do_stop())
    # Let the task enter stop() (TERM wait runs first), then cancel
    # REPEATEDLY during the cleanup.
    await asyncio.sleep(0.5)
    task.cancel()
    for _ in range(6):
        await asyncio.sleep(0.4)
        if task.done():
            break
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # BEFORE the cancel surfaced: child confirmed reaped — both in
    # asyncio's view and in the OS.
    assert child.returncode is not None
    with pytest.raises(ProcessLookupError):
        _os.kill(pid, 0)
    assert mgr._process is None and mgr.is_running is False
    # Manager fully reusable after the storm.
    st = await mgr.start(_minimal_config(10824))
    assert st.state == "running"
    await mgr.stop()


@pytest.mark.asyncio
async def test_test_config_cancel_during_timeout_cleanup_reaps(
    tmp_path,
):
    """Regression (final repair #2): test_config timeout begins
    cleanup → task cancelled DURING cleanup → zero live child
    before the task returns/raises."""
    pid_file = tmp_path / "test-config-during-timeout.pid"
    body = textwrap.dedent(f"""
        #!/usr/bin/env python3
        import os, signal, sys, time
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        with open({str(pid_file)!r}, "w") as fh:
            fh.write(str(os.getpid()))
        cfg_path = sys.argv[sys.argv.index("-c") + 1]
        open(cfg_path, "rb").read()
        while True:
            time.sleep(0.1)
    """)
    binary = tmp_path / "stub-xray-test-hang3"
    binary.write_text(body.lstrip("\n"))
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)

    mgr = XrayRuntimeManager(
        binary_path=str(binary), config_dir=tmp_path / "xray-run", socks_port=10825
    )

    async def _do_test():
        # 2s timeout: TERM arrives first, cleanup starts, then the
        # cancels land INSIDE the cleanup.
        return await mgr.test_config(_minimal_config(10825), timeout=2.0)

    task = asyncio.ensure_future(_do_test())
    pid = None
    for _ in range(200):
        await asyncio.sleep(0.05)
        if pid_file.exists():
            pid = int(pid_file.read_text().strip())
            break
    assert pid is not None
    # Cancel while the timeout cleanup is running (timeout fires
    # at ~2s; hit the window right after it).
    await asyncio.sleep(2.05)
    task.cancel()
    # Whether the task returns normally or raises, the child must
    # be gone BEFORE that observable outcome.
    await task
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert mgr._config_path is not None  # manager state untouched


@pytest.mark.asyncio
async def test_stop_survives_outer_cancellation_and_reaps(tmp_path, runtime_dir):
    """Child that ignores SIGTERM: an outer timeout cancelling stop()
    mid-TERM still forces KILL and reap; manager ends clean."""
    binary = _write_stub_binary(tmp_path, "socks")

    class _TermIgnoreBinary:
        """Wrapper stub that ignores SIGTERM (exits only on KILL)."""

        @staticmethod
        def write(dir_path: Path) -> Path:
            body = textwrap.dedent(f"""
                #!/usr/bin/env python3
                import json, signal, socket, sys
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                cfg_path = sys.argv[sys.argv.index("-c") + 1]
                with open(cfg_path, encoding="utf-8") as fh:
                    cfg = json.load(fh)
                port = cfg["inbounds"][0]["port"]
                srv = socket.socket()
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                srv.bind(("127.0.0.1", port))
                srv.listen(8)
                print("READY", flush=True)
                while True:
                    conn, _ = srv.accept()
                    try:
                        conn.recv(1024)
                    except OSError:
                        pass
                    finally:
                        conn.close()
            """)
            target = dir_path / "stub-xray-termignore"
            target.write_text(body.lstrip("\n"))
            target.chmod(0o755)
            return target

    runtime_dir.mkdir(parents=True, exist_ok=True)
    stub_path = _TermIgnoreBinary.write(runtime_dir)
    mgr = XrayRuntimeManager(
        binary_path=str(stub_path), config_dir=runtime_dir, socks_port=10821
    )
    await mgr.start(_minimal_config(10821))
    child = mgr._process
    assert child is not None

    # Outer 1s timeout cancels stop() mid-TERM — the manager must
    # still force KILL and reap; whether the outer caller observes
    # TimeoutError or a normal return is irrelevant, the child state
    # is what matters.
    async def _outer():
        await asyncio.wait_for(mgr.stop(), timeout=1.0)

    try:
        await asyncio.wait_for(_outer(), timeout=5.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass

    for _ in range(40):
        await asyncio.sleep(0.1)
        if child.returncode is not None:
            break
    assert child.returncode is not None, "child survived outer-cancelled stop"
    assert mgr.is_running is False
    # Handle-ownership invariant: the handle is cleared ONLY after
    # the child is confirmed reaped — never a cleared handle with a
    # live child.
    assert mgr._process is None
    assert child.returncode is not None

    # A subsequent start and restart both work with a NEW child —
    # the cancelled stop left the manager fully reusable.
    st2 = await mgr.start(_minimal_config(10821))
    assert st2.state == "running" and st2.pid != child.pid
    st3 = await mgr.restart(_minimal_config(10821))
    assert st3.state == "running" and st3.pid != child.pid
    await mgr.stop()
    assert mgr._process is None
    assert mgr.is_running is False
