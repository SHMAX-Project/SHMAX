"""Auxiliary, bounded MTProto BOT participant discovery.

A fresh in-memory session is authenticated with the existing bot token.
No session file, phone login, receive handler or human authentication exists.
The Bot API bridge retains its sole polling worker.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
from dataclasses import asdict, dataclass

from sqlalchemy.dialects.sqlite import insert
from sqlalchemy import select

from ..active_target import get_active_target
from ..database.models import Setting
from ..users.repository import TelegramUsersRepository

PAGE_SIZE = 200
MAX_PARTICIPANTS = 20_000
SYNC_TIMEOUT_SECONDS = 180
FLOOD_WAIT_CAP_SECONDS = 30
API_ID_KEY = "tg_member_sync_api_id"
API_HASH_KEY = "tg_member_sync_api_hash"
STATE_KEY = "tg_member_sync_state"


@dataclass
class SyncStats:
    last_sync_at: int = 0
    found: int = 0
    new: int = 0
    updated: int = 0
    status: str = "not_checked"
    group_id: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class MemberSyncError(Exception):
    pass


class MemberSyncService:
    def __init__(self, *, db, secret_store, client_factory=None, **_):
        self._db = db
        self._secrets = secret_store
        self._client_factory = client_factory
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._closed = False
        self._runs: set[asyncio.Task] = set()
        self._reschedule = False
        self._pending_groups = set()

    def configured(self) -> bool:
        return all(self._secrets.has_secret(k) for k in
                   (API_ID_KEY, API_HASH_KEY, "telegram_bot_token"))

    def save_credentials(self, api_id: int, api_hash: str) -> None:
        self._secrets.set_secret(API_ID_KEY, str(api_id))
        if api_hash:
            self._secrets.set_secret(API_HASH_KEY, api_hash)

    async def state(self, group_id: str | None = None) -> dict:
        async with self._db.session() as s:
            row = await s.get(Setting, STATE_KEY+(':'+group_id if group_id else ''))
            stats = json.loads(row.value) if row else SyncStats().to_dict()
        return {**stats, "configured": self.configured(), "running": self._lock.locked(),
                "api_id": int(self._secrets.get_secret(API_ID_KEY))
                if self._secrets.has_secret(API_ID_KEY) else None,
                "api_hash_saved": self._secrets.has_secret(API_HASH_KEY)}

    async def _save_state(self, stats: SyncStats):
        async with self._db.session() as s:
            value = json.dumps(stats.to_dict())
            await s.execute(insert(Setting).values(key=STATE_KEY, value=value)
                            .on_conflict_do_update(index_elements=[Setting.key], set_={"value": value}))
            if stats.group_id:
                await s.execute(insert(Setting).values(key=STATE_KEY+':'+stats.group_id,value=value)
                    .on_conflict_do_update(index_elements=[Setting.key],set_={'value':value}))
            await s.commit()

    def schedule(self, delay: float = 0, *, group_id: str | None = None):
        if self._closed or not self.configured():
            return
        self._pending_groups.add(group_id)
        if self._task and not self._task.done():
            self._reschedule = True
            return

        async def run():
            await asyncio.sleep(delay)
            while not self._closed:
                self._reschedule = False
                try:
                    pending=self._pending_groups.copy();self._pending_groups.clear()
                    if None in pending:
                        from ..database.models import TelegramTarget
                        from ..group_access import target_ready
                        async with self._db.session() as session:
                            targets=(await session.scalars(select(TelegramTarget))).all()
                        pending={t.chat_id for t in targets if target_ready(t)}
                    for selected in sorted(pending):await self.sync(selected)
                except Exception:
                    import logging
                    logging.getLogger(__name__).warning("Member sync could not persist its result")
                if not self._reschedule and not self._pending_groups:
                    break
        self._task = asyncio.create_task(run(), name="telegram-member-sync")

    async def close(self):
        self._closed = True
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        runs = list(self._runs)
        for task in runs:
            task.cancel()
        await asyncio.gather(*runs, return_exceptions=True)

    def _make_client(self, transport=None):
        from telethon import TelegramClient
        from telethon.sessions import MemorySession
        network = {}
        transport = transport or {}
        proxy = transport.get("proxy")
        mtproxy = transport.get("mtproxy")
        if isinstance(mtproxy, dict):
            from telethon.network.connection.tcpmtproxy import ConnectionTcpMTProxyRandomizedIntermediate
            network = {"connection": ConnectionTcpMTProxyRandomizedIntermediate,
                       "proxy": (mtproxy["host"], mtproxy["port"], mtproxy["secret"])}
        elif isinstance(proxy, dict):
            if proxy["scheme"] not in {"socks5", "socks4", "http"}:
                raise MemberSyncError("unsupported_proxy")
            network = {"proxy": {"proxy_type": proxy["scheme"], "addr": proxy["host"],
                                 "port": proxy["port"], "username": proxy.get("username"),
                                 "password": proxy.get("password"), "rdns": True}}
        return TelegramClient(
            MemorySession(), int(self._secrets.get_secret(API_ID_KEY)),
            self._secrets.get_secret(API_HASH_KEY), receive_updates=False,
            auto_reconnect=False, connection_retries=1, request_retries=0,
            flood_sleep_threshold=0, timeout=20, **network,
        )

    async def _enumerate(self, group_id: str):
        from telethon.tl.functions.channels import GetParticipantsRequest
        from telethon.tl.types import (ChannelParticipantsRecent,
                                       ChannelParticipantAdmin, ChannelParticipantCreator)
        token = self._secrets.get_secret("telegram_bot_token")
        if self._client_factory:
            client = self._client_factory()
        else:
            from ..startup_restore import load_telegram_transport
            transport = await load_telegram_transport(self._db, self._secrets)
            client = self._make_client(transport)
        try:
            await client.connect()
            # sign_in(bot_token=...) uses auth.importBotAuthorization. A fresh
            # MemorySession cannot silently reuse a human identity.
            me = await client.sign_in(bot_token=token)
            if not me.bot or str(me.id) != token.split(":", 1)[0]:
                raise MemberSyncError("identity_mismatch")
            entity = await client.get_input_entity(int(group_id))
            offset = 0
            retried = False
            users = {}
            while True:
                request = GetParticipantsRequest(entity, ChannelParticipantsRecent(),
                                                 offset, PAGE_SIZE, hash=0)
                try:
                    page = await client(request)
                except Exception as exc:
                    from telethon.errors import FloodWaitError
                    if isinstance(exc, FloodWaitError):
                        if retried or not 0 < exc.seconds <= FLOOD_WAIT_CAP_SECONDS:
                            raise MemberSyncError("rate_limited") from None
                        retried = True
                        await asyncio.sleep(exc.seconds)
                        continue
                    raise
                for participant in page.participants:
                    user = next((u for u in page.users if u.id == participant.user_id), None)
                    if user is None or user.bot or user.id == me.id:
                        continue
                    owner = isinstance(participant, ChannelParticipantCreator)
                    admin = owner or isinstance(participant, ChannelParticipantAdmin)
                    users[str(user.id)] = dict(
                        user_id=str(user.id), username=user.username,
                        display_name=" ".join(x for x in (user.first_name, user.last_name) if x) or None,
                        membership="owner" if owner else "admin" if admin else "member",
                        is_admin=admin, source="sync")
                size = len(page.participants)
                offset += size
                if size == 0 and offset < page.count:
                    raise MemberSyncError("incomplete")
                if offset >= page.count:
                    return list(users.values())
                if offset >= MAX_PARTICIPANTS:
                    raise MemberSyncError("participant_limit")
        finally:
            # Disconnect completes even when a request or startup is cancelled.
            task = asyncio.create_task(client.disconnect())
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise

    async def sync(self, group_id: str | None = None) -> SyncStats:
        task = asyncio.current_task()
        self._runs.add(task)
        try:
            return await self._sync(group_id)
        finally:
            self._runs.discard(task)

    async def _sync(self, group_id: str | None = None) -> SyncStats:
        if self._closed:
            return SyncStats(status="stopped")
        if self._lock.locked():
            return SyncStats(status="running")
        async with self._lock:
            stats = SyncStats(last_sync_at=int(dt.datetime.now(dt.timezone.utc).timestamp()))
            if not self.configured():
                stats.status = "not_configured"
            else:
                target = await get_active_target(self._db)
                group_id = group_id or (target.chat_id if target and target.validated_at else None)
                from ..group_access import find_target, target_ready
                target = await find_target(self._db,group_id) if group_id else None
                if not target_ready(target):group_id=None
                stats.group_id=group_id
                if not group_id:
                    stats.status = "not_paired"
                else:
                    try:
                        async with asyncio.timeout(SYNC_TIMEOUT_SECONDS):
                            participants = await self._enumerate(group_id)
                            if target:
                                current = await find_target(self._db,group_id)
                                if not current or str(current.chat_id) != str(group_id):
                                    raise MemberSyncError("target_changed")
                            repo = TelegramUsersRepository(self._db)
                            for participant in participants:
                                _, created, changed = await repo.observe_member(**participant)
                                from ..group_access import observe
                                await observe(self._db,target.id,participant['user_id'],membership=participant['membership'],
                                              is_admin=participant['is_admin'],source='sync')
                                stats.found += 1
                                stats.new += int(created)
                                stats.updated += int(changed)
                        stats.status = "ok"
                    except MemberSyncError as exc:
                        stats.status = str(exc)
                    except TimeoutError:
                        stats.status = "timeout"
                    except ImportError:
                        stats.status = "unavailable"
                    except Exception:
                        # Never log exception text; upstream errors can contain credentials.
                        stats.status = "telegram_error"
            await self._save_state(stats)
            return stats


async def observe_membership(db, *, chat_id: str, user_id: str, username,
                             display_name, status: str, is_member: bool = False):
    from ..group_access import find_target, target_ready, observe
    target = await find_target(db,str(chat_id))
    if not target_ready(target):
        return
    membership = {"creator": "owner", "administrator": "admin", "member": "member",
                  "left": "left", "kicked": "kicked", "restricted": "member" if is_member else "left"}.get(status)
    if membership:
        await TelegramUsersRepository(db).observe_member(
            user_id=user_id, username=username, display_name=display_name,
            membership=membership, is_admin=membership in {"owner", "admin"}, source="event")
        await observe(db,target.id,user_id,membership=membership,is_admin=membership in {'owner','admin'},source='event')
