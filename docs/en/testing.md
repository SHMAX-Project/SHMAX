# Testing

Current release: [v0.2.0 validation and limits](../../release/v0.2.0/VALIDATION.md). Older results below are retained as historical evidence.

- Backend: `cd backend && .venv/bin/python -m pytest`, one process. Controlled boundaries: MAX test-fake, local Bot API, injected GitHub HTTP and tmp_path.
- Frontend: `cd frontend && npm run test:unit && npm run build`, including TypeScript.
- Browser: `npm run test:e2e`, sequential desktop-1440x900 / mobile-375x812, real backend/SPA with synthetic external services.
- Journal: `python scripts/benchmark-journal.py --output measurements.json` submits 100,000 synthetic events and measures storage, memory, queries and cleanup. Synthetic results are not live messenger capacity guarantees.
- Debian 13 amd64: production Dockerfile/real Xray binary, signed clean bootstrap, published-version upgrade, forced readiness rejection/cold rollback, persistent-data and journal checks after restart. Use synthetic session fixtures, never production credentials.

v0.1.2 results: 1002 backend passed / 9 local skips; 51 frontend passed; 26 browser passed. Debian additionally: 38 recovery/journal/queue/TG regressions and 5 real-Xray tests passed. macOS skips cover systemd (1), opt-in SPA smoke (3) and unavailable Xray binary (5), checked separately through Debian/Playwright.

Regressions cover MAX listener generations/cleanup/no-SMS recovery, Xray switch/rollback, one Telegram poller, journal privacy/retention/aggregation/disk failure, terminal delivery errors, albums/checkpoints/replies/ACL, signed updates and wheel VERSION.

[v0.1.2 release notes](../../release/v0.1.2/RELEASE_NOTES.en.md).
