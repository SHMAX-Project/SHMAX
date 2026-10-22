import datetime as dt
from types import SimpleNamespace as NS
import pytest
from sqlalchemy import select
from tests.test_media_retry_lifetime import rt
from shmax.database.models import MaxAccount,TelegramAccount,TelegramTarget,TelegramUser,TelegramGroupAccess,DeliveryQueueItem,MessageMap
from shmax.routing.repository import RoutesRepository
from shmax.adapters.normalize import normalize_max
from shmax.adapters.max.base import MaxEvent
from shmax.domain.models import NormalizedMessage,ChatRef,SenderRef,ReplyRef
from shmax.domain.enums import ChatKind

class Telegram:
    def __init__(self):self.sent=[];self.topics=[];self.next=100
    async def create_topic(self,**kwargs):
        self.topics.append(kwargs);return str(500+len(self.topics))
    async def send_message(self,**kwargs):
        self.next+=1;self.sent.append(('text',kwargs));return NS(external_message_id=str(self.next))
    async def send_reply(self,**kwargs):return await self.send_message(**kwargs)
    async def send_media_group(self,**kwargs):
        ids=[]
        for _ in kwargs['media_refs']:self.next+=1;ids.append(str(self.next))
        self.sent.append(('album',kwargs));return NS(external_message_id=ids[0],all_external_ids=ids)
    async def send_media(self,**kwargs):return await self.send_message(**kwargs)

class Max:
    def __init__(self):self.sent=[];self.next=1000
    async def send_text(self,**kwargs):
        self.sent.append(kwargs);self.next+=1;return NS(external_message_id=str(self.next))
    async def send_reply(self,**kwargs):return await self.send_text(**kwargs)

async def seed(rt):
    rt._tg=Telegram();rt._max=Max()
    async with rt.db.session() as s:
        s.add_all([MaxAccount(id=1,label='fixture'),TelegramAccount(id=1,label='fixture')]);await s.flush()
        for i,chat in [(1,'-1001'),(2,'-1002'),(3,'-1003')]:
            s.add(TelegramTarget(id=i,account_id=1,chat_id=chat,title=chat,is_active=True,is_forum=True,
                validated_at=dt.datetime.now(dt.timezone.utc),missing_permissions='[]',is_admin_hub=i==3,auto_add_chats=i==3))
        for user in ['alice','bob','admin']:
            s.add(TelegramUser(user_id=user,display_name=user,role='read_only',approved=False,blocked=False))
        await s.flush()
        for target,user in [(1,'alice'),(2,'bob'),(3,'admin')]:s.add(TelegramGroupAccess(target_id=target,user_id=user,approved=True,blocked=False,role='operator'))
        await s.commit()
        repo=RoutesRepository(s)
        await repo.upsert(max_account_id=1,max_chat_id='max-A',telegram_account_id=1,telegram_chat_id='-1001',telegram_thread_id='11',label='Отдел A')
        await repo.upsert(max_account_id=1,max_chat_id='max-B',telegram_account_id=1,telegram_chat_id='-1002',telegram_thread_id='22',label='Отдел B')

async def rows(rt):
    async with rt.db.session() as s:return list((await s.scalars(select(DeliveryQueueItem).order_by(DeliveryQueueItem.id))).all())

async def drain(rt):
    for _ in range(20):
        rec=await rt._claim_one(120)
        if not rec:return
        await rt._drain_one(rec)
    raise AssertionError('queue did not settle')

def incoming(identity='m1',chat='max-A'):
    return normalize_max(MaxEvent(identity,chat,'max-author','hello',raw=NS(sender=NS(first_name='Александр'))))

def telegram(user,group='-1001',thread='11',identity='t1',reply=None):
    return NormalizedMessage(source='telegram',chat=ChatRef('telegram',group,ChatKind.GROUP),
        sender=SenderRef('telegram',user,display_name=user),external_id=identity,text='answer',
        timestamp=1,attachments=(),metadata={'thread_id':thread},reply_to=reply)

@pytest.mark.asyncio
async def test_max_private_group_and_admin_overview_have_independent_deliveries(rt):
    await seed(rt);await rt._on_inbound('max',incoming())
    queued=await rows(rt);assert {r.target_chat_id for r in queued}=={'-1001','-1003'}
    await drain(rt)
    assert all(r.status=='delivered' for r in await rows(rt))
    assert {kw['chat_id'] for _,kw in rt._tg.sent}=={'-1001','-1003'}
    assert all('Александр' in kw.get('text','') for _,kw in rt._tg.sent)
    assert len(rt._tg.topics)==1
    await rt._on_inbound('max',incoming());assert len(await rows(rt))==2
    async with rt.db.session() as s:
        mapped=(await s.scalars(select(MessageMap))).all();assert len(mapped)==2
        assert {m.telegram_chat_id for m in mapped}=={'-1001','-1003'}

@pytest.mark.asyncio
async def test_grants_are_group_scoped_and_admin_mirror_waits_for_max(rt):
    await seed(rt)
    await rt._on_inbound('telegram',telegram('bob'));assert await rows(rt)==[]
    await rt._on_inbound('telegram',telegram('alice'))
    queued=await rows(rt);assert len(queued)==2
    assert queued[1].depends_on_id==queued[0].id
    first=await rt._claim_one(120);assert first.direction=='telegram_to_max'
    assert await rt._claim_one(120) is None
    await rt._drain_one(first);await drain(rt)
    assert len(rt._max.sent)==1 and rt._max.sent[0]['chat_id']=='max-A'
    assert {kw['chat_id'] for _,kw in rt._tg.sent}=={'-1003'}
    assert all(r.status=='delivered' for r in await rows(rt))
    async with rt.db.session() as s:
        mapped=(await s.scalars(select(MessageMap).order_by(MessageMap.id))).all()
        assert len(mapped)==2 and mapped[0].max_message_id==mapped[1].max_message_id

@pytest.mark.asyncio
async def test_revocation_before_send_cancels_primary_and_dependent_copy(rt):
    await seed(rt);await rt._on_inbound('telegram',telegram('alice'))
    async with rt.db.session() as s:
        grant=await s.scalar(select(TelegramGroupAccess).where(TelegramGroupAccess.target_id==1));grant.approved=False;await s.commit()
    await drain(rt)
    assert rt._max.sent==[] and rt._tg.sent==[]
    assert [r.status for r in await rows(rt)]==['failed','failed']

@pytest.mark.asyncio
async def test_new_max_chat_is_durably_queued_before_admin_topic_creation(rt):
    await seed(rt);await rt._on_inbound('max',incoming(chat='new-chat'))
    assert len(await rows(rt))==1 and rt._tg.topics==[]
    await drain(rt);assert len(rt._tg.topics)==1
    assert (await rows(rt))[0].status=='delivered'
    async with rt.db.session() as s:
        route=await RoutesRepository(s).find_by_max(max_account_id=1,max_chat_id='new-chat',telegram_chat_id='-1003')
        assert route and route.telegram_thread_id

@pytest.mark.asyncio
async def test_disabled_destination_cannot_receive_a_previously_queued_message(rt):
    await seed(rt);await rt._on_inbound('max',incoming())
    async with rt.db.session() as s:
        target=await s.get(TelegramTarget,1);target.is_active=False;await s.commit()
    await drain(rt)
    assert {kw['chat_id'] for _,kw in rt._tg.sent}=={'-1003'}
    assert [r.status for r in await rows(rt)]==['failed','delivered']

@pytest.mark.asyncio
async def test_mirror_respects_destination_attribution_settings(rt):
    await seed(rt)
    async with rt.db.session() as s:
        hub=await s.get(TelegramTarget,3)
        hub.show_sender=False;hub.show_source_chat=False
        await s.commit()
    await rt._on_inbound('telegram',telegram('alice'))
    await drain(rt)
    assert len(rt._tg.sent)==1
    text=rt._tg.sent[0][1]['text']
    assert 'answer' in text and 'alice' not in text and 'Отдел A' not in text
    # The original MAX attribution is independent of overview preferences.
    assert 'alice' in rt._max.sent[0]['text']

@pytest.mark.asyncio
async def test_reply_reference_cannot_cross_group_or_topic(rt):
    await seed(rt)
    await rt._on_inbound('max',incoming());await drain(rt)
    async with rt.db.session() as s:
        mapped=await s.scalar(select(MessageMap).where(MessageMap.telegram_chat_id=='-1001'))
        original_id=mapped.telegram_message_id
    forged=ReplyRef('telegram','-1001',original_id,thread_id='22')
    await rt._on_inbound('telegram',telegram('alice',identity='forged',reply=forged))
    queued=await rows(rt)
    primary=next(r for r in queued if r.direction=='telegram_to_max')
    assert primary.payload.get('reply_to_max_message_id') is None

@pytest.mark.asyncio
async def test_admin_reply_goes_to_max_and_mirrors_only_linked_private_group(rt):
    await seed(rt)
    await rt._on_inbound('max',incoming());await drain(rt)
    async with rt.db.session() as s:
        mapped=await s.scalar(select(MessageMap).where(MessageMap.telegram_chat_id=='-1003'))
        thread=mapped.telegram_thread_id;message=mapped.telegram_message_id
    await rt._on_inbound('telegram',telegram('admin',group='-1003',thread=thread,
        reply=ReplyRef('telegram','-1003',message,thread_id=thread)))
    await drain(rt)
    assert len(rt._max.sent)==1
    assert rt._max.sent[0]['chat_id']=='max-A'
    assert rt._tg.sent[-1][1]['chat_id']=='-1001'
    assert not any(kw['chat_id']=='-1002' for _,kw in rt._tg.sent)

@pytest.mark.asyncio
async def test_captionless_three_photo_fanout_keeps_shared_cache_and_each_delivery(rt,tmp_path):
    from dataclasses import replace
    from pathlib import Path
    from shmax.domain.models import Attachment
    from shmax.domain.enums import AttachmentKind
    await seed(rt)
    base=tmp_path/'source.jpg';base.write_bytes(b'JPEGDATA')
    async def fetch(source,attachment):
        assert source=='max'
        assert base.exists()
        return NS(media_ref='photo:'+str(base),kind='photo',path=base,filename='source.jpg')
    rt._fetch_media=fetch
    original=rt._tg.send_media_group
    async def send(**kwargs):
        assert len(kwargs['media_refs'])==3
        assert all(Path(ref.partition(':')[2]).is_file() for ref in kwargs['media_refs'])
        assert all(ref!='photo:'+str(base) for ref in kwargs['media_refs'])
        return await original(**kwargs)
    rt._tg.send_media_group=send
    event=replace(incoming(),text='',attachments=tuple(Attachment(kind=AttachmentKind.PHOTO,payload={'id':str(i)},caption=None) for i in range(3)))
    await rt._on_inbound('max',event);await drain(rt)
    assert [r.status for r in await rows(rt)]==['delivered','delivered']
    albums=[kw for kind,kw in rt._tg.sent if kind=='album']
    assert len(albums)==2 and all('Александр' in kw['caption'] for kw in albums)
    assert base.exists()

@pytest.mark.asyncio
async def test_explicit_legacy_deny_cannot_be_bypassed_by_group_grant(rt):
    from shmax.database.models import AclRule
    await seed(rt)
    async with rt.db.session() as s:
        s.add(AclRule(scope='chat',scope_ref='-1001',principal='alice',permission='send_message',allow=False));await s.commit()
    await rt._on_inbound('telegram',telegram('alice'))
    assert await rows(rt)==[]

@pytest.mark.asyncio
async def test_manual_retry_of_primary_rearms_failed_admin_copy(rt):
    from shmax.queue.repository import DeliveryQueueRepository
    await seed(rt);await rt._on_inbound('telegram',telegram('alice'))
    queued=await rows(rt)
    async with rt.db.session() as s:
        repo=DeliveryQueueRepository(s)
        await repo.mark_failed(queue_id=queued[0].id,last_error='Synthetic temporary failure')
        assert await repo.reset_for_retry(queue_id=queued[0].id)
    assert [r.status for r in await rows(rt)]==['pending','pending']
    await drain(rt)
    assert len(rt._max.sent)==1 and len(rt._tg.sent)==1
    assert all(r.status=='delivered' for r in await rows(rt))

@pytest.mark.asyncio
async def test_purge_confirmed_primary_keeps_waiting_mirror_deliverable(rt):
    from shmax.queue.repository import DeliveryQueueRepository
    await seed(rt);await rt._on_inbound('telegram',telegram('alice'))
    first=await rt._claim_one(120)
    async with rt.db.session() as s:
        assert not await DeliveryQueueRepository(s).purge(queue_id=first.id)
    await rt._drain_one(first)
    async with rt.db.session() as s:
        assert await DeliveryQueueRepository(s).purge(queue_id=first.id)
    await drain(rt)
    assert len(rt._max.sent)==1 and len(rt._tg.sent)==1
    assert (await rows(rt))[0].status=='delivered'

@pytest.mark.asyncio
async def test_purge_unconfirmed_primary_cancels_mirror_without_stranding_retry(rt):
    from shmax.queue.repository import DeliveryQueueRepository
    await seed(rt);await rt._on_inbound('telegram',telegram('alice'))
    queued=await rows(rt)
    async with rt.db.session() as s:
        repo=DeliveryQueueRepository(s)
        assert await repo.purge(queue_id=queued[0].id)
        assert not await repo.reset_for_retry(queue_id=queued[1].id)
    await drain(rt)
    assert rt._tg.sent==[] and rt._max.sent==[]
    assert (await rows(rt))[0].status=='failed'

@pytest.mark.asyncio
async def test_replayed_max_update_cannot_duplicate_normalized_legacy_pending_job(rt):
    from shmax.queue.repository import DeliveryQueueRepository
    await seed(rt)
    async with rt.db.session() as s:
        hub=await s.get(TelegramTarget,3);hub.is_active=False;await s.commit()
        await DeliveryQueueRepository(s).enqueue(direction='max_to_telegram',target_chat_id='-1001',target_thread_id='11',
            idempotency_key='max:max-A:m1:max_to_telegram:-1001',
            payload={'direction':'max_to_telegram','max_account_id':1,'max_chat_id':'max-A','max_message_id':'m1','telegram_account_id':1,
                'target_chat_id':'-1001','text':'legacy accepted message','marker':'legacy-marker'})
    await rt._on_inbound('max',incoming())
    assert len(await rows(rt))==1
    await drain(rt)
    assert len(rt._tg.sent)==1 and (await rows(rt))[0].status=='delivered'
