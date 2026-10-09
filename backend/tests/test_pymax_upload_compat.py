"""Real PyMax 2.4.1 Client/MessageService/UploadService, mocked server only."""
import json
from types import SimpleNamespace as NS
import pytest
from pymax import Client,Photo
from pymax.api.messages.service import MessageService
from pymax.api.uploads.service import UploadService
from pymax.protocol import Opcode
from shmax.adapters.max.pymax_adapter import PyMaxMaxAdapter
from shmax.adapters.max.config import PyMaxAdapterConfig
from shmax.adapters.max.photo_upload import PhotoUploadProtocolError,DeliveryConfirmationUncertain
from tests.test_live_photo_repair import picture


def server(tmp_path,monkeypatch,*,url='https://iu.oneme.ru/upload?apiToken=TOP_SECRET&photoIds=123',result=None,init_fail=False,send_fail=False,status=200):
    app=NS(config=NS(proxy=None,upload_timeout=5),dispatcher=NS(on_internal=lambda event:lambda fn:fn))
    calls=[];http=[]
    async def invoke(opcode,payload=None):
        calls.append((opcode,payload))
        if opcode==Opcode.PHOTO_UPLOAD:
            if init_fail:raise ConnectionError('TOP_SECRET')
            return NS(payload={'url':url} if url else {'info':[]})
        if opcode==Opcode.FILE_UPLOAD:return NS(payload={'info':[{'url':'https://upload.oneme.ru/file?token=TOP_SECRET','fileId':88,'token':'TOKEN'}]})
        assert opcode==Opcode.MSG_SEND
        if send_fail:raise TimeoutError('TOP_SECRET')
        return NS(payload={'id':'999','chatId':payload['chatId'],'time':1700000000000,'type':'USER','text':payload['message'].get('text') or ''})
    app.invoke=invoke;app.api=NS(uploads=UploadService(app),messages=MessageService(app))
    class Content:
        async def iter_chunked(self,size):yield json.dumps(result if result is not None else {'photos':{'123':{'token':'REAL_TOKEN'}}}).encode()
    class Response:
        content=Content()
        async def __aenter__(self):
            future=app.api.uploads.file_upload_waiters.get(88)
            if future is not None:future.set_result(NS(file_id=88))
            return self
        async def __aexit__(self,*args):pass
        async def json(self,**kwargs):return result if result is not None else {'photos':{'123':{'token':'REAL_TOKEN'}}}
    Response.status=status
    class Session:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        def post(self,*args,**kwargs):
            http.append((args,kwargs));return Response()
    monkeypatch.setattr('aiohttp.ClientSession',Session)
    client=Client.__new__(Client);client._app=app
    adapter=PyMaxMaxAdapter(config=PyMaxAdapterConfig(phone=None,session_dir=tmp_path/'session'))
    adapter._handle=NS(client=client,started=True)
    return adapter,app,calls,http


@pytest.mark.asyncio
async def test_original_sdk_valid_photo_and_missing_query_failure(tmp_path,monkeypatch):
    adapter,app,calls,http=server(tmp_path,monkeypatch)
    path=tmp_path/'photo.jpg';path.write_bytes(picture())
    token=await app.api.uploads.upload_photo(Photo(path=str(path)))
    assert token.photo_token=='REAL_TOKEN' and len(http)==1
    adapter,app,calls,http=server(tmp_path,monkeypatch,url='https://iu.oneme.ru/upload?apiToken=TOP_SECRET')
    from pymax.exceptions import UploadError
    with pytest.raises(UploadError,match='photoIds'):await app.api.uploads.upload_photo(Photo(path=str(path)))
    assert not http


@pytest.mark.asyncio
@pytest.mark.parametrize('query',['&photoIds=123',''])
async def test_real_client_photo_upload_descriptor_and_destination(tmp_path,monkeypatch,query,caplog):
    import logging
    caplog.set_level(logging.INFO)
    logging.getLogger().addHandler(caplog.handler)
    logger=logging.getLogger("shmax.adapters.max.photo_upload")
    logger.disabled=False
    logger.propagate=True
    caplog.set_level(logging.INFO,logger=logger.name)
    url='https://iu.oneme.ru/upload?apiToken=TOP_SECRET'+query
    adapter,app,calls,http=server(tmp_path,monkeypatch,url=url)
    path=tmp_path/'photo.jpg';path.write_bytes(picture())
    sent=await adapter.send_media('-987','photo:'+str(path),caption='[Роман]: Вот документ')
    assert sent.external_message_id=='999'
    assert len(http)==1 and http[0][0][0]==url
    sends=[payload for opcode,payload in calls if opcode==Opcode.MSG_SEND]
    assert len(sends)==1 and sends[0]['chatId']==-987
    assert sends[0]['message']['text']=='[Роман]: Вот документ'
    assert sends[0]['message']['attaches']==[{'_type':'PHOTO','photoToken':'REAL_TOKEN'}]
    assert 'TOP_SECRET' not in caplog.text and 'REAL_TOKEN' not in caplog.text
    assert 'photo_ids_present' in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize('result',[{'photos':{}},{'photos':{'a':{'token':'A'},'b':{'token':'B'}}},{'changed':{'token':'SENSITIVE'}}])
async def test_changed_response_fails_closed_without_fabrication(tmp_path,monkeypatch,result):
    adapter,app,calls,http=server(tmp_path,monkeypatch,url='https://iu.oneme.ru/upload?apiToken=TOP_SECRET',result=result)
    path=tmp_path/'photo.jpg';path.write_bytes(picture())
    with pytest.raises(PhotoUploadProtocolError):await adapter.send_media('42','photo:'+str(path))
    assert not [p for op,p in calls if op==Opcode.MSG_SEND]
    assert len(http)==1


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,data',[('pdf',b'%PDF-1.4 test'),('txt',b'plain text'),('zip',b'PK\x03\x04test'),('bin',b'\x00\xff\x01')])
@pytest.mark.parametrize('reference',['document','file'])
async def test_document_uses_real_sdk_file_upload_preserves_name(tmp_path,monkeypatch,kind,data,reference):
    adapter,app,calls,http=server(tmp_path,monkeypatch)
    path=tmp_path/'cached';path.write_bytes(data)
    sent=await adapter.send_media('42',reference+':'+str(path),caption='[Роман]: файл',filename='Отчёт.'+kind)
    assert sent.external_message_id=='999'
    assert [op for op,p in calls]==[Opcode.FILE_UPLOAD,Opcode.MSG_SEND]
    posted=http[0][1]
    assert 'Content-Disposition' in posted['headers']
    chunks=[chunk async for chunk in posted['data']]
    assert b''.join(chunks)==data
    assert calls[-1][1]['message']['attaches']==[{'_type':'FILE','fileId':88}]


@pytest.mark.asyncio
async def test_failed_initialization_is_retryable_and_no_send(tmp_path,monkeypatch):
    from shmax.adapters.max.errors import MaxTransportError
    adapter,app,calls,http=server(tmp_path,monkeypatch,init_fail=True)
    path=tmp_path/'photo.jpg';path.write_bytes(picture())
    with pytest.raises(MaxTransportError):await adapter.send_media('42','photo:'+str(path))
    assert not http and len(calls)==1


@pytest.mark.asyncio
async def test_missing_init_url_permanent(tmp_path,monkeypatch):
    adapter,app,calls,http=server(tmp_path,monkeypatch,url=None)
    path=tmp_path/'photo.jpg';path.write_bytes(picture())
    with pytest.raises(PhotoUploadProtocolError):await adapter.send_media('42','photo:'+str(path))
    assert not http


@pytest.mark.asyncio
async def test_lost_send_confirmation_requires_review(tmp_path,monkeypatch):
    adapter,app,calls,http=server(tmp_path,monkeypatch,send_fail=True)
    path=tmp_path/'photo.jpg';path.write_bytes(picture())
    with pytest.raises(DeliveryConfirmationUncertain):await adapter.send_media('42','photo:'+str(path))
    assert len(http)==1 and len([p for op,p in calls if op==Opcode.MSG_SEND])==1


@pytest.mark.asyncio
async def test_saved_messages_zero_destination_valid_for_text_media_reply(tmp_path,monkeypatch):
    from shmax.domain.models import ReplyRef
    adapter,app,calls,http=server(tmp_path,monkeypatch)
    path=tmp_path/'photo.jpg';path.write_bytes(picture())
    await adapter.send_text('0','Saved Messages')
    await adapter.send_media('0','photo:'+str(path),reply_to=ReplyRef(transport='max',chat_external_id='0',message_external_id='77'))
    file=tmp_path/'file.pdf';file.write_bytes(b'%PDF')
    await adapter.send_media('0','document:'+str(file))
    await adapter.send_reply('0',ReplyRef(transport='max',chat_external_id='0',message_external_id='77'),'reply')
    sends=[payload for opcode,payload in calls if opcode==Opcode.MSG_SEND]
    assert len(sends)==4 and all(p['chatId']==0 for p in sends)
    assert sends[1]['message']['link']['messageId']==77


from tests.test_media_retry_lifetime import rt

@pytest.mark.asyncio
@pytest.mark.parametrize('error,attempts,state',[(PhotoUploadProtocolError('bad_schema'),0,'failed'),(DeliveryConfirmationUncertain('review'),0,'failed'),(TimeoutError('transient'),0,'retry'),(TimeoutError('transient'),7,'failed')])
async def test_runtime_failure_policy_retains_item(rt,tmp_path,error,attempts,state):
    from shmax.queue.repository import DeliveryQueueRepository
    from shmax.database.models import DeliveryQueueItem
    from sqlalchemy import select
    cache=tmp_path/'media.jpg';cache.write_bytes(picture())
    async with rt.db.session() as session:
        row=await DeliveryQueueRepository(session).enqueue(direction='telegram_to_max',target_chat_id='42',payload={'media_path':str(cache),'text':'caption'},idempotency_key='unique-source-id')
        await session.commit()
    async def failed_send(rec):raise error
    rt._deliver=failed_send
    from dataclasses import replace
    row=replace(row,attempts=attempts)
    await rt._drain_one(row)
    async with rt.db.session() as session:
        records=(await session.execute(select(DeliveryQueueItem))).scalars().all()
        assert len(records)==1 and records[0].status==state
        assert records[0].idempotency_key=='unique-source-id'
    assert cache.exists()


@pytest.mark.asyncio
async def test_static_sticker_download_convert_real_pymax_upload(tmp_path,monkeypatch):
    from shmax.runtime import AppRuntime
    from shmax.media.pipeline import MediaPipeline
    from shmax.domain.models import Attachment
    from shmax.domain.enums import AttachmentKind
    from shmax.adapters.telegram.bot_api import TelegramBotApiAdapter
    from shmax.adapters.telegram.config import TransportConfig,TransportMode
    adapter,app,calls,http=server(tmp_path,monkeypatch,url='https://iu.oneme.ru/upload?apiToken=TOP_SECRET')
    tg=TelegramBotApiAdapter(TransportConfig(mode=TransportMode.BOT_API_DIRECT,bot_token='123:SECRET'))
    class TGSession:
        api=NS(file_url=lambda token,path:'https://api.telegram.org/file/bot'+token+'/'+path)
        async def stream_content(self,**kwargs):yield picture('WEBP')
    async def get_file(ref):return NS(file_path='stickers/a.webp',file_size=1000)
    tg._bot=NS(get_file=get_file,session=TGSession());tg._started=True
    runtime=AppRuntime.__new__(AppRuntime);runtime._tg=tg;runtime._max=adapter;runtime.media_pipeline=MediaPipeline(tmp_path/'cache')
    stored=await runtime._fetch_media('telegram',Attachment(kind=AttachmentKind.STICKER,payload={'file_id':'sticker'}))
    assert stored.mime=='image/png'
    await adapter.send_media('0',stored.media_ref,caption='[Роман]:')
    assert calls[-1][1]['chatId']==0
    assert len(http)==1 and calls[-1][1]['message']['attaches'][0]['_type']=='PHOTO'


@pytest.mark.asyncio
async def test_sdk_file_exception_logs_redacted(tmp_path,monkeypatch,caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    logging.getLogger().addHandler(caplog.handler)
    for name in ["pymax.api.uploads.service","shmax.adapters.max.pymax_adapter"]:
        logging.getLogger(name).disabled=False
        logging.getLogger(name).propagate=True
        caplog.set_level(logging.DEBUG,logger=name)
    adapter,app,calls,http=server(tmp_path,monkeypatch)
    async def failure(opcode,payload=None):raise ConnectionError('https://signed/TOP_SECRET')
    app.invoke=failure
    path=tmp_path/'file.pdf';path.write_bytes(b'%PDF')
    with pytest.raises(Exception):await adapter.send_media('42','document:'+str(path))
    assert 'TOP_SECRET' not in caplog.text
    assert 'MAX SDK media event' in caplog.text

from tests.test_backup_api import client


def test_manual_retry_resets_budget_and_preserves_same_queue_item(client):
    import sqlite3
    root=client.app.state.paths.root
    with sqlite3.connect(root/'db/shmax.db') as db:
        cursor=db.execute("INSERT INTO delivery_queue(direction,target_chat_id,payload,status,attempts,next_attempt_at,last_error,created_at,updated_at,idempotency_key) VALUES('telegram_to_max','0','{}','failed',8,CURRENT_TIMESTAMP,'schema_error',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'original-source')")
        identity=cursor.lastrowid;db.commit()
    assert client.post(f'/api/queue/{identity}/retry').status_code==200
    with sqlite3.connect(root/'db/shmax.db') as db:
        row=db.execute('SELECT id,status,attempts,last_error,idempotency_key,target_chat_id FROM delivery_queue').fetchone()
        assert row==(identity,'pending',0,None,'original-source','0')
        assert db.execute('SELECT count(*) FROM delivery_queue').fetchone()[0]==1


@pytest.mark.asyncio
@pytest.mark.parametrize('status,error',[(400,PhotoUploadProtocolError),(401,__import__('shmax.adapters.max.errors',fromlist=['MaxAuthError']).MaxAuthError),(429,__import__('shmax.adapters.max.errors',fromlist=['MaxTransportError']).MaxTransportError),(500,__import__('shmax.adapters.max.errors',fromlist=['MaxTransportError']).MaxTransportError)])
async def test_upload_http_failure_classification(tmp_path,monkeypatch,status,error):
    adapter,app,calls,http=server(tmp_path,monkeypatch,status=status)
    path=tmp_path/'photo.jpg';path.write_bytes(picture())
    with pytest.raises(error):await adapter.send_media('0','photo:'+str(path))
    assert not [p for op,p in calls if op==Opcode.MSG_SEND]


@pytest.mark.asyncio
async def test_telegram_document_download_to_sdk_saved_messages(tmp_path,monkeypatch):
    from urllib.parse import unquote
    from shmax.runtime import AppRuntime
    from shmax.media.pipeline import MediaPipeline
    from shmax.domain.models import Attachment
    from shmax.domain.enums import AttachmentKind
    from shmax.adapters.telegram.bot_api import TelegramBotApiAdapter
    from shmax.adapters.telegram.config import TransportConfig,TransportMode
    adapter,app,calls,http=server(tmp_path,monkeypatch)
    tg=TelegramBotApiAdapter(TransportConfig(mode=TransportMode.BOT_API_DIRECT,bot_token='123:SECRET'))
    data=b'%PDF-1.7\nactual document bytes'
    filename='Документ с пробелами.pdf'
    class TGSession:
        api=NS(file_url=lambda token,path:'https://api.telegram.org/file/bot'+token+'/'+path)
        async def stream_content(self,**kwargs):yield data
    async def get_file(ref):return NS(file_path='documents/source.pdf',file_size=len(data))
    tg._bot=NS(get_file=get_file,session=TGSession());tg._started=True
    runtime=AppRuntime.__new__(AppRuntime);runtime._tg=tg;runtime._max=adapter;runtime.media_pipeline=MediaPipeline(tmp_path/'cache')
    stored=await runtime._fetch_media('telegram',Attachment(kind=AttachmentKind.DOCUMENT,payload={'file_id':'document','file_name':filename,'mime_type':'application/pdf'}))
    assert stored.path.read_bytes()==data and stored.filename==filename
    await adapter.send_media('0',stored.media_ref,filename=stored.filename,caption='[Роман]: Документ')
    assert calls[-1][1]['chatId']==0 and calls[-1][1]['message']['attaches'][0]['_type']=='FILE'
    assert len(http)==1 and filename in unquote(http[0][1]['headers']['Content-Disposition'])
