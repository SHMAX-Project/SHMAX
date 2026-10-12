"""Ordered multi-attachment delivery with durable confirmed-send checkpoints.

Telegram groups succeed as a unit. No blind group-to-single retry after timeout:
Telegram supplies no idempotency key, so ambiguous remote success can duplicate.
"""
from pathlib import Path
from types import SimpleNamespace

from ..domain.enums import AttachmentKind
from ..domain.models import Attachment


async def deliver_album(*, payload, chat_id, thread_id, reply_to, adapter, fetch, save, terminal=False):
    progress = payload.setdefault('album_progress', {'next': 0, 'ids': [], 'failed': [], 'caption_sent': False, 'followup_sent': False})
    items = payload['media_items']
    caption = payload.get('album_caption', '')
    while progress['next'] < len(items):
        start = progress['next']
        batch, fault = [], None
        try:
            for index in range(start, min(start+10, len(items))):
                item = items[index]
                if batch and (item['kind'] != 'photo' or batch[0][1] != 'photo'):
                    break
                try:
                    stored = await fetch('max', Attachment(kind=AttachmentKind(item['kind']), payload=item['payload'], caption=item.get('caption')))
                    if stored is None:
                        raise RuntimeError('supported media unavailable')
                    batch.append((stored.media_ref, stored.kind))
                except Exception as exc:
                    fault = exc
                    break
                if stored.kind != 'photo' or not hasattr(adapter, 'send_media_group') or progress.get('single_mode'):
                    break
        except BaseException:
            for ref, kind in batch:
                Path(ref.partition(':')[2]).unlink(missing_ok=True)
            raise
        if batch:
            refs = [ref for ref, kind in batch]
            media_caption = (None if progress['caption_sent'] else caption or None) or items[start].get('caption')
            try:
                if len(batch) >= 2 and all(kind == 'photo' for ref, kind in batch):
                    result = await adapter.send_media_group(chat_id=chat_id, media_refs=refs, thread_id=thread_id, caption=media_caption, reply_to=reply_to, item_captions=[items[start+i].get('caption') for i in range(len(batch))])
                    ids = list(result.all_external_ids)
                    if len(ids) != len(batch):
                        raise RuntimeError('album confirmation count mismatch')
                else:
                    result = await adapter.send_media(chat_id=chat_id, media_ref=refs[0], kind=batch[0][1], thread_id=thread_id, caption=media_caption, **({'reply_to':reply_to} if reply_to else {}))
                    ids = [str(result.external_message_id)]
            except Exception as exc:
                from .diagnostics import MediaGroupRejected
                if isinstance(exc, MediaGroupRejected) and len(batch) >= 2:
                    progress['single_mode'] = True
                    await save(payload)
                    continue
                if not terminal:
                    raise
                progress['failed'].extend(range(start, start+len(batch)))
                progress['next'] += len(batch)
                await save(payload)
            else:
                progress['ids'].extend(str(i) for i in ids)
                progress['next'] += len(batch)
                progress['caption_sent'] = True
                await save(payload)
            finally:
                for ref in refs:
                    Path(ref.partition(':')[2]).unlink(missing_ok=True)
        if fault is not None:
            if not terminal:
                raise fault
            progress['failed'].append(progress['next'])
            progress['next'] += 1
            await save(payload)
    text = payload.get('text', '')
    chunks = []
    chunk, size = '', 0
    for character in text:
        width = len(character.encode('utf-16-le')) // 2
        if size+width > 4096:
            chunks.append(chunk);chunk,size='',0
        chunk += character;size += width
    if chunk:chunks.append(chunk)
    for index in range(progress.get('followup_next',0),len(chunks)):
        result = await (adapter.send_reply(chat_id=chat_id,reply_to=reply_to,text=chunks[index],thread_id=thread_id) if reply_to else adapter.send_message(chat_id=chat_id,text=chunks[index],thread_id=thread_id))
        progress['ids'].append(str(result.external_message_id))
        progress['followup_next'] = index+1
        progress['followup_sent'] = index+1 == len(chunks)
        await save(payload)
    ids = progress['ids']
    return SimpleNamespace(external_message_id=ids[0] if ids else None, all_external_ids=ids,
                           state='failed' if progress['failed'] else 'delivered', error='media_unavailable' if progress['failed'] else None)
