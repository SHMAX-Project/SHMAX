# Privacy / Конфиденциальность

SHMAX is self-hosted. External services you connect to have their own policies.

## What SHMAX collects

- **No telemetry.** There are no embedded analytics, crash-reporting or usage trackers.
- **Local logs** stay under `/var/lib/shmax/logs` and do not leave automatically.
  Sanitize identifiers before sharing them.

## What leaves your machine

| Data | Destination | When |
|---|---|---|
| MAX authentication/session and bridged messages | MAX | configured MAX authentication and bridge operation |
| Bot token and bridged messages | Telegram Bot API, through the selected transport | configured Telegram verification and bridge operation |
| API ID/API Hash, bot authorization and participant queries | Telegram MTProto, through the sync transport | optional full member sync |
| Subscription request and proxy traffic | configured subscription/proxy/Xray provider | fetching a subscription or using that transport |
| Update checks/downloads; optional GitHub token restricted to api.github.com | GitHub and allowed release origins | manual checks/downloads and configured 24h checks |

## What stays local

The DB, SecretStore, sessions, state, backups and configuration live in
`/var/lib/shmax`. Operator deployment settings also live in `/opt/shmax`.
Member discovery stores Telegram IDs, display names/usernames, membership and
administrator metadata, discovery source/timestamps and SHMAX ACL locally.
Telegram administrator metadata does not authorize SHMAX access.

API Hash is encrypted in SecretStore; the auxiliary bot session exists only in
memory. MAX sessions are separate permission-protected files. A normal `.shmax`
backup includes the SecretStore master key and is not encrypted as a whole;
protect it as a secret or use CLI/API whole-archive encryption. See
[RU backups](docs/ru/backups.md) / [EN backups](docs/en/backups.md).

## External services

MAX, Telegram, subscription/proxy/Xray providers and GitHub process requests
under their own policies. SHMAX cannot change those policies.
