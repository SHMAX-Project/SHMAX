# Telegram bot

1. Open @BotFather, send `/newbot` and choose a name.
2. The username must end with `bot`.
3. Paste the token into SHMAX. It is encrypted at rest; the API returns a mask.
4. In BotFather, use `/setprivacy`, choose the bot → **Disable Privacy Mode**
   for ordinary group messages. Also check administrator permissions in the
   [forum group](telegram-group.md).
5. Save and verify the bot through the selected Telegram network.

`getMe` confirms API reachability and bot identity, not delivery in a specific
group. If returned, `can_read_all_group_messages` describes disabled Privacy Mode.
SHMAX warns when `getMe` does not confirm this. That warning does not prove the
bot cannot receive messages: check administrator rights, BotFather settings and
a live message in General.

Token verification does not start the continuous listener. It starts when requesting
a pairing code; a code is issued only with a live listener. Repeated requests do
not create another update polling loop.

[Member synchronization](telegram-group.md#automatic-participant-discovery) is
optional, requires separate API ID/API Hash and uses the same bot. It needs no
human Telegram account and grants users no access.
