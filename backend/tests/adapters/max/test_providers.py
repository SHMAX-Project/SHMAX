"""Tests for the secret-input providers.

These tests verify that pre-filled secrets are returned verbatim
and that the providers never include the secret in ``__repr__``
or in any returned string.
"""

from __future__ import annotations

import asyncio

from shmax.adapters.max.providers import (
    InteractivePasswordProvider,
    InteractiveSmsProvider,
)


def test_sms_provider_pre_filled_returns_code():
    p = InteractiveSmsProvider(code="123456")
    assert asyncio.run(p.get_code(phone="+1234")) == "123456"


def test_sms_provider_does_not_leak_code_in_repr():
    p = InteractiveSmsProvider(code="987654")
    # ``repr`` is allowed to mention the class name but not the code.
    assert "987654" not in repr(p)


def test_password_provider_pre_filled_returns_password():
    p = InteractivePasswordProvider(password="top-secret")
    assert asyncio.run(p.get_password(hint="my hint")) == "top-secret"


def test_password_provider_does_not_leak_password_in_repr():
    p = InteractivePasswordProvider(password="another-secret")
    assert "another-secret" not in repr(p)


def test_read_secret_line_falls_back_when_stdin_is_not_a_tty(
    capsys, monkeypatch
):
    """Regression: when stdin is redirected (no TTY), ``termios``
    raises ``termios.error``. The provider must fall back to
    plain ``readline`` instead of crashing with the raw OS error.

    This is the exact failure mode ``scripts/max_probe.py login
    --phone ...`` hit during the Stage 1B dry run.
    """
    import io
    import sys
    import termios

    from shmax.adapters.max.providers import _read_secret_line

    fake_stdin = io.StringIO("424242\n")

    def _fake_fileno():
        return 0  # any int is fine: tcgetattr will be mocked

    fake_stdin.fileno = _fake_fileno  # type: ignore[attr-defined]
    monkeypatch.setattr(sys, "stdin", fake_stdin)

    def _boom(_fd):
        raise termios.error("not a tty")

    monkeypatch.setattr(termios, "tcgetattr", _boom)
    out = _read_secret_line("code: ")
    assert out == "424242"
    captured = capsys.readouterr()
    assert "code: " in captured.out


def test_read_secret_line_handles_eof(monkeypatch):
    """When ``sys.stdin.readline()`` itself raises ``EOFError``
    (subprocess stdin closed), the provider must propagate it
    cleanly instead of letting a ``termios`` exception mask it.
    """
    import sys

    import pytest

    from shmax.adapters.max.providers import _read_secret_line

    class _ClosedStdin:
        def fileno(self) -> int:
            return 0

        def readline(self) -> str:
            raise EOFError

    monkeypatch.setattr(sys, "stdin", _ClosedStdin())
    with pytest.raises(EOFError):
        _read_secret_line("code: ")
