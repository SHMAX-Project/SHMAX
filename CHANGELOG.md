# Changelog

All notable changes to SHMAX are documented here.
Format: Keep a Changelog; versioning: SemVer.

## 0.1.3 — 2026-10-20

### Fixed
- MAX albums without text/caption and without a sender display name no longer raise AttributeError before durable enqueue.
- Incoming-message processing failures now emit a sanitized ERROR journal event with direction and correlation ID instead of remaining only in technical logs.
- Retain UTF-16 caption limits, long-text delivery, photo order, topic routing, checkpoints and native ID aliases.

### Validation and limits
- Added real SQLite queue and album-delivery regressions for three photos, missing attribution and caption length boundaries; added journal error/privacy regression.
- No new database migration or session-format change. Pre-enqueue failures from old versions are not recovered automatically; journal errors do not create separate Telegram notifications.

## 0.1.2 — 2026-10-20

- Fix Xray switching, recoverable rollback and Telegram adapter installation.
- Recover MAX saved-session connections with one generation-aware listener.
- Distinguish temporary verification failures from revoked authentication.
- Add a private persistent Web UI event journal with bounded retention and aggregation.
- Drain safe Xray diagnostics; reject empty payloads and permanent Telegram request failures.
- Preserve v0.1.1 photo albums, native ID aliases, checkpoints and persistent sessions.

### Validation
- Backend: 1002 passed, 9 skipped locally; frontend: 51 passed; Playwright: 26 passed.
- Debian 13 amd64: 38 recovery/journal/queue regressions and 5 real-Xray checks passed.
- Signed clean installation, v0.1.1 upgrade, forced cold rollback and journal persistence after a real container restart passed.
- Journal stress: 100,000 synthetic events, 35.48 MiB before cleanup, indexed median query 0.43 ms.

### Limits
- Journal defaults: 90 days, 100,000 records, conservative 128 MiB SQLite-file budget; incremental cleanup and visible diagnostic-drop counters. It is not a filesystem quota or lossless audit ledger.
- Telegram has no send idempotency key; a lost response after remote acceptance can still produce a duplicate on retry. Existing failed deliveries are not replayed automatically.


## 0.1.1 — 2026-10-12

### Fixed
- Multiple photos from one MAX message now reach Telegram instead of retaining
  only the first photo and marking the rest as unsupported attachments.
- Native Telegram albums contain 2–10 consecutive photos, preserving captions,
  order, authorship, forum topics and replies. Single photos retain their existing path.
- Durable queue checkpoints preserve confirmed progress across retries and restarts.
  Every Telegram album message ID resolves to the canonical MAX message for replies.
- Bounded download retries and expired media URL refresh through PyMax preserve
  delivery of remaining valid attachments while recording permanent partial failures.
- Update compatibility from 0.1.0 to 0.1.1 preserves existing configuration and data.

### Validation
- Backend: 985 passed, 4 skipped; frontend: 46 passed; typecheck passed.
- 24 album regression cases use actual PyMax 2.4.1 models.
- Isolated Debian 13 amd64 upgrade and rollback checks passed, including persisted
  settings, routes, queue/history, encrypted secrets and synthetic session fixtures.

### Delivery semantics
- Telegram provides no idempotency key: remote acceptance followed by a lost response
  or a crash before a durable checkpoint can still duplicate a send on retry.

## 0.1.0 — published 2026-10-10

### Fixed
- Production packaging: explicit directory modes and non-root code normalization prevent import failure after private-umask extraction. Local release update adds verified cold backup, integration acceptance, protected diagnostics before rollback and exact-image rollback.
- Telegram automatically recovers saved transport after delayed SOCKS/Xray startup,
  transient getMe failure and polling termination, with serialized single-consumer
  replacement, bounded cancellable backoff and sanitized permanent-error cooldown.
- Explicit Telegram proxy modes never fall back to direct; AUTO skips unconfigured
  proxies. Xray liveness and Telegram reachability are reported separately.

### Release preparation
- Release metadata 0.1.0; real demo screenshots; source/production archives,
  immutable-source packaging and read-only verification workflows.
- Publication blocked until history-secret and mascot-rights owner decisions.

### Added
- Clickable home branding, wizard exit controls and authenticated setup dismissal
  separate from completion; incomplete dashboard with individual configuration links.
- Password-encrypted one-file portable v2 export/download and fresh-owner restore
  onboarding, bounded authenticated extraction, forward migration, hostd staging,
  readiness/rollback and interrupted-operation recovery. Destination owner survives;
  old web sessions/admins are revoked and unfinished deliveries quarantined.
- Bidirectional static sticker image/native Telegram delivery; animated/video
  previews with bounded decoding and sanitized media outcome diagnostics.

### Fixed
- MAX and Telegram settings open focused editors; disconnected/incomplete systems
  remain accessible to authenticated administrators without forced onboarding.
  Unsaved configuration drafts are guarded on navigation and reload.
- Per-client PyMax 2.4.1 photo upload compatibility for missing photoIds; document/file
  reference aliases and original filenames; Saved Messages chat_id=0 regressions.
  Permanent and uncertain media failures stop automatic retries; manual retry
  resets attempts while retaining queue identity.
- Real aiogram PhotoSize lists now retain the largest photo file_id for getFile;
  downloaded files receive a validated extension required by PyMax uploads.
- Telegram attachment downloads use the configured bot session/transport without
  a silent direct-network fallback; production legacy live in-process restore is blocked.

- Optional Telegram member sync using the existing bot identity and API ID/API Hash;
  imports silent members, tracks chat_member events, keeps message discovery as a
  fallback. New users stay unapproved; Telegram admin grants no SHMAX admin.
  Sessions are memory-only; sync failures do not stop the bridge.

### Changed
- Bot verification warns when getMe does not confirm disabled Privacy Mode;
  it does not claim to prove delivery in a particular group.
- RU/EN instructions and Russian in-product help aligned with member sync,
  backup contents and the controls actually available in the UI.
- Version source is the single `backend/shmax/VERSION` file
  (`0.1.0-dev` during development; release builds inject the tag).

## 0.1.0 (initial public release, prepared)

### Added
- MAX (ordinary user account) ↔ Telegram bridge with one MAX chat
  ↔ one Telegram forum topic mapping, replies both ways,
  media/caption/attribution, controlled humanized fallback.
- Telegram transports: Direct, SOCKS5, HTTP proxy, Xray
  subscription (VLESS/Trojan incl. Reality; local Xray managed by
  SHMAX, SOCKS bound to loopback only).
- 9-step setup wizard with built-in help cards (Admin, MAX,
  Telegram Network, Bot, Group, MAX Chats, Users, Test, Finish).
- Telegram users ACL: approval, roles, blocking, nicknames.
- Diagnostics/watchdogs for MAX and Telegram, logs viewer.
- Backups: DB + encrypted SecretStore + state, 0600 archives,
  CSRF-protected API, traversal-safe API restore with safety backup. Normal
  archives include the master key; whole-archive encryption is optional.
- Updates: GitHub Releases discovery (stable/beta, SemVer,
  manifest/SHA256 verification, SSRF-hardened), 24h auto-check,
  host-side transactional update with automatic rollback via
  shmax-hostd.
- Boot orchestration: systemd unit waits for the configured bind
  address, repairs broken host-port publication, verifies
  host-visible readiness.
- Installer (install/repair/upgrade/uninstall, Debian 13,
  ODROID-XU4/ARMv7 aware); upgrade never requires a setup token.
- Russian in-app help and Russian/English repository documentation; operations support guide
  (docs/AI_ASSISTANT_GUIDE.ru.md).

### Limitations (V1)
- MAX ordinary-user accounts only (no bot API on MAX side).
- Update install requires shmax-hostd on the host; without it the
  UI reports unavailability instead of pretending.
- Single appliance instance; no clustering.
