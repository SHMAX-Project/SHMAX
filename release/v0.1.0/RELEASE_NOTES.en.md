# SHMAX v0.1.0 — first public release (prepared, unpublished)

SHMAX is an independent self-hosted MAX ↔ Telegram gateway, not an official
product of either platform. One ordinary MAX account connects to your Telegram
bot and private forum supergroup; each selected MAX chat maps to one forum topic.

- Automatic recovery of saved Telegram transport with a single polling consumer.
- Verified local production-archive updates with backup, health checks and rollback.
- Docker application and metadata permission normalization for non-root startup after extraction with umask 077.
- Bidirectional messages, reply mapping, user approval, roles and operator signatures.
  Telegram administrators do not automatically receive SHMAX administrative access.
- Optional member synchronization using the existing bot and Telegram API ID/Hash;
  chat_member events and ordinary messages remain discovery paths.
- Text, photos, documents and supported video. Static stickers become images;
  animated/video stickers use a preview or an understandable fallback.
  Saved Messages `chat_id=0` is valid; PyMax 2.4.1 photoIds and document/file compatibility.
- Onboarding, skip after owner creation, focused integration editors, unsaved-change
  protection, mobile interface and diagnostics.
- Local backups and password-encrypted portable v2 `.shmax` export, fresh-owner
  restore, database/configuration/SecretStore preservation and rollback.

**Platform:** Debian 13 amd64, Docker + Compose v2 and systemd. The owner confirmed
live installation, message/media delivery, upgrade with retained configuration,
reboot and health. ARMv7/ODROID-XU4 is an experimental designed target; physical
acceptance is incomplete. No live acceptance claim is made for other platforms.

**Install:** verify SHA256SUMS, extract the production `shmax-v0.1.0.tar.gz`, enter
`shmax-v0.1.0`, inspect the installer and run
`sudo bash deploy/installer/install.sh --mode install`.
See [installation](../../docs/en/installation.md) and [setup](../../docs/en/first-setup.md).
Provide your MAX account, BotFather bot, private forum group and network transport.
API ID/API Hash are needed only for optional member synchronization.

**Migration:** protect the encrypted archive and password. Local copies are not
portable encrypted exports. Stop the source bridge before starting the restored
instance. Destination owner credentials survive; old web sessions/admins are revoked.
External services may require reauthentication. Physical cross-architecture migration
has not passed acceptance.

**Limitations:** one paired forum; external protocols/SDKs can change. Native animated
sticker transfer is not promised. Permanent/uncertain failures need operator review.
Delivery followed by a crash before outcome persistence can still cause duplication.
Protect the web UI instead of exposing it publicly without access controls.

Source license: [AGPL-3.0-or-later](../../LICENSE).
[Media](../../docs/en/media.md) · [Backups](../../docs/en/backups.md) ·
[Security](../../SECURITY.md) · [DEMO screenshots](../../docs/assets/screenshots/README.md).
Repository: https://github.com/SHMAX-Project/SHMAX . Private vulnerability reporting is enabled. Code and release remain unpublished.
Original duck artwork and independent public source history are prepared locally.
