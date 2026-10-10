#!/usr/bin/env python3
"""Generate the standalone installer from its reviewed shell and shared verifier."""
from pathlib import Path
import sys
root=Path(__file__).resolve().parents[1]
verifier=(root/'backend/shmax/host/release_download.py').read_text()
main='''
import sys
try:
    destination=Path(sys.argv[1]); local=sys.argv[2]
    if local:
        import shutil
        candidate=Path(local).resolve()
        metadata=json.loads((candidate/'release-manifest.json').read_text())
        tag=metadata['version']
        for name in ['SHA256SUMS','SHA256SUMS.sig',f'shmax-{tag}.tar.gz']:
            shutil.copyfile(candidate/name,destination/name)
        archive=verify_package(destination,tag)
    else:
        tag,archive=download_latest(destination)
    source=extract_package(archive,destination/'source')
    (destination/'archive-path').write_text(str(archive))
    (destination/'source-path').write_text(str(source))
    (destination/'version').write_text(tag)
except (ReleaseDownloadError, OSError, ValueError, KeyError, tarfile.TarError) as exc:
    print('[!] '+str(exc),file=sys.stderr)
    raise SystemExit(1)
'''
content=(root/'deploy/bootstrap/install-header.sh').read_text()+verifier+main+(root/'deploy/bootstrap/install-footer.sh').read_text()
target=root/'install.sh'
if '--check' in sys.argv:
    if not target.exists() or target.read_text()!=content:raise SystemExit('install.sh is stale; run scripts/generate-bootstrap.py')
else:
    target.write_text(content);target.chmod(0o755)
