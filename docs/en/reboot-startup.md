# Reboot / startup orchestration

Reproduced on real Debian 13: Docker restored the container before
the LAN address existed → bind failed (Exited 128); later a plain
start produced a healthy container WITHOUT a published port.
`shmax-boot.service` (After=docker.service, network-online.target;
Restart=on-failure) waits bounded for the configured bind address
to actually exist, force-recreates the container when the
publication is missing/broken, verifies `docker port`, then curls
`/health/ready` on the bind URL. Boot orchestration and the local UI do not
require Internet; message delivery needs MAX and Telegram connectivity. Data is preserved.

Boot also depends on `shmax-hostd.service`: the helper must create its socket before
the container starts. Boot reads SHMAX_BIND_ADDR/SHMAX_PORT from `/opt/shmax/.env`
without executing shell commands. An explicit 0.0.0.0 bind uses loopback for HTTP
checks; ordinary LAN bindings are never widened to wildcard. After the address
appears, exact port publication and host readiness are verified; incorrect or
missing mappings trigger recreation. Wait/retry checks are bounded; systemd
retries failures with backoff.

## Host address and automatic startup

The configured bind address must survive reboot: use a DHCP reservation or persistent host addressing. SHMAX waits for that address; it cannot reserve a DHCP lease. After an upgrade, check application and hostd versions, host-address readiness, MAX/Telegram, published ports and journal; repeat after an authorized reboot without manually starting services. Do not widen the bind address to bypass an addressing error. v0.2.0 was verified through component restarts and a full existing Debian VM reboot with data preserved.
