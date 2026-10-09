"""Live acceptance A11 — boot orchestration.

Drives the REAL deploy/boot/shmax-boot.sh against a staged host:
fake docker/ip/curl executables on PATH simulate the reproduced
failure modes:

* bind address missing at boot → address appears later → the
  script waits, recreates the container and verifies the port;
* container "running+healthy" but published port EMPTY
  (A11.1) → the script must NOT declare success; it recreates
  until `docker port` shows the publication.
"""

from __future__ import annotations

import os
import stat
import subprocess
import textwrap
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "boot" / "shmax-boot.sh"


def _stage_host(tmp_path: Path, *, bind_present: bool, port_published: bool,
                container_running: bool, ready_ok: bool) -> Path:
    """Create a fake bin dir + compose file + state the fakes read."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    compose = tmp_path / "opt" / "compose.yaml"
    compose.parent.mkdir()
    compose.write_text(
        'services:\n  web:\n    ports:\n      - "192.168.10.20:8787:8787"\n'
    )
    (state / "bind_present").write_text("1" if bind_present else "0")
    (state / "port_published").write_text("1" if port_published else "0")
    (state / "container_running").write_text("1" if container_running else "0")
    (state / "ready_ok").write_text("1" if ready_ok else "0")
    (state / "up_calls").write_text("0")

    docker = bindir / "docker"
    docker.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        STATE="{state}"
        if [[ "$1" == "port" ]]; then
            if [[ "$(cat "$STATE/port_published")" == "1" ]]; then
                echo "8787/tcp -> 192.168.10.20:8787"
            fi
            exit 0
        fi
        if [[ "$1" == "ps" ]]; then
            if [[ "$(cat "$STATE/container_running")" == "1" ]]; then
                echo "shmax-web-1"
            fi
            exit 0
        fi
        if [[ "$1" == "compose" ]]; then
            # recreate: publication now works, container running.
            echo 1 > "$STATE/port_published"
            echo 1 > "$STATE/container_running"
            echo "$(( $(cat "$STATE/up_calls") + 1 ))" > "$STATE/up_calls"
            exit 0
        fi
        exit 0
    """))
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)

    ip = bindir / "ip"
    ip.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        if [[ "$(cat "{state}/bind_present")" == "1" ]]; then
            echo "2: eth0    inet 192.168.10.20/16"
        fi
        exit 0
    """))
    ip.chmod(ip.stat().st_mode | stat.S_IXUSR)

    curl = bindir / "curl"
    curl.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        if [[ "$(cat "{state}/ready_ok")" == "1" ]]; then
            exit 0
        fi
        exit 7
    """))
    curl.chmod(curl.stat().st_mode | stat.S_IXUSR)

    return state


def _run_boot(state: Path, env_extra: dict) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PATH"] = f"{state.parent / 'bin'}:{env['PATH']}"
    env["SHMAX_OPT"] = str(state.parent / "opt")
    env["SHMAX_BOOT_WAIT_TOTAL"] = env_extra.get("WAIT", "12")
    env["SHMAX_BOOT_POLL_INTERVAL"] = "1"
    env["SHMAX_BOOT_CURL_TIMEOUT"] = "2"
    return subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True,
        timeout=120,
    )


def test_boot_waits_for_late_bind_address_then_recreates(tmp_path):
    """The reproduced reboot race: bind absent at boot, appears
    later; orchestration must wait, recreate, verify the port and
    pass host readiness."""
    state = _stage_host(tmp_path, bind_present=False, port_published=False,
                        container_running=False, ready_ok=True)
    # Simulate the LAN address arriving after ~2 polls.
    orig = (state / "bind_present")
    import threading

    def _arrive():
        import time
        time.sleep(2)
        orig.write_text("1")

    threading.Thread(target=_arrive, daemon=True).start()
    result = _run_boot(state, {})
    assert result.returncode == 0, result.stderr
    assert "SHMAX is ready" in result.stderr
    assert int((state / "up_calls").read_text()) >= 1, (
        "container must be (re)created to establish the binding"
    )


def test_boot_repairs_missing_publication_despite_internal_health(tmp_path):
    """A11.1: running container, EMPTY published port — internal
    health is not sufficient; the script recreates and verifies
    the HOST port before declaring ready."""
    state = _stage_host(tmp_path, bind_present=True, port_published=False,
                        container_running=True, ready_ok=True)
    result = _run_boot(state, {})
    assert result.returncode == 0, result.stderr
    assert int((state / "up_calls").read_text()) >= 1
    assert "host port published" in result.stderr


def test_boot_fails_when_readiness_never_passes(tmp_path):
    state = _stage_host(tmp_path, bind_present=True, port_published=True,
                        container_running=True, ready_ok=False)
    env = {"WAIT": "2"}
    result = _run_boot(state, env)
    assert result.returncode != 0, (
        "must not declare success without host-visible readiness"
    )


def test_installer_installs_boot_unit():
    src = (Path(__file__).resolve().parents[2] / "deploy" / "installer" / "install.sh").read_text()
    assert "ensure_boot_unit" in src
    unit = (Path(__file__).resolve().parents[2] / "deploy" / "systemd" / "shmax-boot.service").read_text()
    assert "After=docker.service network-online.target" in unit
    assert "Restart=on-failure" in unit
    boot = SCRIPT.read_text()
    # The A11.1 lesson is pinned: internal health is NOT sufficient.
    assert "docker port" in boot
    assert "health/ready" in boot


def test_boot_uses_persisted_env_bind_and_host_url(tmp_path):
    state = _stage_host(tmp_path, bind_present=False, port_published=False,
                        container_running=False, ready_ok=True)
    (state.parent/'opt/compose.yaml').write_text('services:\n  web:\n    ports:\n      - "${SHMAX_BIND_ADDR:-127.0.0.1}:${SHMAX_PORT:-8787}:8787"\n')
    sentinel=tmp_path/'must-not-execute'
    (state.parent/'opt/.env').write_text(f'SHMAX_BIND_ADDR=192.168.10.20\nSHMAX_PORT=8787\nBAD=$(touch {sentinel})\n')
    curl=state.parent/'bin/curl'
    curl.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "{state}/curl_args"\nexit 0\n')
    import threading, time
    def arrive():
        time.sleep(1); (state/'bind_present').write_text('1')
    threading.Thread(target=arrive,daemon=True).start()
    result=_run_boot(state,{})
    assert result.returncode==0,result.stderr
    assert 'http://192.168.10.20:8787/health/ready' in (state/'curl_args').read_text()
    assert not sentinel.exists()
    assert int((state/'up_calls').read_text())>=1


def test_wrong_host_port_is_not_accepted(tmp_path):
    state = _stage_host(tmp_path,bind_present=True,port_published=True,
                        container_running=True,ready_ok=True)
    docker=state.parent/'bin/docker'
    docker.write_text(docker.read_text().replace('echo "8787/tcp -> 192.168.10.20:8787"',
        'if [[ "$(cat "$STATE/up_calls")" == "0" ]]; then echo "8787/tcp -> 192.168.10.20:87878"; else echo "8787/tcp -> 192.168.10.20:8787"; fi'))
    result=_run_boot(state,{})
    assert result.returncode==0,result.stderr
    assert int((state/'up_calls').read_text())>=1
