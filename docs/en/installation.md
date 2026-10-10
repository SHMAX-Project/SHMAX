# Installation modes

`deploy/installer/install.sh`; add `--dry-run` to preview.

| Mode | Behavior | Setup token |
|---|---|---|
| install | full install: user, dirs, Docker, container, boot unit | only if setup is incomplete |
| repair | re-apply layout, data preserved | never |
| upgrade | refresh deployment artifacts, data preserved | never |
| uninstall | remove app, keep data (see `--purge`) | — |

After a successful upgrade the installer does NOT require a setup
token and does not fail (live bug A10 fixed).

Paths: `/opt/shmax` deployment; `/var/lib/shmax` persistent data.
Debian 13 + systemd; the `shmax-boot.service` unit is installed
automatically.

## Hostd and systemd after a clean install

Run `sudo bash deploy/installer/install.sh --mode install` from the complete
extracted SHMAX archive. A standalone install.sh download is insufficient: the
installer needs backend, Dockerfile, compose, boot and systemd files.

The installer creates a separate root-owned `/opt/shmax-hostd` runtime: a Python
>=3.12 venv, copied Python package and only hostd/CLI/backup/update dependencies.
No host Node or developer checkout is required. This runtime survives replacement
of `/opt/shmax`; repair/upgrade refresh it idempotently.

`shmax-hostd.service` creates `/run/shmax/hostd.sock` itself; no socket unit is needed.
The directory is root:shmax 0750 and socket root:shmax 0660. The service preserves
the directory on restart, removes/recreates the socket and refuses to replace a
regular file. The container mounts the directory read-only at `/run/shmax/hostd`
and connects to `/run/shmax/hostd/hostd.sock`; it receives no Docker socket.

Boot is installed unchanged: `ExecStart=/opt/shmax/boot/shmax-boot.sh`. Both units
must pass `systemd-analyze verify` before enable/start; unit verification, hostd
startup, missing socket or incorrect permissions fail installation. A socket
connect is also checked under the container UID/GID (default 8787:8787).

Uninstall disables/removes both units, hostd runtime and runtime directory,
preserving `/var/lib/shmax`. Only explicitly confirmed `--purge` removes user data.

## v0.1.0 archive installation

Download the production archive, SHA256SUMS and build-info.json from
[SHMAX Releases](https://github.com/SHMAX-Project/SHMAX/releases):

```bash
sha256sum -c SHA256SUMS --ignore-missing
tar -xzf shmax-v0.1.0.tar.gz
cd shmax-v0.1.0
# Inspect deploy/installer/install.sh first.
sudo bash deploy/installer/install.sh --mode install
```

The source archive includes Dockerfile/frontend sources/lock; the production
archive also includes built frontend/dist. Docker still fetches dependencies,
base images and Xray: an archive is not a fully offline installation.
Debian 13 amd64, root/sudo, Docker/Compose v2 and systemd are required; missing
host tools are installed by the installer.

```bash
sudo bash deploy/installer/install.sh --mode repair
sudo bash deploy/installer/install.sh --mode upgrade
sudo bash deploy/installer/install.sh --mode uninstall
```

All three preserve `/var/lib/shmax`. `--purge` deletes data and requires a separate
explicit decision; do not use it for ordinary app removal. Create a
[backup](backups.md) before upgrade/removal; [fresh-owner restore](restore.md) uses
portable encrypted exports rather than ordinary local copies.

Do not execute an unknown `curl | sudo bash`. Download the full archive, verify
its hash through a trusted channel and inspect the script. A hash from the same
compromised site alone does not establish authenticity. No live one-line URL exists yet.
