"""Portable v2: bounded disk snapshot, Argon2id, ordered AES-GCM records.

Only magic/version, salt and nonce prefix are public. Each 1 MiB record
and the mandatory end record authenticate the header, index and length.
Files are fully authenticated before validation/extraction; no active data
is touched by this module. The local v1 backup format remains separate.
"""
from __future__ import annotations
import gzip
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import struct
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath
from argon2.low_level import hash_secret_raw, Type
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from .. import __version__

MAGIC = b'SHMAXP\x02\x00'
CHUNK = 1024 * 1024
MAX_BYTES = 512 * CHUNK
MAX_FILE = 128 * CHUNK
MAX_FILES = 10000
ROOTS = ('db', 'config', 'secrets', 'sessions', 'state')
BAD_PASSWORD = 'Неверный пароль или повреждённая резервная копия.'

class PortableError(ValueError):
    pass

def password_key(password: str, salt: bytes) -> bytes:
    if not isinstance(password, str) or not 10 <= len(password) <= 256:
        raise PortableError('Пароль резервной копии: от 10 до 256 символов.')
    # Fixed parameters, never controlled by archive metadata: 32 MiB on ARMv7.
    return hash_secret_raw(password.encode('utf-8'),salt,3,32768,1,32,Type.ID)

def _private(path: Path, mode='wb'):
    fd=os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,0o600)
    return os.fdopen(fd,mode)

def encrypt(payload: Path, output: Path, password: str):
    salt=secrets.token_bytes(16); prefix=secrets.token_bytes(8)
    header=MAGIC+salt+prefix; cipher=AESGCM(password_key(password,salt))
    try:
        with payload.open('rb') as source, _private(output) as dest:
            dest.write(header); count=0; total=0
            while True:
                block=source.read(CHUNK); total+=len(block)
                if total>MAX_BYTES: raise PortableError('Резервная копия слишком велика.')
                record=struct.pack('>II',count,len(block))
                dest.write(struct.pack('>I',len(block)))
                dest.write(cipher.encrypt(prefix+struct.pack('>I',count),block,header+record))
                count+=1
                if not block: break
            dest.flush(); os.fsync(dest.fileno())
    except Exception:
        output.unlink(missing_ok=True);raise

def decrypt(source: Path, output: Path, password: str):
    if source.stat().st_size>MAX_BYTES+MAX_BYTES//CHUNK*20+64:
        raise PortableError('Резервная копия слишком велика.')
    try:
        with source.open('rb') as src:
            header=src.read(len(MAGIC)+24)
            if len(header)!=len(MAGIC)+24 or not header.startswith(MAGIC): raise ValueError()
            prefix=header[-8:]; cipher=AESGCM(password_key(password,header[len(MAGIC):len(MAGIC)+16]))
            with _private(output) as dest:
                count=0;total=0
                while True:
                    raw=src.read(4)
                    if len(raw)!=4: raise ValueError()
                    length=struct.unpack('>I',raw)[0]
                    if length>CHUNK: raise ValueError()
                    total+=length
                    if total>MAX_BYTES: raise ValueError()
                    ciphertext=src.read(length+16)
                    if len(ciphertext)!=length+16: raise ValueError()
                    block=cipher.decrypt(prefix+struct.pack('>I',count),ciphertext,header+struct.pack('>II',count,length))
                    if length==0:
                        if src.read(1): raise ValueError()
                        break
                    dest.write(block); count+=1
    except Exception as exc:
        output.unlink(missing_ok=True)
        raise PortableError(BAD_PASSWORD) from exc

def digest(path: Path) -> str:
    sha=hashlib.sha256()
    with path.open('rb') as src:
        for block in iter(lambda:src.read(CHUNK),b''): sha.update(block)
    return sha.hexdigest()

def _safe_name(name: str) -> bool:
    parts=PurePosixPath(name).parts
    return bool(parts) and not name.startswith('/') and '\\' not in name and '\x00' not in name and '..' not in parts and ':' not in name and parts[0] in ROOTS and len(name)<512

def schema_revision(db: Path) -> str:
    with sqlite3.connect(f'file:{db}?mode=ro',uri=True) as conn:
        return conn.execute('SELECT version_num FROM alembic_version').fetchone()[0]

def snapshot(data: Path, output: Path) -> dict:
    """Caller must freeze/quiesce the appliance while this snapshot runs."""
    inventory=[];total=0
    with tempfile.TemporaryDirectory(prefix='shmax-portable-snapshot-') as tmp:
        stage=Path(tmp)
        for root in ROOTS:
            base=data/root
            if base.is_symlink(): raise PortableError('Небезопасный путь данных.')
            if not base.exists(): continue
            for src in base.rglob('*'):
                if src.is_symlink(): raise PortableError('Символические ссылки в данных не поддерживаются.')
                if not src.is_file(): continue
                rel=src.relative_to(data); name=rel.as_posix()
                if not _safe_name(name): raise PortableError('Небезопасный путь данных.')
                if src.name.endswith(('.log','.tmp','.pid','.pyc','-wal','-shm','-journal','.lock','.so','.dylib','.dll','.exe')) or src.name in {'setup_secret','current_update.json'} or any(p in {'cache','logs','tmp','__pycache__','bin','.venv'} for p in rel.parts): continue
                target=stage/rel;target.parent.mkdir(parents=True,exist_ok=True)
                with src.open('rb') as f: magic=f.read(16)
                if magic.startswith((b'\x7fELF', b'MZ')): continue
                if magic==b'SQLite format 3\x00':
                    with sqlite3.connect(f'file:{src}?mode=ro',uri=True) as source, sqlite3.connect(target) as dest:
                        pages=source.execute('PRAGMA page_count').fetchone()[0]
                        page_size=source.execute('PRAGMA page_size').fetchone()[0]
                        if pages * page_size > MAX_FILE:raise PortableError('База данных слишком велика.')
                        source.backup(dest, pages=256)
                else:
                    if src.stat().st_size>MAX_FILE: raise PortableError('Файл резервной копии слишком велик.')
                    shutil.copyfile(src,target)
                size=target.stat().st_size;total+=size
                if size>MAX_FILE or total>MAX_BYTES or len(inventory)>=MAX_FILES: raise PortableError('Резервная копия слишком велика.')
                inventory.append({'path':name,'size':size,'sha256':digest(target)})
        db=stage/'db/shmax.db'
        if not db.is_file(): raise PortableError('База данных отсутствует.')
        manifest={'format_version':2,'application_version':__version__,'created_at':int(time.time()),'backup_type':'portable','schema_revision':schema_revision(db),'entries':inventory}
        required={'db/shmax.db'}
        if not required.issubset({e['path'] for e in inventory}): raise PortableError('Неполная резервная копия.')
        (stage/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False))
        with _private(output) as out, tarfile.open(fileobj=out,mode='w:gz',format=tarfile.USTAR_FORMAT) as tar:
            for name in ['manifest.json']+[e['path'] for e in inventory]:
                tar.add(stage/name,arcname=name,recursive=False,filter=lambda info: _tar_info(info))
    return manifest

def _tar_info(info):
    info.uid=info.gid=0;info.uname=info.gname='';info.mode=0o600;info.mtime=int(info.mtime)
    return info

def validate_stage(stage: Path, manifest: dict, *, migrate: bool = False):
    from ..database.migrations import _make_alembic_config, run_migrations_sync, ALEMBIC_DIR
    from alembic.script import ScriptDirectory
    from ..security import SecretStore
    if manifest.get('format_version')!=2 or manifest.get('backup_type')!='portable': raise PortableError('Неподдерживаемый формат резервной копии.')
    def version(v):
        match=re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([A-Za-z0-9.-]+))?(?:\+[A-Za-z0-9.-]+)?',str(v))
        if not match:raise PortableError('Неизвестная версия SHMAX.')
        major,minor,patch,pre=match.groups()
        parts=[]
        if pre:
            for part in pre.split('.'):
                if not part or (part.isdigit() and len(part)>1 and part[0]=='0'):raise PortableError('Неизвестная версия SHMAX.')
                parts.append((0,int(part)) if part.isdigit() else (1,part))
        return (int(major),int(minor),int(patch),pre is None,tuple(parts))
    if version(manifest.get('application_version'))>version(__version__):raise PortableError('Резервная копия создана более новой версией SHMAX. Обновите приложение перед восстановлением.')
    cfg=_make_alembic_config('sqlite:///'+str(stage/'db/shmax.db'));cfg.set_main_option('script_location',str(ALEMBIC_DIR))
    allowed={r.revision for r in ScriptDirectory.from_config(cfg).walk_revisions()}
    revision=manifest.get('schema_revision')
    if revision not in allowed or schema_revision(stage/'db/shmax.db')!=revision: raise PortableError('Несовместимая версия базы данных.')
    if not (stage/'secrets').is_dir(): raise PortableError('Отсутствует материал восстановления секретов.')
    # Inspect first: constructing an empty SecretStore would generate a key.
    from ..security.secret_store import _active_pair_paths
    key,store,_=_active_pair_paths(stage/'secrets')
    if not key or not key.is_file() or not store or not store.is_file(): raise PortableError('Отсутствует ключ или хранилище секретов.')
    SecretStore(stage/'secrets')  # fail closed on missing/corrupt master key
    with sqlite3.connect(stage/'db/shmax.db') as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok': raise PortableError('База данных повреждена.')
    if migrate:
        # Absolute migration path independent of hostd working directory.
        from alembic import command
        command.upgrade(cfg,'head')

def _check_tar_stream(payload: Path):
    """Reject extended headers before tarfile can allocate attacker-sized metadata.

    Portable v2 writes USTAR regular files only. Scan fixed-size headers and
    bounded body chunks; decompressed padding/trailing bytes are bounded too.
    """
    consumed=0;count=0;ended=False
    with gzip.open(payload,'rb') as source:
        while True:
            header=source.read(512);consumed+=len(header)
            if consumed>MAX_BYTES+MAX_FILES*1024+1024*1024:raise PortableError('Резервная копия слишком велика.')
            if not header:break
            if len(header)!=512:raise PortableError('Резервная копия повреждена.')
            if not any(header):ended=True;continue
            if ended:raise PortableError('Небезопасные данные после конца архива.')
            info=tarfile.TarInfo.frombuf(header,'utf-8','surrogateescape')
            count+=1
            if not info.isfile() or info.size<0 or info.size>MAX_FILE or count>MAX_FILES+1:raise PortableError('Небезопасная структура резервной копии.')
            remaining=((info.size+511)//512)*512
            while remaining:
                block=source.read(min(remaining,65536))
                if not block:raise PortableError('Резервная копия повреждена.')
                remaining-=len(block);consumed+=len(block)
                if consumed>MAX_BYTES+MAX_FILES*1024+1024*1024:raise PortableError('Резервная копия слишком велика.')
        if not ended:raise PortableError('Резервная копия повреждена.')

def extract(payload: Path, stage: Path) -> dict:
    stage.mkdir(mode=0o700,exist_ok=False); seen={};total=0;manifest=None
    try:
        _check_tar_stream(payload)
        with tarfile.open(payload,mode='r|gz') as tar:
            for member in tar:
                name=member.name
                if not member.isfile() or (name!='manifest.json' and not _safe_name(name)) or name in seen or member.size>MAX_FILE or len(seen)>MAX_FILES: raise PortableError('Небезопасная структура резервной копии.')
                total+=member.size
                if total>MAX_BYTES: raise PortableError('Резервная копия слишком велика.')
                if name=='manifest.json' and member.size>1024*1024: raise PortableError('Манифест слишком велик.')
                target=stage/name;target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                with tar.extractfile(member) as src, _private(target) as dst:
                    shutil.copyfileobj(src,dst,CHUNK)
                seen[name]={'size':member.size,'sha256':digest(target)}
        manifest=json.loads((stage/'manifest.json').read_text())
        entries=manifest.get('entries')
        if not isinstance(entries,list) or len(entries)!=len(seen)-1: raise PortableError('Неполный манифест.')
        indexed={e['path']:{'size':e['size'],'sha256':e['sha256']} for e in entries}
        actual={k:v for k,v in seen.items() if k!='manifest.json'}
        if indexed!=actual or len(indexed)!=len(entries): raise PortableError('Нарушена целостность резервной копии.')
        validate_stage(stage,manifest)
        return manifest
    except PortableError:
        shutil.rmtree(stage,ignore_errors=True);raise
    except Exception as exc:
        shutil.rmtree(stage,ignore_errors=True);raise PortableError('Резервная копия повреждена или неполна.') from exc

def rebind_owner(stage: Path, owner: dict):
    """Import application/Telegram ACL; replace all web logins with destination owner."""
    from ..security import SecretStore
    db_path=stage/'db/shmax.db'
    with sqlite3.connect(db_path) as db:
        db.execute('DELETE FROM admin_sessions');db.execute('DELETE FROM setup_tokens');db.execute('DELETE FROM admin_users')
        db.execute('INSERT INTO admin_users (username,password_hash,role,disabled,created_at) VALUES (?, ?, ?, 0, CURRENT_TIMESTAMP)',(owner['username'],owner['password_hash'],'owner'))
        db.execute("INSERT INTO settings(key,value,updated_at) VALUES('setup_completed','true',CURRENT_TIMESTAMP) ON CONFLICT(key) DO UPDATE SET value='true'")
        # Source pending work must NEVER auto-replay on a second host. Preserve
        # idempotency keys, maps and completed rows; quarantine unfinished work.
        db.execute("UPDATE delivery_queue SET status='failed',last_error='migration_review_required' WHERE status IN ('pending','retry','processing')")
        db.commit()
    SecretStore(stage/'secrets').set_secret('session_signing_key',secrets.token_urlsafe(32))
