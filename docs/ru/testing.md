# Тесты

- Backend: `cd backend && .venv/bin/python -m pytest` (ОДИН
  процесс, ~800 тестов, внешние границы замоканы: MAX — test-fake,
  Bot API — локальный мок, GitHub — инъекция http-колбэков,
  файловая система — tmp_path).
- Frontend: `npx vitest run`, `npx tsc -b`, `npm run build`.
- E2E (Playwright): проекты `desktop-1440x900` и `mobile-375x812`
  гонять ПОСЛЕДОВАТЕЛЬНО, если оба используют Xray-порт 10833.
- Ключевые регрессии закреплены: echo-подавление без видимых
  маркеров, pair-code только при живом poller, отменобезопасный
  reap Xray, SSRF/SHA обновлений, boot-оркестрация, wheel с
  VERSION.
