# Security Policy / Политика безопасности

SHMAX is a self-hosted appliance that holds sensitive material:
Telegram bot tokens, Telegram API Hash, MAX session data, Xray/VLESS credentials.
Please help us keep it safe.

## Supported versions / Поддерживаемые версии

| Version | Supported |
|---------|-----------|
| Current local release candidate (`0.1.0`) | reports accepted |
| 0.1.0 | prepared; not a published release yet |

## Reporting a vulnerability / Как сообщить об уязвимости

**GitHub Private Vulnerability Reporting: PENDING — NOT ENABLED OR VERIFIED.**

Final organization/repository configuration is centralized in
[PUBLICATION_CONFIG.md](release/v0.1.0/PUBLICATION_CONFIG.md). No active reporting
URL exists yet in this policy.

Once the approved public repository exists, its administrator must enable
Private Vulnerability Reporting, verify the “Report a vulnerability” entry on
Security → Advisories, and verify maintainer access and notification settings.
Only after this check should this policy advertise an active reporting route.
Follow [GitHub's configuration instructions](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository).

The owner must confirm the organization administrator and enable/verify GitHub
Private Vulnerability Reporting at the public-repository stage before release
publication. No personal security email is invented. While reporting is pending,
do not send exploit details,
credentials or reproduction material through public issues or pull requests.

**Приватные сообщения об уязвимостях: ОЖИДАЮТ ВКЛЮЧЕНИЯ И ПРОВЕРКИ.**
Планируемая организация — `SHMAX`; её существование, доступность имени и права
владельца не подтверждены. После создания утверждённого публичного репозитория
администратор должен включить функцию, проверить форму Security → Advisories и
доступ/уведомления сопровождающего. До этого рабочий канал здесь не заявляется.
Настройка GitHub Private Vulnerability Reporting обязательна до публикации релиза.
Не публикуйте детали эксплуатации, токены или данные авторизации в issues/PR.

Please include:

1. SHMAX version (`shmax version` or the dashboard footer);
2. platform/architecture (Debian 13, arm64/armv7);
3. affected component (web API, bridge, updater, installer, hostd);
4. reproduction steps or a PoC description;
5. impact assessment.

**Never include** real secrets in a report: bot tokens, MAX
credentials, Telegram API Hash, Xray subscription URLs, session files or
SecretStore key material. Redact logs.

## Responsible disclosure / Ответственное раскрытие

Maintainers should coordinate disclosure privately. No response SLA or active
support organization is promised. Credit and disclosure timing should be agreed
with the reporter.

## Scope notes

- The web container is intentionally unprivileged: it must never
  receive the Docker socket. Reports showing privilege escalation
  from the web container to the host are critical.
- The update path verifies release artifacts (SHA256 + trusted
  origins). Reports on bypassing `SHMAX_UPDATE_REPO_URL` trust or
  token exfiltration are in scope.

## Portable backup boundary

Portable v2 always encrypts all payload and metadata with fixed-cost Argon2id
(32 MiB, 3 passes) and ordered AES-256-GCM records with authenticated termination.
Restore authenticates before bounded disk extraction and validates paths, file
types, hashes, key material, application/schema version and SQLite integrity.
Limits: 512 MiB total, 128 MiB/file, 10,000 files, 1 MiB manifest; no links/devices.
Admin/owner export and owner-only restore use CSRF/rate limits and private jobs.
Passwords never persist. Hostd uses fixed commands/job IDs, a mutual-exclusion
lock, durable pause/stop/swap journal and readiness-driven rollback. No Docker
socket is exposed to web. Destination credentials survive; old web sessions/admins
and setup tokens are removed, cookie signing rotates, pending deliveries are
quarantined for review. Local previous/ safety copies remain sensitive plaintext.
Legacy in-process restore is rejected in production; offline CLI is owner-operated.
Media downloads are bounded, use configured Telegram transport and trusted MAX
HTTPS origins; image decoding runs in a time/pixel/size-bounded worker. Logs use
static failure reasons, not token-bearing URLs or raw external exceptions.
