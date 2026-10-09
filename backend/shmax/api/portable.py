"""Authenticated portable export/import; hostd owns destructive lifecycle."""
import asyncio
import json
import os
import re
import secrets
import shutil
import time
from pathlib import Path
from fastapi import Request, Depends, HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field
from sqlalchemy import select
from ..backup import portable
from ..database.models import AdminUser
from ..host.protocol import HostHelperProtocol
from ..config import RuntimeMode

class PasswordRequest(BaseModel):
    password: str = Field(min_length=10,max_length=256)
    confirmation: str | None = Field(default=None,max_length=256)


def install_routes(app, *, db, layout, settings, require_admin, require_csrf, limiter):
    from fastapi.exceptions import RequestValidationError
    from fastapi.exception_handlers import request_validation_exception_handler
    from fastapi.responses import JSONResponse

    @app.exception_handler(RequestValidationError)
    async def private_validation(request,exc):
        if request.url.path.startswith('/api/portable/'):
            return JSONResponse(status_code=422,content={'detail':'Некорректные параметры резервной копии. Пароль должен содержать 10–256 символов.'})
        return await request_validation_exception_handler(request,exc)

    operations=asyncio.Lock()
    base=layout.root/'backups/portable'

    async def privileged(request: Request, username=Depends(require_admin)):
        async with db.session() as session:
            user=(await session.execute(select(AdminUser).where(AdminUser.username==username))).scalar_one()
            if user.role not in {'owner','admin'} or user.disabled:raise HTTPException(403,'Требуются права администратора.')
        return username

    def cleanup():
        if not base.exists():return
        for job in base.iterdir():
            if job.is_symlink() or not job.is_dir():continue
            if time.time()-job.stat().st_mtime>86400 and not (job/'previous').exists() and not (job/'journal.json').exists():shutil.rmtree(job,ignore_errors=True)

    def new_job(username):
        base.mkdir(parents=True,exist_ok=True,mode=0o700);cleanup()
        if base.is_symlink():raise HTTPException(400,'Небезопасный путь резервных копий.')
        job=base/secrets.token_hex(16);job.mkdir(mode=0o700)
        (job/'request.json').write_text(json.dumps({'username':username,'created_at':time.time()}));os.chmod(job/'request.json',0o600)
        return job

    def owned_job(identity,username):
        if not re.fullmatch('[a-f0-9]{32}',identity):raise HTTPException(404,'Операция не найдена.')
        job=base/identity
        if job.is_symlink() or job.resolve()!=base.resolve()/identity or not job.is_dir():raise HTTPException(404,'Операция не найдена.')
        try:request=json.loads((job/'request.json').read_text())
        except Exception:raise HTTPException(404,'Операция не найдена.')
        if request['username']!=username:raise HTTPException(403,'Операция принадлежит другому администратору.')
        return job

    def rate(scope,username):
        if limiter.is_locked(scope,username):raise HTTPException(429,'Слишком много попыток. Повторите позже.')
        limiter.record_attempt(scope,username,success=False)

    deps=[Depends(privileged),Depends(require_csrf)]

    @app.post('/api/portable/export',dependencies=deps)
    async def export(req: PasswordRequest, request: Request):
        username=request.state.session_user;rate('portable_export',username)
        if req.password!=req.confirmation:raise HTTPException(400,'Пароли не совпадают.')
        if operations.locked():raise HTTPException(409,'Другая операция уже выполняется.')
        async with operations:
            job=new_job(username)
            try:
                if settings.runtime_mode==RuntimeMode.TEST:
                    await asyncio.to_thread(portable.snapshot,layout.root,job/'payload.tar.gz')
                else:
                    helper=HostHelperProtocol(Path(os.environ.get('SHMAX_HOSTD_SOCKET','/run/shmax/hostd.sock')),timeout_seconds=180)
                    result=await helper.portable_snapshot(job.name)
                    if not result.ok:raise portable.PortableError('Hostd не смог создать согласованный снимок. Экспорт отменён.')
                output=job/'export.shmax'
                await asyncio.to_thread(portable.encrypt,job/'payload.tar.gz',output,req.password)
                # Verify whole authenticated file AND internal inventory before download.
                await asyncio.to_thread(portable.decrypt,output,job/'verify.tar.gz',req.password)
                await asyncio.to_thread(portable.extract,job/'verify.tar.gz',job/'verify-stage')
                for name in ['payload.tar.gz','verify.tar.gz','verify-stage']:
                    path=job/name
                    if path.is_dir():shutil.rmtree(path)
                    else:path.unlink()
                filename=time.strftime('shmax-backup-%Y-%m-%d')+'.shmax'
                limiter.reset('portable_export',username)
                return FileResponse(output,media_type='application/octet-stream',filename=filename,background=BackgroundTask(shutil.rmtree,job,True),headers={'Cache-Control':'no-store'})
            except Exception as exc:
                shutil.rmtree(job,ignore_errors=True)
                detail=str(exc) if isinstance(exc,portable.PortableError) else 'Не удалось создать резервную копию.'
                raise HTTPException(400,detail)

    @app.post('/api/portable/import',dependencies=deps)
    async def upload(request: Request):
        username=request.state.session_user;rate('portable_upload',username)
        async with db.session() as session:
            user=(await session.execute(select(AdminUser).where(AdminUser.username==username))).scalar_one()
            if user.role!='owner':raise HTTPException(403,'Восстановление разрешено только владельцу.')
        if request.headers.get('content-type','').split(';')[0]!='application/octet-stream':raise HTTPException(415,'Выберите файл .shmax.')
        max_upload=portable.MAX_BYTES+1024*1024
        length=request.headers.get('content-length')
        if length and (not length.isdigit() or int(length)>max_upload):raise HTTPException(413,'Резервная копия слишком велика.')
        job=new_job(username);written=0
        try:
            with portable._private(job/'upload.shmax') as dest:
                async for chunk in request.stream():
                    written+=len(chunk)
                    if written>max_upload:raise HTTPException(413,'Резервная копия слишком велика.')
                    dest.write(chunk)
            if written<64:raise HTTPException(400,'Резервная копия повреждена.')
            return {'job_id':job.name,'phase':'uploaded'}
        except BaseException:
            shutil.rmtree(job,ignore_errors=True);raise

    @app.post('/api/portable/{identity}/inspect',dependencies=deps)
    async def inspect(identity: str,req: PasswordRequest,request: Request):
        username=request.state.session_user;job=owned_job(identity,username);rate('portable_decrypt',username)
        if (job/'confirmed').exists():raise HTTPException(409,'Восстановление уже запущено.')
        async with operations:
            (job/'payload.tar.gz').unlink(missing_ok=True);shutil.rmtree(job/'preview-stage',ignore_errors=True)
            try:
                await asyncio.to_thread(portable.decrypt,job/'upload.shmax',job/'payload.tar.gz',req.password)
                manifest=await asyncio.to_thread(portable.extract,job/'payload.tar.gz',job/'preview-stage')
            except portable.PortableError as exc:
                (job/'payload.tar.gz').unlink(missing_ok=True)
                shutil.rmtree(job/'preview-stage',ignore_errors=True)
                raise HTTPException(400,str(exc))
            (job/'validated').write_text(portable.digest(job/'payload.tar.gz'));os.chmod(job/'validated',0o600)
            limiter.reset('portable_decrypt',username)
            return {'job_id':identity,'application_version':manifest['application_version'],'created_at':manifest['created_at'],'schema_revision':manifest['schema_revision'],'contents':sorted({e['path'].split('/')[0] for e in manifest['entries']}),'compatible':True,'warnings':['Остановите старый SHMAX перед восстановлением: два экземпляра не должны одновременно использовать один Telegram Bot/MAX аккаунт.','Новый владелец сохранит свой логин и пароль. Старые веб-сеансы и веб-администраторы не импортируются.','Незавершённые доставки будут приостановлены для ручной проверки, чтобы не повторять сообщения.','MAX или Telegram могут потребовать повторную авторизацию; остальные настройки сохранятся.']}

    @app.post('/api/portable/{identity}/restore',dependencies=deps)
    async def restore(identity: str,request: Request):
        username=request.state.session_user;job=owned_job(identity,username)
        async with db.session() as session:
            user=(await session.execute(select(AdminUser).where(AdminUser.username==username))).scalar_one()
            if user.role!='owner':raise HTTPException(403,'Восстановление разрешено только владельцу.')
        async with operations:
            if not (job/'validated').is_file() or not (job/'payload.tar.gz').is_file():raise HTTPException(400,'Сначала проверьте резервную копию.')
            if (job/'confirmed').exists():raise HTTPException(409,'Восстановление уже запущено.')
            if (job/'validated').read_text()!=portable.digest(job/'payload.tar.gz'):raise HTTPException(400,'Резервная копия изменилась. Повторите проверку.')
            with portable._private(job/'confirmed') as file:file.write(b'confirmed')
            try:result=await app.state.host.portable_restore(identity)
            except Exception:result=None
            if result is None or not result.ok:
                (job/'confirmed').unlink(missing_ok=True)
                raise HTTPException(503,'Hostd недоступен. Данные не изменены; восстановление не запущено.')
            return {'job_id':identity,'phase':'queued','reauthentication_required':True}

    @app.get('/api/portable/{identity}/status',dependencies=deps)
    async def status(identity: str,request: Request):
        job=owned_job(identity,request.state.session_user)
        if not (job/'status.json').is_file():return {'phase':'validated' if (job/'validated').exists() else 'uploaded'}
        return json.loads((job/'status.json').read_text())
