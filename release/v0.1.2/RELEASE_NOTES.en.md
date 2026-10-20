# SHMAX v0.1.2 — connection recovery and persistent event journal

Stable patch release, 20 October 2026. This release adds saved-session MAX recovery, fixes Xray/Telegram switching and introduces a persistent Web UI event journal.

## Changes

- **Automatic MAX recovery:** one lifecycle owner reconnects the saved session with bounded timeouts and exponential backoff with jitter. Recovery cannot initiate SMS, logout or relogin. Revoked authentication requires explicit operator login.
- **One current MAX listener:** close the old consumer and both pending waiters, rebind to the new generation and ignore obsolete SDK callbacks.
- **Reliable Xray switching:** fix the complete TransportConfig/bot-token path, serialize against Telegram recovery and distinguish network application, adapter installation and rollback. Failed switches attempt to restore the previous node. No direct bypass or weakened TLS.
- **Accurate health states:** RECONNECTING / VERIFICATION_UNAVAILABLE are distinct from AUTH_REQUIRED; the dashboard/settings show attempts, retry delay and last successful connection.
- **Persistent administrator event journal:** real incoming/normalized/queued messages, delivery attempts/results, connection events and administrator actions. Filters, search, dates, details, cursor pagination and ten-second refresh on the visible page. Only allowlisted metadata; no message text or credentials.
- **Bounded journal:** defaults of 90 days / 100,000 records / 128 MiB file budget, isolated SQLite, producer queue capped at 1024, off-loop batching, incremental cleanup and repetitive-failure aggregation. Overload can drop diagnostic events; counters are visible. This is a conservative SQLite-file budget, not a filesystem quota or lossless audit ledger.
- **Delivery validation and diagnostics:** reject empty payloads before sending, stop retrying identical permanent Telegram 400/401/403 requests and drain Xray stderr asynchronously using fixed safe categories. Existing failed deliveries are not replayed automatically.

Preserve v0.1.1 photo albums, ID aliases, durable checkpoints, replies/topics, ACLs, Saved Messages (`chat_id=0`) and encrypted sessions. Telegram has no send idempotency key: remote acceptance followed by a lost response can still duplicate a send on retry. Historical media errors are not all attributed to one cause.

## Validation

- Backend **1002 passed, 9 skipped** locally; frontend **51 passed**, TypeScript/Vite build PASS.
- Playwright **26 passed**, desktop 1440×900 and mobile 375×812, real backend/SPA with synthetic external boundaries.
- Debian 13 amd64: **38 recovery/journal/queue/TG regressions**, **5 real-Xray checks**; signed clean installation, v0.1.1 upgrade, forced cold rollback and journal/administrator persistence after an actual Docker restart passed.
- Local skips: systemd unavailable on macOS (1), opt-in SPA smoke (3), Xray binary unavailable locally (5). Equivalent checks ran separately in Debian/Playwright.
- 100,000 synthetic events: 35.48 MiB before cleanup; 1000 records / 0.36 MiB after a test retention reduction; median indexed query 0.43 ms. One local pipeline benchmark measured 249.9 → 225.6 msg/s (−9.74% with the journal), with no drops. These are synthetic measurements, not live messenger capacity guarantees.

## Upgrade

```bash
sudo shmax update
# or a verified local production archive:
sudo shmax update /path/to/shmax-v0.1.2.tar.gz
```

Verify the signature and free disk space; retain backups away from a failing disk. The updater builds before downtime, creates and verifies a cold persistent-data backup, then checks application and configured-integration readiness. Failed readiness restores the previous image and cold data. A normal v0.1.1 upgrade retains configuration and authentication; a genuinely revoked external session still requires login. A configuration-only backup does not replace a complete snapshot.

[Journal retention/privacy](../../docs/en/event-journal.md) · [Updates](../../docs/en/updates.md) · [Changelog](../../CHANGELOG.md).
