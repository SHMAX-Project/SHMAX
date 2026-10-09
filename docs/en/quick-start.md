# Quick start

Debian 13, root, Docker (the installer installs it if missing):

From the root of the complete extracted SHMAX archive:

```bash
sudo bash deploy/installer/install.sh --mode install
```

The installer creates the user and directories (`/opt/shmax`,
`/var/lib/shmax`), installs Docker if needed, builds and starts
the container, waits for `/health/ready`, and — for a FIRST
install only — prints the one-time setup URL + QR code.
Upgrade/repair never require a setup token.

Then open the URL in your LAN and follow the 9-step wizard
(first-setup.md).

Check `systemctl status shmax-boot.service`,
`docker compose -f /opt/shmax/compose.yaml ps`, and host readiness at
`http://<bind-address>:<port>/health/ready`. Use the actual bind/port from
/opt/shmax/.env: probe 127.0.0.1 for 0.0.0.0, or the configured LAN address.
