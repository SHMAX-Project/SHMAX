# Security model

No Docker socket in the web container; host actions only via
shmax-hostd's fixed op set (no shell strings). Fail-closed ACL,
topic routing, network fallbacks. Tokens, API Hash and proxy
passwords use SecretStore (AES-256-GCM); API returns masks. MAX sessions are
separate 0600 files. The DB contains user identifiers and delivery data; do not
publish DB files, sessions, keys or unredacted logs.
Mechanisms: Argon2id admin password, signed sessions + revocation
table, CSRF, rate limiting, security headers, 0600 backups,
update SHA256/origin verification, Xray SOCKS loopback-only.
Full secret table and threat notes: [../ru/security.md](../ru/security.md).


A normal local `.shmax` includes the master key and is not encrypted as a whole.
Protect it as a secret or create `.shmax.enc` via CLI/API. See [backups](backups.md).

## Member synchronization

Optional MTProto **bot** sync discovers silent members; `chat_member` updates membership. New users stay unapproved (`read_only`); Telegram admin grants no SHMAX admin. API Hash is encrypted in SecretStore; the session exists only in memory. The bridge works without sync configuration. See [Telegram group](telegram-group.md#automatic-participant-discovery) for network, backup and Privacy Mode limitations.

### User signature mutation

The existing nickname PATCH requires an authenticated administrative session and CSRF. Server validation trims whitespace, limits values to 64 Unicode characters and rejects newlines, controls and invisible formatting controls while preserving emoji joiners. Empty values clear the override. Mutation leaves ACL, membership and Telegram-admin state unchanged; synchronization never overwrites the signature.

## Portable backups

Portable v2 encrypts the entire file and manifest with Argon2id/AES-GCM.
Export requires admin/owner; import/restore requires owner, CSRF and rate limits.
Hostd accepts only fixed operations and a job ID; web never receives Docker access.
A shared host lock prevents conflicting mutations; a durable journal precedes
pause/stop/swap and supports startup recovery. Hostd has CAP_CHOWN,
CAP_DAC_OVERRIDE and CAP_FOWNER for data access/ownership; web has no capabilities.
The retained previous/ safety copy is unencrypted and sensitive.
See [limits/format](backups.md) and [owner policy](restore.md).
