# Development

Python ≥ 3.12 venv + `pip install -e '.[dev]'`; Node 20 +
`npm ci`. Layers: the core never imports transport libraries
(PyMax/aiogram/Telethon). Adapters use these libraries; the auxiliary
`telegram_members` service uses Telethon for bot-only sync. Help texts have ONE
source: `frontend/src/help.tsx`; version has ONE source: `backend/shmax/VERSION`. From backend run
`.venv/bin/python -m uvicorn shmax.main:app`; in a separate terminal from frontend
run `npm run dev`. See [testing](testing.md), [release process](release-process.md)
and [Contributing](../../CONTRIBUTING.md).
