# Security model

This stable entry point replaces obsolete stage instructions. Current guides:

- [Русский — канонический](ru/security.md)
- [English](en/security.md)

SecretStore protects tokens/API Hash/proxy credentials. MAX session files are
permission-protected; DB user/delivery data is not encrypted. A normal .shmax
contains the master key: protect it as a secret or use whole-archive encryption.
