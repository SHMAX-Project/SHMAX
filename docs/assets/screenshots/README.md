# Скриншоты v0.1.0 / Screenshots

**DEMO, не реальные подключения.** Production React SPA и реальный FastAPI
backend работают в изолированном временном окружении. ONLY external MAX/Telegram
boundaries use local test mocks. Green status is the demo boundary result, not
proof of a live service connection. UI/DOM/internal APIs are not replaced.

Все видимые ID, имена и названия — fixtures из frontend/e2e. Аккаунт
`fake-max-user`, бот `424242`, group/topic/member IDs — вымышленные тестовые
объекты, не production identifiers. Password/token/hash inputs пусты.
Диагностика показывает нейтральный ephemeral /tmp demo path, not a server path.
Mac Darwin/arm64 is the demo host, not a claim of live platform acceptance.

Desktop width 1440 CSS pixels (900 high; users full-page 951 high); mobile
375×812 CSS pixels / 750×1624 PNG. Actual screenshot captures, no retouching.

| File | Actual route | State |
|---|---|---|
| [dashboard-desktop.png](dashboard-desktop.png) | `/dashboard` | Главная / Dashboard |
| [max-settings.png](max-settings.png) | `/max` | Управление MAX |
| [telegram-settings.png](telegram-settings.png) | `/telegram` | Bot/network controls and optional member sync |
| [chat-routing.png](chat-routing.png) | `/chats` | Чат MAX → тема Telegram |
| [users.png](users.png) | `/users` | Approval, roles, signatures |
| [backups.png](backups.png) | `/backups` | Local copies and encrypted export |
| [restore.png](restore.png) | `/setup/restore` | Fresh-owner restore before password entry |
| [initial-setup.png](initial-setup.png) | `/setup/choice` | Start / restore / skip |
| [diagnostics.png](diagnostics.png) | `/diagnostics` | Local diagnostics, external checks not_tested |
| [mobile-dashboard.png](mobile-dashboard.png) | `/dashboard` | 375×812 CSS pixels, DPR 2 |

Recreate: production frontend build, then from frontend:
`SHMAX_E2E_TEMP_ROOT=/tmp npx playwright test`. The release screenshot spec runs
after wizard initialization; portable test captures the real fresh-owner setup
choices and empty restore form before supplying the encrypted archive/password.
No live VM, accounts, customer messages or screenshot upload are involved.

Legacy wizard screenshots were removed from current source because they predate
current controls and include phone/pairing/subscription demo artifacts. They remain only in the private development archive and are absent from the
independent public history. The current images use the original SHMAX duck.
