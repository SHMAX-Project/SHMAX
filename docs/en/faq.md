# FAQ

**MAX bot instead of an account?** No, V1 uses an ordinary MAX
account. **Where is my data?** /var/lib/shmax; it survives
updates and container recreation; backups protect against deletion or disk failure. **Internet needed at boot?** No for the UI and boot orchestration;
message delivery needs MAX/Telegram connectivity, and updates need GitHub.
**Custom IP/port?** Edit compose.yaml + .env in /opt/shmax.
**Automatic updates?** Checks yes, installs manual only.


## Member synchronization

To import existing silent members, save optional API ID/API Hash from
my.telegram.org on Telegram. API Hash is secret. SHMAX uses the existing bot,
without a human userbot. On Users click “Синхронизировать с Telegram” and
check the result. Members need not message the bot when full sync is configured.
Future membership changes use chat_member; visible messages remain a fallback.
Missing configuration and sync failures do not stop the bridge.

New members are unapproved and read_only; Telegram admin grants no SHMAX admin.
Existing ACL and nickname are preserved. See
[Telegram group](telegram-group.md#automatic-participant-discovery).
