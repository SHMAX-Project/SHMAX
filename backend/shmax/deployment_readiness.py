"""Private local update acceptance evidence; no credentials or identifiers."""
import asyncio
import json
import os
import sqlite3
import time


async def publish_readiness(runtime, path, boot_id):
    journal = getattr(runtime, "journal", None)
    recorded_update = journal is None
    while True:
        result = {'at': time.time(), 'boot_id': boot_id, 'integrations': {}}
        try:
            snapshot = await asyncio.wait_for(runtime.integration_snapshot(), 5)
            for name in ('max','telegram'):
                adapter = runtime.max_adapter if name == 'max' else runtime.telegram_adapter
                configured = snapshot[name]['configured']
                if name == 'telegram':
                    configured = configured or runtime.secrets.has_secret('telegram_bot_token')
                else:
                    state = getattr(getattr(runtime.max_auth, 'snapshot', None), 'state', None)
                    configured = configured or getattr(state, 'value', None) == 'AUTHENTICATED'
                healthy = False
                if configured and adapter.is_real():
                    try:
                        health = await asyncio.wait_for(adapter.health(), 8)
                        healthy = bool(health.get('connected') and health.get('authenticated'))
                        if name == 'telegram': healthy = healthy and health.get('polling') is True
                        else: healthy = healthy and runtime._max_listener_started
                    except Exception: pass
                result['integrations'][name] = {'configured': bool(configured), 'healthy': healthy}
        except Exception:
            result['integrations'] = {n:{'configured':True,'healthy':False} for n in ('max','telegram')}
        target = path.with_suffix('.tmp')
        fd = os.open(target, os.O_WRONLY|os.O_CREAT|os.O_TRUNC, 0o600)
        with os.fdopen(fd,'w') as f: json.dump(result,f)
        os.replace(target,path)
        if not recorded_update:
            try:
                recorded_update = await asyncio.to_thread(journal.record_installed_update, boot_id)
            except (OSError, sqlite3.Error):
                # Readiness remains independent; a transient busy or missing journal
                # is retried from the committed marker on a later iteration.
                journal.failures += 1
        await asyncio.sleep(5)
