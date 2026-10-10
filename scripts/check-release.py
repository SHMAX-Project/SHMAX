#!/usr/bin/env python3
"""Validate actual archive bytes and updater manifest, with no installation."""
import hashlib,json,pathlib,sys,tarfile
out=pathlib.Path(sys.argv[1]);manifest=json.loads((out/'release-manifest.json').read_text())
required={'compose.yaml','deploy/installer/install.sh','backend/shmax/VERSION','backend/pyproject.toml','backend/alembic.ini','LICENSE','NOTICE','frontend/package-lock.json'}
for line in (out/'SHA256SUMS').read_text().splitlines():
 expected,name=line.split('  ');archive=out/name
 assert hashlib.sha256(archive.read_bytes()).hexdigest()==expected,name
 if name=='shmax':
  assert archive.read_bytes().startswith(b'#!/usr/bin/env python3\n')
  assert archive.stat().st_mode & 0o111
  continue
 with tarfile.open(archive) as t:
  members=t.getmembers();prefix=members[0].name.split('/')[0]+'/'
  names={m.name[len(prefix):] for m in members}
  assert required<=names,required-names
  for m in members:
   assert (m.isfile() or m.isdir()) and not m.name.startswith('/') and '..' not in pathlib.PurePosixPath(m.name).parts,m.name
   assert not any(x in {'.git','.venv','node_modules','.env','__pycache__'} for x in pathlib.PurePosixPath(m.name).parts),m.name
   assert not m.name.endswith(('.sqlite','.db','.session','.shmax','.pem','.key','.log')),m.name
  assert t.getmember(prefix+'deploy/installer/install.sh').mode & 0o111
  if '-source.' not in name:assert 'frontend/dist/index.html' in names
  else:assert 'frontend/dist/index.html' not in names
 if name==manifest['artifact']:assert expected==manifest['sha256']
assert manifest['version'].startswith('v') and manifest['channel'] == ('beta' if '-' in manifest['version'] else 'stable')
print('Checksums, safe paths, files, executable installer, source/production distinction and updater manifest: PASS')
