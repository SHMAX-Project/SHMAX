"""Live acceptance A2 — the canonical VERSION must survive packaging.

Source checkout, built wheel and a fresh isolated install must all
report the same version.
"""

from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from pathlib import Path

from shmax import __version__ as source_version

BACKEND = Path(__file__).resolve().parents[1]


def test_source_version_matches_version_file():
    vfile = (BACKEND / "shmax" / "VERSION").read_text().strip()
    assert vfile == source_version
    assert vfile != "0.0.0-dev"


def test_wheel_contains_version_and_install_reports_it(tmp_path):
    """Build the wheel, verify VERSION ships inside, then import the
    package from the extracted wheel content in an isolated
    subprocess — a fresh install must report the exact VERSION."""
    out = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", str(BACKEND),
         "--no-deps", "-w", str(tmp_path)],
        capture_output=True, text=True, timeout=300,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    wheels = list(Path(tmp_path).glob("shmax-*.whl"))
    assert wheels, "wheel was not built"
    with zipfile.ZipFile(wheels[0]) as zf:
        names = zf.namelist()
        assert "shmax/VERSION" in names, (
            "VERSION must be included as package data"
        )
        packaged = zf.read("shmax/VERSION").decode().strip()
        assert packaged == source_version
        # Extract and import WITHOUT the source tree on sys.path.
        extract = tmp_path / "extracted"
        zf.extractall(extract)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(extract)
    probe = subprocess.run(
        [sys.executable, "-c", "import shmax; print(shmax.__version__)"],
        capture_output=True, text=True, env=env, timeout=60,
        cwd=tmp_path,  # never the repo cwd
    )
    assert probe.returncode == 0, probe.stderr[-1000:]
    assert probe.stdout.strip() == source_version
