"""Disposable guest fixtures, not real messenger credentials."""
import hashlib,json,os,sqlite3,sys
from pathlib import Path
root=Path('/var/lib/shmax')
names=['config/bootstrap-acceptance.json','sessions/max/bootstrap-acceptance.session','sessions/telegram/bootstrap-acceptance.session','secrets/bootstrap-acceptance.enc']
if sys.argv[1]=='seed':
    for name in names:
        p=root/name;p.write_bytes(('synthetic acceptance data: '+name).encode());p.chmod(0o600);os.chown(p,8787,8787)
    db=sqlite3.connect(root/'db/shmax.db');db.execute('create table bootstrap_acceptance (value text)');db.execute("insert into bootstrap_acceptance values ('persisted')");db.commit();db.close()
    expected={name:{'hash':hashlib.sha256((root/name).read_bytes()).hexdigest(),'mode':(root/name).stat().st_mode&0o777,'uid':(root/name).stat().st_uid} for name in names}
    Path('/tmp/expected-shmax-data.json').write_text(json.dumps(expected));print('Synthetic settings, secret and session fixtures saved; SQLite marker inserted')
else:
    for name,v in json.loads(Path('/tmp/expected-shmax-data.json').read_text()).items():
        p=root/name;assert hashlib.sha256(p.read_bytes()).hexdigest()==v['hash'];assert p.stat().st_mode&0o777==v['mode'];assert p.stat().st_uid==v['uid']
    db=sqlite3.connect(root/'db/shmax.db');assert db.execute('select value from bootstrap_acceptance').fetchone()[0]=='persisted';assert db.execute('pragma integrity_check').fetchone()[0]=='ok';db.close()
    print('PASS: saved settings, encrypted-secret fixture, MAX/Telegram session fixtures, permissions and SQLite contents preserved')
