"""Bounded v1 → portable staging conversion; never touches active data.

Supports the original gzip-tar and SHMX1 Argon2id/AES-GCM backups. Fully
authenticate encrypted input before parsing it; verify every manifest entry,
then repack only portable data roots. Old web logins are rebound by hostd.
"""
from __future__ import annotations
import base64
import gzip
import json
import shutil
import tarfile
import tempfile
from pathlib import Path,PurePosixPath
from argon2.low_level import hash_secret_raw,Type
from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
from . import portable as p

MAGIC=b'SHMX1\x00'


def decrypt_v1(source,output,password):
    if not 8<=len(password)<=256:raise p.PortableError(p.BAD_PASSWORD)
    try:
        with source.open('rb') as src:
            if src.readline(32).rstrip(b'\r\n')!=MAGIC:raise ValueError()
            salt=base64.b64decode(src.readline(64).strip(),validate=True)
            nonce=base64.b64decode(src.readline(64).strip(),validate=True)
            if len(salt)!=16 or len(nonce)!=12:raise ValueError()
            key=hash_secret_raw(password.encode(),salt,3,64*1024,4,32,Type.ID)
            cipher=Cipher(algorithms.AES(key),modes.GCM(nonce)).decryptor()
            cipher.authenticate_additional_data(MAGIC)
            remainder=b'';tail=b'';total=0;ended=False
            with p._private(output) as dest:
                while not ended:
                    block=src.read(65536)
                    if not block:raise ValueError()
                    if b'\n' in block:
                        block=block.split(b'\n',1)[0].rstrip(b'\r');ended=True
                    remainder+=block
                    size=len(remainder)//4*4
                    decoded=base64.b64decode(remainder[:size],validate=True)
                    remainder=remainder[size:]
                    tail+=decoded
                    if len(tail)>16:
                        part=tail[:-16];tail=tail[-16:]
                        total+=len(part)
                        if total>p.MAX_BYTES:raise ValueError()
                        dest.write(cipher.update(part))
                if remainder or len(tail)!=16:raise ValueError()
                dest.write(cipher.finalize_with_tag(tail))
    except Exception as exc:
        output.unlink(missing_ok=True)
        raise p.PortableError(p.BAD_PASSWORD) from None


def _check_tar(payload):
    consumed=0;count=0;ended=False
    limit=p.MAX_BYTES+p.MAX_FILES*1024+1024*1024
    with gzip.open(payload,'rb') as source:
        while True:
            raw=source.read(512);consumed+=len(raw)
            if consumed>limit:raise p.PortableError('Резервная копия слишком велика.')
            if not raw:break
            if len(raw)!=512:raise ValueError()
            if not any(raw):ended=True;continue
            if ended:raise ValueError()
            item=tarfile.TarInfo.frombuf(raw,'utf-8','surrogateescape');count+=1
            # Python's v1 tar writer can add bounded PAX path metadata.
            if item.type not in {tarfile.REGTYPE,tarfile.AREGTYPE,tarfile.DIRTYPE,tarfile.XHDTYPE}:raise ValueError()
            maximum=65536 if item.type==tarfile.XHDTYPE else p.MAX_FILE
            if item.size<0 or item.size>maximum or count>p.MAX_FILES*2+2:raise ValueError()
            remaining=(item.size+511)//512*512
            while remaining:
                block=source.read(min(remaining,65536))
                if not block:raise ValueError()
                remaining-=len(block);consumed+=len(block)
                if consumed>limit:raise p.PortableError('Резервная копия слишком велика.')
        if not ended:raise ValueError()


def _name(name):
    parts=PurePosixPath(name).parts
    return bool(parts) and len(name)<512 and not name.startswith('/') and '\\' not in name and ':' not in name and '\x00' not in name and '..' not in parts and '/'.join(parts)==name.rstrip('/')


def _extract(payload,stage):
    _check_tar(payload)
    stage.mkdir(mode=0o700)
    seen={};total=0
    with tarfile.open(payload,mode='r|gz') as tar:
        for item in tar:
            name=item.name
            if not _name(name) or item.issym() or item.islnk():raise ValueError()
            if item.isdir():continue
            if not item.isfile() or name in seen or item.size>p.MAX_FILE or len(seen)>=p.MAX_FILES+2:raise ValueError()
            if name in {'manifest.json','backup_format.txt'} and item.size>1024*1024:raise ValueError()
            total+=item.size
            if total>p.MAX_BYTES:raise p.PortableError('Резервная копия слишком велика.')
            target=stage/name;target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            with tar.extractfile(item) as src,p._private(target) as dest:shutil.copyfileobj(src,dest,p.CHUNK)
            seen[name]={'size':item.size,'sha256':p.digest(target)}
    manifest=json.loads((stage/'manifest.json').read_text())
    if manifest.get('format')!='shmax-backup-v1':raise ValueError()
    entries=manifest.get('entries')
    if not isinstance(entries,list) or len(entries)>p.MAX_FILES:raise ValueError()
    indexed={e['path']:{'size':e['size'],'sha256':e['sha256']} for e in entries}
    actual={k:v for k,v in seen.items() if k not in {'manifest.json','backup_format.txt'}}
    if len(indexed)!=len(entries) or indexed!=actual:raise p.PortableError('Нарушена целостность старой резервной копии.')
    return manifest


def convert(source:Path,output:Path,password:str):
    """Produce a verified portable payload with original version provenance."""
    if source.stat().st_size>p.MAX_BYTES+1024*1024:raise p.PortableError('Резервная копия слишком велика.')
    try:
        with tempfile.TemporaryDirectory(prefix='legacy-',dir=output.parent) as temp:
            root=Path(temp)
            with source.open('rb') as src:magic=src.read(8)
            payload=source
            if magic.startswith(MAGIC):
                payload=root/'authenticated.tar.gz';decrypt_v1(source,payload,password)
            elif not magic.startswith(b'\x1f\x8b'):
                raise p.PortableError('Неподдерживаемый формат старой резервной копии.')
            legacy=_extract(payload,root/'source')
            # snapshot omits executable/runtime files and host deployment state.
            modern=p.snapshot(root/'source',root/'modern.tar.gz')
            modern.update(application_version=legacy['shmax_version'],created_at=legacy['created_at'])
            # Repack the source, replacing only the generated v2 manifest.
            stage=root/'modern'
            p.extract(root/'modern.tar.gz',stage)
            (stage/'manifest.json').write_text(json.dumps(modern,separators=(',',':')))
            p.validate_stage(stage,modern)
            with p._private(output) as dest,tarfile.open(fileobj=dest,mode='w:gz',format=tarfile.USTAR_FORMAT) as tar:
                for entry in modern['entries']:
                    file=stage/entry['path'];info=tarfile.TarInfo(entry['path']);info.size=file.stat().st_size;info.mode=0o600
                    with file.open('rb') as src:tar.addfile(info,src)
                raw=json.dumps(modern,separators=(',',':')).encode();info=tarfile.TarInfo('manifest.json');info.size=len(raw);info.mode=0o600
                import io
                tar.addfile(info,io.BytesIO(raw))
            return modern
    except p.PortableError:
        output.unlink(missing_ok=True);raise
    except Exception:
        output.unlink(missing_ok=True)
        raise p.PortableError('Старая резервная копия повреждена, неполна или не поддерживается.') from None
