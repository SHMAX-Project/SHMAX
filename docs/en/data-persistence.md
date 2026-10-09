# Data / filesystem map

## THE APPLICATION IS DISPOSABLE. USER DATA IS PERSISTENT.

```
/opt/shmax          deployment; compose.yaml + .env preserved by the updater
/var/lib/shmax      persistent root (never delete casually)
├── db/shmax.db     SQLite + Alembic
├── secrets/        SecretStore AES-256-GCM, master key 0600
├── sessions/max/   MAX session
├── sessions/telegram/  compatibility directory; Bot API has no session file
                       # member-sync session is memory-only
├── state/          watchdogs, telegram_auto, setup_secret
├── backups/        *.shmax archives (0600)
└── cache/          disposable
/run/shmax/hostd.sock  hostd unix socket (0660, group shmax)
```

Cache is disposable. Recreate the container while preserving the data mount.
Replace application files while preserving operator compose.yaml and .env; do
not delete /opt/shmax wholesale without saving them. The updater preserves both.
Never casually delete:
/var/lib/shmax, secrets/, sessions/. Survives container recreation:
everything under /var/lib/shmax. Backups contain: DB snapshot,
SecretStore (still encrypted) **and its master key**, state, sessions, Xray cached node —
NOT cache/logs/tmp/symlinks-outside-root.

Normal archives are not encrypted as a whole: see [backup protection](backups.md).
