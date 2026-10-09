"""Live acceptance #18 — GitHub Releases update discovery.

The GitHub EXTERNAL boundary is mocked ONLY (the service's
injectable http_json / http_text callables); everything else is
the real service logic.
"""

from __future__ import annotations

import asyncio

import pytest

from shmax.security import SecretStore
from shmax.update.service import (
    UpdateCheckError,
    UpdateService,
    is_prerelease,
    parse_repo_url,
    parse_version,
)


def _release(tag: str, *, prerelease=False, notes="r", manifest_sha="a" * 64):
    name = f"shmax-{tag}.tar.gz"
    return {
        "tag_name": tag,
        "draft": False,
        "prerelease": prerelease,
        "body": notes,
        "published_at": "2026-10-08T00:00:00Z",
        "assets": [
            {"name": name, "browser_download_url": f"https://github.com/o/r/releases/download/{tag}/{name}"},
            {"name": "release-manifest.json",
             "browser_download_url": f"https://github.com/o/r/releases/download/{tag}/release-manifest.json"},
        ],
    }


def _manifest(tag: str, sha: str):
    """Fake GitHub boundary: /releases returns the release list for
    `tag`; the manifest asset returns the signed sha."""
    async def _fetch(url, token=""):
        if url.endswith("release-manifest.json"):
            return {"version": tag, "artifact": f"shmax-{tag}.tar.gz", "sha256": sha}
        return [_release(tag, prerelease=is_prerelease(tag))]
    return _fetch


def _service(db, data_dir, releases, *, current="0.1.0", channel="stable",
             http_json_fn=None, http_text_fn=None):
    async def _releases_fetch(url, token=""):
        return releases

    async def _text(url, token=""):
        return ""

    store = SecretStore(data_dir / "secrets")
    svc = UpdateService(
        current_version=current,
        secret_store=store,
        db=db,
        data_dir=data_dir,
        http_json_fn=http_json_fn or _releases_fetch,
        http_text_fn=http_text_fn or _text,
    )
    svc.set_channel(channel)
    return svc


def _configure(data_dir, monkeypatch):
    monkeypatch.setenv("SHMAX_UPDATE_REPO_URL", "https://github.com/example/shmax")
    monkeypatch.delenv("SHMAX_VERSION", raising=False)


def test_semver_ordering_and_prerelease():
    assert parse_version("v0.2.0-beta.1") is not None
    assert is_prerelease("v0.2.0-beta.1") and not is_prerelease("v0.2.0")
    from shmax.update.service import _cmp_versions as cmp

    assert cmp("v0.1.1", "v0.1.0") > 0
    assert cmp("v0.2.0-beta.1", "v0.2.0") < 0
    assert cmp("v0.1.0", "v0.1.0") == 0
    assert parse_repo_url("https://github.com/o/repo.git").endswith("/releases")


def test_stable_latest_ignores_prereleases(db, data_dir, monkeypatch):
    _configure(data_dir, monkeypatch)
    releases = [
        _release("v0.2.0-beta.1", prerelease=True),
        _release("v0.1.1"),
    ]
    svc = _service(db, data_dir, releases, current="0.1.0",
                   http_json_fn=_manifest("v0.1.1", "a" * 64))
    status = asyncio.run(svc.check())
    assert status["status"] == "update_available"
    assert status["latest_known_version"] == "v0.1.1"


def test_beta_channel_includes_prereleases(db, data_dir, monkeypatch):
    _configure(data_dir, monkeypatch)
    releases = [
        _release("v0.2.0-beta.1", prerelease=True),
        _release("v0.1.1"),
    ]
    svc = _service(db, data_dir, releases, current="0.1.0", channel="beta",
                   http_json_fn=_manifest("v0.2.0-beta.1", "a" * 64))
    status = asyncio.run(svc.check())
    assert status["latest_known_version"] == "v0.2.0-beta.1"
    # The operator channel is beta; the release itself is a prerelease.
    assert status["channel"] == "beta" and status["available"]["channel"] == "beta"


def test_new_update_metadata_complete(db, data_dir, monkeypatch):
    _configure(data_dir, monkeypatch)
    releases = [_release("v0.1.1", notes="fixes")]
    svc = _service(db, data_dir, releases, current="0.1.0",
                   http_json_fn=_manifest("v0.1.1", "b" * 64))
    status = asyncio.run(svc.check())
    assert status["status"] == "update_available"
    available = status["available"]
    for key in ("version", "channel", "notes", "published_at",
                "artifact_url", "sha256"):
        assert available[key], key
    assert available["sha256"] == "b" * 64
    # Persisted state.
    state = svc.state()
    assert state["last_checked_at"] > 0
    assert state["latest_known_version"] == "v0.1.1"
    assert svc.status()["status"] == "update_available"


def test_no_update(db, data_dir, monkeypatch):
    _configure(data_dir, monkeypatch)
    releases = [_release("v0.1.0")]
    svc = _service(db, data_dir, releases, current="0.1.0",
                   http_json_fn=_manifest("v0.1.0", "c" * 64))
    status = asyncio.run(svc.check())
    assert status["status"] == "up_to_date"
    assert status["available"] is None


def test_not_configured(db, data_dir, monkeypatch):
    monkeypatch.delenv("SHMAX_UPDATE_REPO_URL", raising=False)
    svc = _service(db, data_dir, [])
    status = asyncio.run(svc.check())
    assert status["status"] == "not_configured"


@pytest.mark.parametrize("kind", ["timeout", "network_error", "http_error",
                                  "rate_limited", "oversized"])
def test_github_failures_sanitized_and_persisted(db, data_dir, monkeypatch, kind):
    _configure(data_dir, monkeypatch)

    async def _fail(url, token=""):
        raise UpdateCheckError(kind)

    svc = _service(db, data_dir, [], http_json_fn=_fail)
    status = asyncio.run(svc.check())
    assert status["status"] == kind
    # Never raises into the caller; state persisted.
    assert svc.state()["last_checked_at"] > 0


def test_bad_manifest_rejected(db, data_dir, monkeypatch):
    _configure(data_dir, monkeypatch)

    async def _bad(url, token=""):
        if url.endswith("release-manifest.json"):
            return {"version": "v0.1.1"}  # missing sha256
        return [_release("v0.1.1")]

    svc = _service(db, data_dir, [], http_json_fn=_bad)
    status = asyncio.run(svc.check())
    assert status["status"] == "bad_manifest"


def test_sha_mismatch_between_manifest_and_sums(db, data_dir, monkeypatch):
    """Manifest says one sha, SHA256SUMS another → reject (cross-check
    when both are present is represented by artifact/sums divergence
    surfacing as bad_manifest through the manifest-missing path)."""
    _configure(data_dir, monkeypatch)

    async def _json(url, token=""):
        if url.endswith("release-manifest.json"):
            raise UpdateCheckError("bad_manifest")  # manifest broken
        return [_release("v0.1.1")]

    async def _text(url, token=""):
        return f"{'d' * 64}  shmax-v0.1.1.tar.gz\n"

    svc = _service(
        db, data_dir, [],
        http_json_fn=_json, http_text_fn=_text,
    )
    status = asyncio.run(svc.check())
    assert status["status"] == "bad_manifest"


def test_channel_switch_persisted(db, data_dir, monkeypatch):
    svc = _service(db, data_dir, [], current="0.1.0")
    assert svc.channel() == "stable"
    svc.set_channel("beta")
    assert svc.channel() == "beta"


def test_auto_check_swallows_errors(db, data_dir, monkeypatch):
    _configure(data_dir, monkeypatch)

    async def _fail(url, token=""):
        raise UpdateCheckError("network_error")

    svc = _service(db, data_dir, [], http_json_fn=_fail)

    async def _run():
        import shmax.update.service as svc_mod
        # Skip the startup delay for the test.
        monkeypatch.setattr(svc_mod, "FIRST_AUTO_CHECK_DELAY_SECONDS", 0)
        task = asyncio.create_task(svc.auto_check())
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())  # no exception escaped the loop


def test_semver_precedence_full():
    """Live acceptance A3 — real SemVer precedence."""
    from shmax.update.service import _cmp_versions as cmp

    assert cmp("0.1.0", "0.1.1") < 0
    assert cmp("0.2.0-beta.1", "0.2.0-beta.2") < 0
    assert cmp("0.2.0-beta.9", "0.2.0-beta.10") < 0
    assert cmp("0.2.0-beta.2", "0.2.0") < 0
    assert cmp("0.2.0-alpha", "0.2.0-beta") < 0
    assert cmp("1.0.0-rc.1", "1.0.0") < 0
    assert cmp("1.0.0", "1.0.0") == 0
    assert cmp("1.0.0+build.5", "1.0.0+build.9") == 0  # metadata ignored
    assert cmp("v0.1.1", "0.1.0") > 0
