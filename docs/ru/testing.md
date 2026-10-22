# Тесты

Текущий выпуск: [v0.2.0 — проверки и ограничения](../../release/v0.2.0/VALIDATION.md). Результаты старых выпусков ниже сохранены как история.

- Backend: `cd backend && .venv/bin/python -m pytest` в одном процессе. Внешние границы контролируются: MAX test-fake, локальный Bot API, инъекция GitHub HTTP, tmp_path.
- Frontend: `cd frontend && npm run test:unit && npm run build` (включает TypeScript).
- Playwright: `npm run test:e2e`, последовательно desktop-1440x900 / mobile-375x812 с настоящим backend/SPA и синтетическими внешними сервисами.
- Журнал: `python scripts/benchmark-journal.py --output measurements.json` — 100 000 синтетических событий, хранение, память, запросы и очистка. Синтетические результаты не являются обещанием скорости реальных MAX/Telegram.
- Debian 13 amd64: реальный production Dockerfile/Xray binary, signed clean bootstrap, upgrade от опубликованной версии, forced readiness failure и холодный rollback, сохранность persistent data и журнала после restart. Session fixtures синтетические; не используйте production credentials.

Для v0.1.2: 1002 backend passed, 9 local skips; 51 frontend passed; 26 browser passed. Дополнительно Debian: 38 recovery/journal/queue/TG и 5 real-Xray tests passed. Пропуски на macOS — systemd (1), opt-in SPA smoke (3), Xray binary (5); соответствующие проверки выполнены отдельно в Debian/Playwright.

Закреплены MAX listener generations/cleanup/no-SMS recovery, Xray switch/rollback, один Telegram poller, journal privacy/retention/aggregation/disk failure, постоянные delivery errors, albums/checkpoints/replies/ACL, signed updates и wheel VERSION.

[Заметки v0.1.2](../../release/v0.1.2/RELEASE_NOTES.ru.md).
