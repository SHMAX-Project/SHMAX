"""E2E external lifecycle boundary: replace Docker only, run real app + hostd."""
import argparse
import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from shmax.host.server import HostdServer
from shmax.host.portable import HostPortable
from shmax.host.updater import _default_readiness

parser=argparse.ArgumentParser();parser.add_argument('--data',required=True);parser.add_argument('--port',required=True,type=int);parser.add_argument('--socket',required=True)
args=parser.parse_args();process=None
root=Path(args.data);root.mkdir(parents=True,exist_ok=True)
opt=root.parent/'deployment';opt.mkdir(exist_ok=True);(opt/'.env').write_text(f'SHMAX_BIND_ADDR=127.0.0.1\nSHMAX_PORT={args.port}\n')
env=dict(os.environ,SHMAX_DATA_DIR=str(root),SHMAX_HOSTD_SOCKET=args.socket,SHMAX_RUNTIME_MODE='test',SHMAX_PORT=str(args.port))
log=open(root.parent/'backend.log','a')

def start():
    global process
    process=subprocess.Popen([sys.executable,'-m','uvicorn','shmax.main:app','--host','127.0.0.1','--port',str(args.port),'--log-level','warning'],env=env,stdout=log,stderr=log)

async def runner(argv):
    global process
    operation=argv[-2]
    if operation=='stop':
        if process and process.poll() is None:
            process.terminate();await asyncio.to_thread(process.wait,20)
    elif operation=='start':start()
    else:raise RuntimeError('unexpected Docker boundary operation')
    return SimpleNamespace(returncode=0)

async def main():
    start();server=HostdServer(socket_path=args.socket)
    server._portable=HostPortable(root,opt,runner=runner,readiness=_default_readiness,ready_seconds=40)
    loop=asyncio.get_running_loop()
    for sig in [signal.SIGTERM,signal.SIGINT]:loop.add_signal_handler(sig,server._stop.set)
    try:await server.serve_forever()
    finally:
        if process and process.poll() is None:
            process.terminate();await asyncio.to_thread(process.wait,20)

asyncio.run(main())
