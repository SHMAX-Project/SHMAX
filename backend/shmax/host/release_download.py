"""Official stable-release download, pinned signer, bounded HTTPS and safe extraction."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tarfile
import urllib.error
import urllib.parse
import urllib.request

REPOSITORY = 'SHMAX-Project/SHMAX'
PUBLIC_KEY = '-----BEGIN PUBLIC KEY-----\nMIIBojANBgkqhkiG9w0BAQEFAAOCAY8AMIIBigKCAYEAoO7gR9esXfr5pQ0VH1aA\nraVEYU6llm0vmyUvbxmk6CNwv2Xc66WALiKTVzyp+kjwQrvrOc/Zir4MPkKw9Hu7\n86W81/pkrIvBYdpTmCslg7V8YNXfuANWnWOW0l9IFia2VvUmqNB9mgL/LPbz6DKK\n8mSb8GLVSdW4hKVP7jwq1xJmqTXDjHMVwSsNewjirtf4khRYuuVLvg4vsTzuMwqz\nanBMGrKzgtiZX699tGtIuwvewrP35WiQ80HadODBo+ZRaAlv4bW0QcPetBf84VNe\nvHWtulh7if8CmcXKbzsfw7MjbveSOzVnFOO14JoL+JSaG7jBolc32w3IxFb4ccIy\na/4eqg0nv4HIW4nRpXHYC37KXXAjZUwhih8gTyLumzbX72iLhEPHo0K19v2Tu5Br\nOJ4nYnORVcqo5RsZVE3R8VOzWVic+VEU73FfK/gavWMBILK3sR6Ukvb3hxwr0pxA\nvNxfca6m+X9AGnoOG1Dl8wAEkK1pScq9lH7nqyEENE7RAgMBAAE=\n-----END PUBLIC KEY-----\n'
MAX_ARCHIVE = 256 * 1024**2

class ReleaseDownloadError(RuntimeError):
    pass

class OfficialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl, redirected=True)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def validate_url(url, *, redirected=False):
    p = urllib.parse.urlsplit(url)
    hosts = {'api.github.com', 'github.com'}
    if redirected:
        hosts |= {'release-assets.githubusercontent.com', 'objects.githubusercontent.com'}
    if p.scheme != 'https' or p.hostname not in hosts or p.username or p.password or p.port not in {None, 443} or p.fragment:
        raise ReleaseDownloadError('Untrusted release URL; refusing download')
    if p.hostname == 'github.com' and not p.path.startswith('/'+REPOSITORY+'/releases/download/'):
        raise ReleaseDownloadError('Release belongs to another repository')

def fetch(url, limit):
    validate_url(url)
    req = urllib.request.Request(url, headers={'User-Agent':'SHMAX-installer/1', 'Accept':'application/vnd.github+json'})
    try:
        with urllib.request.build_opener(OfficialRedirect()).open(req, timeout=60) as response:
            data = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ReleaseDownloadError('No published stable SHMAX release is available yet. Draft releases are never installed.') from None
        raise ReleaseDownloadError(f'GitHub download failed (HTTP {exc.code}); no application changes made') from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ReleaseDownloadError('Unable to download from official GitHub; check connectivity and retry') from exc
    if len(data) > limit:
        raise ReleaseDownloadError('Release download exceeds permitted size')
    return data

def verify_package(directory: Path, tag: str):
    if not re.fullmatch(r'v\d+\.\d+\.\d+', tag):
        raise ReleaseDownloadError('Stable version must have form vX.Y.Z')
    name = f'shmax-{tag}.tar.gz'
    key = directory / 'verification-key.pem'
    key.write_text(PUBLIC_KEY)
    if (directory/'SHA256SUMS.sig').stat().st_size != 384:
        raise ReleaseDownloadError('Invalid RSA-3072 release signature size')
    result = subprocess.run(['openssl','dgst','-sha256','-verify',str(key),'-signature',str(directory/'SHA256SUMS.sig'),str(directory/'SHA256SUMS')],capture_output=True)
    if result.returncode:
        raise ReleaseDownloadError('Release signature verification failed; refusing to execute package')
    entries = {}
    for line in (directory/'SHA256SUMS').read_text().splitlines():
        m = re.fullmatch(r'([0-9a-f]{64})  ([A-Za-z0-9_.-]+)', line)
        if not m or m[2] in entries:
            raise ReleaseDownloadError('Invalid signed checksum inventory')
        entries[m[2]] = m[1]
    if name not in entries:
        raise ReleaseDownloadError('Production archive absent from signed inventory')
    archive = directory / name
    if not archive.is_file() or archive.stat().st_size > MAX_ARCHIVE or hashlib.sha256(archive.read_bytes()).hexdigest() != entries[name]:
        raise ReleaseDownloadError('Production archive checksum mismatch')
    return archive

def download_latest(directory: Path, fetcher=fetch):
    release = json.loads(fetcher('https://api.github.com/repos/'+REPOSITORY+'/releases/latest', 2*1024**2))
    tag = release.get('tag_name', '')
    if release.get('draft') or release.get('prerelease') or not re.fullmatch(r'v\d+\.\d+\.\d+', tag):
        raise ReleaseDownloadError('Refusing draft, prerelease or malformed release')
    names = ['SHA256SUMS','SHA256SUMS.sig',f'shmax-{tag}.tar.gz']
    assets = {}
    for asset in release.get('assets', []):
        name = asset.get('name')
        if name in names:
            if name in assets: raise ReleaseDownloadError('Duplicate release asset')
            assets[name] = asset
    if set(assets) != set(names):
        raise ReleaseDownloadError('Stable release lacks signed installation artifacts')
    for name in names:
        asset = assets[name];url = asset.get('browser_download_url', '')
        expected_url = f'https://github.com/{REPOSITORY}/releases/download/{tag}/{name}'
        if url != expected_url or asset.get('state') != 'uploaded':
            raise ReleaseDownloadError('Unexpected release asset origin or state')
        data = fetcher(url, MAX_ARCHIVE if name.endswith('.tar.gz') else 1024*1024)
        digest = asset.get('digest')
        if digest and digest != 'sha256:'+hashlib.sha256(data).hexdigest():
            raise ReleaseDownloadError('GitHub asset digest mismatch')
        (directory / name).write_bytes(data)
    return tag, verify_package(directory, tag)

def extract_package(archive: Path, directory: Path):
    with tarfile.open(archive,'r:gz') as tar:
        members = tar.getmembers(); seen = set(); total = 0; roots = set()
        for member in members:
            p=Path(member.name);total += member.size
            if not p.parts or p.is_absolute() or '..' in p.parts or member.name in seen or not (member.isfile() or member.isdir()) or total > 2*1024**3:
                raise ReleaseDownloadError('Unsafe release archive')
            seen.add(member.name);roots.add(p.parts[0])
        if len(roots)!=1: raise ReleaseDownloadError('Archive must have one root directory')
        root_name=next(iter(roots));manifest_name=root_name+'/package-manifest.json'
        try: manifest=json.load(tar.extractfile(manifest_name))['files']
        except (KeyError, TypeError, ValueError): raise ReleaseDownloadError('Missing package inventory') from None
        files={m.name[len(root_name)+1:]:m for m in members if m.isfile() and m.name!=manifest_name}
        if set(files)!=set(manifest):raise ReleaseDownloadError('Package inventory mismatch')
        for name,member in files.items():
            if hashlib.sha256(tar.extractfile(member).read()).hexdigest()!=manifest[name]:raise ReleaseDownloadError('Package file checksum mismatch')
        tar.extractall(directory,filter='data')
    root=directory/root_name
    if (root/'backend/shmax/VERSION').read_text().strip()!=archive.name.removeprefix('shmax-v').removesuffix('.tar.gz'):
        raise ReleaseDownloadError('Archive version mismatch')
    for name in ['deploy/installer/install.sh','frontend/dist/index.html','Dockerfile']:
        if not (root/name).is_file():raise ReleaseDownloadError('Incomplete production package')
    return root
