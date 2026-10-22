# Troubleshooting by symptom

Canonical symptom table (RU): [../ru/troubleshooting.md](../ru/troubleshooting.md).
Quick reminders: UI unreachable → check `docker port shmax-web-1`
(internal health is NOT sufficient); force recreate via
`docker compose up -d --force-recreate` in /opt/shmax. MAX
unhealthy → re-login. Telegram through Xray broken → pick another
node. /shmax_connect ignored → code expiry, network, bot permissions, listener health
and /setprivacy → Disable. An unseen command does not prove enabled Privacy Mode;
getMe does not prove group delivery.
After a failed update, check whether automatic rollback succeeded; if rollback
also failed, follow the reported manual recovery instructions.
Never delete /var/lib/shmax as a "fix".


## Member synchronization

Optional MTProto **bot** sync discovers silent members; `chat_member` updates membership. New users stay unapproved (`read_only`); Telegram admin grants no SHMAX admin. API Hash is encrypted in SecretStore; the session exists only in memory. The bridge works without sync configuration. See [Telegram group](telegram-group.md#automatic-participant-discovery) for network, backup and Privacy Mode limitations.

For photos/stickers see [media support and limits](media.md). Static stickers
are delivered as images/native Telegram stickers where possible; animated/video
stickers use previews, or one text fallback when no preview is available.
For password/corruption/rollback_failed see [restore](restore.md); preserve the
safety copy and journal instead of deleting persistent data.

## Temporary MAX failures in v0.1.2

RECONNECTING and VERIFICATION_UNAVAILABLE mean recovery or an unavailable check, not proven session revocation. Check connectivity and the [event journal](event-journal.md), then allow bounded retries. Reauthenticate for AUTH_REQUIRED after a confirmed authentication failure. Do not delete session files to fix a network outage.

## Text arrived but a captionless album did not

In v0.1.1/v0.1.2 an album without caption and sender display name could raise AttributeError before enqueue, visible only in technical logs. Upgrade to v0.2.0 (the fix was introduced in v0.1.3). Such an old message will not retry automatically; arrange any resend separately. In v0.1.3 processing errors appear as `message.processing_failed` journal events.
