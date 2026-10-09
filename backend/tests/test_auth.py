"""Auth primitives tests."""

from __future__ import annotations

import asyncio

import pytest

from shmax.auth import PasswordHasher, RateLimiter, SessionSigner
from shmax.auth.rate_limit import RateLimitConfig


def test_password_hash_and_verify():
    h = PasswordHasher()
    digest = h.hash("correct horse battery staple")
    assert h.verify("correct horse battery staple", digest)
    assert not h.verify("wrong", digest)


def test_session_round_trip():
    signer = SessionSigner("test-secret-do-not-use-in-prod", max_age_seconds=60)
    sess = signer.issue(username="admin", role="owner")
    token = signer.serialize(sess)
    again = signer.deserialize(token)
    assert again is not None
    assert again.username == "admin"
    assert again.role == "owner"
    assert again.csrf_token == sess.csrf_token


def test_session_bad_signature_rejected():
    signer = SessionSigner("secret-a")
    signer_b = SessionSigner("secret-b")
    sess = signer.issue(username="admin", role="owner")
    token = signer.serialize(sess)
    assert signer_b.deserialize(token) is None


def test_session_requires_secret():
    with pytest.raises(ValueError):
        SessionSigner("")


def test_rate_limiter_locks_out_after_threshold():
    cfg = RateLimitConfig(max_attempts=3, window_seconds=60, lockout_seconds=10)
    rl = RateLimiter(cfg)
    for _ in range(2):
        rl.record_attempt("login", "u", success=False)
    assert not rl.is_locked("login", "u")
    rl.record_attempt("login", "u", success=False)
    assert rl.is_locked("login", "u")
    assert rl.lockout_remaining("login", "u") > 0


def test_rate_limiter_success_clears():
    cfg = RateLimitConfig(max_attempts=2, window_seconds=60, lockout_seconds=10)
    rl = RateLimiter(cfg)
    rl.record_attempt("login", "u", success=False)
    rl.record_attempt("login", "u", success=True)
    assert not rl.is_locked("login", "u")
    rl.record_attempt("login", "u", success=False)
    assert not rl.is_locked("login", "u")
