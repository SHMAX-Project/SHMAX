# Event journal and technical logs

Since v0.1.2 the **Logs / Журнал** page is a persistent application event journal, not a file tail. It shows incoming messages, queue activity, delivery attempts/results, MAX/Telegram/Xray states, startup and administrator actions. Use filters, search, dates, details and refresh. See [retention and privacy](event-journal.md).

The administrator-only SQLite journal lives at `/var/lib/shmax/state/events.db` and survives restarts/updates. Defaults are 90 days, 100,000 records and a conservative 128 MiB file budget. Cleanup is incremental; overload may drop diagnostic events, with counters visible in the UI. Message text, recipients and credentials are excluded.

For deeper diagnosis use `shmax logs --tail 100`, `docker logs shmax-web-1 --tail 100`, or `/var/lib/shmax/logs/shmax.log` when rotated file logging is enabled. Redact identifiers and secrets before sharing technical logs. Never share session files, SecretStore or backups. Rotate any exposed credential and report through Security Advisories.
