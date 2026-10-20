# Diagnostics and health

`/health/live` and `/health/ready` (used by the installer, boot
orchestration and the updater) are CONTAINER-INTERNAL checks; the
boot orchestration additionally verifies the published host port
and host-side readiness. The Diagnostics page shows MAX/Telegram
health loaded when the page opens; refresh the page for a new check.
Telegram connection checks are available in its settings. Dedicated MAX/Telegram recovery managers own reconnection; watchdog snapshots remain in `state/watchdogs.json`. RECONNECTING / VERIFICATION_UNAVAILABLE differ from AUTH_REQUIRED: an unavailable check is not proof of revoked authentication. See [MAX](max.md) and the [event journal](event-journal.md). CLI: `shmax doctor`.
