# What SHMAX is and why

```
MAX (ordinary user account) ↔ SHMAX ↔ Telegram bot ↔ private
Telegram supergroup with forum topics
```

Each selected MAX chat maps to its own Telegram topic. Messages
flow both ways: replies in a topic go back to the MAX chat,
authors are attributed (`[Name]:`), media is delivered with
captions.

Self-hosted on Debian 13 + Docker: you own the database, sessions
and logs. THE APPLICATION IS DISPOSABLE; USER DATA IS PERSISTENT
(`/var/lib/shmax`). Telegram may use Direct, SOCKS5, HTTP proxy or
an Xray subscription (VLESS/Trojan, Reality); MAX and Telegram use separate configured network paths; MAX has an independent direct, HTTP, SOCKS5 or Xray connection; see [MAX networking](max-network.md). Includes ACL, backups and verified
GitHub-Releases updates with automatic rollback.

Not supported in V1: MAX bots, clustering, Xray SOCKS exposed to
the LAN, telemetry (none exists — see ../../PRIVACY.md).

## v0.2.0

Routes, source/sender labels and send grants are configured per Telegram group. A MAX chat can have multiple destinations; administrator overview groups create topics automatically. Durable jobs and message mappings are destination-specific; albums retain their members and topic in each group. MAX networking is independent of Telegram. See [groups and access](multi-group.md), [MAX networking](max-network.md) and [media](media.md).
