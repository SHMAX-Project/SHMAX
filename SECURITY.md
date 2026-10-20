# Security Policy / Политика безопасности

SHMAX is a self-hosted appliance that holds sensitive material:
Telegram bot tokens, Telegram API Hash, MAX session data, Xray/VLESS credentials.
Please help us keep it safe.

## Supported versions / Поддерживаемые версии

| Version | Supported |
|---------|-----------|
| 0.1.3 | latest stable release; reports accepted |
| 0.1.2 | previous stable release; reports accepted |
| 0.1.1 | previous stable release; reports accepted |
| 0.1.0 | published stable release; reports accepted |

## Reporting a vulnerability / Как сообщить об уязвимости

**GitHub Private Vulnerability Reporting: ENABLED.**

Repository: [SHMAX-Project/SHMAX](https://github.com/SHMAX-Project/SHMAX).
Private reporting was enabled by the owner account on 2026-10-10 and verified
in the repository Security overview. Use
[Security → Advisories](https://github.com/SHMAX-Project/SHMAX/security/advisories)
to report privately. If the private report option is unavailable, do not post
exploit details in public issues or pull requests.

**Приватные сообщения об уязвимостях: ВКЛЮЧЕНЫ.**
Организация `SHMAX-Project`, репозиторий `SHMAX`. Функция включена 10 октября
2026 года, статус Enabled проверен в Security. Сообщайте через приватную форму
Security → Advisories. Не публикуйте детали эксплуатации, токены или данные
авторизации в issues/PR. Настройки личных уведомлений сопровождающего отдельно
не проверялись; срок ответа не гарантируется.

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
