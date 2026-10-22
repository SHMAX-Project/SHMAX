# Telegram group / forum / topics

1. Create a group and add your bot.
2. Enable Topics (Forum/Supergroup).
3. Promote the bot to administrator with send-message and manage-topic rights.
4. In SHMAX, request a one-time pairing code.
5. Send `/shmax_connect <CODE>` in **General**.
6. SHMAX discovers and validates the group; no manual chat_id is needed.

If pairing fails, check Topics, bot permissions, Telegram connectivity, the live
listener and code expiry. Set `/setprivacy` → **Disable Privacy Mode** in
BotFather. An unseen command alone does not prove that Privacy Mode is enabled.
See [Telegram bot](telegram-bot.md).

`🔧 SHMAX` is a system topic for service notifications; it is excluded from bridging.

## Automatic participant discovery

Full synchronization is **optional** and can import existing silent members:
they do not need to message the bot to appear on Users.

1. Obtain Telegram **API ID and API Hash** from [my.telegram.org](https://my.telegram.org).
2. Open **Telegram**, “Синхронизация участников · необязательно”.
3. Enter both values and click “Сохранить настройки синхронизации”.
   API Hash is a secret: never send it to support, logs or chat.
4. On **Users** (also the wizard Users step), click
   **“Синхронизировать с Telegram”**. Check the result, attempt time and
   found/new/updated counts. An incomplete import is not reported as success.

SHMAX uses the **existing bot**, not a human Telegram account or userbot.
SHMAX requires no Telegram phone, OTP or QR login. The bot needs access to the
paired forum and appropriate administrator permissions; Telegram restrictions
may prevent a full member listing. Other bots are excluded.

Sync runs in the background after pairing and when a configured system restarts;
there is no frequent list polling. Future membership changes are tracked through
`chat_member`. Visible messages remain a discovery fallback. Without API ID/API Hash,
full enumeration is unavailable; this is normal and the bridge keeps working.
Sync failures also do not stop the bridge.

**Discovery is not authorization.** New users are unapproved, unblocked and
`read_only`. Telegram administrator status grants no SHMAX administrator role.
Existing approval, blocking, role and local nickname are preserved; the SHMAX
administrator controls access on [Users](users-acl.md).

API Hash is encrypted in SecretStore and never returned after saving. Sync sessions
exist only in memory; there are no extra session files to back up. Users and sync
settings are included in normal backups: [archive contents and protection](backups.md).
See [Telegram network](telegram-network.md) for connectivity limitations and
[architecture](architecture.md) for technical limits.

### Local signature and synchronization

“Подпись в MAX” (`nickname`) is configured on the SHMAX user card. Sync refreshes Telegram name, username, membership and Telegram-admin status while preserving the local signature and ACL. After a Telegram profile rename, “Анна” still produces `[Анна]: Клиенту уже ответили.` The Telegram profile is unchanged. See [Users and ACL](users-acl.md#signature-in-max).

## v0.2.0

Routes, source/sender labels and send grants are configured per Telegram group. A MAX chat can have multiple destinations; administrator overview groups create topics automatically. Durable jobs and message mappings are destination-specific; albums retain their members and topic in each group. MAX networking is independent of Telegram. See [groups and access](multi-group.md), [MAX networking](max-network.md) and [media](media.md).
