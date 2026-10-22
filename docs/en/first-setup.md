# Initial setup in v0.2.0 — 10 steps

Create the owner, then choose Start setup, Restore from backup or Skip setup.
An in-place upgrade preserves existing configuration and does not require
running onboarding again.

1. **Administrator:** create the local SHMAX owner from the one-time setup link; save the password.
2. **MAX network:** choose direct, proxy or Xray; save and check the connection before entering a phone number.
3. **MAX account:** enter the phone and code, then 2FA password when requested. Do not repeatedly request codes. Enable 2FA in the official MAX app first if required.
4. **Telegram network:** independently choose Direct, SOCKS5, HTTP or Xray; apply the selected node and test it.
5. **Telegram bot:** use @BotFather `/newbot`, save and verify the token; check `/setprivacy → Disable`. An empty field does not overwrite a saved token.
6. **Telegram group:** create a private forum group, enable Topics, add the bot with send/manage-topic rights. Obtain a code and send `/shmax_connect CODE` in General; wait for confirmation.
7. **MAX chats:** create a topic mapping for each selected chat. Configure additional groups, existing topics and admin overview later in group settings.
8. **Users:** approve trusted senders for the first group. Discovery and Telegram admin status do not grant SHMAX permission. Optional MTProto bot member discovery can be configured later.
9. **Test:** run the checks; fix the component named by a failed check and repeat. `getMe` alone does not prove that ordinary group messages are received.
10. **Finish:** open the dashboard and verify incoming text/media and a Telegram → MAX reply.

Back returns to the preceding step. Home branding exits the wizard; unsaved
changes require confirmation. Leaving does not delete configuration or cancel
ongoing MAX authentication. Skip records dismissal rather than completed setup;
resume from the dashboard without creating a second owner.

Configure MAX account/network and Telegram bot/network/groups independently
after setup. A network failure or expired session does not reset other settings.
A backup can be restored immediately after owner creation. Stop the old instance
before transferring a shared MAX account/Telegram bot. See [restore](restore.md).
