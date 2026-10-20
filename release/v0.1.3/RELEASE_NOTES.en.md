# SHMAX v0.1.3 — captionless album fix

## Fixes

- MAX albums without text or caption no longer fail before enqueue when the sender display name is unavailable. An absent caption is handled as an empty string. Photo order, routing and the Telegram topic are retained.
- Caption limits use UTF-16 units; long text remains available for separate delivery. Durable queue checkpoints and native ID aliases are preserved.
- Incoming-message processing exceptions create an ERROR `message.processing_failed` journal event with direction, correlation ID and a safe error class. Message bodies and exception text are excluded.

- Portable backup snapshots exclude transient SQLite rollback journals (`-journal`) alongside `-wal`/`-shm`, avoiding a race when SQLite removes these files.

The album defect existed in v0.1.1 and v0.1.2. This patch retains the other v0.1.2 improvements. No database migration or session-format change is required.

## Limits

A journal error does not create a separate Telegram notification. Messages that failed before enqueue in older versions are not recovered automatically. Historical deliveries are not replayed. A lost response after Telegram accepts a send may still lead to a duplicate.

## Validation

Regressions cover three photos without a sender display name: no caption, a caption and the 1024 UTF-16-unit boundary, through the actual SQLite queue and a test transport. A separate test checks visible processing errors without private contents. Full results ship in the release `validation.json`. Production checks do not send test messages to operational chats.

## Update

```bash
sudo shmax update /path/to/shmax-v0.1.3.tar.gz
```

Use the signed production archive. The standard updater creates a local root-only cold backup before replacing the container, verifies readiness and rolls back on failure. Backups and private keys are excluded from the release.
