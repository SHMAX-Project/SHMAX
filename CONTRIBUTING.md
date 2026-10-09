# Contributing / Участие в разработке

Thanks for considering a contribution! Language: Russian and
English are both welcome; Russian is canonical for user-facing
text.

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
