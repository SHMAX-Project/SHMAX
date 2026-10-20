#!/usr/bin/env python3
"""Validate the frozen public branding, screenshot provenance and license inventory."""
import hashlib,json,pathlib,re,struct,tomllib
root=pathlib.Path(__file__).resolve().parents[1]
version=(root/'backend/shmax/VERSION').read_text().strip()
assert re.fullmatch(r'\d+\.\d+\.\d+',version),version
assert tomllib.loads((root/'backend/pyproject.toml').read_text())['project']['version']==version
assert json.loads((root/'frontend/package.json').read_text())['version']==version
assert tomllib.loads((root/'backend/pyproject.toml').read_text())['project']['license']['text']=='AGPL-3.0-or-later'
assert json.loads((root/'frontend/package.json').read_text())['license']=='AGPL-3.0-or-later'
assert 'GNU AFFERO GENERAL PUBLIC LICENSE' in (root/'LICENSE').read_text()
assert 'SPDX-License-Identifier: AGPL-3.0-or-later' in (root/'frontend/public/shmax-duck.svg').read_text()
for name in ('NOTICE','TRADEMARKS.md','THIRD_PARTY_NOTICES.md','release/v0.1.0/DEPENDENCY_LICENSES.json','release/v0.1.0/NODE_LICENSES.json'):
 assert (root/name).is_file(),name
assert len(list((root/'third-party-licenses').rglob('*LICENSE*')))>100
manifest=root/f'release/v{version}/SCREENSHOTS.json'
if not manifest.exists():manifest=root/'release/v0.1.0/SCREENSHOTS.json'
shots=json.loads(manifest.read_text())
assert len(shots)>=10
assert {e['file'] for e in shots}=={p.name for p in (root/'docs/assets/screenshots').glob('*.png')}
for entry in shots:
 p=root/'docs/assets/screenshots'/entry['file'];data=p.read_bytes()
 assert data[:8]==b'\x89PNG\r\n\x1a\n'
 assert struct.unpack('>II',data[16:24])==(entry['width'],entry['height'])
 assert hashlib.sha256(data).hexdigest()==entry['sha256'],p.name
 off=8
 while off<len(data):
  size=int.from_bytes(data[off:off+4],'big');kind=data[off+4:off+8]
  assert kind not in (b'eXIf',b'tEXt',b'zTXt',b'iTXt'),p.name
  off+=size+12
for name,size in [('favicon.png',32),('shmax-icon-256.png',256),('shmax-icon.png',512)]:
 data=(root/'frontend/public'/name).read_bytes()
 assert struct.unpack('>II',data[16:24])==(size,size)
 built=root/'frontend/dist'/name
 if built.exists():assert built.read_bytes()==data,name
print('PASS: version, AGPL metadata, original SVG, dependency notices, current screenshot hashes/privacy metadata, icon sizes and compiled assets')
