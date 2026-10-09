"""Diagnostics tests."""

from __future__ import annotations

import pytest

from shmax.diagnostics.doctor import Doctor, CheckStatus
from shmax.security import SecretStore


@pytest.mark.asyncio
async def test_doctor_reports_system_and_paths(data_dir, db):
    secret_store = SecretStore(data_dir / "secrets")
    doctor = Doctor(data_dir, db, secret_store)
    report = await doctor.run()
    names = {r.name for r in report.results}
    assert "system" in names
    assert "data_root" in names
    assert "filesystem_writable" in names
    assert "database" in names
    assert "secret_store" in names
    assert "queue" in names
    # External integrations must be reported as NOT_TESTED, not FAIL.
    ext = {r.name for r in report.results if r.name.startswith(("MAX", "Telegram", "Proxy"))}
    for r in report.results:
        if r.name in ext:
            assert r.status == CheckStatus.NOT_TESTED, f"{r.name} should be NOT_TESTED, was {r.status}"
