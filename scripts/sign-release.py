#!/usr/bin/env python3
"""Offline release signing; the private key is never copied into artifacts."""
import argparse
from pathlib import Path
import subprocess
p=argparse.ArgumentParser();p.add_argument('directory',type=Path);p.add_argument('--key',required=True,type=Path);a=p.parse_args()
root=Path(__file__).resolve().parents[1]
subprocess.run(['python3',str(root/'scripts/check-release.py'),str(a.directory.resolve())],check=True)
subprocess.run(['openssl','dgst','-sha256','-sign',str(a.key.resolve()),'-out',str(a.directory/'SHA256SUMS.sig'),str(a.directory/'SHA256SUMS')],check=True)
subprocess.run(['openssl','dgst','-sha256','-verify',str(root/'deploy/bootstrap/release-public.pem'),'-signature',str(a.directory/'SHA256SUMS.sig'),str(a.directory/'SHA256SUMS')],check=True)
print('Release SHA256 inventory signed and verified against committed public key')
