#!/usr/bin/env python3
"""E2E stub 'xray' binary.

Implements a minimal SOCKS5 server (no-auth) on the port from the
Xray config and forwards every CONNECT to SHMAX_E2E_FORWARD
(host:port) — which the Playwright global setup points at the mock
Telegram Bot API server. This lets the full wizard flow exercise
getMe THROUGH the selected transport without a real Xray.
"""
import asyncio
import json
import os
import sys


async def handle(client_reader, client_writer, fwd_host, fwd_port):
    try:
        data = await client_reader.read(1024)
        if not data or data[0] != 5:
            return
        client_writer.write(b"\x05\x00")  # no-auth
        await client_writer.drain()
        req = await client_reader.read(1024)
        if len(req) < 10 or req[1] != 1:
            return
        client_writer.write(b"\x05\x00\x00\x01" + b"\x00" * 6)
        await client_writer.drain()
        remote_reader, remote_writer = await asyncio.open_connection(
            fwd_host, fwd_port
        )
        async def pipe(src, dst):
            try:
                while True:
                    chunk = await src.read(65536)
                    if not chunk:
                        break
                    dst.write(chunk)
                    await dst.drain()
            except (OSError, asyncio.CancelledError):
                pass
        await asyncio.gather(
            pipe(client_reader, remote_writer),
            pipe(remote_reader, client_writer),
        )
    except (OSError, asyncio.CancelledError):
        pass
    finally:
        try:
            client_writer.close()
        except Exception:
            pass


async def main():
    cfg_path = sys.argv[sys.argv.index("-c") + 1]
    with open(cfg_path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    # Emulate `xray run -test -c cfg`: validate and exit 0.
    if "-test" in sys.argv:
        if not cfg.get("inbounds") or not cfg.get("outbounds"):
            sys.exit(1)
        print("Configuration OK", flush=True)
        sys.exit(0)
    port = cfg["inbounds"][0]["port"]
    fwd = os.environ.get("SHMAX_E2E_FORWARD", "")
    fwd_host, _, fwd_port = fwd.rpartition(":")
    print("STUB-XRAY READY", flush=True)
    server = await asyncio.start_server(
        lambda r, w: handle(r, w, fwd_host, int(fwd_port)),
        "127.0.0.1", port,
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    import traceback

    err_log = os.environ.get("SHMAX_STUB_ERRLOG")
    try:
        asyncio.run(main())
    except BaseException:
        if err_log:
            with open(err_log, "a", encoding="utf-8") as fh:
                traceback.print_exc(file=fh)
        raise
