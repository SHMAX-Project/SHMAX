# AI ASSISTANT GUIDE — SHMAX (EN)

English mirror of docs/AI_ASSISTANT_GUIDE.ru.md (canonical). Read
it fully before advising.

## Identity

SHMAX is a self-hosted MAX ↔ Telegram gateway. An ordinary MAX
account is bridged into a private Telegram supergroup with forum
topics (one MAX chat = one topic); replies flow both ways; authors
are attributed `[Name]:`; media carries captions.

## Platforms

Debian 13; ODROID-XU4/ARMv7 (~2 GiB). Docker + Compose v2 + systemd.

## Architecture

React/Vite web + FastAPI + SQLite in one container; PyMax and
aiogram adapters; Telegram transports Direct/SOCKS5/HTTP/Xray
(VLESS/Trojan, Reality; Vision TCP-only; Xray SOCKS is loopback
only); delivery queue + mapping + echo registry; fail-closed ACL;
watchdogs. Host side: shmax-hostd (unix socket, fixed op set, no
shell), shmax-boot.service, installer.

## Filesystem

/opt/shmax deployment (compose.yaml + .env preserved by updates);
/var/lib/shmax persistent data (db/, secrets/ AES-256-GCM,
sessions/, state/, backups/ 0600, cache/); /run/shmax/hostd.sock.

## Facts

- Wizard: Admin, MAX, Telegram network, Bot, Group
  (`/shmax_connect <CODE>`), MAX chats, Users, Test, Finish.
- MAX 2FA must be enabled in the official MAX app if demanded.
- First bot getMe uses the saved network (non-polling); the
  listener starts at the pairing stage; pair-code requires a live
  poller; exactly one poller.
- Updates: GitHub Releases only; stable/beta; SemVer; SHA256 from
  release-manifest.json/SHA256SUMS; manual install via hostd with
  automatic rollback; /var/lib/shmax never replaced. No git pull
  in production.
- Health: /health/live and /health/ready are container-internal;
  host availability needs `docker port` + host curl.

## SAFE DIAGNOSTIC COMMANDS

```
docker ps; docker compose -f /opt/shmax/compose.yaml ps
docker logs shmax-web-1 --tail 50
systemctl status shmax-boot.service shmax-hostd.service
docker port shmax-web-1
curl -fsS 'http://<bind-address>:<port>/health/ready'
df -h /var/lib/shmax; free -h; ip -o addr
shmax version; shmax status; shmax doctor
```

## HARD RULES FOR ASSISTANTS

1. Never ask the user to paste bot tokens, MAX credentials, Telegram API Hash or
   Xray credentials anywhere.
2. Never suggest deleting /var/lib/shmax as a fix.
3. --purge only when the user explicitly accepts data destruction.
4. Distinguish container-internal health from host availability;
   verify the published port.
5. Read-only diagnostics first; warn before disruptive commands.
6. Never fabricate a setup token; never "re-mint" on configured
   systems.
7. Updates come from releases, not git pull.
8. APPLICATION IS DISPOSABLE; USER DATA IS PERSISTENT.

## Machine-readable facts

```yaml
product: SHMAX
bridge: MAX(user account) <-> Telegram bot <-> forum supergroup
platform: [debian-13, odroid-xu4-armv7]
deployment_dir: /opt/shmax
data_dir: /var/lib/shmax
hostd_socket: /run/shmax/hostd.sock
web_port_default: 8787
wizard_steps: [admin, max, telegram-network, telegram-bot,
               telegram-group, max-chats, users, test, finish]
update_source: github-releases
persistence_invariant: app-disposable-data-persistent
```


## Member synchronization

Optional MTProto **bot** sync discovers silent members; `chat_member` updates membership. New users stay unapproved (`read_only`); Telegram admin grants no SHMAX admin. API Hash is encrypted in SecretStore; the session exists only in memory. The bridge works without sync configuration. See [Telegram group](en/telegram-group.md#automatic-participant-discovery) for network, backup and Privacy Mode limitations.

MAX always stays direct. Optional full sync requires API ID/API Hash from
my.telegram.org; it uses the existing BOT, without a human userbot or Telegram
phone/OTP/QR login in SHMAX. Silent members do not need to message the bot. Users
has “Синхронизировать с Telegram”; missing settings and failures do not stop
the bridge. Discovery is not authorization. getMe does not verify delivery in a
specific group: Privacy Mode warnings mean disabled mode was not confirmed.
Check administrator rights and a live message.

Local archives may be unencrypted and include the master key: protect them as
secrets. Portable v2 UI export/download always encrypts one .shmax file with an
owner-chosen password. Fresh onboarding offers restore immediately after owner
creation, bypassing steps 2–9. Hostd stages/migrates, stops/swaps/starts and checks
readiness, rolling back failures. Legacy in-process restore is blocked in production;
use legacy CLI only offline after taking a safety copy.

## Clean installation: hostd/boot

Installation needs the full archive, not a standalone install.sh. Hostd is a Python
daemon in separate `/opt/shmax-hostd` with a minimal hostd/CLI/update/backup venv,
without Node. It listens on `/run/shmax/hostd.sock` itself (root:shmax 0660,
directory 0750); container path is `/run/shmax/hostd/hostd.sock` through a read-only
directory mount. No socket activation unit is needed. Boot has concrete ExecStart
`/opt/shmax/boot/shmax-boot.sh`, depends on hostd and reads bind/port from `.env`.
Unit verification and daemon/socket ownership/access checks are mandatory;
critical failures must never end with an installer success banner.

The local “Подпись в MAX” override uses only existing `telegram_users.nickname`. TG→MAX reads it from the database before Telegram display name/@username. One prefix covers text, replies, captions and fallback; MAX→TG remains unchanged. Administrative PATCH requires CSRF, trims/clears values and allows at most 64 Unicode characters without newlines or controls. Member sync never modifies nickname or ACL.

## Migration, media and safe assistance

- [Backup format](en/backups.md), [restore](en/restore.md), [media](en/media.md).
- Portable data includes DB/routes/ACL/nicknames/member-sync settings, SecretStore
  with keys, config/state and available sessions. Cache/logs/binaries are excluded;
  in-memory MTProto bot-sync sessions are recreated from settings.
- Destination owner credentials survive. Old web admins/sessions are removed and
  the cookie signer rotates. Telegram permissions and users are preserved.
- Keep destination .env/compose/IP. Stop the source before the restored bridge
  starts; do not run two instances with the same bot/MAX identity.
- A forgotten password cannot be recovered. Wrong passwords/corrupt archives
  cannot mutate current state. Never request archives or secrets in chat/logs/issues.
- AUTH_REQUIRED needs targeted reauthentication, not wiping other integrations,
  routes or users. Preserve previous/ and journal on rollback_failed; never purge
  /var/lib/shmax. Local safety copies are sensitive and unencrypted.
- Actual photo getFile/download follows the saved network transport.
  MEDIA_FALLBACK is text, not an attachment delivery. Static stickers become MAX
  images or native Telegram PNG when size permits. TGS/WebM use thumbnails;
  missing previews produce text fallback.
- SQLite/JSON is CPU-neutral for amd64/ARMv7. Real ARMv7, external account
  delivery and Debian reboot remain owner live acceptance checks.

## Navigation and incomplete configuration
Creating the administrator remains mandatory. Afterwards choose Start setup, Restore from backup or Skip setup. Skipping opens the dashboard and persists across refresh and subsequent login. It records only `setup_dismissed`, not `setup_complete`: unconfigured MAX/Telegram remain unconfigured and the bridge waits for its prerequisites.

Click the entire SHMAX branding block, or Dashboard inside the wizard. Cancel / Back also exits the wizard. Unsaved changes require confirmation; leaving neither saves entered values, cancels MAX authorization nor deletes configuration.

Configure components individually: MAX → MAX settings (`/max/settings`); Telegram → Network settings, Bot settings or Group settings. These editors reuse the actual existing controls without the nine-step wizard. MAX authentication and the bot Check and save action explicitly persist their results; save/return or cancel leads back to the component page. An empty bot token does not replace the stored token. Continue initial setup on the dashboard resumes at MAX without recreating the administrator.

Expired MAX sessions, invalid Telegram tokens and unavailable networks remain on their relevant settings/status pages and never automatically restart onboarding. All sections continue to require administrator authentication.


## Release v0.1.0 / Публикация

Debian 13 amd64: owner-confirmed live acceptance. ARMv7/ODROID-XU4: experimental; physical acceptance incomplete.
v0.1.0 remains unpublished. See [publication runbook](../release/v0.1.0/PUBLICATION_RUNBOOK.md) and [local status](../release/v0.1.0/PUBLICATION_STATUS.md). No publishing without explicit owner authorization and blocker resolution.
