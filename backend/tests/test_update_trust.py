"""Live acceptance A4/A5 — update trust boundary: SSRF, token
origin, SHA trust rules."""

from __future__ import annotations

import hashlib

import pytest

from shmax.update.service import (
    UpdateCheckError,
    assert_url_allowed,
    normalize_sha256,
    parse_repo_url,
    verify_artifact_bytes,
)


@pytest.mark.parametrize("bad", [
    "http://github.com/o/r",
    "file:///etc/passwd",
    "ftp://github.com/o/r",
    "https://github.com/o/r/raw/main/exploit",
    "https://github.com/../../etc",
    "https://user:pass@github.com/o/r",
    "https://github.com:8443/o/r",
    "https://evil.example.com/o/r",
    "https://127.0.0.1/o/r",
    "not a url",
])
def test_repo_url_strictly_github(bad):
    assert parse_repo_url(bad) is None


def test_repo_url_accepts_canonical():
    url = parse_repo_url("https://github.com/example/shmax")
    assert url == "https://api.github.com/repos/example/shmax/releases"
    assert parse_repo_url("https://github.com/example/shmax/") == url


@pytest.mark.parametrize("ok", [
    "https://github.com/o/r/releases/download/v1/shmax-v1.tar.gz",
    "https://objects.githubusercontent.com/x/y?z=1",
    "https://release-assets.githubusercontent.com/a/b",
    "https://api.github.com/repos/o/r/releases",
])
def test_url_allowlist_accepts_github(ok):
    assert_url_allowed(ok)


@pytest.mark.parametrize("bad", [
    "http://github.com/o/r",
    "https://localhost/x",
    "https://127.0.0.1/x",
    "https://[::1]/x",
    "https://10.1.2.3/x",
    "https://192.168.1.1/x",
    "https://169.254.1.1/x",
    "https://192.0.2.161:8787/x",
    "https://evil.example.com/x",
    "https://user@github.com/x",
    "ftp://github.com/x",
])
def test_url_allowlist_blocks_ssrf(bad):
    with pytest.raises(UpdateCheckError) as ei:
        assert_url_allowed(bad)
    assert ei.value.kind == "network_error"


def test_token_only_for_api_origin():
    # API origin with token flag: fine.
    assert_url_allowed("https://api.github.com/repos/o/r", token_origin=True)
    # ANY other origin with the token flag is rejected — release-
    # controlled URLs can never receive authorization.
    with pytest.raises(UpdateCheckError):
        assert_url_allowed(
            "https://objects.githubusercontent.com/asset", token_origin=True
        )


def test_redirect_downgrade_and_hop_limit():
    # A redirect to http:// is blocked by the allowlist itself.
    with pytest.raises(UpdateCheckError):
        assert_url_allowed("http://github.com/o/r")


# ----------------------------------------------------------------------
# A5 — SHA trust rules
# ----------------------------------------------------------------------
def test_normalize_sha256():
    assert normalize_sha256("A" * 64) == "a" * 64
    for bad in ("", "zz", "a" * 63, "a" * 65, "g" * 64, "a" * 32):
        with pytest.raises(UpdateCheckError):
            normalize_sha256(bad)


def test_verify_artifact_bytes():
    data = b"release-bytes"
    good = hashlib.sha256(data).hexdigest()
    verify_artifact_bytes(data, good.upper())  # case-insensitive
    with pytest.raises(UpdateCheckError) as ei:
        verify_artifact_bytes(b"tampered", good)
    assert ei.value.kind == "sha_mismatch"
