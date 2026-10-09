# Logs

`/var/lib/shmax/logs/shmax.log` (rotated), `shmax logs --tail N`,
`docker logs shmax-web-1`, and the Logs page in the UI. Secrets
are masked and never logged; if you find one, rotate it and report
via Security Advisories.
