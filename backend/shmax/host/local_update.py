"""Owner-invoked local release transaction. No network release resolution."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
import urllib.request


class LocalUpdateError(RuntimeError):
    pass


def extract_release(archive: Path, destination: Path) -> Path:
    """Validate paths, duplicates, sizes and the embedded file manifest before use."""
    with tarfile.open(archive, 'r:gz') as tar:
        members = tar.getmembers()
        names = set()
        total = 0
        for m in members:
            p = Path(m.name)
            if p.is_absolute() or '..' in p.parts or not (m.isfile() or m.isdir()) or m.name in names:
                raise LocalUpdateError('unsafe or duplicate archive member')
            names.add(m.name); total += m.size
            if total > 2 * 1024**3:
                raise LocalUpdateError('unpacked release exceeds 2 GiB')
        file_names = {m.name for m in members if m.isfile()}
        prefixes = {Path(n).parts[0] for n in file_names}
        if len(prefixes) != 1:
            raise LocalUpdateError('release must have one root directory')
        prefix = next(iter(prefixes))
        manifest_name = prefix + '/package-manifest.json'
        if manifest_name not in names:
            raise LocalUpdateError('release has no embedded integrity manifest')
        manifest = json.load(tar.extractfile(manifest_name))
        expected = manifest['files']
        if set(expected) != {n[len(prefix)+1:] for n in file_names if n != manifest_name}:
            raise LocalUpdateError('release file inventory mismatch')
        for m in members:
            if m.isdir(): continue
            blob = tar.extractfile(m).read()
            rel = m.name[len(prefix)+1:]
            if m.name != manifest_name and hashlib.sha256(blob).hexdigest() != expected[rel]:
                raise LocalUpdateError('release content checksum mismatch')
            target = destination / m.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)
            target.chmod(0o755 if m.mode & 0o111 else 0o644)
        root = destination / prefix
        for d in root.rglob('*'):
            if d.is_dir(): d.chmod(0o755)
        root.chmod(0o755)
        for required in ['Dockerfile','backend/shmax/VERSION','deploy/compose/compose.yaml','frontend/dist/index.html']:
            if not (root / required).is_file():
                raise LocalUpdateError('incomplete production release')
        return root


def file_sha(path):
    with Path(path).open('rb') as f: return hashlib.file_digest(f, 'sha256').hexdigest()


def replace_web_image(text, image):
    # Only the supported single-service deployment block; never rewrite bind/env.
    match = re.search(r'(?m)^  web:\s*\n', text)
    if not match: raise LocalUpdateError('unsupported Compose layout; web service missing')
    end = re.search(r'(?m)^  [A-Za-z_][^ ]*:', text[match.end():])
    boundary = match.end()+end.start() if end else len(text)
    block = text[match.end():boundary]
    images = list(re.finditer(r'(?m)^    image:.*$', block))
    if len(images)!=1: raise LocalUpdateError('unsupported Compose image configuration')
    block = re.sub(r'(?m)^    image:.*$', '    image: '+image, block)
    return text[:match.end()]+block+text[boundary:]


def read_env(path: Path) -> dict:
    values = {}
    for line in path.read_text().splitlines():
        if line and not line.lstrip().startswith('#') and '=' in line:
            k,v = line.split('=',1); values[k.strip()] = v.strip().strip('\"\'')
    return values


def redact(text: str) -> str:
    text = re.sub(r'https?://\S+', '[URL_REDACTED]', text)
    text = re.sub(r'\b\d{5,}:[A-Za-z0-9_-]+', '[TOKEN_REDACTED]', text)
    text = re.sub(r'(?i)(password|token|secret|api_hash|key)\s*[=:]\s*\S+', r'\1=[REDACTED]', text)
    text = re.sub(r'(?<!\w)\+?\d[\d ()-]{9,}\d(?!\w)', '[NUMBER_REDACTED]', text)
    return text


class LocalReleaseUpdater:
    def __init__(self, opt=Path('/opt/shmax'), data=Path('/var/lib/shmax'), backups=Path('/var/backups/shmax'), timeout=180):
        self.opt, self.data, self.backups, self.timeout = Path(opt), Path(data), Path(backups), timeout
        self.report = None
        self.env = os.environ.copy()

    def run(self, argv, *, check=True, timeout=120):
        r = subprocess.run(argv, capture_output=True, text=True, env=self.env, timeout=timeout)
        if self.report:
            with (self.report/'commands.log').open('a') as f:
                output = '' if argv[:2] == ['docker', 'inspect'] else r.stdout+r.stderr
                f.write(redact(' '.join(argv)+'\n'+output)+'\n')
        if check and r.returncode:
            raise LocalUpdateError('command failed: '+argv[0]+' (protected diagnostic report retained)')
        return r.stdout

    def candidate(self, image, deployment_id):
        self.override = self.report / 'compose.candidate.json'
        self.override.write_text(json.dumps({'services':{'web':{'image':image,'environment':{'SHMAX_DEPLOYMENT_ID':deployment_id}}}}))

    def compose(self, *args):
        argv = ['docker','compose','--project-directory',str(self.opt),'-f',str(self.opt/'compose.yaml')]
        if getattr(self, 'override', None): argv += ['-f',str(self.override)]
        return self.run([*argv,*args])

    def diagnostics(self, container):
        """Called before rollback removes/recreates the failed container."""
        for name, argv in [('container.json',['docker','inspect',container]), ('container.log',['docker','logs','--tail','400',container])]:
            try:
                if name.endswith('.json'):
                    raw=json.loads(self.run(argv,check=False))[0]
                    result={k:raw.get(k) for k in ['Id','Image','State']}
                    result['Config']={k:raw.get('Config',{}).get(k) for k in ['User','WorkingDir','Entrypoint','Cmd']}
                    text=json.dumps(result,indent=2)
                else:
                    r=subprocess.run(argv,capture_output=True,text=True,timeout=20,env=self.env);text=r.stdout+r.stderr
                (self.report/name).write_text(redact(text))
            except Exception as exc:
                (self.report/(name+'.error')).write_text(type(exc).__name__)

    def ready(self, host, port, container, *, integrations=True):
        deadline=time.monotonic()+self.timeout
        while time.monotonic()<deadline:
            try:
                with urllib.request.urlopen(f'http://{host}:{port}/health/ready',timeout=3) as response:
                    health=json.load(response)
                state=json.loads(self.run(['docker','inspect',container],check=False))[0]['State']
                if health.get('status')=='ok' and all(health.get(k) for k in ['database','storage','runtime']) and state.get('Running') and state.get('Health',{}).get('Status') == 'healthy':
                    if not integrations:return True
                    probe=json.loads((self.data/'state/deployment-readiness.json').read_text())
                    if time.time()-probe['at']<20 and probe['boot_id']==self.boot_id and all(not v['configured'] or v['healthy'] for v in probe['integrations'].values()):
                        (self.report/'integrations.json').write_text(json.dumps(probe,indent=2));return True
            except Exception:pass
            time.sleep(2)
        return False

    def apply(self, archive: Path):
        if os.geteuid()!=0:raise LocalUpdateError('run with sudo on the SHMAX host')
        archive=Path(archive).resolve()
        if not archive.is_file() or archive.stat().st_size>256*1024**2:raise LocalUpdateError('missing or oversized release archive')
        # Lock persists outside the deployment tree across rollback.
        self.opt.parent.mkdir(parents=True,exist_ok=True)
        with (self.opt.parent/'.shmax-update.lock').open('a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise LocalUpdateError('another update is running') from None
            return self._apply(archive)

    def _apply(self, archive):
        env=read_env(self.opt/'.env');host=env.get('SHMAX_BIND_ADDR','127.0.0.1');port=env.get('SHMAX_PORT','8787')
        if host in ['0.0.0.0','::']:raise LocalUpdateError('explicit host binding required')
        self.env.update(env)
        stamp=time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'-'+hashlib.sha256(archive.read_bytes()).hexdigest()[:10]
        self.report=self.backups/('update-'+stamp);self.report.mkdir(parents=True,mode=0o700,exist_ok=False)
        self.report.chmod(0o700)
        with tempfile.TemporaryDirectory(prefix='shmax-update-') as tmp:
            source=extract_release(archive,Path(tmp))
            needed=sum(p.stat().st_size for p in self.data.rglob('*') if p.is_file())*2+3*1024**3
            for location in [self.data,self.opt,self.backups]:
                if shutil.disk_usage(location).free<needed:raise LocalUpdateError('insufficient free space for backup/build/rollback')
            container=self.compose('ps','-q','web').strip()
            if not container:raise LocalUpdateError('existing web container missing; refusing clean installation')
            info=json.loads(self.run(['docker','inspect',container]))[0]
            old_image=info['Image'];user=info['Config']['User'];uid,gid=user.split(':') if ':' in user else (user,user)
            if not uid.isdigit() or not gid.isdigit():raise LocalUpdateError('numeric existing container UID/GID required')
            old_env=(self.opt/'.env').read_bytes();old_stat=(self.opt/'.env').stat()
            old_compose=(self.opt/'compose.yaml').read_bytes();compose_stat=(self.opt/'compose.yaml').stat()
            replace_web_image(old_compose.decode(), 'preflight')
            docker_root=self.run(['docker','info','--format','{{.DockerRootDir}}']).strip()
            if shutil.disk_usage(docker_root).free < 3*1024**3:raise LocalUpdateError('insufficient Docker storage space')
            tag='shmax:update-'+stamp.lower();rollback='shmax:rollback-'+stamp.lower()
            self.run(['docker','image','tag',old_image,rollback])
            (self.report/'previous-image-id').write_text(old_image)
            self.run(['tar','--acls','--xattrs','--numeric-owner','-cpf',str(self.report/'deployment.tar'),'-C',str(self.opt.parent),self.opt.name])
            # Build before the outage. Never overwrite the old image's tag.
            self.run(['docker','build','--build-arg','SHMAX_UID='+uid,'--build-arg','SHMAX_GID='+gid,'-t',tag,str(source)],timeout=1800)
            print('Краткий перерыв MAX/Telegram: создаётся проверенная cold backup и обновляется приложение.',flush=True)
            stopped=False
            try:
                stopped=True
                self.compose('stop','web')
                # Cold full backup retains DB, secrets, sessions, settings, permissions.
                cold=self.report/'data.tar'
                self.run(['tar','--acls','--xattrs','--numeric-owner','-cpf',str(cold),'-C',str(self.data.parent),self.data.name],timeout=600)
                verify=self.report/'verify';verify.mkdir(mode=0o700)
                self.run(['tar','--acls','--xattrs','--numeric-owner','-xpf',str(cold),'-C',str(verify)],timeout=600)
                restored=verify/self.data.name
                for original in self.data.rglob('*'):
                    other=restored/original.relative_to(self.data)
                    a,b=original.stat(),other.stat()
                    if (a.st_uid,a.st_gid,a.st_mode&0o7777)!=(b.st_uid,b.st_gid,b.st_mode&0o7777):raise LocalUpdateError('backup ownership/mode mismatch')
                    if original.is_file() and file_sha(original)!=file_sha(other):raise LocalUpdateError('backup content mismatch')
                db=sqlite3.connect('file:'+str(restored/'db/shmax.db')+'?mode=ro',uri=True)
                try:
                    if db.execute('pragma integrity_check').fetchone()[0]!='ok' or db.execute('pragma foreign_key_check').fetchall():raise LocalUpdateError('backup SQLite integrity failure')
                finally:db.close()
                (self.report/'backup.sha256').write_text(file_sha(cold))
                # Pin the exact new image through Compose environment; retain original .env bytes.
                self.boot_id=stamp;self.env['SHMAX_IMAGE']=tag;self.env['SHMAX_DEPLOYMENT_ID']=stamp
                self.candidate(tag, stamp)
                self.compose('up','-d','--no-build','--force-recreate','web')
                new_container=self.compose('ps','-q','web').strip()
                if not self.ready(host,port,new_container):
                    self.diagnostics(new_container)
                    raise LocalUpdateError('application/integration acceptance timeout')
                candidate_text=replace_web_image(old_compose.decode(),tag)
                compose_target=self.opt/'compose.yaml.update.tmp'
                with compose_target.open('x') as f:f.write(candidate_text)
                os.chmod(compose_target,compose_stat.st_mode&0o7777);os.chown(compose_target,compose_stat.st_uid,compose_stat.st_gid)
                os.replace(compose_target,self.opt/'compose.yaml')
                (self.data/'state/update-deployment-id').write_text(stamp)
                (self.data/'state/update-deployment-id').chmod(0o644)
                # Persist only image/id settings; preserve all other bytes, ownership and mode.
                text=old_env.decode();text=re.sub(r'^SHMAX_(?:IMAGE|DEPLOYMENT_ID)=.*\n?', '',text,flags=re.M)
                target=self.opt/'.env.update.tmp'
                with target.open('x') as f:f.write(text.rstrip('\n')+'\nSHMAX_IMAGE='+tag+'\nSHMAX_DEPLOYMENT_ID='+stamp+'\n')
                os.chmod(target,old_stat.st_mode&0o7777);os.chown(target,old_stat.st_uid,old_stat.st_gid);os.replace(target,self.opt/'.env')
                # Install retained source separately; running image and data are never patched.
                retained=self.opt/'releases'/stamp;retained.parent.mkdir(exist_ok=True);shutil.copytree(source,retained)
                host_lib=Path('/opt/shmax-hostd/lib')
                if (host_lib/'shmax').is_dir():
                    # Update the CLI/update code used by subsequent host requests.
                    # Current hostd process remains running; dependencies unchanged.
                    for rel in ['shmax/host/local_update.py','shmax/host/updater.py','shmax/cli/main.py']:
                        installed=host_lib/rel; saved=self.report/'host-code'/rel
                        saved.parent.mkdir(parents=True,exist_ok=True)
                        if installed.is_file():shutil.copy2(installed,saved)
                        temporary=installed.with_suffix('.update.tmp')
                        shutil.copyfile(source/'backend'/rel,temporary)
                        temporary.chmod(0o644);os.replace(temporary,installed)
                # First owner-invoked bootstrap installs local-update capability;
                # legacy commands keep using the separate existing host runtime.
                updater_dir=Path('/opt/shmax-updater')
                updater_dir.mkdir(mode=0o755,exist_ok=True)
                payload=updater_dir/'shmax.pyz'
                staged=updater_dir/'shmax.pyz.new'
                shutil.copyfile(source/'deploy/update/shmax.pyz',staged)
                staged.chmod(0o755);os.replace(staged,payload)
                wrapper=Path('/usr/local/bin/shmax.new')
                wrapper.write_text('#!/bin/sh\nif [ "$1" = update ] && [ "$2" != check ] && [ "$2" != apply ]; then exec python3 /opt/shmax-updater/shmax.pyz "$@"; fi\nexec /opt/shmax-hostd/bin/shmax "$@"\n')
                wrapper.chmod(0o755);os.replace(wrapper,Path('/usr/local/bin/shmax'))
                print('UPDATE VERIFIED; report: '+str(self.report),flush=True)
                return self.report
            except BaseException as exc:
                if stopped:
                    saved_host=self.report/'host-code'
                    if saved_host.exists():
                        for saved in saved_host.rglob('*'):
                            if saved.is_file():shutil.copy2(saved,Path('/opt/shmax-hostd/lib')/saved.relative_to(saved_host))
                    (self.opt/'.env').write_bytes(old_env)
                    os.chmod(self.opt/'.env',old_stat.st_mode&0o7777)
                    os.chown(self.opt/'.env',old_stat.st_uid,old_stat.st_gid)
                    (self.opt/'compose.yaml').write_bytes(old_compose)
                    os.chmod(self.opt/'compose.yaml',compose_stat.st_mode&0o7777)
                    os.chown(self.opt/'compose.yaml',compose_stat.st_uid,compose_stat.st_gid)
                    try:
                        failed=self.compose('ps','-aq','web').strip()
                        if failed:self.diagnostics(failed)
                    except Exception:pass
                    self.candidate(old_image, '')
                    self.env['SHMAX_IMAGE']=old_image;self.env.pop('SHMAX_DEPLOYMENT_ID',None)
                    try:
                        self.compose('up','-d','--no-build','--force-recreate','web')
                        old_container=self.compose('ps','-q','web').strip()
                        if not self.ready(host,port,old_container,integrations=False):raise LocalUpdateError('previous application failed readiness')
                        (self.report/'rollback.json').write_text(json.dumps({'healthy':True,'image':old_image}))
                    except Exception as rollback_exc:
                        raise LocalUpdateError('UPDATE AND ROLLBACK FAILED; protected report: '+str(self.report)) from rollback_exc
                raise LocalUpdateError('update failed; previous image restored; protected report: '+str(self.report)) from exc
