# GitHub Releases updates

This stable entry point replaces obsolete stage instructions. Current guides:

- [Русский — канонический](ru/updates.md)
- [English](en/updates.md)

Checks run every 24 hours and manually. Installation is manual via shmax-hostd
with SHA256 validation, safety backup and host readiness. The previous deployment
is restored on failure; failed rollback is reported explicitly. User data is preserved.
