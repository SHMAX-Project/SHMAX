"""Persisted local signatures: API authorization/validation and real delivery."""
import asyncio
import pytest
from shmax.users.nickname import normalize_nickname
from shmax.users.repository import TelegramUsersRepository
from shmax.bridge.attribution import attribute_text, resolve_author_label
from tests.test_backup_api import client
from tests.test_tg_caption_attribution_e2e import (
    env, _route, _make_fetcher, _FakeAiogramMessage, _FakeChat, _FakeFromUser,
    _telegram_event_for, queue_payload, _MaxRecorder, _Shim, _payload_rec,
)
from shmax.adapters.normalize import normalize_inbound
from shmax.runtime import AppRuntime


@pytest.mark.parametrize('value,expected', [('  Роман  ','Роман'),('   ',None),(None,None),
    ('Анна 💀 😈','Анна 💀 😈'),('Family 👨‍👩‍👧‍👦','Family 👨‍👩‍👧‍👦'),('🤝'*64,'🤝'*64)])
def test_signature_normalization(value,expected):
    assert normalize_nickname(value)==expected


@pytest.mark.parametrize('value',['Анна\nПетрова','Анна\r','A\x00B','A\tB','A\x7fB','A\u202eB','A\u2028B','А'*65])
def test_signature_rejects_format_injection(value):
    with pytest.raises(ValueError): normalize_nickname(value)


def test_api_set_change_clear_preserves_acl(client):
    user='42'
    assert client.patch(f'/api/telegram/users/{user}/approval',json={'approved':True,'role':'operator'}).status_code==200
    assert client.patch(f'/api/telegram/users/{user}/blocked',json={'blocked':True}).status_code==200
    before=next(u for u in client.get('/api/telegram/users').json()['users'] if u['user_id']==user)
    for value,expected in [('  Роман  ','Роман'),('Анна 😈','Анна 😈'),(' '*4,None),(None,None)]:
        response=client.patch(f'/api/telegram/users/{user}/nickname',json={'nickname':value})
        assert response.status_code==200,response.text
        assert response.json()['nickname']==expected
        after=next(u for u in client.get('/api/telegram/users').json()['users'] if u['user_id']==user)
        assert {k:v for k,v in after.items() if k!='nickname'}=={k:v for k,v in before.items() if k!='nickname'}


def test_api_validation_and_authorization(client):
    path='/api/telegram/users/42/nickname'
    for value in ['bad\nname','bad\x00name','a'*65]:
        assert client.patch(path,json={'nickname':value}).status_code==422
    assert client.patch(path,json={'nickname':'Роман'},headers={'X-CSRF-Token':'invalid'}).status_code==403
    client.cookies.clear()
    assert client.patch(path,json={'nickname':'Роман'}).status_code in (401,403)


@pytest.mark.asyncio
async def test_membership_and_display_refresh_never_touch_signature(db):
    repo=TelegramUsersRepository(db)
    await repo.observe_member(user_id='42',username='old',display_name='Ромка',membership='member',is_admin=False,source='mtproto')
    await repo.set_nickname(user_id='42',nickname='Рома')
    await repo.set_approval(user_id='42',approved=True,role='operator')
    await repo.set_blocked(user_id='42',blocked=True)
    for source,status,is_admin in [('mtproto','member',False),('chat_member','administrator',True),('chat_member','left',False)]:
        await repo.observe_member(user_id='42',username='new',display_name='Roman',membership=status,is_admin=is_admin,source=source)
        row=await repo.get('42')
        assert row.nickname=='Рома' and row.approved and row.blocked and row.role=='operator'
    await repo.register_or_update(user_id='42',username='new',display_name='Roman again')
    assert (await repo.get('42')).nickname=='Рома'


@pytest.mark.parametrize('nickname,expected',[('Роман','[Роман]: Привет'),('', '[Ромка 💀 Шалапай 😈]: Привет')])
def test_actual_tg_text_delivery_uses_stored_override(env,nickname,expected):
    repo=TelegramUsersRepository(env.db_shim)
    asyncio.run(repo.set_nickname(user_id='777000',nickname=nickname))
    bridge,s=env.bridge(_make_fetcher());_route(s)
    msg=_FakeAiogramMessage(_FakeChat(-100999,'Work','supergroup'),_FakeFromUser(777000,'Ромка 💀 Шалапай 😈','quostiid'),None)
    msg.photo=None;msg.media=None;msg.text='Привет';msg.caption=None
    event=normalize_inbound('telegram',_telegram_event_for(msg))
    event.metadata['telegram_user_nickname']='FORGED'  # never trusts an event override
    assert asyncio.run(bridge.handle_telegram_event(event))
    payload=queue_payload(bridge)
    assert payload['text']==expected
    mx=_MaxRecorder();asyncio.run(AppRuntime._deliver(_Shim(mx),_payload_rec(payload)))
    assert mx.calls==[('text',expected)]
    assert expected.count(payload['author_label'])==1
    asyncio.run(s.close())


def test_actual_media_caption_fallback_and_clear(env):
    repo=TelegramUsersRepository(env.db_shim)
    asyncio.run(repo.set_nickname(user_id='777000',nickname='Анна'))
    bridge,s=env.bridge(_make_fetcher());_route(s)
    msg=_FakeAiogramMessage(_FakeChat(-100999,'Work','supergroup'),_FakeFromUser(777000,'Telegram name','anna'),'Клиенту уже ответили.',with_sticker=True)
    event=normalize_inbound('telegram',_telegram_event_for(msg))
    assert asyncio.run(bridge.handle_telegram_event(event))
    payload=queue_payload(bridge)
    mx=_MaxRecorder();asyncio.run(AppRuntime._deliver(_Shim(mx),_payload_rec(payload)))
    assert mx.calls[0]==('media','[Анна]: Клиенту уже ответили.')
    assert '\n'.join(text for _,text in mx.calls).count('[Анна]:')==1
    assert 'Неподдерживаемое вложение' in '\n'.join(text for _,text in mx.calls)
    asyncio.run(repo.set_nickname(user_id='777000',nickname=None))
    assert (asyncio.run(repo.get('777000'))).nickname is None
    asyncio.run(s.close())


def test_inline_attribution_idempotent_and_emoji():
    label=resolve_author_label(persisted_nickname='👨‍👩‍👧‍👦').label
    text=attribute_text(text='Привет',label=label,inline=True)
    assert text=='[👨‍👩‍👧‍👦]: Привет'
    assert attribute_text(text=text,label=label,inline=True)==text


def test_clear_override_restores_actual_delivery(env):
    repo=TelegramUsersRepository(env.db_shim)
    asyncio.run(repo.set_nickname(user_id='777000',nickname='Рома'))
    asyncio.run(repo.set_nickname(user_id='777000',nickname='   '))
    bridge,s=env.bridge(_make_fetcher());_route(s)
    msg=_FakeAiogramMessage(_FakeChat(-100999,'Work','supergroup'),_FakeFromUser(777000,'Telegram name','roman'),None)
    msg.photo=None;msg.text='Привет';msg.media=None
    assert asyncio.run(bridge.handle_telegram_event(normalize_inbound('telegram',_telegram_event_for(msg))))
    assert queue_payload(bridge)['text']=='[Telegram name]: Привет'
    asyncio.run(s.close())


def test_max_to_telegram_keeps_max_author_even_with_local_nickname(env):
    from shmax.domain.models import NormalizedMessage,ChatRef,SenderRef
    from shmax.domain.enums import ChatKind
    asyncio.run(TelegramUsersRepository(env.db_shim).set_nickname(user_id='777000',nickname='Telegram override'))
    bridge,s=env.bridge(_make_fetcher());_route(s)
    event=NormalizedMessage(source='max',chat=ChatRef(transport='max',external_id='max-chat-1',kind=ChatKind.GROUP),
        sender=SenderRef(transport='max',external_id='777000',display_name='Автор MAX'),external_id='max-new',text='Привет',timestamp=1)
    assert asyncio.run(bridge.handle_max_event(event))
    assert queue_payload(bridge)['text']=='[Автор MAX]:\nПривет'
    asyncio.run(s.close())


def test_native_reply_uses_stored_signature(env):
    from shmax.domain.models import NormalizedMessage,ChatRef,SenderRef,ReplyRef
    from shmax.domain.enums import ChatKind
    asyncio.run(TelegramUsersRepository(env.db_shim).set_nickname(user_id='777000',nickname='Роман'))
    bridge,s=env.bridge(_make_fetcher());_route(s)
    asyncio.run(bridge._mapping.create_for_max_to_tg(max_account_id=1,max_chat_id='max-chat-1',max_message_id='mx-original',
        telegram_account_id=1,telegram_chat_id='-100999',telegram_thread_id='100',telegram_message_id='tg-original',outbound_marker_value='test-marker'))
    event=NormalizedMessage(source='telegram',chat=ChatRef(transport='telegram',external_id='-100999',kind=ChatKind.GROUP),
        sender=SenderRef(transport='telegram',external_id='777000',display_name='Telegram name'),external_id='tg-reply',text='Ответ',timestamp=1,
        metadata={'thread_id':'100'},reply_to=ReplyRef(transport='telegram',chat_external_id='-100999',message_external_id='tg-original',thread_id='100'))
    assert asyncio.run(bridge.handle_telegram_event(event))
    payload=queue_payload(bridge)
    assert payload['reply_to_max_message_id']=='mx-original'
    mx=_MaxRecorder();asyncio.run(AppRuntime._deliver(_Shim(mx),_payload_rec(payload)))
    assert mx.calls==[('reply','[Роман]: Ответ')]
    asyncio.run(s.close())
