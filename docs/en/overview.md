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
an Xray subscription (VLESS/Trojan, Reality); only Telegram
traffic goes through Xray; MAX stays direct. Includes ACL, backups and verified
GitHub-Releases updates with automatic rollback.

Not supported in V1: MAX bots, clustering, Xray SOCKS exposed to
the LAN, telemetry (none exists — see ../../PRIVACY.md).
