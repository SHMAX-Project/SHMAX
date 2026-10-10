# SHMAX v0.1.0

The first release of SHMAX, a self-hosted gateway between MAX and Telegram.
SHMAX connects selected MAX chats to topics in a private Telegram group and
provides a web interface for connections, routing and access management.

## Release features

- Bidirectional text, photos, documents and supported video.
- Reply mapping, sender attribution and topic routing.
- Participant approval, roles, blocking and optional member synchronization.
- Direct Telegram connections, SOCKS5, HTTP proxies and managed Xray transport.
- Automatic recovery of saved Telegram transport.
- Initial setup wizard, separate integration settings and a mobile interface.
- Local backups and encrypted `.shmax` exports for data migration.
- Verified production-archive updates with backups, application health checks
  and automatic rollback.
- Non-root container execution and correct application file permissions.

## Requirements and installation

Debian 13 amd64, Docker Engine, Compose v2 and systemd. Installation requires
server administrator access, your own MAX account, a Telegram bot and a private
group with topics enabled.

Download `shmax-v0.1.0.tar.gz` and `SHA256SUMS`, verify the checksum and extract
the archive. From the `shmax-v0.1.0` directory, run:

```bash
sudo bash deploy/installer/install.sh --mode install
```

[Installation](../../docs/en/installation.md) · [Initial setup](../../docs/en/first-setup.md).

## Data and security

Configuration, the database and credentials persist through updates.
Use an encrypted export for migration and stop the source gateway before
starting the restored instance. Ordinary local backups contain authorization
material and require secure storage.

[Backups](../../docs/en/backups.md) · [Restore](../../docs/en/restore.md) ·
[Security policy](../../SECURITY.md).

## Release files

Production archive, source archive, the `shmax` update bootstrap tool, checksums,
manifest, build information and validation results.
See [media documentation](../../docs/en/media.md) for attachment handling.

SHMAX is licensed under [AGPL-3.0-or-later](../../LICENSE).
It is an independent project and is not affiliated with the companies operating MAX or Telegram.
