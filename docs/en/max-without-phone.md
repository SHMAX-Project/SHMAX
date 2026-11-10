# MAX on your server, conversations in Telegram

Prefer not to install another messenger on your personal phone, while contacts
or work chats already use MAX? SHMAX moves the MAX connection to **your server
or VPS** and everyday conversations to Telegram topics.

Participants only need Telegram on their phones. The official MAX mobile app
does not take part in normal SHMAX message forwarding: the server connects
through its adapter and stores the authorized account session.

[Install](installation.md) · [Connect an account](max.md) · [Русский](../ru/max-without-phone.md)

## Personal use

1. Prepare a Debian 13 amd64 server or a suitable VPS.
2. Install SHMAX and sign in to your MAX account through the web dashboard.
3. Connect your Telegram bot to a private group with topics enabled.
4. Choose MAX chats and approve your Telegram user to send replies.
5. Receive and reply from Telegram on your phone or computer.

This separates selected conversations from your main phone without keeping
the MAX mobile app installed just to read chats. The server must stay running
and reach both providers. SHMAX does not need a separate device continuously
running the official MAX app to forward messages.

## Teams and corporate use

An administrator connects a **work MAX account** on a company server or VPS.
Route conversations to separate private Telegram groups and assign MAX send
permissions per group. Staff reply from Telegram; every employee does not
need to install MAX on their personal phone.

The server manages the MAX session. Staff do not receive the account's SMS
codes, password or session file. The administrator controls access,
connectivity, backups and updates. One SHMAX instance serves one MAX account
and can serve several Telegram working groups.

Choose destinations and members according to your organization's data-handling
rules. SHMAX does not make Telegram an automatically approved corporate
repository. [Teams and administrator overview](multi-group.md).

## What initial authentication still requires

The server replaces the continuously running mobile client for messaging.
It **does not eliminate the MAX account or its sign-in requirements**.

- You need access to the account's phone number, verification code and 2FA
  password when requested. Enter them only into your own protected SHMAX dashboard.
- MAX's current official help describes registration through
  [an app or the web version](https://help.max.ru/help/about/ustanovka_i_registraciya_v_max/kak_zaregistrirovatsya_v_max).
  SHMAX connects an account; registration of a new account by SHMAX itself is not promised.
- When an account already has an active session, MAX may require setting up
  a sign-in password on an authorized device first.
  [Official sign-in conditions](https://help.max.ru/help/account/dostup_k_prophilyu/kak_voiti_v_prophil_max_esli_on_uzhe_est).
- [MAX's 2FA help](https://help.max.ru/help/account/zashshita_prophilya/kak_ustanovit_ili_otklyuchit_parol_dlya_vhoda_v_max)
  says this password cannot be enabled in the web version. The owner may need
  an official client to prepare the account. SHMAX does not bypass 2FA.
- If a session is revoked or the provider requires verification again, the
  owner signs in again. An ordinary temporary network outage does not itself
  require a fresh SMS code.

The accurate promise is **no MAX app on participants' phones for everyday
messaging**, not that an official client can never be needed under any circumstances.

## Privacy and scope

You select chats and manage the server. Messages still pass through MAX and
Telegram, and the SHMAX server can access the connected account. This is not
anonymization, cross-platform end-to-end encryption or a way to hide an account
from MAX.

Telegram membership controls reading; SHMAX grants control sending. Keep
personal groups private and separate teams where needed. Administrator
overview may include personal chats from the connected account.

This workflow covers supported messages and attachments. Calls, Digital ID,
Gosuslugi, payments and other app functions are not SHMAX capabilities.
[Media limits](media.md) · [Security](security.md).

Provider requirements were checked against official help on November 10, 2026
and can change. This describes an option for people who prefer not to install
MAX on a personal phone, not a claim about the opinions of everyone in Russia.
