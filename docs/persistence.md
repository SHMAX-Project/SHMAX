# Persistent data

This stable entry point replaces obsolete stage instructions. Current guides:

- [Русский — канонический](ru/data-persistence.md)
- [English](en/data-persistence.md)

Application containers are replaceable; /var/lib/shmax is persistent.
Preserve operator compose.yaml and .env in /opt/shmax. Bot API has no session
file; optional bot member-sync sessions are memory-only. Backups include the master key.
