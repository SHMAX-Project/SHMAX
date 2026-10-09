"""Installer dry-run tests.

The installer is a bash script; these tests exercise it in
``--dry-run`` mode so we can verify the contract on macOS / CI
without requiring root or modifying the system.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALLER = REPO_ROOT / "deploy" / "installer" / "install.sh"


def _run(*args: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        [str(INSTALLER), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


@pytest.mark.skipif(not INSTALLER.exists(), reason="installer missing")
def test_installer_help():
    rc, out, _ = _run("--help")
    assert rc == 0
    assert "Modes" in out or "install" in out.lower()


@pytest.mark.skipif(not INSTALLER.exists(), reason="installer missing")
def test_installer_dry_run_install_reports_arch_and_lan():
    rc, out, _ = _run("--dry-run", "--mode", "install")
    assert rc == 0
    assert "[shmax]" in out
    assert "architecture" in out
    assert "LAN IP" in out
    assert "install complete" in out


@pytest.mark.skipif(not INSTALLER.exists(), reason="installer missing")
def test_installer_dry_run_repair_skips_data():
    rc, out, _ = _run("--dry-run", "--mode", "repair")
    assert rc == 0
    assert "repair mode" in out
    assert "preserving data" in out


@pytest.mark.skipif(not INSTALLER.exists(), reason="installer missing")
def test_installer_dry_run_upgrade_refreshes_artifacts():
    rc, out, _ = _run("--dry-run", "--mode", "upgrade")
    assert rc == 0
    assert "upgrade mode" in out
    assert "deployment artifacts" in out


@pytest.mark.skipif(not INSTALLER.exists(), reason="installer missing")
def test_installer_dry_run_uninstall_keeps_data_by_default():
    rc, out, _ = _run("--dry-run", "--mode", "uninstall")
    assert rc == 0
    assert "uninstall mode" in out
    assert "keeping /var/lib/shmax" in out


@pytest.mark.skipif(not INSTALLER.exists(), reason="installer missing")
def test_installer_dry_run_uninstall_purge_warns():
    rc, out, err = _run("--dry-run", "--mode", "uninstall", "--purge")
    assert rc == 0
    combined = out + err
    assert "dry-run + --purge" in combined
    assert "would remove /var/lib/shmax" in combined


@pytest.mark.skipif(not INSTALLER.exists(), reason="installer missing")
def test_installer_dry_run_ready_banner_and_url():
    rc, out, _ = _run("--dry-run", "--mode", "install")
    assert rc == 0
    assert "SHMAX READY" in out
    assert "Шмакс жив" in out
    # The URL is printed via the banner.
    assert re.search(r"http://[\d.]+:\d{2,5}", out) is not None


@pytest.mark.skipif(not INSTALLER.exists(), reason="installer missing")
def test_installer_dry_run_qr_or_fallback_message():
    rc, out, _ = _run("--dry-run", "--mode", "install")
    assert rc == 0
    # Either we rendered a QR (qrencode or python) or we printed
    # the fallback message.
    has_qr = "█" in out or "▀" in out or "▄" in out
    has_fallback = "no QR tool available" in out
    assert has_qr or has_fallback


@pytest.mark.skipif(not INSTALLER.exists(), reason="installer missing")
def test_installer_idempotent_repair():
    """Two consecutive --dry-run --mode install runs must both
    succeed. The script does not actually touch state in dry-run,
    but the contract is that re-running is safe."""
    rc1, _, _ = _run("--dry-run", "--mode", "install")
    rc2, _, _ = _run("--dry-run", "--mode", "repair")
    assert rc1 == 0 and rc2 == 0


@pytest.mark.skipif(not INSTALLER.exists(), reason="installer missing")
def test_installer_unknown_mode_fails():
    rc, out, err = _run("--dry-run", "--mode", "garbage")
    assert rc != 0
    assert "unknown mode" in (out + err)


# ----------------------------------------------------------------------
# Surgical live-acceptance: canonical probe host (LAN readiness +
# token mint) and owner URL printed from the actual bind.
# ----------------------------------------------------------------------
def _extract_env_line(script: str, func: str, var: str):
    """Rough static helper: returns lines of `func` mentioning var."""
    lines = script.splitlines()
    out = []
    inside = False
    for line in lines:
        if line.startswith(f"{func}()"):
            inside = True
        elif inside and line.startswith("}") and line.count("{") == 0:
            inside = False
        if inside and var in line:
            out.append(line.strip())
    return out


@pytest.mark.skipif(not INSTALLER.exists(), reason="installer missing")
def test_probe_host_uses_lan_for_concrete_bind():
    """A concrete LAN bind (192.0.2.161) must be probed directly:
    readiness and token mint must NOT use 127.0.0.1 there."""
    script = INSTALLER.read_text()
    # The canonical helper exists and implements the rules.
    assert "shmax_probe_host()" in script
    # Readiness + token mint go through the canonical base URL.
    readiness_body = script[script.index("wait_for_ready()"):script.index("mint_setup_token()")]
    assert "$(shmax_base_url)" in readiness_body
    assert 'http://127.0.0.1:${SHMAX_PORT}' not in readiness_body
    assert "$(shmax_base_url)/api/setup/token" in script
    assert 'local url="http://127.0.0.1:${SHMAX_PORT}/api/setup/token"' not in script
    # Owner-facing URLs come from the actual bind (ui_host).
    assert 'url="$(print_setup_url "$ui_host"' in script


@pytest.mark.skipif(not INSTALLER.exists(), reason="installer missing")
def test_probe_host_rules_match_spec():
    """Extracted helper logic: LAN -> LAN; 127.0.0.1/0.0.0.0/empty -> loopback."""
    import shlex
    helper_fn = INSTALLER.read_text()
    start = helper_fn.index("shmax_probe_host()")
    end = helper_fn.index("\n}", start) + 2
    fn_src = helper_fn[start:end]
    # Execute just this function in a bash subprocess.
    for bind, expected in (
        ("192.0.2.161", "192.0.2.161"),
        ("127.0.0.1", "127.0.0.1"),
        ("0.0.0.0", "127.0.0.1"),
        ("", "127.0.0.1"),
    ):
        proc = subprocess.run(
            ["bash", "-c",
             f'{fn_src}\nBIND_ADDR="{bind}"; shmax_probe_host'],
            capture_output=True, text=True, timeout=10,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == expected, (
            f"bind={bind!r}: expected {expected}, got {proc.stdout.strip()!r}"
        )


@pytest.mark.skipif(not INSTALLER.exists(), reason="installer missing")
def test_owner_setup_url_uses_actual_bind():
    """The printed Web UI / owner setup URL host is the ACTUAL bind
    (BIND_ADDR), not a freshly detected LAN IP."""
    script = INSTALLER.read_text()
    assert 'log "Web UI:          http://${ui_host}:${SHMAX_PORT}"' in script
    # Owner URL is built from ui_host, not lan_ip.
    main_part = script[script.index("main \"$@\"")::-1]  # not useful; use whole
    assert 'url="$(print_setup_url "$ui_host"' in script
    assert 'url="$(print_setup_url "$lan_ip"' not in script
