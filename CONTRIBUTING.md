# Contributing / Участие в разработке

Thanks for considering a contribution! Language: Russian and
English are both welcome; Russian is canonical for user-facing
text.

## Как помочь проекту

- **Нашли ошибку?** Откройте [сообщение об ошибке](https://github.com/SHMAX-Project/SHMAX/issues/new?template=bug.yml). Укажите версию SHMAX, ОС и архитектуру, шаги воспроизведения, ожидаемый и фактический результат. Приложите только очищенные от секретов логи.
- **Проблема с установкой?** Используйте [шаблон установки](https://github.com/SHMAX-Project/SHMAX/issues/new?template=installation.yml); укажите способ установки и этап, на котором она остановилась.
- **Нужна функция?** Опишите реальный сценарий через [предложение](https://github.com/SHMAX-Project/SHMAX/issues/new?template=feature.yml). Для маршрутизатора полезны примеры связей аккаунтов, групп и требуемых прав.
- **Есть ARM-сервер?** Помогите проверить свою платформу: модель, ОС, архитектура, установка, подключение и доставка тестовых сообщений. Разделяйте успешную сборку и проверку реальной работы.
- **Хотите изменить код или документацию?** Для крупного изменения сначала согласуйте подход в Issue, затем откройте PR с описанием результата и проверок. Небольшие исправления документации можно присылать сразу.

Контакт по проекту: [v.fomenko.me@gmail.com](mailto:v.fomenko.me@gmail.com).
Вопросы и предложения лучше размещать в Issues, чтобы ответы помогали другим.
Уязвимости сообщайте приватно по [SECURITY.md](SECURITY.md).
Никогда не публикуйте токены, пароли, файлы сессий, базы с реальной перепиской
или ссылки на прокси-подписки.

## How to help

Report bugs or installation failures using the Issue templates; include the SHMAX
version, OS/architecture, reproduction steps and sanitized logs. Feature requests
should describe a real workflow. ARM reports should name the hardware and separate
build success from actual operation. Discuss larger changes in an Issue before
opening a PR; small documentation fixes are welcome directly.

Project contact: [v.fomenko.me@gmail.com](mailto:v.fomenko.me@gmail.com).
Use public Issues for ordinary questions and [SECURITY.md](SECURITY.md) for private
vulnerability reports. Never post credentials, session files or real message databases.

## Prerequisites

- Python ≥ 3.12 (backend)
- Node 20+ (frontend; local install is fine — there is no system
  Node dependency)
- bash (installer/boot tests), no Docker required for tests

## Setup

```bash
cd backend
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'
cd ../frontend
npm ci
```

## Running the gates

```bash
# Start in the repository root; commands below continue in the selected directory.
# backend (ONE process)
cd backend && .venv/bin/python -m pytest
# frontend unit + types + build
cd ../frontend && npx vitest run && npx tsc -b && npm run build
# e2e: independent local Xray ports 10833/10834, one Playwright worker
npx playwright test --project=desktop-1440x900
npx playwright test --project=mobile-375x812
```

## Expectations

- One logical change per PR; tests for behavior changes.
- Never commit secrets (tokens, subscription URLs, session
  files). CI runs a pinned redacted full-history gitleaks scan and fails on unreviewed candidates.
- Security-sensitive areas (auth, SecretStore, ACL, updater,
  hostd) require an accompanying regression test and must not
  weaken the invariants: no Docker socket in the web container;
  `APPLICATION IS DISPOSABLE, USER DATA IS PERSISTENT`; updates
  verified by SHA256 from trusted manifests only.
- User-visible Russian text lives in `frontend/src/help.tsx` and
  `docs/ru/**`; keep English mirrors in sync.

## Release process

See `docs/ru/release-process.md` and `scripts/build-release.sh`.
