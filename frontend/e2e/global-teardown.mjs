// Playwright globalTeardown — stops the backends and Bot API
// mocks started by globalSetup (same process, shared handles).
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const STATE_FILE = path.join(HERE, '.e2e-state.json');

export async function globalTeardown() {
  const handles = globalThis.__shmaxE2E;
  if (handles) {
    // Kill the BACKENDS first: their aiogram long-poll keeps a
    // persistent connection open to the mock, and mock.stop()
    // (server.close) blocks until every connection is gone.
    for (const b of Object.values(handles.backends)) {
      b.proc.kill('SIGTERM');
    }
    await new Promise((r) => setTimeout(r, 500));
    for (const b of Object.values(handles.backends)) {
      if (b.proc.exitCode === null) b.proc.kill('SIGKILL');
    }
    for (const m of Object.values(handles.mocks)) {
      try {
        m.server?.closeAllConnections?.();
      } catch { /* older node */ }
      await m.stop().catch(() => {});
    }
  }
  if (fs.existsSync(STATE_FILE)) fs.unlinkSync(STATE_FILE);
}

export default globalTeardown;
