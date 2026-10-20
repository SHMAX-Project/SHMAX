# Install SHMAX

The one-line bootstrap supports Debian 13 amd64 with systemd and root/sudo.
Both installation methods use the same application and persistent `/var/lib/shmax` data.
Network access to GitHub, Debian/Docker repositories and build dependencies is required.

## One-line installation

```bash
curl -fsSL https://raw.githubusercontent.com/SHMAX-Project/SHMAX/main/install.sh | sudo bash
```

The bootstrap downloads the latest published stable release from the official
repository, verifies its RSA-3072/SHA-256 signature using the embedded public key,
checks archive and file integrity, then runs the full installer. Missing Docker
Engine, Compose v2 and dependencies are installed from official APT repositories.
The installer does not reconfigure SSH or disable the firewall.

Stable release [v0.1.2](https://github.com/SHMAX-Project/SHMAX/releases/tag/v0.1.2) is available.
Drafts and prereleases are rejected.

After successful health checks, open the printed web address and setup link in your
browser. Re-running on an existing installation offers a safe update and preserves
settings, keys and sessions. Confirmation is read from the terminal; unattended
updates do not start automatically. Partial deployments require repair, not a fresh install.

## Manual archive installation

Manual installation remains supported. Download from
[SHMAX Releases](https://github.com/SHMAX-Project/SHMAX/releases):

- `shmax-v0.1.2.tar.gz`, the full production package;
- `SHA256SUMS` and `SHA256SUMS.sig`;
- the official repository's `deploy/bootstrap/release-public.pem` verification key.

Check the public key fingerprint against the official release documentation, then:

```bash
openssl dgst -sha256 -verify release-public.pem -signature SHA256SUMS.sig SHA256SUMS
sha256sum -c SHA256SUMS --ignore-missing
tar -xzf shmax-v0.1.2.tar.gz
cd shmax-v0.1.2
# Inspect the full installer before execution.
sudo bash deploy/installer/install.sh --mode install
```

Stop if either verification fails. Use the production archive; the source archive
omits built `frontend/dist`. Docker still downloads base images, dependencies and
Xray, so a manually transferred archive does not make installation fully offline.

## Updates

Latest signed stable release:

```bash
sudo shmax update
```

A manually downloaded and verified production archive:

```bash
sudo shmax update /absolute/path/shmax-v0.1.2.tar.gz
```

Both use the local transaction: build before downtime, verified cold backup,
candidate startup and readiness checks, with previous-image and data rollback on
failure. Protected reports are stored in `/var/backups/shmax`.

## Full installer modes

Run from the complete extracted archive; add `--dry-run` to preview.

| Mode | Behavior |
|---|---|
| `--mode install` | Initial application, Docker and service installation |
| `--mode repair` | Repair deployment while retaining data |
| `--mode upgrade` | Refresh deployment while retaining data; back up first |
| `--mode uninstall` | Remove application, retain data |

Prefer `shmax update` for ordinary updates because it includes backup, acceptance
and rollback. `--purge` deletes user data and requires separate confirmation.

## Paths and services

- `/opt/shmax`: deployment, compose and boot files.
- `/var/lib/shmax`: persistent database, settings, secrets, sessions and backups.
- `/opt/shmax-hostd`: separate root-owned Python runtime and CLI.
- `shmax-hostd.service` and `shmax-boot.service`: verified before startup.

The application runs without root and connects to a restricted hostd socket;
its container does not receive the Docker socket.

[First setup](first-setup.md) · [Backups](backups.md) ·
[Restore](restore.md) · [Web interface updates](updates.md).

SHA-256 fingerprint of the published PEM key file: `35cd7ddcf79cc3d66a1698e9be32d5d042815b9dacae08b6a088a9c03adbd4ac`.
