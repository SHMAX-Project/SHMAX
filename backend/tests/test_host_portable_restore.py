import json
import sqlite3
from types import SimpleNamespace as NS
import pytest
from tests.test_portable_backup import appliance
from shmax.backup.portable import snapshot,extract
from shmax.host.portable import HostPortable
from shmax.database.migrations import run_migrations_sync
from shmax.security import SecretStore


def destination(tmp_path):
    root=tmp_path/'destination';(root/'db').mkdir(parents=True)
    run_migrations_sync('sqlite:///'+str(root/'db/shmax.db'))
    SecretStore(root/'secrets').set_secret('destination_marker','keep-on-failure')
    with sqlite3.connect(root/'db/shmax.db') as db:
        db.execute("INSERT INTO admin_users(username,password_hash,role,disabled,created_at) VALUES('new-owner','new-owner-hash','owner',0,CURRENT_TIMESTAMP)");db.commit()
    (root/'state').mkdir();(root/'state/setup_secret').write_text('NEW_HOST_SECRET')
    return root


def prepare(root,source):
    job=root/'backups/portable'/('a'*32);job.mkdir(parents=True)
    (job/'request.json').write_text(json.dumps({'username':'new-owner'}))
    snapshot(source,job/'payload.tar.gz')
    return job


@pytest.mark.asyncio
async def test_host_restore_stops_migrates_rebinds_restarts(appliance,tmp_path):
    root=destination(tmp_path);job=prepare(root,appliance);calls=[]
    opt=tmp_path/'opt';opt.mkdir();(opt/'.env').write_text('SHMAX_BIND_ADDR=192.168.1.22\nSHMAX_PORT=8899\n')
    async def runner(argv):calls.append(argv);return NS(returncode=0)
    urls=[]
    async def ready(url):urls.append(url);return True
    host=HostPortable(root,opt,runner=runner,readiness=ready)
    await host.restore(job.name)
    assert json.loads((job/'status.json').read_text())['phase']=='completed'
    assert [c[-2] for c in calls]==['stop','start']
    assert urls==['http://192.168.1.22:8899/health/ready']
    assert (root/'state/setup_secret').read_text()=='NEW_HOST_SECRET'
    assert SecretStore(root/'secrets').get_secret('telegram_bot_token')=='123:TOP_SECRET'
    with sqlite3.connect(root/'db/shmax.db') as db:
        assert db.execute('SELECT username,password_hash FROM admin_users').fetchall()==[('new-owner','new-owner-hash')]
        assert db.execute('SELECT nickname FROM telegram_users').fetchone()[0]=='Роман'
        assert db.execute("SELECT value FROM settings WHERE key='setup_completed'").fetchone()[0]=='true'
    assert (job/'previous/db/shmax.db').exists()
    assert not (job/'payload.tar.gz').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['start','readiness','rollback'])
async def test_restore_failure_rolls_back_or_reports_failure(appliance,tmp_path,failure):
    root=destination(tmp_path);job=prepare(root,appliance);starts=0;checks=0
    async def runner(argv):
        nonlocal starts
        if argv[-2]=='start':
            starts+=1
            if failure=='start' and starts==1:return NS(returncode=1)
        return NS(returncode=0)
    async def ready(url):
        nonlocal checks
        checks+=1
        return False if failure=='rollback' else (checks>1 if failure=='readiness' else True)
    host=HostPortable(root,tmp_path/'opt',runner=runner,readiness=ready,ready_seconds=0)
    await host.restore(job.name)
    phase=json.loads((job/'status.json').read_text())['phase']
    assert phase==('rollback_failed' if failure=='rollback' else 'failed')
    assert SecretStore(root/'secrets').get_secret('destination_marker')=='keep-on-failure'
    with sqlite3.connect(root/'db/shmax.db') as db:
        assert db.execute('SELECT username FROM admin_users').fetchone()[0]=='new-owner'


@pytest.mark.asyncio
async def test_interrupted_partial_swap_recovers(appliance,tmp_path):
    import os
    root=destination(tmp_path);job=prepare(root,appliance);previous=job/'previous';previous.mkdir()
    os.replace(root/'db',previous/'db');(root/'db').mkdir()
    (root/'db/broken').write_text('partial')
    (job/'journal.json').write_text(json.dumps({'phase':'swapping','roots':['db'],'installed':['db']}))
    async def runner(argv):return NS(returncode=0)
    async def ready(url):return True
    host=HostPortable(root,tmp_path/'opt',runner=runner,readiness=ready)
    await host.recover_interrupted()
    assert (root/'db/shmax.db').exists() and not (root/'db/broken').exists()
    assert json.loads((job/'status.json').read_text())['phase']=='failed'

@pytest.mark.asyncio
@pytest.mark.parametrize('phase,expected',[('snapshot',['unpause']),('stopping',['stop','start'])])
async def test_recovery_before_first_swap_restores_container(tmp_path,phase,expected):
    root=destination(tmp_path);job=root/'backups/portable'/('b'*32);job.mkdir(parents=True)
    (job/'journal.json').write_text(json.dumps({'phase':phase,'roots':[],'installed':[]}))
    calls=[]
    async def runner(argv):calls.append(argv[-2]);return NS(returncode=0)
    async def ready(url):return True
    host=HostPortable(root,tmp_path/'opt',runner=runner,readiness=ready)
    await host.recover_interrupted()
    assert calls==expected
    assert (root/'db/shmax.db').exists()

@pytest.mark.asyncio
async def test_corrupt_journal_does_not_prevent_hostd_start(tmp_path):
    root=destination(tmp_path);job=root/'backups/portable'/('b'*32);job.mkdir(parents=True)
    (job/'journal.json').write_text('{broken')
    await HostPortable(root,tmp_path/'opt').recover_interrupted()
    assert (root/'db/shmax.db').exists()

@pytest.mark.asyncio
async def test_host_mutations_reject_concurrent_portable_operation(tmp_path,monkeypatch):
    from shmax.host.server import HostdServer
    from shmax.host.protocol import HostRequest
    monkeypatch.setenv('SHMAX_DATA_DIR',str(tmp_path))
    server=HostdServer();fd=server._portable.lock()
    try:
        result=await server._dispatch(HostRequest(op='restart',args={}))
        assert result=={'error':'Another host operation is in progress.'}
    finally:
        import os
        os.close(fd)
