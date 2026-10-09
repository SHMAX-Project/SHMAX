// Playwright globalSetup / globalTeardown.
//
// Starts, per Playwright project (desktop + mobile):
//   * one local Telegram Bot API mock (the ONLY mocked boundary —
//     everything else is the production SHMAX app);
//   * one real FastAPI backend (uvicorn, SHMAX_RUNTIME_MODE=test
//     so the MAX side runs against its in-process fake) serving
//     the built SPA;
// and records the ports + setup tokens into .e2e-state.json for
// the specs.

import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { MockBotApi } from './mock-bot-api.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, '..', '..');
const STATE_FILE = path.join(HERE, '.e2e-state.json');

const PROJECTS = {
  'desktop-1440x900': { backendPort: 8765, mockPort: 9901, xraySocksPort: 10833 },
  'mobile-375x812': { backendPort: 8767, mockPort: 9903, xraySocksPort: 10834 },
};

function wait(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function startBackend(name, { backendPort, mockPort, xraySocksPort }, mock) {
  const dataDir = fs.mkdtempSync(path.join(process.env.SHMAX_E2E_TEMP_ROOT || os.tmpdir(), `shmax-demo-${name}-`));
  const venvPython = path.join(REPO, 'backend', '.venv', 'bin', 'python');
  if (!fs.existsSync(venvPython)) {
    throw new Error(`backend venv python not found at ${venvPython}`);
  }
  const env = {
    ...process.env,
    SHMAX_DATA_DIR: dataDir,
    SHMAX_RUNTIME_MODE: 'test',
    SHMAX_HOST: '127.0.0.1',
    SHMAX_PORT: String(backendPort),
    SHMAX_LOG_LEVEL: process.env.E2E_VERBOSE ? 'DEBUG' : 'INFO',
    // External Telegram boundary → local mock.
    SHMAX_TELEGRAM_API_BASE: `${mock.base}/bot{token}/{method}`,
    // E2E timing: the MAX OTP transition lands 3 s after start —
    // the late-start OTP regression (control-review P0 #1) needs
    // the WAITING_CODE transition itself to be observable.
    SHMAX_E2E_OTP_DELAY: '3',
    // Slow verify: VERIFYING_CODE lasts 2.5 s before authenticated.
    SHMAX_E2E_VERIFY_DELAY: '2.5',
    // Network bootstrap: direct Telegram is UNREACHABLE (probe URL
    // points at a dead port) — the wizard must go through Xray.
    SHMAX_TELEGRAM_REACHABILITY_URL: 'http://127.0.0.1:59999',
    // Stub xray binary: proxies the local SOCKS to the mock Bot API.
    SHMAX_XRAY_BINARY: `${REPO}/frontend/e2e/stub-xray.py`,
    SHMAX_XRAY_SOCKS_PORT: String(xraySocksPort),
    SHMAX_E2E_FORWARD: `127.0.0.1:${mock.port}`,
    SHMAX_STUB_ERRLOG: `/tmp/shmax-e2e-stub-${name}.errlog`,
    // Surgical P0 #1: MAX demands 2FA and the password verification
    // phase (VERIFYING_PASSWORD) lasts 2.5 s — observable.
    SHMAX_E2E_2FA: '1',
    SHMAX_E2E_2FA_DELAY: '2.5',
  };
  const proc = spawn(
    venvPython,
    ['-m', 'uvicorn', 'shmax.main:app', '--host', '127.0.0.1', '--port', String(backendPort),
     '--log-level', process.env.E2E_VERBOSE ? 'info' : 'warning'],
    { env, cwd: path.join(REPO, 'backend'), stdio: ['ignore', 'pipe', 'pipe'] },
  );
  proc.stdout.on('data', () => {});
  proc.stderr.on('data', (d) => process.env.E2E_VERBOSE && process.stderr.write(d));
  // Wait for /health/live.
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`http://127.0.0.1:${backendPort}/health/live`);
      if (res.ok) break;
    } catch {
      /* not up yet */
    }
    if (proc.exitCode !== null) {
      throw new Error(`backend ${name} exited early with code ${proc.exitCode}`);
    }
    await wait(300);
  }
  return { proc, dataDir };
}

export async function globalSetup() {
  const state = { projects: {} };
  const mocks = {};
  const backends = {};
  try {
    for (const [name, cfg] of Object.entries(PROJECTS)) {
      const mock = await new MockBotApi().start();
      mocks[name] = mock;
      const backend = await startBackend(name, cfg, mock);
      backends[name] = backend;
      // Mint the setup token from localhost (trusted source).
      const res = await fetch(`http://127.0.0.1:${cfg.backendPort}/api/setup/token`, { method: 'POST' });
      if (!res.ok) throw new Error(`setup token mint failed: ${res.status}`);
      const { token } = await res.json();
      state.projects[name] = {
        backendURL: `http://127.0.0.1:${cfg.backendPort}`,
        mockURL: mock.base,
        setupToken: token,
        xraySocksPort: cfg.xraySocksPort,
      };
    }
    state._mocks = mocks;
    state._backends = Object.fromEntries(
      Object.entries(backends).map(([k, v]) => [k, { pid: v.proc.pid, dataDir: v.dataDir }]),
    );
    // Keep live handles for teardown in a module-level singleton.
    globalThis.__shmaxE2E = { mocks, backends };
    fs.writeFileSync(STATE_FILE, JSON.stringify(state, null, 2));
  } catch (e) {
    // Best-effort cleanup so a failed boot does not leak processes.
    for (const b of Object.values(backends)) b.proc.kill('SIGKILL');
    for (const m of Object.values(mocks)) {
      try { m.server?.closeAllConnections?.(); } catch { /* older node */ }
      await m.stop().catch(() => {});
    }
    throw e;
  }
}

export async function globalTeardown() {
  const handles = globalThis.__shmaxE2E;
  if (handles) {
    for (const m of Object.values(handles.mocks)) await m.stop().catch(() => {});
    for (const b of Object.values(handles.backends)) {
      b.proc.kill('SIGTERM');
      await wait(300);
      if (b.proc.exitCode === null) b.proc.kill('SIGKILL');
    }
  }
  if (fs.existsSync(STATE_FILE)) fs.unlinkSync(STATE_FILE);
}

export default globalSetup;
