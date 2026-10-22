# SHMAX v0.2.0

- Multiple Telegram groups with separate MAX chat bindings and send permissions.
- Admin forum groups automatically create topics for new MAX chats and show both directions of conversation.
- Configurable source chat and actual sender labels.
- Independent MAX networking: direct, HTTP, SOCKS5 or Xray, separate from Telegram.
- Dashboard CPU, memory and disk graphs, group bindings and network state.
- Ten-step initial setup verified on desktop and mobile.
- Preview and import of plaintext and encrypted v0.1.0–v0.1.3 application backups.

The separate backup helper receives the application version and migration files during upgrades; prior files are retained for rollback.

## Upgrade and limitations

Migration preserves groups, routes, signatures, saved sessions and queued records. Previous grants apply only to the previous active group. Legacy MAX queue keys become destination-aware to prevent replay. Old unfinished Telegram jobs without a sender ID require manual review because their permissions cannot be reliably reconstructed. Cross-server backup restore quarantines unfinished source queue records to prevent duplicate delivery.

Take a cold backup on the original server before upgrading. Restore a verified pre-upgrade backup to return to the old schema; reverse migration would lose group data and is refused. Updater cold snapshots without an application manifest remain a separate offline recovery mechanism.

One MAX account is supported. New Telegram groups must support forum topics. Telegram membership determines read visibility. Metric history stays in memory for up to one hour and resets on restart. Network paths were tested without sending real test messages. The existing Debian deployment recovered MAX and Telegram after the signed upgrade and full reboot.

## Validation and security

The signed upgrade, schema migration, host helper, preserved settings/queues, component restart and full VM reboot were verified on an existing Debian 13 amd64 deployment. Album/media/fanout regressions, desktop/mobile setup and layout, and imports from all four previous release backup writers passed. [Detailed evidence and limits](VALIDATION.md).

Use the signed archives below; keep `SHA256SUMS` and `SHA256SUMS.sig` together and verify with the unchanged trusted public key. Private keys and server backups are excluded. Existing v0.1.0–v0.1.3 releases remain unchanged.
