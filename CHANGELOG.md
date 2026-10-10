# Changelog

All notable changes to SHMAX are documented here.
Format: Keep a Changelog; versioning: SemVer.

## 0.1.0 — prepared locally; not published

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
- Russian in-app help and Russian/English repository documentation; AI-assistant support guide
  (docs/AI_ASSISTANT_GUIDE.ru.md).

### Limitations (V1)
- MAX ordinary-user accounts only (no bot API on MAX side).
- Update install requires shmax-hostd on the host; without it the
  UI reports unavailability instead of pretending.
- Single appliance instance; no clustering.
