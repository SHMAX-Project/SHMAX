"""Restricted host-side portable snapshot/restore lifecycle.

The web supplies ONLY a random job ID; host paths/commands are fixed.
Restore survives the initiating HTTP connection and owns stop/swap/start,
readiness, rollback and an fsynced journal for interrupted transactions.
"""
from __future__ import annotations
import asyncio
import fcntl
import json
import os
import re
import shutil
import sqlite3
import time
from pathlib import Path
from ..backup.portable import ROOTS, PortableError, snapshot, extract, validate_stage, rebind_owner
from .updater import _default_runner, _default_readiness

class HostPortable:
    def __init__(self, data=Path('/var/lib/shmax'), opt=Path('/opt/shmax'), *, runner=_default_runner, readiness=_default_readiness, ready_seconds=90):
        self.data=Path(data);self.opt=Path(opt);self.runner=runner;self.readiness=readiness;self.ready_seconds=ready_seconds
        self.tasks=set()

    def job(self, identity):
        if not re.fullmatch('[a-f0-9]{32}',str(identity)): raise PortableError('Недопустимая операция.')
        path=self.data/'backups/portable'/identity
        if path.is_symlink() or path.resolve()!=self.data.resolve()/'backups/portable'/identity or not path.is_dir(): raise PortableError('Операция не найдена.')
        return path

    def lock(self):
        path=self.data/'backups/.portable.lock';path.parent.mkdir(parents=True,exist_ok=True)
        fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except Exception:os.close(fd);raise PortableError('Другая операция резервного копирования уже выполняется.')
        return fd

    def status(self,job,phase,**values):
        target=job/'status.json'; tmp=job/'.status.tmp'
        fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as out:
            json.dump({'phase':phase,**values},out);out.flush();os.fsync(out.fileno())
        # Job directory is destination-owned; preserve readable status after restart.
        owner=job.stat();os.chown(tmp,owner.st_uid,owner.st_gid);os.replace(tmp,target)

    async def command(self,*args):
        result=await asyncio.wait_for(self.runner(['docker','compose','--project-directory',str(self.opt),'-f',str(self.opt/'compose.yaml'),*args,'web']),timeout=60)
        if result.returncode:raise PortableError('Не удалось выполнить операцию контейнера.')

    async def create_snapshot(self,identity):
        job=self.job(identity);fd=self.lock()
        paused=False
        try:
            self.status(job,'snapshot')
            self.journal(job,{'phase':'snapshot','roots':[]})
            await self.command('pause');paused=True
            manifest=await asyncio.to_thread(snapshot,self.data,job/'payload.tar.gz')
            owner=job.stat();os.chown(job/'payload.tar.gz',owner.st_uid,owner.st_gid)
            return manifest
        finally:
            try:
                if paused:
                    await self.command('unpause')
                    self.journal(job,{'phase':'snapshot_done','roots':[]})
            finally:os.close(fd)

    def submit_restore(self,identity):
        job=self.job(identity);fd=self.lock()
        try:
            self.status(job,'queued')
            task=asyncio.create_task(self.restore(identity,fd=fd))
            self.tasks.add(task);task.add_done_callback(self.tasks.discard)
        except Exception:
            os.close(fd);raise
        return {'job_id':identity,'phase':'queued'}

    @staticmethod
    def sync_dirs(*paths):
        for path in paths:
            fd=os.open(path,os.O_RDONLY)
            try:os.fsync(fd)
            finally:os.close(fd)

    def journal(self,job,state):
        tmp=job/'journal.tmp'
        fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as out:
            json.dump(state,out);out.flush();os.fsync(out.fileno())
        os.replace(tmp,job/'journal.json')
        fd=os.open(job,os.O_RDONLY);os.fsync(fd);os.close(fd)

    def owner(self,job):
        request_file=job/'request.json'
        if request_file.is_symlink() or request_file.stat().st_size>4096:raise PortableError('Небезопасная операция.')
        request=json.loads(request_file.read_text())
        with sqlite3.connect(f'file:{self.data / "db/shmax.db"}?mode=ro',uri=True) as db:
            db.row_factory=sqlite3.Row
            row=db.execute('SELECT username,password_hash,role,disabled FROM admin_users WHERE username=?',(request['username'],)).fetchone()
        if not row or row['disabled'] or row['role']!='owner': raise PortableError('Восстановление разрешено только владельцу SHMAX.')
        return dict(row)

    def permissions(self,stage,uid,gid):
        for root,dirs,files in os.walk(stage,followlinks=False):
            os.chown(root,uid,gid);os.chmod(root,0o700)
            for name in files:
                path=Path(root)/name
                if path.is_symlink():raise PortableError('Небезопасный путь восстановления.')
                os.chown(path,uid,gid);os.chmod(path,0o600)

    async def healthy(self):
        # New host deployment configuration is authoritative, never imported.
        values={'SHMAX_BIND_ADDR':'127.0.0.1','SHMAX_PORT':'8787'}
        if (self.opt/'.env').is_file():
            for line in (self.opt/'.env').read_text().splitlines():
                key,_,value=line.partition('=')
                if key in values:values[key]=value.strip().strip('\"\'')
        import ipaddress
        addr=str(ipaddress.IPv4Address(values['SHMAX_BIND_ADDR']))
        if addr=='0.0.0.0':addr='127.0.0.1'
        port=int(values['SHMAX_PORT'])
        if not 1<=port<=65535:raise PortableError('Недопустимый порт нового сервера.')
        deadline=time.monotonic()+self.ready_seconds
        while True:
            if await self.readiness(f'http://{addr}:{port}/health/ready'):return
            if time.monotonic()>=deadline:raise PortableError('Проверка готовности не завершилась успешно.')
            await asyncio.sleep(1)

    async def rollback(self,job,state):
        if not isinstance(state.get('roots'),list) or len(state['roots'])>len(ROOTS) or len(set(state['roots']))!=len(state['roots']) or any(root not in ROOTS for root in state['roots']):
            raise PortableError('Небезопасный журнал восстановления.')
        if any(root not in state['roots'] for root in state.get('installed',[])):
            raise PortableError('Небезопасный журнал восстановления.')
        await self.command('stop')
        old=job/'previous'
        if old.is_symlink():raise PortableError('Небезопасная страховочная копия.')
        for root in reversed(state['roots']):
            target=self.data/root
            if (old/root).is_symlink() or target.is_symlink():raise PortableError('Небезопасный путь восстановления.')
            if (old/root).exists():
                if target.exists():shutil.rmtree(target)
                os.replace(old/root,target)
                self.sync_dirs(self.data,old)
            elif root in state.get('installed',[]):
                shutil.rmtree(target,ignore_errors=True)
        self.journal(job,{'phase':'rolled_back','roots':[]})
        await self.command('start');await self.healthy()

    async def restore(self,identity,*,fd=None):
        job=self.job(identity);fd=self.lock() if fd is None else fd
        state={'phase':'staging','roots':[],'installed':[]};stopped=False
        try:
            # Let the initiating HTTP request return before stopping its container.
            await asyncio.sleep(.5)
            self.status(job,'validating')
            stage=job/'host-stage'
            manifest=await asyncio.to_thread(extract,job/'payload.tar.gz',stage)
            self.status(job,'migrating')
            await asyncio.to_thread(validate_stage,stage,manifest,migrate=True)
            state['phase']='stopping';self.journal(job,state)
            stopped=True
            await self.command('stop')
            # Read destination owner only AFTER requests/workers are stopped.
            owner=self.owner(job)
            identity_stat=(self.data/'db/shmax.db').stat()
            rebind_owner(stage,owner)
            # Keep destination installer bootstrap secret, never import source one.
            setup=self.data/'state/setup_secret'
            if setup.is_file():
                (stage/'state').mkdir(exist_ok=True);shutil.copyfile(setup,stage/'state/setup_secret')
            self.permissions(stage,identity_stat.st_uid,identity_stat.st_gid)
            self.status(job,'restoring')
            previous=job/'previous';previous.mkdir(mode=0o700)
            state['phase']='swapping';self.journal(job,state)
            for root in ROOTS:
                target=self.data/root
                # Journal intent before rename; recovery inspects actual old path.
                state['roots'].append(root);self.journal(job,state)
                if target.exists():
                    os.replace(target,previous/root)
                    self.sync_dirs(self.data,previous)
                if (stage/root).exists():
                    # Record intent before install, so a crash after rename is recoverable.
                    state['installed'].append(root);self.journal(job,state)
                    os.replace(stage/root,target)
                    self.sync_dirs(self.data,stage)
            self.status(job,'restarting');await self.command('start')
            self.status(job,'checking');await self.healthy()
            state['phase']='committed';self.journal(job,state)
            self.status(job,'completed',owner=owner['username'])
            # Keep previous/ as local safety recovery state (never portable-exported).
        except Exception:
            failed=False
            try:
                if state['roots']:await self.rollback(job,state)
                elif stopped:await self.command('start');await self.healthy()
            except Exception:failed=True
            self.status(job,'rollback_failed' if failed else 'failed',detail='Восстановление не завершено. Предыдущее состояние сохранено.' if not failed else 'Автоматическое восстановление предыдущего состояния не удалось. Требуется восстановление владельцем из локальной страховочной копии.')
        finally:
            os.close(fd)
            for name in ['payload.tar.gz','host-stage','preview-stage','upload.shmax']:
                path=job/name
                if path.is_dir():shutil.rmtree(path,ignore_errors=True)
                else:path.unlink(missing_ok=True)

    async def recover_interrupted(self):
        base=self.data/'backups/portable'
        if not base.is_dir():return
        for job in base.iterdir():
            if not re.fullmatch('[a-f0-9]{32}',job.name) or job.is_symlink():continue
            journal=job/'journal.json'
            if journal.is_symlink() or not journal.is_file() or journal.stat().st_size>4096:continue
            try:state=json.loads(journal.read_text())
            except (ValueError,OSError):continue
            if not isinstance(state,dict):continue
            if state.get('phase') in {'snapshot','stopping','swapping'}:
                fd=self.lock()
                try:
                    if state['phase']=='snapshot':
                        await self.command('unpause')
                        self.journal(job,{'phase':'snapshot_done','roots':[]})
                    else:await self.rollback(job,state)
                    self.status(job,'failed',detail='Прерванное восстановление отменено; предыдущее состояние восстановлено.')
                except Exception:self.status(job,'rollback_failed',detail='Прерванное восстановление требует ручного восстановления из локальной страховочной копии.')
                finally:os.close(fd)
