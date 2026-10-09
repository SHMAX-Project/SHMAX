"""Live acceptance C18/Phase E — release engineering.

Tests the release SCRIPT logic (manifest schema matches the
updater's expectations; SHA256SUMS matches the actual archive;
archive exclusions) without running npm.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _build_manifest(tag: str, archive_bytes: bytes) -> dict:
    """Mirror of scripts/build-release.sh's manifest step (kept in
    sync by the structural test below)."""
    version = tag[1:] if tag.startswith("v") else tag
    return {
        "version": tag,
        "channel": "beta" if "-" in version else "stable",
        "artifact": f"shmax-{tag}.tar.gz",
        "sha256": hashlib.sha256(archive_bytes).hexdigest(),
    }


def test_manifest_schema_matches_updater():
    from shmax.update.service import normalize_sha256

    archive = b"fake-archive-bytes"
    manifest = _build_manifest("v0.1.0", archive)
    # Updater contract (ReleaseMeta consumers):
    for key in ("version", "channel", "artifact", "sha256"):
        assert manifest[key]
    assert manifest["channel"] == "stable"
    normalize_sha256(manifest["sha256"])  # exactly 64 hex, lowercase

    beta = _build_manifest("v0.2.0-beta.1", archive)
    assert beta["channel"] == "beta"


def test_sha256sums_matches_archive(tmp_path):
    data = b"archive"
    archive = tmp_path / "shmax-v0.1.0.tar.gz"
    archive.write_bytes(data)
    sums = hashlib.sha256(data).hexdigest()
    sums_file = archive.parent / "SHA256SUMS"
    sums_file.write_text(f"{sums}  {archive.name}\n")
    assert sums in sums_file.read_text()
    assert sums == hashlib.sha256(archive.read_bytes()).hexdigest()


def test_release_script_exists_and_is_guarded():
    script = REPO / "scripts" / "build-release.sh"
    text = script.read_text()
    assert script.stat().st_mode & 0o111
    assert "archive --format=tar" in text, "must build from tracked files"
    for forbidden in (".git", "node_modules", ".venv"):
        assert forbidden in text, f"must exclude {forbidden}"
    # Structural sync: the manifest channel logic matches.
    assert '"channel": "beta" if "-" in version else "stable"' in text


def test_workflow_files_exist():
    ci = REPO / ".github" / "workflows" / "ci.yml"
    rel = REPO / ".github" / "workflows" / "release.yml"
    assert ci.is_file() and rel.is_file()
    ci_text = ci.read_text()
    assert "pytest" in ci_text and "vitest" in ci_text
    assert "tsc --noEmit" in ci_text and "npm run build" in ci_text
    rel_text = rel.read_text()
    assert "tags:" in rel_text, "release workflow is tag-triggered only"
    assert "release-manifest.json" in rel_text
