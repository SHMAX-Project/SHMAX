# SHMAX architecture

> **Historical developer reference / Историческая справка.** This document records
> an earlier development stage; commands, dependency versions, acceptance checklists
> and product descriptions below are not current operator instructions. Use
> [RU documentation](ru/README.md), [EN documentation](en/README.md) and the
> [current Operations guide](AI_ASSISTANT_GUIDE.en.md). The current wizard has nine steps;
> Telegram uses the existing bot, with optional bot-only member sync (no human
> userbot). Sync sessions are memory-only. A normal .shmax backup includes the
> SecretStore master key and is not encrypted as a whole. MAX stays direct.


SHMAX is a self-hosted gateway between MAX and Telegram. It is
designed so that the application container can be replaced at
will without losing user data, and so that live transport libraries
(PyMax, aiogram, Telethon) can be swapped without rewriting
business logic.

## Layered view

```
                 ┌──────────────────────────────────────┐
                 │      SHMAX Core (Stage 1B)          │
                 │  (FastAPI, /health, paths,         │
                 │   routing engine, queue worker,    │
                 │   bridge, backup, doctor, CLI)     │
                 └──────────────┬───────────────────────┘
                                │ depends only on adapter ABCs
                                ▼
                 ┌──────────────────────────────────────┐
                 │  Adapter layer (Stage 1A / 1B)      │
                 │  MaxAdapter ABC + PyMaxMaxAdapter  │
                 │  TelegramAdapter ABC               │
                 │   ├─ TelegramBotApiAdapter (aiogram)
                 │   └─ TelegramMtProtoAdapter (Telethon)
                 │  FakeMaxAdapter / FakeTelegramAdapter (tests)
                 └──────────────┬───────────────────────┘
                                │
                                ▼
                 ┌──────────────────────────────────────┐
                 │   MAX account     Telegram account │
                 └──────────────────────────────────────┘
```

## Hard architectural rules

1. **Core never imports transport libraries.** No `import pymax`,
   `import aiogram`, or `import telethon` anywhere under
   `shmax/core`, `shmax/routing`, `shmax/bridge`, `shmax/queue`,
   `shmax/database`, `shmax/auth`, `shmax/security`,
   `shmax/backup`, `shmax/update`, or `shmax/cli`. Enforced
   by `tests/test_bridge.py::test_core_module_does_not_import_pymax`
   and the corresponding grep-based review in CI.
2. **Application is disposable; user data is persistent.** The
   container can be deleted at any time; everything under
   `$SHMAX_DATA_DIR` survives.
3. **Routes are keyed by id, never by name.** Title and display
   name are display-only. Two MAX chats with the same name still
   belong to different routes.
4. **Outbound messages carry a hidden marker** so the receiving
   transport's echo of our own delivery is dropped at the
   `BridgeEngine` boundary.

## Subsystems

### Domain (`shmax.domain`)

Framework-free dataclasses:

- `NormalizedMessage` — what Core consumes from a transport
- `ChatRef`, `SenderRef`, `ReplyRef` — identity references
- `Attachment` — media envelope (opaque `payload` dict)
- `DeliveryResult` — typed delivery outcome
- `OutboundMessage` — what Core hands to an adapter for delivery
- `Direction`, `ChatKind`, `AttachmentKind`, `DeliveryState`, `Role`

### Database (`shmax.database`)

SQLAlchemy 2.x async + SQLite + Alembic migration files.

Tables (see `backend/alembic/versions/0001_initial.py`):

| Table             | Purpose                                                |
|-------------------|--------------------------------------------------------|
| `settings`        | key/value runtime configuration                        |
| `admin_users`     | local administrators (Argon2id hashes)                |
| `setup_tokens`    | one-time bootstrap tokens (hashed)                     |
| `max_accounts`    | MAX account records                                    |
| `max_chats`       | cached MAX chat descriptors                            |
| `telegram_accounts` | Telegram bot identity records                        |
| `telegram_targets` | Telegram chats used as bridge targets                 |
| `telegram_users`  | known Telegram users (ACL)                            |
| `routes`          | MAX chat ↔ Telegram topic mappings                    |
| `acl_rules`       | role-based access rules                                |
| `message_map`     | persistent MAX ↔ Telegram message id mapping          |
| `delivery_queue`  | durable outbox with retry                              |
| `audit_log`       | administrative events                                  |
| `schema_meta`     | ad-hoc meta                                            |

WAL mode, foreign keys, busy timeout, all per-connection.

### SecretStore (`shmax.security`)

AES-256-GCM via `cryptography`. Master key at
`$SHMAX_DATA_DIR/secrets/master.key` (mode 0600). Store at
`$SHMAX_DATA_DIR/secrets/store.bin`. The store is append-only
on disk and rebuildable from disk on every process restart.
No plaintext of any secret ever appears in logs, repr, or
debug output.

### Auth (`shmax.auth`)

- Argon2id for password hashing.
- Signed cookie sessions via `itsdangerous` with constant-time
  verification and absolute expiry.
- CSRF token in a separate cookie + header.
- Login rate limiter (sliding window + lockout).
- One-time setup tokens (SHA-256 hashed on disk).

### Adapters (`shmax.adapters.{max,telegram}`)

`MaxAdapter` and `TelegramAdapter` are ABCs from Stage 0.
PyMax-backed `PyMaxMaxAdapter` exists and is validated for
imports / construction / domain translation. The Telegram side
has `TelegramBotApiAdapter` (aiogram v3) and
`TelegramMtProtoAdapter` (Telethon); both are imported and
have unit-testable factory hooks so tests never touch the
network. `FakeMaxAdapter` and `FakeTelegramAdapter` provide
in-memory implementations for the offline E2E test suite.

### Bridge engine (`shmax.bridge`)

The Core logic that ties MAX and Telegram together:

```
MAX event → strip outbound marker → dedup → route lookup → enqueue
queue worker → call adapter → on success: persist message_map
```

Loop prevention: every outbound message carries a 32-hex-char
marker. The receiving transport echoes the message back; the
bridge detects the marker and drops it. The marker is also
embedded in the `message_map.outbound_marker` column so
lookups are O(1).

### Persistent queue (`shmax.queue`)

The outbox. States: `pending → processing → delivered | retry | failed`.
Exponential backoff with full jitter; default `max_attempts = 8`.
`retry` rows become claimable again once `next_attempt_at` is in
the past. Worker restarts pick up exactly where they left off
because the queue is a SQLite table.

### Backup (`shmax.backup`)

`.shmax` tar.gz archive with a JSON manifest and per-entry
SHA-256. Includes DB snapshot, config, encrypted secrets,
sessions, and state. Excludes `cache/` and the `backups/` dir
itself. `validate_backup` re-hashes every entry before restore.

### Update state machine (`shmax.update`)

Pure-Python state machine:

```
CHECK → BACKUP → PULL → STOP → MIGRATE → START → HEALTHCHECK → DONE
                       (failure on any of these)
                              ↓
                          ROLLBACK → FAILED
```

Hooks are async callables; production wires Docker, but the
machine is fully unit-testable with recording fakes.

### CLI (`shmax.cli`)

`python -m shmax.cli {version,status,doctor,backup,setup-link,
update,rollback,logs}`. Reads the same persistent root as the
FastAPI app.

### Diagnostics (`shmax.diagnostics`)

The doctor runs offline self-checks (system, data root,
filesystem, database, schema, secret store, queue, config).
External integrations report `NOT_TESTED` in offline mode —
not `FAIL` — so the operator can tell the difference between
"broken" and "not wired up yet".

### API (`shmax.api`)

FastAPI surface:

- `GET /health` — Docker healthcheck
- `GET /` — minimal SHMAX READY landing
- `GET /api/status` — dashboard snapshot
- `POST /api/setup/token`, `POST /api/setup/admin` — bootstrap
- `POST /api/auth/login`, `POST /api/auth/logout` — admin auth
- `GET /api/doctor`, `GET /api/queue`, `GET /api/routes`,
  `POST /api/routes`, `POST /api/routes/{id}/toggle`
- `GET /api/max/chats`, `GET /api/telegram/chats`
- `GET /api/backups`, `POST /api/backups`,
  `POST /api/backups/{name}/restore`
- `GET /api/update/status`, `POST /api/update/check`
- `GET /api/logs`

Admin endpoints require the session cookie + CSRF header.
