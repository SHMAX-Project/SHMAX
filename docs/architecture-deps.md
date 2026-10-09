# Architecture dependency audit (V1)

> **Historical developer reference / Историческая справка.** This document records
> an earlier development stage; commands, dependency versions, acceptance checklists
> and product descriptions below are not current operator instructions. Use
> [RU documentation](ru/README.md), [EN documentation](en/README.md) and the
> [current AI guide](AI_ASSISTANT_GUIDE.en.md). The current wizard has nine steps;
> Telegram uses the existing bot, with optional bot-only member sync (no human
> userbot). Sync sessions are memory-only. A normal .shmax backup includes the
> SecretStore master key and is not encrypted as a whole. MAX stays direct.


SHMAX targets three Debian 13 architectures:

| Arch | Status           | Notes                                      |
| ---- | ---------------- | ------------------------------------------ |
| amd64 | Supported       | Production tier.                           |
| arm64 | Supported       | Production tier (Raspberry Pi 4/5, etc.).  |
| armv7 | Experimental    | Builds; some wheels may need cross-build.  |

This file tracks which runtime dependencies might block armv7
because of native extension availability, and which we
pin to versions that are known to ship multi-arch wheels.

## Direct runtime dependencies

| Package        | Pinned range          | amd64 | arm64 | armv7 | Notes                                 |
| -------------- | --------------------- | ----- | ----- | ----- | ------------------------------------- |
| fastapi        | >=0.111,<1.0          | ✓     | ✓     | ✓     | Pure Python.                          |
| uvicorn        | >=0.30,<1.0           | ✓     | ✓     | ✓     | Pulls uvloop on supported archs.      |
| pydantic       | >=2.6,<3.0            | ✓     | ✓     | ✓     | Rust core; manylinux wheels.          |
| pydantic-settings | >=2.2,<3.0         | ✓     | ✓     | ✓     | Pure Python.                          |
| python-multipart | >=0.0.9            | ✓     | ✓     | ✓     | Pure Python.                          |
| maxapi-python  | >=2.4,<3.0            | ✓     | ✓     | ✓     | Pure Python (PyMax).                  |
| aiogram        | >=3.10,<4.0           | ✓     | ✓     | ✓     | Pure Python; aiohttp underneath.      |
| telethon       | >=1.36,<2.0           | ✓     | ✓     | ⚠     | Pure Python, but MTProxy crypto is C; needs manylinux. |
| SQLAlchemy     | >=2.0,<3.0            | ✓     | ✓     | ✓     | C extensions; manylinux wheels ship.  |
| alembic        | >=1.13,<2.0           | ✓     | ✓     | ✓     | Pure Python.                          |
| aiosqlite      | >=0.20                | ✓     | ✓     | ✓     | Pure Python over sqlite3.             |
| greenlet       | >=3.0                 | ✓     | ✓     | ⚠     | Native; manylinux wheels only.        |
| cryptography   | >=42.0                | ✓     | ✓     | ⚠     | Native; manylinux wheels via PyPI.    |
| argon2-cffi    | >=23.0                | ✓     | ✓     | ⚠     | Wraps libargon2; manylinux wheels.    |
| itsdangerous   | >=2.1                 | ✓     | ✓     | ✓     | Pure Python.                          |
| qrcode[pil]    | >=7.4                 | ✓     | ✓     | ⚠     | Pulls Pillow; Pillow ships armv7 wheels. |

Legend: ✓ wheel available, ⚠ experimental / extra care needed.

## Known armv7 caveats

* **Telethon** ships its native MTProxy primitives via
  `pyaes`-style pure Python + optional C. V1 expects this to
  work on armv7, but has not been validated against real
  hardware.
* **greenlet** and **cryptography** depend on Rust/C builds.
  Their manylinux2014 wheels cover armv7; if the base image
  has glibc < 2.17, the wheel install will fall back to a
  source build.
* **argon2-cffi** depends on the libargon2 C library. We do
  NOT install `libargon2-dev` in the runtime image; the
  PyPI manylinux wheel bundles a static build, so this
  works out of the box.

## Image build matrix

The bake file `deploy/docker-bake.hcl` enumerates the
multi-arch matrix:

* `image-amd64` — `linux/amd64`
* `image-arm64` — `linux/arm64`
* `image-armv7` — `linux/arm/v7` (experimental, marked
  separately so a CI run can decide whether to push)
* `image`       — manifest covering amd64 + arm64

## Validation status

| Arch | CI validation | Real-hardware validation |
| ---- | ------------- | ------------------------ |
| amd64 | not yet       | not yet                  |
| arm64 | not yet       | not yet                  |
| armv7 | not yet       | not yet                  |

All three require `docker buildx` against a registry; we do
not push anywhere automatically. Validation is gated on
real-hardware availability.
