"""Bounded resource history: Linux host /proc, separate cgroup limits.

Read only. No shell, host agent, Docker socket, subprocess or new dependency.
Unavailable measurements stay null; history starts with this application run.
"""
from __future__ import annotations
import asyncio
import os
import shutil
import time
from collections import deque
from pathlib import Path
from sqlalchemy import select,func
from .database.models import TelegramTarget,Route,DeliveryQueueItem
from .group_access import target_ready

class ResourceHistory:
    def __init__(self,data_dir,*,proc=Path('/proc'),cgroup=Path('/sys/fs/cgroup'),clock=time.time,interval=5,capacity=720):
        self.data_dir=Path(data_dir);self.proc=Path(proc);self.cgroup=Path(cgroup)
        self.clock=clock;self.interval=interval;self.samples=deque(maxlen=capacity)
        self._previous=None;self._cg_previous=None;self.task=None

    def _read(self,path):
        try:return path.read_text()
        except (OSError,ValueError):return ''

    def collect(self):
        now=self.clock()
        sample={'at':now,'cpu_percent':None,'memory_used':None,'memory_total':None,'memory_percent':None,
            'disk_used':None,'disk_total':None,'disk_percent':None,'load_1':None,
            'container_memory_used':None,'container_memory_limit':None,'container_cpu_percent':None}
        line=self._read(self.proc/'stat').splitlines()
        if line and line[0].startswith('cpu '):
            values=[int(v) for v in line[0].split()[1:9]]
            total=sum(values);idle=values[3]+(values[4] if len(values)>4 else 0)
            if self._previous:
                delta=total-self._previous[0]
                if delta>0:sample['cpu_percent']=round(max(0,min(100,100*(1-(idle-self._previous[1])/delta))),2)
            self._previous=(total,idle)
        memory={}
        for line in self._read(self.proc/'meminfo').splitlines():
            key,_,value=line.partition(':')
            try:memory[key]=int(value.split()[0])*1024
            except (ValueError,IndexError):continue
        total=memory.get('MemTotal');available=memory.get('MemAvailable')
        if total and available is not None:
            used=max(0,total-available)
            sample.update(memory_used=used,memory_total=total,memory_percent=round(100*used/total,2))
        try:sample['load_1']=float(self._read(self.proc/'loadavg').split()[0])
        except (ValueError,IndexError):pass
        try:
            disk=shutil.disk_usage(self.data_dir)
            sample.update(disk_used=disk.used,disk_total=disk.total,disk_percent=round(100*disk.used/disk.total,2))
        except OSError:pass
        # cgroup v2: distinguish the SHMAX container from the host's RAM/CPU.
        try:
            limit=self._read(self.cgroup/'memory.max').strip()
            sample['container_memory_used']=int(self._read(self.cgroup/'memory.current'))
            sample['container_memory_limit']=int(limit) if limit!='max' else None
        except ValueError:pass
        try:
            stat=dict(line.split() for line in self._read(self.cgroup/'cpu.stat').splitlines())
            usage=int(stat['usage_usec'])/1e6
            quota,period=self._read(self.cgroup/'cpu.max').split()
            cpus=float(quota)/float(period) if quota!='max' else os.cpu_count() or 1
            if self._cg_previous:
                elapsed=now-self._cg_previous[0]
                if elapsed>0 and cpus>0:sample['container_cpu_percent']=round(max(0,min(100,100*(usage-self._cg_previous[1])/elapsed/cpus)),2)
            self._cg_previous=(now,usage)
        except (ValueError,KeyError,ZeroDivisionError):pass
        return sample

    async def sample(self):
        try:self.samples.append(await asyncio.to_thread(self.collect))
        except Exception:
            # A resource probe must never stop the messaging bridge.
            return

    async def start(self):
        await self.sample()
        async def loop():
            while True:
                await asyncio.sleep(self.interval)
                await self.sample()
        self.task=asyncio.create_task(loop(),name='shmax-resource-history')

    async def close(self):
        if self.task:
            self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)

    def snapshot(self):
        return {'interval_seconds':self.interval,'retention_seconds':self.interval*self.samples.maxlen,
            'scope':'linux_host' if (self.proc/'stat').exists() else 'unavailable',
            'history_since':self.samples[0]['at'] if self.samples else None,
            'samples':list(self.samples)}


async def group_summary(db):
    async with db.session() as s:
        targets=(await s.scalars(select(TelegramTarget).order_by(TelegramTarget.id))).all()
        routes=(await s.scalars(select(Route))).all()
        queue=dict((await s.execute(select(DeliveryQueueItem.status,func.count()).group_by(DeliveryQueueItem.status))).all())
    return {'groups':[{'id':t.id,'chat_id':t.chat_id,'title':t.title,'enabled':t.is_active,
        'validated':target_ready(t),'admin_hub':t.is_admin_hub,'auto_add_chats':t.auto_add_chats,
        'routes':sum(1 for r in routes if r.telegram_account_id==t.account_id and r.telegram_chat_id==t.chat_id and r.enabled and r.label!='system'),
        'disabled_routes':sum(1 for r in routes if r.telegram_account_id==t.account_id and r.telegram_chat_id==t.chat_id and not r.enabled),
        } for t in targets],'queue':queue}
