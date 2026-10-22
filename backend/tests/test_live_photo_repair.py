"""Production normalizer → configured aiogram download → real PyMax Photo."""
import io
from types import SimpleNamespace as NS
import pytest
from PIL import Image
from aiogram.types import Message
from shmax.adapters.telegram.bot_api import TelegramBotApiAdapter
from shmax.adapters.telegram.config import TransportConfig, TransportMode
from shmax.adapters.normalize import normalize_inbound
from shmax.media.pipeline import MediaPipeline
from shmax.media.diagnostics import MediaFailure
from shmax.runtime import AppRuntime


def picture(format='JPEG'):
    out=io.BytesIO(); Image.new('RGB',(32,32),'red').save(out,format=format); return out.getvalue()


def photo_message(caption=None):
    return Message.model_validate({'message_id':123,'date':1700000000,'chat':{'id':-1001,'type':'supergroup'},
        'from':{'id':7,'is_bot':False,'first_name':'Роман'}, 'caption':caption,
        'photo':[{'file_id':'large','file_unique_id':'NOT_DOWNLOADABLE','width':1000,'height':800},
                 {'file_id':'small','file_unique_id':'UNIQUE','width':50,'height':50}]})


@pytest.mark.asyncio
@pytest.mark.parametrize('caption',[None,'Вот документ'])
async def test_real_aiogram_photo_download_and_pymax_descriptor(tmp_path,caption):
    adapter=TelegramBotApiAdapter(TransportConfig(mode=TransportMode.BOT_API_DIRECT,bot_token='123:SECRET'))
    seen=[]
    class Session:
        api=NS(file_url=lambda token,path:'https://api.telegram.org/file/bot'+token+'/'+path)
        async def stream_content(self,**kwargs):
            seen.append('download'); yield picture()
    async def get_file(ref):
        seen.append(ref); return NS(file_path='photos/real.jpg',file_size=1000)
    adapter._bot=NS(get_file=get_file,session=Session()); adapter._started=True
    runtime=AppRuntime.__new__(AppRuntime); runtime._tg=adapter;runtime._max=None;runtime.media_pipeline=MediaPipeline(tmp_path)
    normalized=normalize_inbound('telegram',adapter._from_aio_message(photo_message(caption)))
    assert normalized.attachments[0].payload['file_id']=='large'
    stored=await runtime._fetch_media('telegram',normalized.attachments[0])
    assert stored.path.suffix=='.jpg' and stored.path.read_bytes()==picture()
    from pymax import Photo
    descriptor=Photo(path=str(stored.path))
    assert descriptor.validate_photo()==('jpg','image/jpg')
    assert seen==['large','download']


@pytest.mark.asyncio
@pytest.mark.parametrize('reason',['getfile','empty','transport','path','size'])
async def test_download_failures_are_sanitized_and_clean(tmp_path,reason):
    adapter=TelegramBotApiAdapter(TransportConfig(mode=TransportMode.BOT_API_DIRECT,bot_token='123:SECRET'))
    class Session:
        api=NS(file_url=lambda token,path:'https://api.telegram.org/file/bot'+token+'/'+path)
        async def stream_content(self,**kw):
            if reason=='transport': raise ValueError('https://botSECRET:password@host/private')
            if reason!='empty': yield b'image'
    async def get_file(ref):
        if reason=='getfile': raise ValueError('SECRET')
        return NS(file_path='../escape' if reason=='path' else 'photos/x.jpg',file_size=99_000_000 if reason=='size' else 4)
    adapter._bot=NS(get_file=get_file,session=Session());adapter._started=True
    with pytest.raises(MediaFailure) as error:
        await adapter.download_attachment({'file_id':'downloadable'},tmp_path/'photo')
    assert 'SECRET' not in str(error.value) and 'password' not in str(error.value)
    assert not (tmp_path/'photo').exists()

@pytest.mark.asyncio
@pytest.mark.parametrize('mode,scheme,host',[
    (TransportMode.BOT_API_DIRECT,None,None),
    (TransportMode.BOT_API_SOCKS5,'socks5','proxy.example'),
    (TransportMode.BOT_API_HTTP,'http','proxy.example'),
    (TransportMode.BOT_API_SOCKS5,'socks5','127.0.0.1'),
])
async def test_getfile_and_download_use_same_configured_session(tmp_path,monkeypatch,mode,scheme,host):
    from shmax.adapters.telegram.config import ProxyConfig
    from aiogram.types import File
    config=TransportConfig(mode=mode,bot_token='123456:SECRET',proxy=ProxyConfig(scheme,host,10808,'user','password') if scheme else None)
    adapter=TelegramBotApiAdapter(config);bot=adapter._make_bot();adapter._bot=bot;adapter._started=True
    called=[];session=bot.session
    if scheme:assert session._proxy is not None
    else:assert session._proxy is None
    async def call(bot,method,timeout=None):
        called.append(type(method).__name__);return File(file_id='actual',file_unique_id='not-for-download',file_path='photos/x.jpg')
    async def stream(**kwargs):
        called.append('download');yield picture()
    monkeypatch.setattr(type(session),'make_request',lambda self,bot,method,timeout=None:call(bot,method,timeout))
    monkeypatch.setattr(session,'stream_content',stream)
    assert await adapter.download_to('actual',tmp_path/'out')==len(picture())
    assert called==['GetFile','download'] and bot.session is session
    await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('animated,video,thumbnail',[(False,False,None),(True,False,'preview'),(False,True,'preview'),(True,False,None),(False,True,None)])
async def test_sticker_transfer_or_preview_and_fallback(tmp_path,animated,video,thumbnail):
    from shmax.domain.enums import AttachmentKind
    from shmax.domain.models import Attachment
    seen=[]
    class Adapter:
        async def download_attachment(self,payload,dest):
            seen.append(payload['file_id']);return dest.write_bytes(picture('WEBP'))
    runtime=AppRuntime.__new__(AppRuntime);runtime._tg=Adapter();runtime._max=None;runtime.media_pipeline=MediaPipeline(tmp_path)
    attachment=Attachment(kind=AttachmentKind.STICKER,payload={'file_id':'sticker','is_animated':animated,'is_video':video,'thumbnail_file_id':thumbnail})
    if (animated or video) and not thumbnail:
        with pytest.raises(MediaFailure,match='preview_unavailable'):await runtime._fetch_media('telegram',attachment)
    else:
        stored=await runtime._fetch_media('telegram',attachment)
        assert stored.kind=='photo' and stored.mime=='image/png'
        assert Image.open(stored.path).mode=='RGBA'
        assert seen==[thumbnail or 'sticker']


@pytest.mark.asyncio
async def test_max_sticker_image_uses_downloadable_sdk_url(tmp_path):
    from pymax.types.domain.attachments import StickerAttachment
    from shmax.adapters.normalize import _pymax_attachments_from_raw
    att=StickerAttachment.model_validate({'_type':'STICKER','url':'https://sticker.max.ru/a.webp','stickerId':1,'width':512,'height':512,'time':1,'stickerType':'STATIC','audio':False})
    normalized=_pymax_attachments_from_raw(NS(attaches=[att]),'1','2')
    assert normalized[0].payload['url']==att.url
    class Adapter:
        async def download_attachment(self,payload,dest):return dest.write_bytes(picture('WEBP'))
    runtime=AppRuntime.__new__(AppRuntime);runtime._max=Adapter();runtime._tg=None;runtime.media_pipeline=MediaPipeline(tmp_path)
    stored=await runtime._fetch_media('max',normalized[0])
    assert stored.kind=='sticker' and stored.path.suffix=='.png'


@pytest.mark.asyncio
async def test_incorrect_image_response_is_rejected_and_cleaned(tmp_path):
    from shmax.domain.models import Attachment
    from shmax.domain.enums import AttachmentKind
    class Adapter:
        async def download_attachment(self,payload,dest):return dest.write_bytes(b'<html>secret error</html>')
    runtime=AppRuntime.__new__(AppRuntime);runtime._tg=Adapter();runtime._max=None;runtime.media_pipeline=MediaPipeline(tmp_path)
    with pytest.raises(MediaFailure,match='invalid_image'):
        await runtime._fetch_media('telegram',Attachment(kind=AttachmentKind.PHOTO,payload={'file_id':'valid'}))
    assert not list((tmp_path/'media').iterdir())


@pytest.mark.asyncio
async def test_pymax_actual_photo_send_and_native_reply(tmp_path):
    from shmax.adapters.max.pymax_adapter import PyMaxMaxAdapter
    from shmax.adapters.max.config import PyMaxAdapterConfig
    from shmax.domain.models import ReplyRef
    adapter=PyMaxMaxAdapter(config=PyMaxAdapterConfig(phone=None,session_dir=tmp_path/'session'))
    path=tmp_path/'photo.jpg';path.write_bytes(picture());calls=[]
    class Client:
        async def send_message(self,**kwargs):
            calls.append(kwargs);assert await kwargs['attachments'][0].read()==picture()
            assert kwargs['attachments'][0].validate_photo()==('jpg','image/jpg')
            return NS(id=999)
    adapter._handle=NS(client=Client(),started=True)
    result=await adapter.send_media('55','photo:'+str(path),caption='[Роман]: Вот документ',reply_to=ReplyRef(transport='max',chat_external_id='55',message_external_id='777'))
    assert len(calls)==1 and calls[0]['reply_to']==777 and calls[0]['text'].count('[Роман]:')==1
    assert result.external_message_id=='999'

@pytest.mark.asyncio
async def test_native_telegram_sticker_uses_sendsticker_not_photo(tmp_path):
    adapter=TelegramBotApiAdapter(TransportConfig(mode=TransportMode.BOT_API_DIRECT,bot_token='123:SECRET'))
    calls=[]
    async def send_sticker(**kwargs):calls.append(kwargs);return NS(message_id=456,chat=NS(id=-1001))
    adapter._bot=NS(send_sticker=send_sticker);adapter._started=True
    path=tmp_path/'sticker.png';path.write_bytes(picture('PNG'))
    from shmax.domain.models import ReplyRef
    result=await adapter.send_media('-1001','sticker:'+str(path),kind='sticker',caption='not allowed',thread_id=123,
        reply_to=ReplyRef(transport='telegram',chat_external_id='-1001',message_external_id='77'))
    assert len(calls)==1 and 'caption' not in calls[0]
    assert calls[0]['reply_to_message_id']==77 and calls[0]['message_thread_id']==123
    assert result.external_message_id=='456'

@pytest.mark.asyncio
async def test_max_upload_diagnostics_hide_credential_exception(tmp_path,caplog):
    import logging
    caplog.set_level(logging.WARNING)
    logging.getLogger().addHandler(caplog.handler)
    for name in ["shmax.adapters.max.pymax_adapter","shmax.adapters.telegram.bot_api"]:
        logging.getLogger(name).disabled=False
        caplog.set_level(logging.WARNING,logger=name)
    from shmax.adapters.max.pymax_adapter import PyMaxMaxAdapter
    from shmax.adapters.max.config import PyMaxAdapterConfig
    adapter=PyMaxMaxAdapter(config=PyMaxAdapterConfig(phone=None,session_dir=tmp_path/'session'))
    path=tmp_path/'photo.jpg';path.write_bytes(picture())
    async def upload_photo():raise ValueError('https://SECRET:password@max.ru/token')
    class Client:
        async def send_message(self,**kwargs):await upload_photo()
    adapter._handle=NS(client=Client(),started=True)
    with pytest.raises(Exception) as error:await adapter.send_media('55','photo:'+str(path),kind='photo')
    assert 'SECRET' not in str(error.value) and 'password' not in caplog.text
    assert error.value.__suppress_context__ and 'max.upload' in caplog.text

@pytest.mark.asyncio
async def test_conversion_timeout_kills_and_reaps_worker(tmp_path,monkeypatch):
    from shmax.media.image import prepare_image
    calls=[]
    class Process:
        def kill(self):calls.append('kill')
        async def wait(self):calls.append('wait');return 0
    async def spawn(*args,**kwargs):return Process()
    async def timeout(awaitable,seconds):awaitable.close();raise TimeoutError
    monkeypatch.setattr('shmax.media.image.asyncio.create_subprocess_exec',spawn)
    monkeypatch.setattr('shmax.media.image.asyncio.wait_for',timeout)
    with pytest.raises(MediaFailure,match='conversion_timeout'):await prepare_image(tmp_path/'image',sticker=True)
    assert calls==['kill','wait']

from tests.test_tg_caption_attribution_e2e import env, _payload_rec

@pytest.mark.asyncio
async def test_real_photo_bridge_nickname_queue_to_pymax_attachment(env,tmp_path):
    from sqlalchemy import update,select
    from shmax.database.models import TelegramUser,DeliveryQueueItem
    from shmax.routing.repository import RoutesRepository
    from shmax.adapters.max.pymax_adapter import PyMaxMaxAdapter
    from shmax.adapters.max.config import PyMaxAdapterConfig
    from aiogram.types import User
    tg=TelegramBotApiAdapter(TransportConfig(mode=TransportMode.BOT_API_DIRECT,bot_token='123:SECRET'))
    class Session:
        api=NS(file_url=lambda token,path:'https://api.telegram.org/file/bot'+token+'/'+path)
        async def stream_content(self,**kwargs):yield picture()
    async def get_file(ref):assert ref=='large';return NS(file_path='photos/live.jpg',file_size=len(picture()))
    tg._bot=NS(get_file=get_file,session=Session());tg._started=True
    mx=PyMaxMaxAdapter(config=PyMaxAdapterConfig(phone=None,session_dir=tmp_path/'session'));sent=[]
    class Client:
        async def send_message(self,**kwargs):
            sent.append(kwargs)
            assert await kwargs['attachments'][0].read()==picture()
            assert kwargs['attachments'][0].validate_photo()[0]=='jpg'
            return NS(id=999)
    mx._handle=NS(client=Client(),started=True)
    runtime=AppRuntime.__new__(AppRuntime);runtime._tg=tg;runtime._max=mx;runtime.media_pipeline=MediaPipeline(tmp_path)
    bridge,session=env.bridge(runtime._fetch_media)
    from dataclasses import replace
    bridge._config=replace(bridge._config,telegram_target_chat_id='-1001')
    await RoutesRepository(session).upsert(max_account_id=1,max_chat_id='55',telegram_account_id=1,telegram_chat_id='-1001',telegram_thread_id='100',enabled=True)
    await session.execute(update(TelegramUser).where(TelegramUser.user_id=='777000').values(nickname='Роман'))
    await session.commit()
    msg=photo_message('Вот документ').model_copy(update={'from_user':User(id=777000,is_bot=False,first_name='Telegram name'),'message_thread_id':100})
    event=normalize_inbound('telegram',tg._from_aio_message(msg))
    assert await bridge.handle_telegram_event(event)
    row=(await session.execute(select(DeliveryQueueItem))).scalars().one()
    assert row.payload['media_outcome']=='MEDIA_PENDING'
    from shmax.config import RuntimeMode
    runtime.runtime_mode=RuntimeMode.TEST
    await runtime._deliver(_payload_rec(row.payload))
    assert len(sent)==1 and sent[0]['text']=='[Роман]: Вот документ'
    await session.close()

@pytest.mark.asyncio
async def test_telegram_attachment_failure_is_sanitized(tmp_path,caplog):
    import logging
    caplog.set_level(logging.WARNING)
    logging.getLogger().addHandler(caplog.handler)
    for name in ["shmax.adapters.max.pymax_adapter","shmax.adapters.telegram.bot_api"]:
        logging.getLogger(name).disabled=False
        caplog.set_level(logging.WARNING,logger=name)
    adapter=TelegramBotApiAdapter(TransportConfig(mode=TransportMode.BOT_API_DIRECT,bot_token='123:SECRET'))
    async def send_photo(**kwargs):raise ValueError('https://botSECRET:password@host/photo')
    adapter._bot=NS(send_photo=send_photo);adapter._started=True
    with pytest.raises(MediaFailure) as error:await adapter.send_media('-1001','photo:'+str(tmp_path/'x.jpg'))
    assert error.value.__suppress_context__ and 'SECRET' not in str(error.value)
    assert 'password' not in caplog.text and 'MEDIA_FAILED' in caplog.text
