# SHMAX Telegram transports

> **Historical developer reference / Историческая справка.** This document records
> an earlier development stage; commands, dependency versions, acceptance checklists
> and product descriptions below are not current operator instructions. Use
> [RU documentation](ru/README.md), [EN documentation](en/README.md) and the
> [current Operations guide](AI_ASSISTANT_GUIDE.en.md). The current wizard has nine steps;
> Telegram uses the existing bot, with optional bot-only member sync (no human
> userbot). Sync sessions are memory-only. A normal .shmax backup includes the
> SecretStore master key and is not encrypted as a whole. MAX stays direct.


SHMAX supports two distinct Telegram transports in Stage 1B.
They are NOT interchangeable: a Bot API token does not work
with MTProto and vice versa.

## Transport mode

`SHMAX_TG_TRANSPORT`:

| Mode                | Library   | Underlying API         | When to use                              |
|---------------------|-----------|------------------------|------------------------------------------|
| `bot_api_direct`    | aiogram   | HTTPS to api.telegram.org | Default for V1 — works for most users |
| `bot_api_socks5`    | aiogram   | HTTPS via SOCKS5 proxy | Bot API blocked, SOCKS proxy available   |
| `bot_api_http`      | aiogram   | HTTPS via HTTP proxy   | Same as above, HTTP-style proxy           |
| `mtproto_direct`    | Telethon  | MTProto over TCP       | Need bot-account features not in Bot API |
| `mtproto_socks`     | Telethon  | MTProto via SOCKS      | MTProto blocked, SOCKS proxy available   |
| `mtproto_proxy`     | Telethon  | MTProto via MTProxy    | Need a Telegram-specific MTProxy relay   |
| `auto`              | (resolve) | picks one              | Default if `SHMAX_TG_TRANSPORT` is unset |

In `auto` mode, SHMAX picks Bot API direct if `bot_token` is
configured and falls back to MTProto direct if `api_id` /
`api_hash` are configured.

## Required configuration

### Bot API (any `bot_api_*` mode)

* `SHMAX_TG_BOT_TOKEN` — token from @BotFather.

Optionally:

* `SHMAX_TG_PROXY` — proxy URL (`socks5://user:pass@host:port`
  or `http://user:pass@host:port`).

### MTProto (any `mtproto_*` mode)

* `SHMAX_TG_API_ID` and `SHMAX_TG_API_HASH` — from
  https://my.telegram.org.
* `SHMAX_TG_BOT_TOKEN` — same BotFather token; Telethon uses
  `client.start(bot_token=...)` so no phone number is needed.
* `SHMAX_TG_PROXY` for SOCKS (separate from Bot API proxy).
* `SHMAX_TG_MTPROXY_HOST`, `SHMAX_TG_MTPROXY_PORT`,
  `SHMAX_TG_MTPROXY_SECRET` for an MTProxy relay.

## What goes where

* `bot_token` lives in the SecretStore under
  `telegram_bot_token`.
* `api_hash` lives in the SecretStore under
  `telegram_api_hash`.
* MTProxy `secret` lives in the SecretStore under
  `telegram_mtproxy_secret`.
* Proxy `username:password` (SOCKS / HTTP) lives in
  `telegram_proxy_userpass`.
* DB columns `telegram_accounts` only reference bot identity
  records (id, username, transport, status). No secrets.

## Why two transports

The Bot API gives us everything V1 needs: send / receive text,
photos, documents, forum topics (`createForumTopic`,
`message_thread_id`), and a stable token-based authentication.
It does not expose user-level features like reading arbitrary
chats or impersonating a human account.

MTProto via Telethon gives us the same bot capabilities plus
the ability to use the official Telegram client libraries for
MTProxy relays, which is the only way to talk to Telegram from
some restricted networks (Iran, Russia, parts of corporate
LANs).

A real SHMAX deployment will normally pick ONE of the two and
stay there. Switching later means re-doing the bridge
authentication but not the route / mapping / queue state.

## Network-time fallback (Stage 2)

The `auto` mode is a pure config-time decision in Stage 1B.
A Stage-2 enhancement will:

1. Try Bot API direct → fail?
2. Try Bot API SOCKS5 → fail?
3. Try MTProto direct → fail?
4. Mark the transport `unavailable`.

For now, the operator sets the mode explicitly via
`SHMAX_TG_TRANSPORT`. The doctor (`shmax doctor`) reports
the configured mode and asks the operator to verify it
manually until live validation lands.

## V1 wizard surface (real wiring)

The Telegram step in the Web SPA now talks to the runtime
through these endpoints (no placeholders):

* `POST /api/telegram/token` — store bot token in SecretStore.
* `POST /api/telegram/verify` — real `getMe` against the
  configured transport; returns the bot identity.
* `GET  /api/telegram/transport` — current transport mode
  and proxy fields (secret values are masked).
* `POST /api/telegram/transport` — set transport + proxy +
  api_id/api_hash + MTProxy. The body accepts a URL of the
  form `socks5://user:pa%40ss@host:port` or `http://...`; the
  URL parser lives at
  `backend/shmax/api/app.py:_parse_proxy_url` and
  URL-decodes the credentials.
* `POST /api/telegram/transport/probe` — issue a real
  `getMe` against the configured transport; returns
  `{ok, bot_id, username, transport}` or an explicit error
  class.
* `POST /api/telegram/group/pair-code` — issue a one-shot
  pairing code (TTL 15 minutes).
* `GET  /api/telegram/group` — operator's chosen target,
  forum flag, bot identity.
* `POST /api/telegram/group/pair` — manually accept a chat
  id when the operator already knows it (e.g. private group).
* `POST /api/telegram/system-topic` — create the
  `🔧 SHMAX` topic in the operator's forum, returning the
  topic id and persisting the system route.

The wizard UI lives in `frontend/src/wizard.tsx` and is
driven by the canonical step registry. The Transport step
exposes only the modes the runtime can actually service:

* `auto`
* `bot_api_direct`
* `bot_api_socks5`
* `bot_api_http`
* `mtproto_direct`
* `mtproto_socks`
* `mtproto_proxy`

Choosing SOCKS5 / HTTP / MTProxy reveals the relevant inputs;
the URL parser is the only entry point for proxy secrets so
the credentials never end up in plain query strings or logs.

`/api/telegram/transport/probe` is the operator's
"Проверить соединение" button: it performs a real
network round-trip and reports either `ok` or a structured
error class (`AuthRequired`, `ConnectionError`,
`MtProxyRejected`, etc.).

### Telethon session persistence

When the operator selects any `mtproto_*` mode, the
Telethon client persists its session at
`/data/sessions/telegram/<id>.session`. That directory is
volume-mounted from the host and survives container
restarts, so the bot does NOT re-authenticate on every
reboot.

### Proxy URL formats

```
socks5://user:pa%40ss@127.0.0.1:9050
http://user:pa%40ss@proxy.example.com:8080
https://user:pa%40ss@proxy.example.com:443
socks5://[2001:db8::1]:1080
```

Unknown schemes (`ftp://`, `file://`) are rejected at the
parser with HTTP 400. Credentials are URL-decoded; the
parser never logs the password.
