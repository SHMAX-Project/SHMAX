"""Fail-closed tests at the release execution trust boundary."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import pytest
from shmax.host import release_download as rd
from shmax.cli.main import build_parser

@pytest.fixture
def signed(tmp_path,monkeypatch):
    key=tmp_path/'private.pem';public=tmp_path/'public.pem'
    subprocess.run(['openssl','genrsa','-out',str(key),'3072'],check=True,capture_output=True)
    subprocess.run(['openssl','rsa','-in',str(key),'-pubout','-out',str(public)],check=True,capture_output=True)
    monkeypatch.setattr(rd,'PUBLIC_KEY',public.read_text())
    archive=tmp_path/'shmax-v0.1.0.tar.gz'
    files={'backend/shmax/VERSION':b'0.1.0\n','deploy/installer/install.sh':b'#!/bin/bash\n','Dockerfile':b'FROM scratch\n','frontend/dist/index.html':b'<html></html>'}
    inventory={'files':{n:hashlib.sha256(b).hexdigest() for n,b in files.items()}}
    with tarfile.open(archive,'w:gz') as t:
        for name,blob in {**files,'package-manifest.json':json.dumps(inventory).encode()}.items():
            member=tarfile.TarInfo('shmax-v0.1.0/'+name);member.size=len(blob);t.addfile(member,io.BytesIO(blob))
    (tmp_path/'SHA256SUMS').write_text(hashlib.sha256(archive.read_bytes()).hexdigest()+'  '+archive.name+'\n')
    subprocess.run(['openssl','dgst','-sha256','-sign',str(key),'-out',str(tmp_path/'SHA256SUMS.sig'),str(tmp_path/'SHA256SUMS')],check=True)
    return tmp_path

def test_valid_signature_and_complete_inventory_extract(signed,tmp_path):
    archive=rd.verify_package(signed,'v0.1.0');root=rd.extract_package(archive,tmp_path/'extract')
    assert (root/'backend/shmax/VERSION').read_text().strip()=='0.1.0'

@pytest.mark.parametrize('target',['SHA256SUMS','SHA256SUMS.sig','shmax-v0.1.0.tar.gz'])
def test_modified_download_rejected(signed,target):
    p=signed/target;p.write_bytes(p.read_bytes()+b'tampered')
    with pytest.raises(rd.ReleaseDownloadError):rd.verify_package(signed,'v0.1.0')

def test_other_signer_rejected(signed,monkeypatch):
    monkeypatch.setattr(rd,'PUBLIC_KEY',(Path(__file__).parents[2]/'deploy/bootstrap/release-public.pem').resolve().read_text())
    with pytest.raises(rd.ReleaseDownloadError,match='signature'):rd.verify_package(signed,'v0.1.0')

@pytest.mark.parametrize('url',['http://github.com/SHMAX-Project/SHMAX/releases/download/v0.1.0/a','https://example.org/a','https://github.com/other/repo/releases/download/v0.1.0/a','https://user:pass@github.com/SHMAX-Project/SHMAX/releases/download/v0.1.0/a'])
def test_untrusted_urls_rejected(url):
    with pytest.raises(rd.ReleaseDownloadError):rd.validate_url(url)

@pytest.mark.parametrize('flags',[{'draft':True},{'prerelease':True},{'tag_name':'v0.1.0-beta'}])
def test_unpublished_release_rejected_before_assets(flags,tmp_path):
    calls=[]
    def fetch(url,limit):
        calls.append(url);return json.dumps({'tag_name':'v0.1.0',**flags}).encode()
    with pytest.raises(rd.ReleaseDownloadError):rd.download_latest(tmp_path,fetch)
    assert len(calls)==1

def test_official_download_checks_asset_digest(signed,tmp_path):
    names=['SHA256SUMS','SHA256SUMS.sig','shmax-v0.1.0.tar.gz'];assets=[]
    for name in names:
        blob=(signed/name).read_bytes();assets.append({'name':name,'state':'uploaded','digest':'sha256:'+hashlib.sha256(blob).hexdigest(),'browser_download_url':f'https://github.com/{rd.REPOSITORY}/releases/download/v0.1.0/{name}'})
    def fetch(url,limit):
        if url.endswith('/latest'):return json.dumps({'tag_name':'v0.1.0','assets':assets}).encode()
        return (signed/url.rsplit('/',1)[1]).read_bytes()
    destination=tmp_path/'download';destination.mkdir()
    assert rd.download_latest(destination,fetch)[0]=='v0.1.0'
    assets[-1]['digest']='sha256:'+'0'*64
    with pytest.raises(rd.ReleaseDownloadError,match='digest'):rd.download_latest(destination,fetch)

def test_update_without_argument_resolves_stable():
    assert build_parser().parse_args(['update']).action=='latest'

def test_bootstrap_generated_source_is_current():
    root=Path(__file__).parents[2]
    subprocess.run(['python3',str(root/'scripts/generate-bootstrap.py'),'--check'],check=True)
    subprocess.run(['bash','-n',str(root/'install.sh')],check=True)
