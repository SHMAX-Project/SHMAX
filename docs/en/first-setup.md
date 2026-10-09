# Setup wizard — exactly 9 steps

After mandatory owner creation, choose Start setup for steps 2–9,
[Restore from backup](restore.md) to import an existing configuration,
or Skip setup to open the dashboard and configure components later.
Restore bypasses the remaining wizard and requires a fresh destination-owner login.


1 **Admin** — local SHMAX administrator (one-time setup link; save
the password). 2 **MAX** — phone attached to MAX; SMS may take
minutes (do not spam resend); if MAX demands 2FA, enable it in the
official MAX app first; the MAX 2FA password may be requested after
the SMS. 3 **Telegram network** — Direct / SOCKS5 / HTTP /
Xray subscription (URL → fetch → pick node → check → "Telegram
reachable"). 4 **Telegram bot** — @BotFather → /newbot → paste
token → /setprivacy → Disable Privacy Mode. 5 **Telegram group** —
create group, add bot, enable Topics, make bot admin (send
messages + manage topics), get one-time code, send
`/shmax_connect <CODE>` in General; the code is issued only when
the bot listener is confirmed alive. 6 **MAX chats** — each
selected chat becomes its own topic; routes editable later.
7 **Users** — approve trusted users only; others cannot write to
MAX. 8 **Test** — MAX session, network, bot, group permissions,
routes/topics, users, queue. 9 **Finish** — everything editable
later; data stays in /var/lib/shmax.

Wizard cards and the Help page share `frontend/src/help.tsx`; repository
documentation is maintained consistently with them.

MAX connects directly; the selected network applies only to Telegram. Users
offers optional [member synchronization](telegram-group.md#automatic-participant-discovery):
API ID/API Hash enable importing silent members with the existing bot, without
a userbot. Configure it on Telegram; this is not a tenth wizard step. Without
credentials the bridge works and discovery uses `chat_member` and visible messages.
New users are unapproved; Telegram admin grants no SHMAX admin role.

A Privacy Mode warning means `getMe` did not confirm disabled mode. Check rights
and live delivery instead of drawing conclusions from the warning alone. See [bot](telegram-bot.md).

## Leave, skip and edit configuration

Creating the administrator remains mandatory. Afterwards choose Start setup, Restore from backup or Skip setup. Skipping opens the dashboard and persists across refresh and subsequent login. It records only `setup_dismissed`, not `setup_complete`: unconfigured MAX/Telegram remain unconfigured and the bridge waits for its prerequisites.

Click the entire SHMAX branding block, or Dashboard inside the wizard. Cancel / Back also exits the wizard. Unsaved changes require confirmation; leaving neither saves entered values, cancels MAX authorization nor deletes configuration.

Configure components individually: MAX → MAX settings (`/max/settings`); Telegram → Network settings, Bot settings or Group settings. These editors reuse the actual existing controls without the nine-step wizard. MAX authentication and the bot Check and save action explicitly persist their results; save/return or cancel leads back to the component page. An empty bot token does not replace the stored token. Continue initial setup on the dashboard resumes at MAX without recreating the administrator.

Expired MAX sessions, invalid Telegram tokens and unavailable networks remain on their relevant settings/status pages and never automatically restart onboarding. All sections continue to require administrator authentication.
