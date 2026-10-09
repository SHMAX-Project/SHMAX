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
