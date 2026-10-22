# Persistent event journal (v0.1.2–v0.2.0)

The administrator Journal page uses `/api/journal`, authenticated server-side,
with 50-row cursor pagination (maximum 200), filters, bounded search, UTC storage
and local/UTC/Moscow display. Date input uses the browser's local timezone.
The visible first page refreshes every ten seconds; unmount cancels pending fetches.

`/data/state/events.db` is isolated from delivery/authentication data. A fixed event
catalog and metadata vocabulary reject arbitrary text, credentials, identifiers,
URLs and tracebacks. Correlation IDs are hashed. Delivery success follows remote
acknowledgement, mapping persistence and durable queue acknowledgement.

Defaults: 90 days, 100,000 records, 128 MiB SQLite-file budget. DELETE journaling
avoids WAL growth. A page-count cap reserves roughly half the budget minus a 1 MiB reserve (minimum 64 KiB with smaller budgets)
for the main database and the remainder for a rollback journal; indexes are included.
Usage counts the greater of logical/allocated sizes of the database and its known
sidecars. This is conservative SQLite-file accounting, not a filesystem quota or
an exact limit on filesystem metadata or externally modified files.

Cleanup deletes at most 1,000 oldest eligible records per transaction and reclaims
up to 256 pages incrementally. Idle cleanup runs once per minute; age cleanup is
eventually applied in bounded steps. No full VACUUM runs on the delivery path.
A 1,024-event bounded queue feeds batches of at most 128 on a worker thread. Under
overload or disk failure diagnostic events may be dropped; visible counters disclose
loss. Message delivery does not wait for journal I/O. This is a diagnostic journal,
not a lossless compliance ledger. Repetitive network failures aggregate by episode;
individual message successes remain independently correlated.

The existing state volume persists across updates/restarts. Standard backups use
SQLite snapshotting for the journal; portable snapshots already handle SQLite.
Retention never prunes application databases, sessions, media, keys or backups.

## Processing failure before enqueue — v0.1.3

`message.processing_failed` is an ERROR with direction and correlation ID. An incoming event failed processing; a delivery queue row may not exist yet. Exception text and message contents are excluded. No separate Telegram notification is sent.
