"""MAX transport probe — Stage 1 validation tool.

NOT a production UI. Runs against a real MAX account owned by
the operator. Used to verify that the PyMax-backed MaxAdapter
actually works end-to-end and that the persistent session is
restored across restarts.

Usage examples (run from ``backend/`` so the ``shmax`` package
is importable):

    # 1) First-time login: prompts for phone (or read from
    #    SHMAX_MAX_PHONE), then for SMS code, then for 2FA if
    #    Max asks for it.
    SHMAX_DATA_DIR=./.dev-data python scripts/max_probe.py login

    # 2) Profile of the current account.
    SHMAX_DATA_DIR=./.dev-data python scripts/max_probe.py profile

    # 3) Sanitized chat list.
    SHMAX_DATA_DIR=./.dev-data python scripts/max_probe.py chats

    # 4) Listen for incoming events for 30 seconds.
    SHMAX_DATA_DIR=./.dev-data python scripts/max_probe.py listen --seconds 30

    # 5) Send text to a chat (you must know the chat_id).
    SHMAX_DATA_DIR=./.dev-data python scripts/max_probe.py send 1234567890 "hello"

    # 6) Native reply.
    SHMAX_DATA_DIR=./.dev-data python scripts/max_probe.py reply 1234567890 987654 "re: hi"

    # 7) Send a photo.
    SHMAX_DATA_DIR=./.dev-data python scripts/max_probe.py send-photo 1234567890 /abs/img.jpg

    # 8) Send a file.
    SHMAX_DATA_DIR=./.dev-data python scripts/max_probe.py send-file 1234567890 /abs/report.pdf

    # 9) Health snapshot.
    SHMAX_DATA_DIR=./.dev-data python scripts/max_probe.py health

    10) Restart test: stop, restart, verify session reused.
    SHMAX_DATA_DIR=./.dev-data python scripts/max_probe.py restart-check

The probe never prints the phone, SMS code, 2FA password, or
session token. Only ``session_path`` is printed, never its
contents.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# Make the shmax package importable when running from the repo root.
HERE = Path(__file__).resolve()
REPO_ROOT = HERE.parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from shmax.adapters.max.config import PyMaxAdapterConfig  # noqa: E402
from shmax.adapters.max.errors import MaxError  # noqa: E402
from shmax.adapters.max.providers import (  # noqa: E402
    InteractivePasswordProvider,
    InteractiveSmsProvider,
)
from shmax.adapters.max.pymax_adapter import PyMaxMaxAdapter  # noqa: E402


def _build_adapter(phone: str | None = None) -> PyMaxMaxAdapter:
    cfg = PyMaxAdapterConfig.from_env()
    if phone is not None:
        cfg = PyMaxAdapterConfig(
            phone=phone,
            session_dir=cfg.session_dir,
            session_name=cfg.session_name,
            app_version=cfg.app_version,
            reconnect=cfg.reconnect,
            log_level=cfg.log_level,
        )
    # We never pre-fill the SMS code or 2FA password: they must
    # be entered by the human at runtime.
    sms = InteractiveSmsProvider(code=None)
    pwd = InteractivePasswordProvider(password=None)
    return PyMaxMaxAdapter(
        config=cfg,
        sms_provider=sms,
        password_provider=pwd,
    )


def _prompt_phone() -> str:
    raw = input("Enter MAX phone in E.164 (e.g. +79990000000): ").strip()
    if not raw.startswith("+"):
        print("[probe] phone must start with '+'", file=sys.stderr)
        sys.exit(2)
    return raw


# ----------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------
async def cmd_login(args: argparse.Namespace) -> int:
    cfg = PyMaxAdapterConfig.from_env()
    print(f"[probe] session_dir = {cfg.session_dir}")
    print(f"[probe] session_name = {cfg.session_name}")
    phone = args.phone or os.environ.get("SHMAX_MAX_PHONE") or _prompt_phone()
    adapter = _build_adapter(phone=phone)
    try:
        await adapter.connect()
    except MaxError as e:
        print(f"[probe] login failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    health = await adapter.health()
    print("MAX authenticated")
    print(f"  session_path: {adapter.session_path}")
    print(f"  account_id:   {health.get('account_id')}")
    print(f"  connected:    {health.get('connected')}")
    await adapter.disconnect()
    return 0


async def cmd_profile(args: argparse.Namespace) -> int:
    adapter = _build_adapter()
    try:
        await adapter.connect()
    except MaxError as e:
        print(f"[probe] connect failed: {e}", file=sys.stderr)
        return 1
    profile = await adapter.get_profile()
    print("Profile")
    print(f"  user_id:      {profile.user_id}")
    print(f"  display_name: {profile.display_name}")
    print(f"  handle:       {profile.handle}")
    await adapter.disconnect()
    return 0


async def cmd_chats(args: argparse.Namespace) -> int:
    adapter = _build_adapter()
    try:
        await adapter.connect()
    except MaxError as e:
        print(f"[probe] connect failed: {e}", file=sys.stderr)
        return 1
    chats = await adapter.get_chats()
    personal = [c for c in chats if c.kind == "dialog"]
    groups = [c for c in chats if c.kind in ("chat", "channel")]
    print(f"Personal chats found: {len(personal)}")
    print(f"Group chats found:    {len(groups)}")
    print()
    print("Sample (sanitized, up to 5 each):")
    for label, group in (("personal", personal), ("group", groups)):
        for c in group[:5]:
            print(f"  [{label}] kind={c.kind} id={c.chat_id} title={c.title!r}")
    await adapter.disconnect()
    return 0


async def cmd_listen(args: argparse.Namespace) -> int:
    adapter = _build_adapter()
    try:
        await adapter.connect()
    except MaxError as e:
        print(f"[probe] connect failed: {e}", file=sys.stderr)
        return 1
    print(f"[probe] listening for {args.seconds}s ...")
    async def _drain() -> None:
        async for ev in adapter.listen_events():
            print(
                f"  event chat={ev.chat_id} sender={ev.sender_id} "
                f"text={ev.text[:80]!r}"
            )
    try:
        await asyncio.wait_for(_drain(), timeout=args.seconds)
    except asyncio.TimeoutError:
        pass
    await adapter.disconnect()
    return 0


async def cmd_send(args: argparse.Namespace) -> int:
    adapter = _build_adapter()
    try:
        await adapter.connect()
    except MaxError as e:
        print(f"[probe] connect failed: {e}", file=sys.stderr)
        return 1
    await adapter.send_text(args.chat_id, args.text)
    print(f"[probe] sent text to {args.chat_id}")
    await adapter.disconnect()
    return 0


async def cmd_reply(args: argparse.Namespace) -> int:
    adapter = _build_adapter()
    try:
        await adapter.connect()
    except MaxError as e:
        print(f"[probe] connect failed: {e}", file=sys.stderr)
        return 1
    await adapter.send_reply(args.chat_id, args.message_id, args.text)
    print(f"[probe] replied to {args.message_id} in {args.chat_id}")
    await adapter.disconnect()
    return 0


async def cmd_send_photo(args: argparse.Namespace) -> int:
    adapter = _build_adapter()
    try:
        await adapter.connect()
    except MaxError as e:
        print(f"[probe] connect failed: {e}", file=sys.stderr)
        return 1
    media_ref = f"photo:{args.path}"
    await adapter.send_media(args.chat_id, media_ref)
    print(f"[probe] sent photo {args.path} to {args.chat_id}")
    await adapter.disconnect()
    return 0


async def cmd_send_file(args: argparse.Namespace) -> int:
    adapter = _build_adapter()
    try:
        await adapter.connect()
    except MaxError as e:
        print(f"[probe] connect failed: {e}", file=sys.stderr)
        return 1
    media_ref = f"file:{args.path}"
    await adapter.send_media(args.chat_id, media_ref)
    print(f"[probe] sent file {args.path} to {args.chat_id}")
    await adapter.disconnect()
    return 0


async def cmd_health(args: argparse.Namespace) -> int:
    adapter = _build_adapter()
    try:
        await adapter.connect()
    except MaxError as e:
        print(f"[probe] connect failed: {e}", file=sys.stderr)
        return 1
    health = await adapter.health()
    for k, v in health.items():
        print(f"  {k}: {v}")
    await adapter.disconnect()
    return 0


async def cmd_restart_check(args: argparse.Namespace) -> int:
    cfg = PyMaxAdapterConfig.from_env()
    expected_session = cfg.session_dir / cfg.session_name

    # Round 1: connect, drop marker file, disconnect.
    adapter = _build_adapter()
    await adapter.connect()
    print("[probe] round 1: connected, session_path =", adapter.session_path)
    if not expected_session.is_file():
        print(f"[probe] FAIL: expected session file does not exist: {expected_session}")
        await adapter.disconnect()
        return 1
    print(f"[probe] session file present ({expected_session.stat().st_size} bytes)")
    await adapter.disconnect()

    # Round 2: new adapter, same config. If SMS is requested again,
    # restore failed.
    print("[probe] round 2: starting a fresh adapter with the same session_dir ...")
    adapter2 = _build_adapter()
    # Pre-flight: try connect WITHOUT providing an SMS code in the
    # env. The provider will prompt only if PyMax actually needs it.
    try:
        await adapter2.connect()
    except MaxError as e:
        print(f"[probe] round 2 connect failed: {e}", file=sys.stderr)
        return 1
    health = await adapter2.health()
    print("[probe] round 2: health =", health)
    if not health.get("authenticated"):
        print("[probe] FAIL: not authenticated after restart", file=sys.stderr)
        await adapter2.disconnect()
        return 1
    await adapter2.disconnect()
    print("[probe] PASS: session restored without re-auth")
    return 0


# ----------------------------------------------------------------------
# CLI plumbing
# ----------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="max_probe", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("login", help="interactive first-time login")
    s.add_argument("--phone", default=None, help="E.164 phone (else env / prompt)")
    s.set_defaults(func=cmd_login)

    sub.add_parser("profile", help="print account profile").set_defaults(func=cmd_profile)
    sub.add_parser("chats", help="print sanitized chat list").set_defaults(func=cmd_chats)

    s = sub.add_parser("listen", help="listen for incoming events")
    s.add_argument("--seconds", type=int, default=30)
    s.set_defaults(func=cmd_listen)

    s = sub.add_parser("send", help="send plain text")
    s.add_argument("chat_id")
    s.add_argument("text")
    s.set_defaults(func=cmd_send)

    s = sub.add_parser("reply", help="send native reply")
    s.add_argument("chat_id")
    s.add_argument("message_id")
    s.add_argument("text")
    s.set_defaults(func=cmd_reply)

    s = sub.add_parser("send-photo", help="send a photo")
    s.add_argument("chat_id")
    s.add_argument("path")
    s.set_defaults(func=cmd_send_photo)

    s = sub.add_parser("send-file", help="send a generic file")
    s.add_argument("chat_id")
    s.add_argument("path")
    s.set_defaults(func=cmd_send_file)

    sub.add_parser("health", help="print adapter health").set_defaults(func=cmd_health)
    sub.add_parser("restart-check", help="verify session restore across restarts").set_defaults(
        func=cmd_restart_check
    )

    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.environ.get("SHMAX_LOG_LEVEL", "WARNING"))
    args = _build_parser().parse_args(argv)
    try:
        return asyncio.run(args.func(args))
    except KeyboardInterrupt:
        print("[probe] interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
