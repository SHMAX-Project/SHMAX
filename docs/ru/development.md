# Разработка

```bash
cd backend && python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cd ../frontend && npm ci
```

- Структура: `backend/shmax/**` (API, bridge, xray, update, host,
  backup), `frontend/src/**`, `deploy/**`.
- Правило слоёв: ядро не импортирует транспортные библиотеки
  (PyMax/aiogram/Telethon). Эти библиотеки используются в адаптерах;
  вспомогательный сервис `telegram_members` использует Telethon для bot-only sync.
- Тексты помощи — единый источник: `frontend/src/help.tsx`.
- Версия — единый источник: `backend/shmax/VERSION`.
- Запуск в dev: из `backend`: `.venv/bin/python -m uvicorn shmax.main:app`;
  в отдельном терминале из `frontend`: `npm run dev`.

Подробнее: [testing.md](testing.md),
[release-process.md](release-process.md), [CONTRIBUTING.md](../../CONTRIBUTING.md).
