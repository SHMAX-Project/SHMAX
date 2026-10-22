"""The separate root helper must understand backups of the new schema."""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import pytest
from shmax.host.local_update import sync_host_runtime,restore_host_runtime


def inventory(root):
    return {str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts}


def test_existing_v013_host_runtime_receives_version_and_backup_migrations_and_can_roll_back(tmp_path):
    backend=Path(__file__).resolve().parents[1];repo=backend.parent
    if not shutil.which('git') or subprocess.run(['git','rev-parse','--verify','v0.1.3'],cwd=repo,capture_output=True).returncode:
        pytest.skip('historical release tag unavailable in packaged runtime')
    archive=subprocess.check_output(['git','archive','v0.1.3','backend/shmax','backend/alembic','backend/alembic.ini'],cwd=repo)
    old=tmp_path/'old';old.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive)) as t:t.extractall(old,filter='data')
    host=tmp_path/'host';shutil.copytree(old/'backend',host);before=inventory(host)
    saved=tmp_path/'saved';sync_host_runtime(repo,host,saved)
    def probe():
        env={**os.environ,'PYTHONPATH':str(host),'PYTHONDONTWRITEBYTECODE':'1'}
        script="import json,shmax;from shmax.database.migrations import _make_alembic_config;from alembic.script import ScriptDirectory;import shmax.backup.portable as b;print(json.dumps([shmax.__version__,b.__version__,ScriptDirectory.from_config(_make_alembic_config('sqlite://')).get_heads()]))"
        return json.loads(subprocess.check_output([sys.executable,'-P','-B','-c',script],cwd=tmp_path,env=env,text=True))
    assert probe()==['0.2.0','0.2.0',['0012_multi_group']]
    restore_host_runtime(host,saved)
    assert inventory(host)==before
    assert probe()==['0.1.3','0.1.3',['0011_telegram_message_alias']]


def test_partial_host_code_install_rolls_back_existing_and_new_files(tmp_path,monkeypatch):
    source=tmp_path/'source';host=tmp_path/'host'
    for base in [source/'backend/shmax',source/'backend/alembic',host/'shmax']:base.mkdir(parents=True)
    (source/'backend/shmax/VERSION').write_text('0.2.0');(source/'backend/shmax/new.py').write_text('new')
    (source/'backend/alembic.ini').write_text('new config');(source/'backend/alembic/env.py').write_text('new migration')
    (host/'shmax/VERSION').write_text('0.1.3');before=inventory(host)
    replace=os.replace
    def fail(a,b):
        if Path(b)==host/'shmax/new.py':raise OSError('simulated disk failure')
        return replace(a,b)
    monkeypatch.setattr(os,'replace',fail)
    saved=tmp_path/'saved'
    with pytest.raises(OSError):sync_host_runtime(source,host,saved)
    restore_host_runtime(host,saved)
    assert inventory(host)==before
