"""Bridge engine with ACL enforcement.

Routing decisions are made by the Core after ACL checks. The
bridge still drops:

* echoes of our own outbound messages (marker-based dedup);
* duplicates already in the persistent message_map;
* events from senders whose ACL role does not allow the
  corresponding action.

Replies are looked up in :mod:`shmax.bridge.message_map` and
enqueued with the foreign message id so the destination
adapter can call its native reply API.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from ..adapters.max.base import MaxAdapter
from ..adapters.telegram.base import TelegramAdapter
from ..auth import AclService
from ..bridge.attribution import (
    attribute_caption,
    attribute_text,
    resolve_author_label,
)
from ..bridge.message_map import MessageMapRepository, outbound_marker
from ..domain.enums import Direction
from ..domain.models import NormalizedMessage
from ..queue.repository import DeliveryQueueRepository
from ..routing.repository import RoutesRepository


logger = logging.getLogger(__name__)


_MARKER_RE = re.compile(r"^\u200Bshmax:([0-9a-f]{32})\s*\n?")


@dataclass(frozen=True)
class BridgeConfig:
    max_account_id: int
    telegram_account_id: int
    # The single Telegram chat the bridge is configured to
    # forward from. Audit-mandated fail-closed ACL: events
    # arriving from any other chat are dropped. When this is
    # None (target not configured) EVERY Telegram→MAX send is
    # denied — "not configured" never means "accept everything".
    telegram_target_chat_id: Optional[str] = None
    # Whether the configured target is a forum (topics). When
    # True, only events whose topic maps to a route are accepted.
    telegram_target_forum: bool = False


class BridgeEngine:
    def __init__(
        self,
        *,
        config: BridgeConfig,
        routes: RoutesRepository,
        mapping: MessageMapRepository,
        queue: DeliveryQueueRepository,
        max_adapter: MaxAdapter,
        telegram_adapter: TelegramAdapter,
        users: "TelegramUsersRepository",
        acl: AclService,
        default_permission: str = "send_message",
        on_enqueue: Optional["Callable[[], Awaitable[None]]"] = None,
        media_fetcher: Optional[
            "Callable[[str, object], Awaitable[Optional[object]]]"
        ] = None,
        db=None,
        journal=None,
    ) -> None:
        self.journal = journal
        self._config = config
        self._routes = routes
        self._mapping = mapping
        self._queue = queue
        self._max = max_adapter
        self._tg = telegram_adapter
        self._acl = acl
        self._users = users
        self._default_permission = default_permission
        self._on_enqueue = on_enqueue
        # Live A1: db handle for the outbound native-id registry
        # (echo suppression without visible markers).
        self._db = db
        # P0 #11: async callable ``(source_transport, attachment)
        # -> StoredMedia | None``. The runtime injects one that
        # downloads supported media into the shared MediaPipeline;
        # the bridge degrades gracefully to a controlled text
        # fallback when it is absent or fails.
        self._media_fetcher = media_fetcher

    # ------------------------------------------------------------------
    # P0 #11 — media handling
    # ------------------------------------------------------------------
    _SUPPORTED_MEDIA_KINDS = {"photo", "document", "video", "voice", "sticker"}

    def _split_attachments(self, event: NormalizedMessage) -> tuple[list[dict], list[Attachment], list[Attachment]]:
        """Split event attachments into (unsupported-fallbacks,
        supported-attachments, other) using the unified
        representation. Unsupported types NEVER disappear — they
        become a visible text notice; nothing is silently
        dropped."""
        from ..domain.enums import AttachmentKind

        unsupported: list[Attachment] = []
        supported: list[Attachment] = []
        for a in event.attachments or ():
            kind = a.kind.value if isinstance(a.kind, AttachmentKind) else str(a.kind)
            if kind == "audio":
                # Audio maps onto the canonical "voice" delivery path.
                supported.append(a)
            elif kind in self._SUPPORTED_MEDIA_KINDS:
                supported.append(a)
            else:
                unsupported.append(a)
        return unsupported, supported, []

    async def _media_entries_for(
        self, *, source: str, supported: list, event: NormalizedMessage
    ) -> tuple[list[dict], list[str]]:
        """Fetch supported attachments through the media fetcher.

        Returns ``(media_entries, failure_labels)``. Each media
        entry is the unified payload dict the queue/worker
        contract understands; failures come back as human-readable
        labels so the fallback text can name what happened.
        """
        media_entries: list[dict] = []
        failures: list[str] = []
        for a in supported:
            label = a.kind.value if hasattr(a.kind, "value") else str(a.kind)
            if self._media_fetcher is None:
                failures.append(label)
                continue
            try:
                stored = await self._media_fetcher(source, a)
            except Exception as e:  # noqa: BLE001
                from ..media.diagnostics import MediaFailure
                import hashlib
                failure = e if isinstance(e, MediaFailure) else MediaFailure("media.fetch", "unexpected_failure")
                logger.warning("media direction=%s category=%s operation=%s exception=%s reason=%s correlation=%s outcome=MEDIA_FALLBACK",
                               source + "_to_" + ("max" if source == "telegram" else "telegram"), label,
                               failure.operation, type(e.__cause__ or e).__name__, failure.reason,
                               hashlib.sha256(str(event.external_id).encode()).hexdigest()[:12])
                stored = None
            if stored is None:
                failures.append(label)
                continue
            media_entries.append(
                {
                    "media_ref": stored.media_ref,
                    "media_kind": stored.kind,
                    "media_filename": stored.filename,
                    "media_mime": stored.mime,
                    "media_size": stored.size,
                    "media_sha256": stored.sha256,
                    "caption": a.caption,
                }
            )
        return media_entries, failures

    # Human names for known attachment kinds (live acceptance #3).
    # Internal enum values must NEVER reach the visible fallback.
    _FALLBACK_LABELS = {
        "sticker": "стикер",
        "poll": "опрос",
        "voice": "голосовое сообщение",
        "audio": "аудио",
        "video": "видео",
        "photo": "фото",
        "document": "файл",
        "contact": "контакт",
        "location": "геолокация",
        "text": "вложение",
    }

    @classmethod
    def _fallback_text(cls, labels: list[str]) -> str:
        """Controlled, user-visible fallback for unsupported or
        undeliverable attachments (audit P0 #11).

        Humanized (live acceptance #3): a known kind renders its
        Russian name; generic/unknown kinds (e.g. internal
        ``other``) render NO type at all — internal enum values
        never reach the Telegram/MAX user."""
        names: list[str] = []
        for raw in sorted(set(labels)):
            human = cls._FALLBACK_LABELS.get(str(raw).strip().lower())
            if human and human not in names:
                names.append(human)
        if names:
            return f"[Неподдерживаемое вложение: {', '.join(names)}]"
        return "[Неподдерживаемое вложение]"

    # ------------------------------------------------------------------
    async def handle_max_event(self, event: NormalizedMessage) -> bool:
        return await self._handle_event(event, direction=Direction.MAX_TO_TELEGRAM)

    async def handle_telegram_event(self, event: NormalizedMessage) -> bool:
        return await self._handle_event(event, direction=Direction.TELEGRAM_TO_MAX)

    async def _handle_event(self, event: NormalizedMessage, *, direction: Direction) -> bool:
        body, marker = _strip_marker(event.text)
        if marker is not None:
            existing = await self._mapping.find_by_marker(marker)
            if existing is not None:
                logger.debug("drop %s echo of our own message (marker=%s)", direction.value, marker)
                return False

        # Live A1: internal echo suppression by native id registry.
        if await self._is_own_echo(event):
            logger.debug(
                "drop %s echo of our own send (native id=%s)",
                direction.value, event.external_id,
            )
            return False

        # Audit-mandated fail-closed ACL for TG→MAX. Per the audit:
        # the operator's MAX account must NOT be allowed to send
        # unless EVERY check passes. We refuse:
        #   - events when no target group is configured
        #   - events from chats other than the configured target group
        #   - DMs / channels / anything that is not the target group
        #   - events from unknown (unmapped) topics — no silent
        #     chat-level fallback inside a forum target
        #   - the system topic (notifications channel, not bridge
        #     traffic)
        #   - unregistered users (TelegramUser row missing)
        #   - unapproved users
        #   - blocked users
        #   - users whose role does not include send_message
        if direction == Direction.TELEGRAM_TO_MAX:
            if not await self._tg_to_max_acl(event):
                return False

        # MAX→TG is NOT subject to the Telegram principal ACL.
        # Audit P0 #6: the MAX sender identity is MESSAGE
        # ATTRIBUTION, not a Telegram authorization principal —
        # requiring a TelegramUser row for a MAX sender was
        # fundamentally wrong. MAX→TG authorization is: a valid
        # MAX event, a mapped MAX chat route, and an active
        # Telegram target (route resolution below enforces the
        # latter two).
        if direction == Direction.TELEGRAM_TO_MAX:
            sender_principal = self._resolve_sender_principal(event, direction)
            decision = await self._acl.check(
                principal=sender_principal,
                permission="send_message",
                scope="chat",
                scope_ref=event.chat.external_id,
            )
            if not decision.allowed:
                logger.info(
                    "ACL denied %s sender=%s chat=%s permission=%s reason=%s",
                    direction.value,
                    sender_principal,
                    event.chat.external_id,
                    "send_message",
                    decision.reason,
                )
                return False

        # Check duplicate external id in the mapping.
        known = await self._find_existing(direction, event)
        if known is not None:
            if self.journal:self.journal.emit("message.duplicate",direction=direction.value)
            logger.debug("drop duplicate %s event id=%s", direction.value, event.external_id)
            return False

        route = await self._resolve_route(direction, event)
        if route is None or not route.enabled:
            return False

        if direction == Direction.MAX_TO_TELEGRAM:
            reply_to_telegram_message_id = await self._resolve_reply_target(
                event=event, dest_transport="telegram"
            )
            media, fallback = await self._prepare_media(event, "max")
            effective_text = self._suppress_structural_caption(
                event, media, body
            )
            await self._enqueue_max_to_tg(
                event=event,
                text=effective_text,
                route=route,
                reply_to_telegram_message_id=reply_to_telegram_message_id,
                media=media,
                fallback=fallback,
            )
        else:
            reply_to_max_message_id = await self._resolve_reply_target(
                event=event, dest_transport="max"
            )
            media, fallback = await self._prepare_media(event, "telegram")
            effective_text = self._suppress_structural_caption(
                event, media, body
            )
            await self._enqueue_tg_to_max(
                event=event,
                text=effective_text,
                route=route,
                reply_to_max_message_id=reply_to_max_message_id,
                media=media,
                fallback=fallback,
            )
        return True

    @staticmethod
    def _suppress_structural_caption(event: NormalizedMessage, media: Optional[dict], body: str) -> str:
        """STRUCTURAL caption dedup (surgical review).

        The adapters mark, at the source boundary, when the message
        text IS the media caption (Bot API: distinct ``text`` /
        ``caption`` fields, mutually exclusive; Telethon: the media
        caption is exposed as ``message``). Only when that
        structural flag is set AND the media entry carries the same
        pipeline-owned caption field is the body suppressed — it is
        the same source representation, delivered once as the media
        caption.

        Independent bodies ("Please look at the second photo"),
        attributions ("Bob: …") and fallback lines are NEVER
        touched: no lexical search happens in composed text.
        """
        if media is None:
            return body
        if not isinstance(event.metadata, dict):
            return body
        if event.metadata.get("text_is_media_caption") is not True:
            return body
        media_caption = str(media.get("caption") or "").strip()
        if media_caption and media_caption == body.strip():
            return ""
        return body

    async def _prepare_media(self, event: NormalizedMessage, source: str):
        """Resolve the event's attachments into (media entry,
        fallback text).

        Multiple MAX attachments remain ordered references for durable worker
        delivery. Single-item and Telegram-origin messages retain the existing
        path. Only genuinely unsupported kinds become unsupported notices."""
        if not event.attachments:
            return None, None
        unsupported, supported, _ = self._split_attachments(event)
        if unsupported and getattr(self,"journal",None):self.journal.emit("attachment.unsupported",component=source)
        labels: list[str] = []
        for a in unsupported:
            labels.append(a.kind.value if hasattr(a.kind, "value") else str(a.kind))
        if source == "max" and len(supported) > 1:
            # Keep every supported reference. Downloads happen in the durable
            # worker so one transient download failure cannot discard photos.
            items = [{"kind": a.kind.value if hasattr(a.kind, "value") else str(a.kind),
                      "payload": dict(a.payload), "caption": a.caption} for a in supported]
            return {"media_items": items, "caption": supported[0].caption}, self._fallback_text(labels) if labels else None
        media_entry: Optional[dict] = None
        if supported:
            entries, failures = await self._media_entries_for(
                source=source, supported=supported, event=event
            )
            labels.extend(failures)
            if entries:
                media_entry = entries[0]
                if len(entries) > 1:
                    labels.append("вложение (сверх первого)")
        fallback = self._fallback_text(labels) if labels else None
        return media_entry, fallback

    async def _resolve_reply_target(
        self, *, event: NormalizedMessage, dest_transport: str
    ) -> Optional[str]:
        """Translate ``event.reply_to`` into the message id of the
        destination transport.

        ``event.reply_to`` may point at either side of the bridge:

        * ``dest_transport`` — used directly, no lookup;
        * the source transport — looked up in the message_map.

        Returns ``None`` when the reply cannot be resolved; the
        worker then sends a plain message.
        """
        if event.reply_to is None:
            return None
        ref = event.reply_to
        if ref.transport == dest_transport:
            return ref.message_external_id
        if ref.transport == "max":
            mapped = await self._mapping.find_by_max(
                max_account_id=self._config.max_account_id,
                max_chat_id=ref.chat_external_id or event.chat.external_id,
                max_message_id=ref.message_external_id,
            )
            if mapped is not None and mapped.telegram_message_id is not None:
                return mapped.telegram_message_id
            return None
        if ref.transport == "telegram":
            # The reply ref may not carry the forum topic (the
            # normalizer builds it from the raw message) — fall
            # back to the EVENT's topic so the mapping lookup
            # matches rows created from forum traffic.
            ref_thread = ref.thread_id
            if ref_thread is None and isinstance(event.metadata, dict):
                ref_thread = event.metadata.get("thread_id")
            mapped = await self._mapping.find_by_telegram(
                telegram_account_id=self._config.telegram_account_id,
                telegram_chat_id=ref.chat_external_id or event.chat.external_id,
                telegram_thread_id=ref_thread,
                telegram_message_id=ref.message_external_id,
            )
            if mapped is not None and mapped.max_message_id is not None:
                return mapped.max_message_id
            return None
        return None

    async def _is_own_echo(self, event: NormalizedMessage) -> bool:
        """True when the event's native (transport, chat, message id)
        matches one of OUR registered outbound sends (live A1)."""
        if self._db is None or not event.external_id:
            return False
        from .sent_registry import SentNativeRepository

        async with self._db.session() as s:
            return await SentNativeRepository(s).exists(
                transport=event.source,
                chat_external_id=event.chat.external_id,
                message_external_id=event.external_id,
            )

    def _resolve_sender_principal(self, event: NormalizedMessage, direction: Direction) -> str:
        """Resolve the ACL principal for the inbound event.

        V1 fail-closed posture: if the event has no sender at
        all, we return an empty principal so the ACL layer
        reports ``unknown_principal``. We do NOT default to
        ``operator`` — that would let any unidentified sender
        post into the bridge.

        Operators pair their Telegram account via
        ``/shmax_connect <code>``; once paired, the
        ``TelegramUser`` row holds a role that becomes the
        principal for subsequent ACL checks.
        """
        if event.sender is None:
            return ""
        return event.sender.external_id

    async def _tg_to_max_acl(self, event: NormalizedMessage) -> bool:
        """Audit-mandated fail-closed ACL for TG→MAX sends.

        Default is DENY. We allow the event through ONLY if
        every check below passes:

          0. A target group IS configured — "not configured"
             denies everything, it never means "accept all".
          1. The inbound chat IS the configured target group
             (not a DM, not a sibling group, not a channel).
          2. The event arrives on a KNOWN MAPPED TOPIC when the
             target is a forum — unknown topics never fall back
             to a chat-level route, and the system topic is
             excluded from bridge traffic.
          3. Sender has a TelegramUser row (must have run
             /start at least once).
          4. ``approved`` is True.
          5. ``blocked`` is False.
          6. ``role`` includes ``send_message``.
        """
        from ..domain.enums import ChatKind

        # 0. Fail-closed when no target is configured.
        target_group = self._config.telegram_target_chat_id
        if not target_group:
            logger.info(
                "ACL denied TG→MAX: no target group configured "
                "(fail-closed; chat=%s)",
                event.chat.external_id,
            )
            return False
        # 1. Only the configured target group; only real group
        # traffic. ChatKind.DIALOG is the V1 representation of a
        # 1:1 conversation; CHANNEL is broadcast — neither is
        # bridge traffic.
        if event.chat.external_id != target_group:
            logger.info(
                "ACL denied TG→MAX: chat=%s does not match target=%s",
                event.chat.external_id,
                target_group,
            )
            return False
        if event.chat.kind != ChatKind.GROUP:
            logger.info(
                "ACL denied TG→MAX: chat kind %s is not group traffic",
                event.chat.kind,
            )
            return False
        if event.sender is None or not event.sender.external_id:
            logger.info("ACL denied TG→MAX: no sender identity")
            return False
        # 2. Topic mapping. The engine resolves the route in
        # _resolve_route; here we only enforce the no-silent-
        # fallback rule by checking that a forum event's topic is
        # known BEFORE the user ACL (cheap informational logs).
        users = getattr(self, "_users", None)
        if users is None:
            logger.warning(
                "ACL denied TG→MAX: users repository not wired"
            )
            return False
        row = await users.get(event.sender.external_id)
        if row is None:
            logger.info(
                "ACL denied TG→MAX: user=%s not registered",
                event.sender.external_id,
            )
            return False
        if row.blocked:
            logger.info(
                "ACL denied TG→MAX: user=%s is blocked", row.user_id
            )
            return False
        if not row.approved:
            logger.info(
                "ACL denied TG→MAX: user=%s is not approved", row.user_id
            )
            return False
        if "send_message" not in (row.role or ""):
            # Role here is one of Role.OWNER/ADMIN/OPERATOR/READ_ONLY;
            # the AclService resolves the default permission set from
            # that enum value. We replicate the lookup here so the
            # audit-mandated ACL can answer yes/no without a second
            # AclService round-trip, but the source of truth is the
            # DEFAULT_PERMISSIONS table in shmax.auth.pairing.
            from ..auth.pairing import DEFAULT_PERMISSIONS
            from ..domain.enums import Role

            try:
                role_enum = Role(row.role)
            except ValueError:
                logger.info(
                    "ACL denied TG→MAX: user=%s role=%r is not a known Role",
                    row.user_id,
                    row.role,
                )
                return False
            perms = DEFAULT_PERMISSIONS.get(role_enum, set())
            if "send_message" not in perms:
                logger.info(
                    "ACL denied TG→MAX: user=%s role=%s lacks send_message",
                    row.user_id,
                    row.role,
                )
                return False
        return True

    async def _find_existing(self, direction: Direction, event: NormalizedMessage):
        if direction == Direction.MAX_TO_TELEGRAM:
            return await self._mapping.find_by_max(
                max_account_id=self._config.max_account_id,
                max_chat_id=event.chat.external_id,
                max_message_id=event.external_id,
            )
        return await self._mapping.find_by_telegram(
            telegram_account_id=self._config.telegram_account_id,
            telegram_chat_id=event.chat.external_id,
            telegram_thread_id=None,
            telegram_message_id=event.external_id,
        )

    async def _resolve_route(self, direction: Direction, event: NormalizedMessage):
        if direction == Direction.MAX_TO_TELEGRAM:
            return await self._routes.find_by_max(
                max_account_id=self._config.max_account_id,
                max_chat_id=event.chat.external_id,
            )
        # TG→MAX route resolution. Audit P0 #7: never silently
        # fall back from an unknown topic. When the configured
        # target is a forum, the event's topic must map to a
        # route EXACTLY — an unmapped topic (including the
        # system topic, label="system") yields no route and the
        # event is dropped.
        thread_id = None
        if isinstance(event.metadata, dict):
            thread_id = event.metadata.get("thread_id")
        if thread_id is None and self._config.telegram_target_forum:
            logger.info(
                "ACL denied TG→MAX: forum target requires a known topic"
            )
            return None
        if thread_id is not None:
            route = await self._routes.find_by_telegram(
                telegram_account_id=self._config.telegram_account_id,
                telegram_chat_id=event.chat.external_id,
                telegram_thread_id=str(thread_id),
            )
            if route is not None:
                if getattr(route, "label", None) == "system":
                    logger.info(
                        "ACL denied TG→MAX: system topic is not bridge traffic"
                    )
                    return None
                return route
            # Unknown topic inside a forum target — fail closed.
            if self._config.telegram_target_forum:
                logger.info(
                    "ACL denied TG→MAX: unmapped topic %s (no chat-level fallback)",
                    thread_id,
                )
                return None
        route = await self._routes.find_by_telegram(
            telegram_account_id=self._config.telegram_account_id,
            telegram_chat_id=event.chat.external_id,
            telegram_thread_id=None,
        )
        if route is not None and getattr(route, "label", None) == "system":
            return None
        return route

    async def on_delivery_delivered(self, *, queue_id: int, payload: dict) -> None:
        direction = payload.get("direction")
        if direction == Direction.MAX_TO_TELEGRAM.value:
            await self._mapping.create_for_max_to_tg(
                max_account_id=payload["max_account_id"],
                max_chat_id=payload["max_chat_id"],
                max_message_id=payload["max_message_id"],
                telegram_account_id=self._config.telegram_account_id,
                telegram_chat_id=payload["target_chat_id"],
                telegram_thread_id=payload.get("target_thread_id"),
                telegram_message_id=str(payload["delivered_external_id"]),
            )
        elif direction == Direction.TELEGRAM_TO_MAX.value:
            await self._mapping.create_for_tg_to_max(
                telegram_account_id=self._config.telegram_account_id,
                telegram_chat_id=payload["source_telegram_chat_id"],
                telegram_thread_id=payload.get("source_telegram_thread_id"),
                telegram_message_id=payload["source_telegram_message_id"],
                max_account_id=self._config.max_account_id,
                max_chat_id=payload["target_chat_id"],
                max_message_id=str(payload["delivered_external_id"]),
            )

    async def _enqueue_max_to_tg(
        self,
        *,
        event: NormalizedMessage,
        text: str,
        route,
        reply_to_telegram_message_id: Optional[str],
        media: Optional[dict] = None,
        fallback: Optional[str] = None,
    ) -> None:
        marker = outbound_marker()
        # Live acceptance #1/#2: the internal marker travels in the
        # queue payload / DB mapping ONLY — never in the visible
        # Telegram text. The MAX author attribution is composed via
        # the canonical helper, exactly once.
        author = resolve_author_label(
            persisted_nickname=None,
            telegram_first_name=getattr(event.sender, "display_name", None),
            telegram_username=None,
        )
        body = text or ""
        if text and author.label:
            body = attribute_text(text=text, label=author.label)
        elif not text and author.label and media is not None and str(
            media.get("caption") or ""
        ).strip():
            # Body suppressed (structural caption dedup): attribute
            # the media caption instead so Telegram still sees the
            # author exactly once.
            media["caption"] = attribute_caption(
                caption=str(media["caption"]), label=author.label
            )
            body = ""
        if media is not None and media.get("media_kind") == "sticker" and not body and author.label:
            body = author.label

        if media is not None and "media_items" in media:
            initial = str(media.get("caption") or "")
            if body:
                album_caption = body + ("\n" + initial if initial else "")
            else:
                album_caption = attribute_caption(caption=initial, label=author.label) or ""
            caption_fits = len(album_caption.encode("utf-16-le")) // 2 <= 1024
            media["album_caption"] = album_caption if caption_fits else (author.label or "")
            body = "" if caption_fits else album_caption
        if fallback:
            body = f"{body}\n{fallback}" if body else fallback
        payload = {
            "direction": Direction.MAX_TO_TELEGRAM.value,
            "media_outcome": "MEDIA_PENDING" if media else ("MEDIA_FALLBACK" if fallback else "NONE"),
            "max_account_id": self._config.max_account_id,
            "max_chat_id": event.chat.external_id,
            "max_message_id": event.external_id,
            "telegram_account_id": self._config.telegram_account_id,
            "text": body,
            "target_chat_id": route.telegram_chat_id,
            "target_thread_id": route.telegram_thread_id,
            "marker": marker,
            "reply_to_telegram_message_id": reply_to_telegram_message_id,
            # Unified attachment representation (P0 #11): the queue
            # payload and the delivery worker agree on kind + ref.
            "attachments": [
                {
                    "kind": a.kind.value if hasattr(a.kind, "value") else str(a.kind),
                    "payload": a.payload,
                    "caption": a.caption,
                }
                for a in event.attachments
            ],
        }
        if media is not None:
            payload.update(media)
        from ..journal import correlation
        payload["journal_correlation"] = correlation.get()
        idem = f"max:{event.chat.external_id}:{event.external_id}:max_to_telegram"
        await self._queue.enqueue(
            direction=payload["direction"],
            target_chat_id=payload["target_chat_id"],
            payload=payload,
            target_thread_id=route.telegram_thread_id,
            idempotency_key=idem,
        )
        if self.journal:self.journal.emit("message.queued",direction=payload["direction"])
        if self._on_enqueue is not None:
            await self._on_enqueue()

    async def _enqueue_tg_to_max(
        self,
        *,
        event: NormalizedMessage,
        text: str,
        route,
        reply_to_max_message_id: Optional[str],
        media: Optional[dict] = None,
        fallback: Optional[str] = None,
    ) -> None:
        marker = outbound_marker()
        # Resolve the author label once per send — see
        # shmax.bridge.attribution for the priority rules. We
        # persist the resolved attribution in the queued payload, so retries
        # keep the same label even if the administrator changes it later.
        # Resolve from the persisted user, not caller-supplied event metadata.
        row = await self._users.get(event.sender.external_id) if event.sender else None
        author = resolve_author_label(
            persisted_nickname=row.nickname if row else None,
            telegram_first_name=(getattr(event.sender, "display_name", None)
                                 or (row.display_name if row else None)),
            telegram_username=(getattr(event.sender, "handle", None)
                               or (row.username if row else None)),
        )
        if text:
            attributed = attribute_text(text=text, label=author.label, inline=True)
            body = attributed
        else:
            # Body suppressed (structural caption dedup): attribution
            # is an INDEPENDENT semantic field — compose it into the
            # media caption via the canonical helper so MAX receives
            # BOTH the author and the caption, exactly once. The
            # original caption is NOT also sent as body.
            if (
                media is not None
                and author.label
            ):
                media["caption"] = attribute_caption(
                    caption=media.get("caption"), label=author.label, inline=True
                )
            body = ""
        if fallback:
            body = f"{body}\n{fallback}" if body else (
                fallback if media is not None else
                attribute_text(text=fallback, label=author.label, inline=True)
            )
        payload = {
            "direction": Direction.TELEGRAM_TO_MAX.value,
            "media_outcome": "MEDIA_PENDING" if media else ("MEDIA_FALLBACK" if fallback else "NONE"),
            "source_telegram_chat_id": event.chat.external_id,
            "source_telegram_thread_id": (
                event.metadata.get("thread_id")
                if isinstance(event.metadata, dict)
                else None
            ),
            "source_telegram_message_id": event.external_id,
            "telegram_account_id": self._config.telegram_account_id,
            "max_account_id": self._config.max_account_id,
            # Live acceptance #1: marker ONLY in queue/mapping
            # metadata — the visible MAX text never carries it.
            "text": body,
            "target_chat_id": route.max_chat_id,
            "target_thread_id": None,
            "marker": marker,
            "reply_to_max_message_id": reply_to_max_message_id,
            "author_label": author.label,
            "attachments": [
                {
                    "kind": a.kind.value if hasattr(a.kind, "value") else str(a.kind),
                    "payload": a.payload,
                    "caption": a.caption,
                }
                for a in event.attachments
            ],
        }
        if media is not None:
            payload.update(media)
        from ..journal import correlation
        payload["journal_correlation"] = correlation.get()
        idem = f"tg:{event.chat.external_id}:{event.external_id}:tg_to_max"
        await self._queue.enqueue(
            direction=payload["direction"],
            target_chat_id=payload["target_chat_id"],
            payload=payload,
            target_thread_id=None,
            idempotency_key=idem,
        )
        if self.journal:self.journal.emit("message.queued",direction=payload["direction"])
        if self._on_enqueue is not None:
            await self._on_enqueue()


def _strip_marker(text: str) -> tuple[str, Optional[str]]:
    if not text:
        return text, None
    match = _MARKER_RE.match(text)
    if not match:
        return text, None
    return text[match.end():], match.group(1)


__all__ = ["BridgeEngine", "BridgeConfig"]
