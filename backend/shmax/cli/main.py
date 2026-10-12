"""``python -m shmax.cli`` implementation."""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
import os
import platform
import sys
from pathlib import Path

def _accepted_backend(opt: Path) -> Path | None:
    """Existing updater retains the verified accepted source under releases/.

    Older updaters copy CLI files but not the host package VERSION/migrations.
    Reuse that immutable accepted package with the existing host Python runtime.
    """
    import re
    try:
        values = dict(line.split("=", 1) for line in (opt / ".env").read_text().splitlines()
                      if line.startswith("SHMAX_DEPLOYMENT_ID="))
        identifier = values.get("SHMAX_DEPLOYMENT_ID", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identifier):
            return None
        backend = (opt / "releases" / identifier / "backend").resolve()
        if not backend.is_relative_to((opt / "releases").resolve()):
            return None
        version = backend / "shmax" / "VERSION"
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?", version.read_text().strip()):
            return None
        if not (backend / "shmax" / "cli" / "main.py").is_file():
            return None
        if os.geteuid() == 0 and (version.stat().st_uid != 0 or version.stat().st_mode & 0o022):
            raise RuntimeError("accepted release ownership is unsafe")
        return backend
    except (OSError, ValueError):
        return None


def _use_accepted_host_release():
    backend = _accepted_backend(Path("/opt/shmax"))
    if backend is not None and backend != Path(__file__).resolve().parents[2]:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(backend)
        os.execve(sys.executable, [sys.executable, "-P", "-m", "shmax.cli", *sys.argv[1:]], environment)


_use_accepted_host_release()


from .. import __service_name__, __version__
from ..config import Settings
from ..core.paths import PathLayout
from ..database import Database, DatabaseSettings, create_all, run_migrations_sync
from ..diagnostics import Doctor
from ..logging_setup import configure_logging
from ..queue.repository import DeliveryQueueRepository
from ..routing.repository import RoutesRepository
from ..security import SecretStore


def _default_data_dir() -> Path:
    raw = os.environ.get("SHMAX_DATA_DIR")
    if raw:
        return Path(raw).expanduser().resolve()
    return Path("/var/lib/shmax")


def _build_database() -> Database:
    settings = DatabaseSettings(data_dir=_default_data_dir())
    return Database(settings)


def _print_kv(label: str, value: str) -> None:
    print(f"  {label:18} {value}")


def cmd_version(_args: argparse.Namespace) -> int:
    print(f"{__service_name__} {__version__}")
    print(f"python {platform.python_version()}")
    return 0


async def _cmd_status(_args: argparse.Namespace) -> int:
    layout = PathLayout(_default_data_dir())
    layout.ensure()
    db = _build_database()
    try:
        await asyncio.to_thread(run_migrations_sync, db.settings.url)
        async with db.session() as s:
            counts = await DeliveryQueueRepository(s).counts_by_state()
    finally:
        await db.dispose()
    secrets = SecretStore(_default_data_dir() / "secrets")
    print(f"SHMAX {__version__}")
    _print_kv("data_dir:", str(_default_data_dir()))
    _print_kv("persistent:", f"{len(layout.all_persistent())} subdirs")
    _print_kv(
        "queue:",
        f"pending={counts.get('pending',0)} retry={counts.get('retry',0)} "
        f"failed={counts.get('failed',0)} delivered={counts.get('delivered',0)}",
    )
    _print_kv("master_key:", "yes" if secrets.has_master_key() else "no")
    return 0


async def _cmd_doctor(_args: argparse.Namespace) -> int:
    layout = PathLayout(_default_data_dir())
    layout.ensure()
    db = _build_database()
    try:
        await asyncio.to_thread(run_migrations_sync, db.settings.url)
        secrets = SecretStore(_default_data_dir() / "secrets")
        doctor = Doctor(_default_data_dir(), db, secrets)
        report = await doctor.run()
    finally:
        await db.dispose()
    for r in report.results:
        marker = {
            "ok": "✓",
            "warning": "!",
            "error": "✗",
            "not_configured": "—",
            "not_tested": "?",
        }.get(r.status.value, "?")
        print(f"{marker:1} {r.name:24} {r.status.value:16} {r.detail}")
    counts = report.by_status()
    print()
    print("summary: " + " ".join(f"{k.value}={v}" for k, v in counts.items()))
    return 0 if report.ok else 1


async def _cmd_backup(args: argparse.Namespace) -> int:
    layout = PathLayout(_default_data_dir())
    layout.ensure()
    from ..backup.manager import create_backup, create_encrypted_backup

    out = (
        Path(args.output)
        if args.output
        else _default_data_dir()
        / "backups"
        / f"shmax-{int(_dt.datetime.now().timestamp())}.shmax"
    )
    if args.passphrase:
        manifest = create_encrypted_backup(
            data_dir=_default_data_dir(),
            out_path=out,
            passphrase=args.passphrase,
            shmax_version=__version__,
        )
        print(f"encrypted backup written: {out}")
    else:
        manifest = create_backup(
            data_dir=_default_data_dir(),
            out_path=out,
            shmax_version=__version__,
        )
        print(f"backup written: {out}")
    print(f"  format:           {manifest.format}")
    print(f"  version:          {manifest.shmax_version}")
    print(f"  schema revision:  {manifest.schema_revision}")
    print(f"  entries:          {len(manifest.entries)}")
    print(f"  components:       {', '.join(manifest.included_components)}")
    return 0


async def _cmd_backup_list(_args: argparse.Namespace) -> int:
    from ..backup.manager import list_backups

    rows = list_backups(_default_data_dir())
    if not rows:
        print("no backups")
        return 0
    for row in rows:
        flag = "enc" if row.get("encrypted") else "txt"
        print(
            f"  {row.get('name','?')[:50]:50} "
            f"{row.get('format','?')} {flag:3} "
            f"size={row.get('size',0)} entries={row.get('entries','?')} "
            f"schema={row.get('schema_revision','')}"
        )
    return 0


async def _cmd_restore(args: argparse.Namespace) -> int:
    from ..backup.manager import is_encrypted, restore_backup

    archive = Path(args.backup)
    if not archive.is_file():
        print(f"no such backup: {archive}")
        return 1
    passphrase = None
    if is_encrypted(archive):
        if not args.passphrase:
            print("encrypted backup requires --passphrase")
            return 2
        passphrase = args.passphrase
    manifest = restore_backup(
        archive_path=archive,
        data_dir=_default_data_dir(),
        passphrase=passphrase,
    )
    print(f"restored: {archive.name}")
    print(f"  entries: {len(manifest.entries)}")
    return 0


async def _cmd_update(args: argparse.Namespace) -> int:
    if args.action == "latest":
        from ..host.release_download import download_latest, ReleaseDownloadError
        from ..host.local_update import LocalReleaseUpdater, LocalUpdateError
        import tempfile
        try:
            if os.geteuid() != 0:
                raise ReleaseDownloadError("Run sudo shmax update on the SHMAX host")
            with tempfile.TemporaryDirectory(prefix="shmax-release-") as tmp:
                tag, archive = await asyncio.to_thread(download_latest, Path(tmp))
                print(f"Verified signed stable release: {tag}", flush=True)
                await asyncio.to_thread(LocalReleaseUpdater(data=_default_data_dir()).apply, archive)
        except (ReleaseDownloadError, LocalUpdateError, OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0
    if args.action not in {"check", "apply"}:
        from ..host.local_update import LocalReleaseUpdater, LocalUpdateError
        try:
            await asyncio.to_thread(LocalReleaseUpdater(data=_default_data_dir()).apply, Path(args.action))
        except LocalUpdateError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0
    if args.action == "check":
        from ..update.service import UpdateService

        svc = UpdateService(
            current_version=__version__,
            secret_store=SecretStore(_default_data_dir() / "secrets"),
            db=None,
            data_dir=_default_data_dir(),
        )
        status = await svc.check()
        print(f"current: {status['current_version']}")
        print(f"available: {status.get('latest_known_version') or '—'}")
        print(f"channel: {status['channel']}")
        print(f"status: {status['status']}")
        return 0
    # Live acceptance A6: REAL host-side update transaction. The
    # web container never reaches this code path — the hostd runs
    # `shmax update apply` where /opt/shmax and Docker live.
    if args.action == "apply":
        from ..host.updater import HostUpdater, UpdateError

        updater = HostUpdater(
            current_version=__version__,
            channel=args.channel or "stable",
            data_dir=_default_data_dir(),
        )
        try:
            result = await updater.apply()
        except UpdateError as e:
            print(f"UPDATE FAILED: {e}", file=sys.stderr)
            return 1
        print(f"update complete: {result.version}")
        print(f"  backup:  {result.backup_path}")
        print(f"  stages:  {' -> '.join(result.stages)}")
        return 0
    return 0


async def _cmd_versions(_args: argparse.Namespace) -> int:
    print(f"current: {__version__}")
    return 0


async def _cmd_rollback(_args: argparse.Namespace) -> int:
    from ..host.updater import HostUpdater, UpdateError

    updater = HostUpdater(data_dir=_default_data_dir())
    try:
        result = await updater.rollback_last()
    except UpdateError as e:
        print(f"ROLLBACK FAILED: {e}", file=sys.stderr)
        return 1
    print("rollback complete: previous deployment restored")
    return 0


async def _cmd_logs(args: argparse.Namespace) -> int:
    path = _default_data_dir() / "logs" / "shmax.log"
    if not path.is_file():
        print(f"no log file at {path} yet")
        return 0
    text = path.read_text(errors="replace").splitlines()
    for line in text[-args.tail :]:
        print(line)
    return 0


async def _cmd_setup_link(_args: argparse.Namespace) -> int:
    """Mint a setup token + secret and print the URL.

    Used by the installer (and by operators who want to mint a
    new bootstrap URL after a factory reset). The token is
    written to the ``setup_tokens`` table; the secret is
    written to ``$SHMAX_DATA_DIR/state/setup_secret`` so the
    web UI can be opened from a LAN phone without exposing
    the secret.
    """
    host = os.environ.get("SHMAX_HOST", "0.0.0.0")
    port = int(os.environ.get("SHMAX_PORT", "8787"))
    import socket

    lan = "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        lan = s.getsockname()[0]
        s.close()
    except Exception:
        pass

    # Lazy imports so the CLI works even when the data dir
    # has not been initialised yet.
    from ..config import DatabaseSettings
    from ..database.engine import Database
    from ..auth import SetupTokenService
    from ..database.models import AdminUser
    import secrets as _secrets
    from sqlalchemy import select

    settings = DatabaseSettings.from_env()
    db = Database(settings)
    try:
        # If an admin already exists, refuse to mint.
        async with db.session() as session:
            existing = (await session.execute(select(AdminUser))).first()
            if existing is not None:
                print("admin already exists; refusing to mint a setup token", file=sys.stderr)
                return 1
            svc = SetupTokenService(session)
            info = await svc.create(ttl=_dt.timedelta(hours=12))

        # Mint a setup secret so the LAN client can prove
        # trust to /api/setup/token.
        secret = _secrets.token_urlsafe(32)
        secret_path = settings.data_dir / "state" / "setup_secret"
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        secret_path.write_text(secret)
        try:
            os.chmod(secret_path, 0o600)
        except OSError:
            pass
    finally:
        await db.dispose()

    print(f"http://{lan}:{port}/setup?token={info.token}&secret={secret}")
    return 0


async def _cmd_uninstall(_args: argparse.Namespace) -> int:
    """Dry-run friendly uninstall.

    Default: remove the application (``/opt/shmax``), KEEP
    ``/var/lib/shmax``. With ``--purge``, also remove data
    after confirmation.
    """
    print("uninstall: this is a dry-run print. Run with --purge to wipe data.")
    print("KEEP /var/lib/shmax (your settings, sessions, DB) by default.")
    if _args.purge:
        print("--purge acknowledged. In production this would remove /var/lib/shmax.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shmax", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("version", help="print version").set_defaults(func=cmd_version)
    sub.add_parser("status", help="print runtime status").set_defaults(func=None)
    sub.add_parser("doctor", help="run diagnostics").set_defaults(func=None)

    p = sub.add_parser("backup", help="create a backup archive")
    p.add_argument("--output", help="explicit output path")
    p.add_argument(
        "--passphrase",
        default=None,
        help="if set, encrypts the backup with Argon2id + AES-256-GCM",
    )
    p.set_defaults(func=None)

    sub.add_parser("backup list", help="list local backups").set_defaults(func=None)

    p = sub.add_parser("restore", help="restore a backup")
    p.add_argument("backup", help="path to .shmax archive")
    p.add_argument("--passphrase", default=None, help="passphrase for encrypted backup")
    p.set_defaults(func=None)

    p = sub.add_parser("setup-link", help="print the local setup URL").set_defaults(func=None)

    p = sub.add_parser("update", help="update operations")
    p.add_argument("action", nargs="?", default="latest", help="latest (default), check, apply, or local production release.tar.gz")
    p.add_argument("--channel", choices=["stable", "beta"], default="stable")
    p.set_defaults(func=None)

    sub.add_parser("versions", help="print installed and available versions").set_defaults(func=None)

    sub.add_parser("rollback", help="rollback the last update").set_defaults(func=None)

    p = sub.add_parser("logs", help="tail the local log file")
    p.add_argument("--tail", type=int, default=100)
    p.set_defaults(func=None)

    p = sub.add_parser("uninstall", help="remove the SHMAX application (keeps data by default)")
    p.add_argument("--purge", action="store_true", help="also remove /var/lib/shmax")
    p.set_defaults(func=None)

    return parser


_DISPATCH = {
    "status": _cmd_status,
    "doctor": _cmd_doctor,
    "backup": _cmd_backup,
    "backup list": _cmd_backup_list,
    "restore": _cmd_restore,
    "setup-link": _cmd_setup_link,
    "update": _cmd_update,
    "versions": _cmd_versions,
    "rollback": _cmd_rollback,
    "logs": _cmd_logs,
    "uninstall": _cmd_uninstall,
}


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.func is None:
        coro = _DISPATCH.get(args.cmd)
        if coro is None:
            parser.error(f"unknown command: {args.cmd}")
        return asyncio.run(coro(args))
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover - exercised via ``python -m``
    sys.exit(main())
