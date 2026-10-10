"""Staged clean-install contract: real copied runtime/daemon, mocked host OS tools."""
import grp
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import sysconfig
import textwrap
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]


def executable(path, text):
    path.write_text(textwrap.dedent(text).lstrip())
    path.chmod(0o755)


@pytest.fixture
def host(tmp_path):
    # Keep AF_UNIX paths below macOS's 104-byte limit.
    import tempfile
    stage = Path(tempfile.mkdtemp(prefix='sh-host-', dir='/tmp')).resolve()
    source = stage / 'source'
    for name in ['deploy', 'backend/shmax', 'backend/alembic']:
        shutil.copytree(ROOT / name, source / name, ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy(ROOT / 'backend/alembic.ini', source / 'backend/alembic.ini')
    filesystem = stage / 'root'
    (filesystem/'etc/systemd/system').mkdir(parents=True)
    (filesystem/'usr/local/bin').mkdir(parents=True)
    bindir = stage / 'bin'; bindir.mkdir()
    calls = stage / 'calls'; calls.write_text('')
    pidfile = stage / 'pid'
    runtime = filesystem / 'opt/shmax-hostd'
    group = grp.getgrgid(os.getgid()).gr_name
    # Only dependency acquisition is replaced. A real isolated venv and the
    # installed source copy are used; existing dependency wheels are reused offline.
    executable(bindir / 'host-python', f'''
        #!{sys.executable}
        import pathlib, subprocess, sys
        if sys.argv[1:3] == ['-m','venv']:
            v = pathlib.Path(sys.argv[3])
            subprocess.run([sys.executable, '-m', 'venv', str(v)], check=True)
            site = v / 'lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages'
            (site / 'test-dependencies.pth').write_text({sysconfig.get_path('purelib')!r} + '\\n')
            launcher = v / 'bin/python'
            launcher.rename(v / 'bin/python-runtime')
            launcher.write_text('#!/usr/bin/env bash\\nif [[ "$1" == "-m" && "$2" == "pip" ]]; then exit 0; fi\\nexec "$(dirname "$0")/python-runtime" "$@"\\n')
            launcher.chmod(0o755)
        else:
            subprocess.run([sys.executable, *sys.argv[1:]], check=True)
    ''')
    executable(bindir / 'systemctl', f'''
        #!{sys.executable}
        import os, pathlib, signal, subprocess, sys, time
        args=sys.argv[1:]; p=pathlib.Path({str(pidfile)!r})
        with open({str(calls)!r},'a') as f: f.write('systemctl '+ ' '.join(args)+'\\n')
        if args[0]=='cat':
            sys.exit(0 if (pathlib.Path({str(filesystem)!r})/'etc/systemd/system'/args[1]).exists() else 1)
        if args[0] in ('stop','disable') and p.exists():
            try: os.kill(int(p.read_text()),signal.SIGTERM)
            except ProcessLookupError: pass
            for _ in range(100):
                if not pathlib.Path({str(filesystem / 'run/shmax/hostd.sock')!r}).exists(): break
                time.sleep(.02)
            p.unlink(missing_ok=True)
        if args[:2]==['enable','--now']:
            sock=pathlib.Path({str(filesystem / 'run/shmax/hostd.sock')!r})
            sock.parent.mkdir(parents=True,exist_ok=True); sock.parent.chmod(0o750)
            env=dict(os.environ, PYTHONPATH={str(runtime / 'lib')!r})
            log=open({str(stage / 'hostd.log')!r},'a')
            proc=subprocess.Popen([{str(runtime / '.venv/bin/python')!r},'-m','shmax.host.server',
                '--socket',str(sock),'--group',{group!r}],env=env,cwd={str(stage)!r},stdout=log,stderr=log)
            p.write_text(str(proc.pid))
        if args[0]=='is-active':
            if not p.exists(): sys.exit(1)
            try: os.kill(int(p.read_text()),0)
            except ProcessLookupError: sys.exit(1)
    ''')
    executable(bindir / 'systemd-analyze', f'''
        #!{sys.executable}
        import os,pathlib,sys
        with open({str(calls)!r},'a') as f: f.write('verify\\n')
        if os.environ.get('FAIL_VERIFY'): sys.exit(1)
        for unit in map(pathlib.Path,sys.argv[2:]):
            text=unit.read_text(); assert '${{SHMAX_OPT}}' not in text
            target=[l.split('=',1)[1].split()[0] for l in text.splitlines() if l.startswith('ExecStart=')][0]
            assert pathlib.Path({str(filesystem)!r} + target).is_file(), target
            assert os.access({str(filesystem)!r} + target, os.X_OK), target
    ''')
    executable(bindir / 'stat', '''
        #!/usr/bin/env bash
        if [[ "$3" == *hostd.sock ]]; then echo '0:8787:660'; else echo '0:8787:750'; fi
    ''')
    executable(bindir / 'setpriv', f'''
        #!{sys.executable}
        import os,sys
        assert sys.argv[1:4]==['--reuid=8787','--regid=8787','--clear-groups']
        os.execv(sys.argv[4],sys.argv[4:])
    ''')
    executable(bindir / 'apt-get', '#!/usr/bin/env bash\nexit 0\n')
    executable(bindir / 'chown', '#!/usr/bin/env bash\nexit 0\n')
    installer = source / 'deploy/installer/install.sh'
    # Redirect filesystem writes only. Installed unit contents stay canonical.
    script = installer.read_text()
    for index, path in enumerate(['/opt/shmax-hostd', '/opt/shmax', '/var/lib/shmax', '/etc/systemd/system', '/run/shmax', '/usr/local/bin/shmax']):
        # Use temporary markers to avoid replacing /opt/shmax in /opt/shmax-hostd twice.
        script = script.replace(path, '@PATH'+str(index)+'@')
    for index, path in enumerate(['/opt/shmax-hostd', '/opt/shmax', '/var/lib/shmax', '/etc/systemd/system', '/run/shmax', '/usr/local/bin/shmax']):
        script = script.replace('@PATH'+str(index)+'@', str(filesystem)+path)
    # Hostd runtime helper is independent of the systemd unit's production path.
    installer.write_text(script)
    env = dict(os.environ, PATH=f'{bindir}:{os.environ["PATH"]}', SHMAX_HOST_PYTHON=str(bindir/'host-python'))

    class Host:
        root=filesystem
        runtime_path=runtime
        call_log=calls
        source_path=source
        def run(self, action, *, fail_verify=False, stdin=None):
            wrappers = '''
                check_production_target() { :; }
                is_debian() { return 0; }
                check_uid_collision() { :; }
                ensure_user() { :; }
                ensure_docker() { :; }
                start_container() { ensure_bind_addr; }
            '''
            return subprocess.run(['bash','-c', f'source "{installer}"\n{wrappers}\n{action}'],
                                  env=dict(env, **({'FAIL_VERIFY':'1'} if fail_verify else {})),
                                  input=stdin, text=True,capture_output=True,timeout=45)
    try:
        yield Host()
    finally:
        if pidfile.exists():
            try: os.kill(int(pidfile.read_text()), signal.SIGTERM)
            except ProcessLookupError: pass
        shutil.rmtree(stage, ignore_errors=True)


def test_clean_layout_runtime_socket_and_idempotent_modes(host):
    result=host.run('do_install')
    assert result.returncode == 0, result.stderr + result.stdout
    boot=host.root/'etc/systemd/system/shmax-boot.service'
    assert boot.read_text() == (ROOT/'deploy/systemd/shmax-boot.service').read_text()
    assert 'ExecStart=/opt/shmax/boot/shmax-boot.sh' in boot.read_text()
    assert '${SHMAX_OPT}' not in boot.read_text()
    assert (host.root/'etc/systemd/system/shmax-hostd.service').exists()
    sock=host.root/'run/shmax/hostd.sock'
    assert sock.is_socket()
    assert stat.S_IMODE(sock.stat().st_mode)==0o660
    assert sock.stat().st_gid==os.getgid()
    assert stat.S_IMODE(sock.parent.stat().st_mode)==0o750
    env=dict(os.environ,PYTHONPATH=str(host.runtime_path/'lib'))
    probe=subprocess.run([str(host.runtime_path/'.venv/bin/python'), '-c',
        '''import sys,importlib.abc
class NoAdapters(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname.split('.')[0] in {'aiogram','telethon','pymax','maxapi','fastapi','uvicorn','PIL','qrcode'}:
   raise ImportError('unexpected host dependency: '+fullname)
sys.meta_path.insert(0,NoAdapters())
import shmax.host.server,shmax.cli.main,shmax.host.updater,shmax.backup.manager
print(shmax.host.server.__file__)'''],env=env,cwd=host.root,text=True,capture_output=True)
    assert probe.returncode==0,probe.stderr
    assert str(host.runtime_path/'lib') in probe.stdout
    cli=subprocess.run([str(host.root/'usr/local/bin/shmax'),'version'],capture_output=True,text=True)
    assert cli.returncode==0,cli.stderr
    preserved=host.root/'var/lib/shmax/state/operator-data';preserved.write_text('keep')
    for mode in ('do_repair','do_repair','do_upgrade','do_upgrade'):
        result=host.run(mode)
        assert result.returncode==0,result.stderr+result.stdout
        assert preserved.read_text()=='keep'
        assert sock.is_socket()
    calls=host.call_log.read_text()
    assert calls.count('verify')==5
    assert 'enable --now shmax-hostd.service' in calls
    assert 'enable shmax-boot.service' in calls
    result=host.run('do_uninstall')
    assert result.returncode==0,result.stderr
    assert preserved.read_text()=='keep'
    assert not boot.exists() and not sock.exists()
    assert not host.runtime_path.exists()
    assert 'disable --now shmax-hostd.service' in host.call_log.read_text()


def test_invalid_systemd_unit_aborts_before_success(host):
    result=host.run('do_install',fail_verify=True)
    assert result.returncode!=0
    assert 'install complete' not in result.stdout
    assert 'enable --now shmax-hostd.service' not in host.call_log.read_text()


def test_purge_requires_confirmation_and_only_then_removes_data(host):
    (host.root/'var/lib/shmax').mkdir(parents=True)
    marker=host.root/'var/lib/shmax/keep';marker.write_text('keep')
    denied=host.run('PURGE=1; do_uninstall',stdin='no\n')
    assert denied.returncode!=0 and marker.exists()
    accepted=host.run('PURGE=1; do_uninstall',stdin='SHMAX\n')
    assert accepted.returncode==0,accepted.stderr
    assert not marker.parent.exists()


@pytest.mark.skipif(shutil.which('systemd-analyze') is None, reason='systemd-analyze unavailable on this host')
def test_actual_systemd_analyze_verify_staged_units(tmp_path):
    root=tmp_path/'root'
    for target in ['opt/shmax/boot/shmax-boot.sh','opt/shmax-hostd/.venv/bin/python']:
        executable_path=root/target; executable_path.parent.mkdir(parents=True,exist_ok=True)
        executable(executable_path,'#!/bin/sh\nexit 0\n')
    units=root/'etc/systemd/system';units.mkdir(parents=True)
    for name in ['shmax-boot.service','shmax-hostd.service']:
        shutil.copy(ROOT/'deploy/systemd'/name,units/name)
    # The isolated root has no OS units. Supply the standard targets needed
    # by service default dependencies, while verifying the real SHMAX units.
    for name in ['sysinit.target','basic.target','shutdown.target','network-online.target','multi-user.target']:
        (units/name).write_text(f'[Unit]\nDescription=Test fixture for {name}\nDefaultDependencies=no\n')
    (units/'docker.service').write_text('[Service]\nExecStart=/opt/shmax/boot/shmax-boot.sh\n')
    result=subprocess.run(['systemd-analyze','verify','--root',str(root),'shmax-boot.service','shmax-hostd.service'],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
