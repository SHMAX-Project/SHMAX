"""Actual archive bytes, restrictive extraction and rollback ordering."""
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile

import pytest

from shmax.host.local_update import extract_release, LocalUpdateError, LocalReleaseUpdater, redact
from shmax.cli.main import build_parser


def package(path, *, corrupt=False, unsafe=False):
    files={n:b'fixture' for n in ['Dockerfile','backend/shmax/VERSION','deploy/compose/compose.yaml','frontend/dist/index.html']}
    files['backend/shmax/api/app.py']=b'fixture_module = True'
    if unsafe:files['../escape']=b'bad'
    manifest={"files":{n:hashlib.sha256(b).hexdigest() for n,b in files.items()}}
    with tarfile.open(path,'w:gz') as t:
        for n,b in {**files,'package-manifest.json':json.dumps(manifest).encode()}.items():
            if corrupt and n=='Dockerfile':b=b'changed'
            m=tarfile.TarInfo('shmax-v0.1.0/'+n);m.size=len(b);m.mode=0o644;t.addfile(m,io.BytesIO(b))
    return path


def test_local_update_command_accepts_archive():
    args=build_parser().parse_args(['update','/tmp/release.tar.gz'])
    assert args.action=='/tmp/release.tar.gz'


def test_extract_under_private_umask_normalizes_nested_code(tmp_path):
    archive=package(tmp_path/'release.tar.gz');old=os.umask(0o077)
    try:root=extract_release(archive,tmp_path/'stage')
    finally:os.umask(old)
    assert (root/'backend/shmax/api').stat().st_mode&0o777==0o755
    assert (root/'backend/shmax/api/app.py').stat().st_mode&0o777==0o644


@pytest.mark.parametrize('kind',['corrupt','unsafe'])
def test_invalid_package_rejected_before_deployment(tmp_path,kind):
    with pytest.raises(LocalUpdateError):extract_release(package(tmp_path/'r.tar.gz',**{kind:True}),tmp_path/'stage')


def test_redaction_preserves_traceback_without_credentials():
    value=redact('ModuleNotFoundError: shmax.api.app token=fixture-secret https://example.invalid/private')
    assert 'ModuleNotFoundError' in value
    assert 'fixture-secret' not in value and 'example.invalid' not in value


def test_diagnostic_report_excludes_environment(tmp_path,monkeypatch):
    updater=LocalReleaseUpdater();updater.report=tmp_path
    raw=[{'Id':'test','Image':'old','State':{'Status':'exited','ExitCode':1},'Config':{'User':'8787:8787','Env':['SECRET=fixture-private']}}]
    monkeypatch.setattr(updater,'run',lambda *a,**k:json.dumps(raw))
    from types import SimpleNamespace
    monkeypatch.setattr('shmax.host.local_update.subprocess.run',lambda *a,**k:SimpleNamespace(stdout='',stderr='ModuleNotFoundError: shmax.api.app'))
    updater.diagnostics('test')
    assert 'fixture-private' not in (tmp_path/'container.json').read_text()
    assert 'ModuleNotFoundError' in (tmp_path/'container.log').read_text()


def test_failed_acceptance_preserves_data_and_diagnoses_before_exact_image_rollback(tmp_path,monkeypatch):
    import shutil
    import sqlite3
    opt=tmp_path/'opt/shmax';opt.mkdir(parents=True)
    (opt/'.env').write_text('SHMAX_BIND_ADDR=192.168.1.20\nSHMAX_PORT=8787\nSECRET=fixture-private\n')
    (opt/'compose.yaml').write_text('services:\n  web:\n    image: old\n')
    data=tmp_path/'data';(data/'db').mkdir(parents=True)
    c=sqlite3.connect(data/'db/shmax.db');c.execute('create table preserved(value text)');c.execute("insert into preserved values ('fixture-session')");c.commit();c.close()
    (data/'session').write_bytes(b'original-session')
    archive=package(tmp_path/'release.tar.gz');events=[]
    class StagedUpdater(LocalReleaseUpdater):
        def run(self,argv,**kw):
            events.append(list(argv))
            if argv[:2]==['docker','inspect']:
                return json.dumps([{'Image':'sha256:original','Config':{'User':f'{os.getuid()}:{os.getgid()}'}}])
            if argv[:2]==['docker','info']:return str(tmp_path)
            if argv[0]=='tar':
                flag=next(a for a in argv if a in ['-cpf','-xpf']);idx=argv.index(flag);path=Path(argv[idx+1]);base=Path(argv[argv.index('-C')+1])
                if flag=='-cpf':
                    with tarfile.open(path,'w') as t:t.add(base/argv[-1],arcname=argv[-1])
                else:
                    with tarfile.open(path) as t:t.extractall(base,filter='data')
            return ''
        def compose(self,*args):
            events.append(['compose',*args,self.env.get('SHMAX_IMAGE','')])
            return 'container' if 'ps' in args else ''
        def ready(self,*a,integrations=True):return not integrations
        def diagnostics(self,container):events.append(['diagnostics',container])
    monkeypatch.setattr(os,'geteuid',lambda:0)
    updater=StagedUpdater(opt,data,tmp_path/'backups')
    original=(opt/'.env').read_bytes();before=(data/'db/shmax.db').read_bytes()
    with pytest.raises(LocalUpdateError,match='previous image restored'):updater.apply(archive)
    assert (opt/'.env').read_bytes()==original
    assert (data/'db/shmax.db').read_bytes()==before
    assert (data/'session').read_bytes()==b'original-session'
    rollback=next(i for i,e in enumerate(events) if e[:2]==['compose','up'] and e[-1]=='sha256:original')
    assert any(e[0]=='diagnostics' for e in events[:rollback])
    assert json.loads((updater.report/'rollback.json').read_text())['healthy'] is True


def test_compose_pins_only_web_image_without_changing_bind():
    from shmax.host.local_update import replace_web_image
    text='services:\n  web:\n    image: old\n    ports:\n      - "172.18.4.97:8787:8787"\n  other:\n    image: unrelated\n'
    changed=replace_web_image(text,'shmax:new')
    assert changed==text.replace('image: old','image: shmax:new')


@pytest.mark.asyncio
async def test_private_readiness_is_fresh_sanitized_and_cancellable(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from shmax.deployment_readiness import publish_readiness
    class Adapter:
        def is_real(self):return True
        async def health(self):return {'connected':True,'authenticated':True,'polling':True,'account_id':'private-owner-id'}
    async def snapshot():return {n:{'configured':True} for n in ('max','telegram')}
    runtime=SimpleNamespace(integration_snapshot=snapshot,max_adapter=Adapter(),telegram_adapter=Adapter(),secrets=SimpleNamespace(has_secret=lambda n:True),max_auth=None,_max_listener_started=True)
    path=tmp_path/'deployment-readiness.json'
    task=asyncio.create_task(publish_readiness(runtime,path,'boot-fixture'))
    try:
        async with asyncio.timeout(2):
            while not path.exists():await asyncio.sleep(0.01)
        result=json.loads(path.read_text());assert result['boot_id']=='boot-fixture'
        assert all(v['healthy'] for v in result['integrations'].values())
        assert 'private-owner-id' not in path.read_text()
        assert path.stat().st_mode&0o777==0o600
    finally:
        task.cancel();await asyncio.gather(task,return_exceptions=True)
    assert task.done()


@pytest.mark.asyncio
async def test_production_remote_apply_uses_same_local_transaction(tmp_path,monkeypatch):
    from shmax.host.updater import HostUpdater
    archive=package(tmp_path/'remote.tar.gz').read_bytes();calls=[]
    async def resolve(channel):return {'version':'v0.1.0','artifact_url':'https://github.com/SHMAX/repository/release.tar.gz','sha256':hashlib.sha256(archive).hexdigest()}
    async def fetch(url):return archive
    def apply(self,path):
        calls.append(Path(path).read_bytes());return tmp_path/'report'
    monkeypatch.setattr(LocalReleaseUpdater,'apply',apply)
    updater=HostUpdater(opt_dir=tmp_path/'opt',data_dir=tmp_path/'data',fetcher=fetch,releases_resolver=resolve)
    result=await updater.apply()
    assert calls==[archive]
    assert result.backup_path==str(tmp_path/'report/data.tar')
