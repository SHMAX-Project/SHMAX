// P0 #1 (control review) — React-level regression tests for the
// MAX auth state-observation lifecycle.
//
// Reproduces the confirmed bug: the mount-time observer stops on
// not_started; a LATER "Получить код" click must restart
// observation so the OTP input appears automatically.
//
// The backend boundary is mocked (fetch); the component under test
// is the production StepMax.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, act, cleanup } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import React from 'react';
import '@testing-library/jest-dom/vitest';

// Real timers: the observation loop's 1.5 s cadence and the
// scripted backend transitions (3.2-4.5 s) are real-time waits —
// interleaving fake timers with React state updates is the
// classic source of missed transitions in these tests.

// ---- fetch mock machinery -------------------------------------------------

type Handler = (url: string, init: RequestInit | undefined) => Promise<any> | any;

let route: Handler = () => ({ state: 'not_started' });

vi.mock('./api', async () => {
  const actual: any = await vi.importActual('./api');
  return {
    ...actual,
    ApiError: class ApiError extends Error {
      status: number;
      constructor(status: number, message: string) {
        super(message);
        this.status = status;
      }
    },
    maxAuthState: () => route('GET /api/max/auth/state', undefined),
    maxAuthStart: (phone: string) =>
      route('POST /api/max/auth/start', { body: JSON.stringify({ phone }) }),
    maxAuthCode: (code: string) =>
      route('POST /api/max/auth/code', { body: JSON.stringify({ code }) }),
    maxAuthPassword: (password: string) =>
      route('POST /api/max/auth/password', { body: JSON.stringify({ password }) }),
    maxAuthResend: () => route('POST /api/max/auth/resend', undefined),
    maxAuthCancel: () => route('POST /api/max/auth/cancel', undefined),
    maxProfile: () => route('GET /api/max/profile', undefined),
  };
});

import { StepMax } from './wizard';

function snap(state: string, extra: Record<string, unknown> = {}) {
  return {
    state,
    phone_hint: '***8888',
    account_id: state === 'authenticated' ? 'acc-1' : null,
    display_name: state === 'authenticated' ? 'Owner' : null,
    handle: null,
    last_error: null,
    last_error_class: null,
    next_action: 'wait',
    updated_at: 1700000000,
    ...extra,
  };
}

function renderMax(onNext = () => {}) {
  return render(
    <MemoryRouter>
      <StepMax onNext={onNext} onPrev={() => {}} />
    </MemoryRouter>,
  );
}

const settle = (ms: number) => act(() => new Promise((r) => setTimeout(r, ms)));

beforeEach(() => {
  // real timers
});

afterEach(() => {
  cleanup();
});

describe('MAX auth observation lifecycle (P0 #1 control review)', () => {
  it('restarts observation when start() runs after the initial observer stopped', { timeout: 30_000 }, async () => {
    const user = userEvent.setup();
    let backendState = 'not_started';
    let stateRequests = 0;

    route = async (url) => {
      if (url.startsWith('GET /api/max/auth/state')) {
        stateRequests += 1;
        return snap(backendState);
      }
      if (url.startsWith('POST /api/max/auth/start')) {
        // The backend transitions LATER — the UI must observe it.
        backendState = 'connecting';
        setTimeout(() => {
          backendState = 'waiting_code';
        }, 2000);
        return snap('connecting');
      }
      throw new Error(`unexpected ${url}`);
    };

    renderMax();

    // Initial fetch; the observer stops on not_started.
    await settle(150);
    const afterIdle = stateRequests;
    expect(afterIdle).toBeGreaterThanOrEqual(1);
    expect(screen.queryByTestId('max-code-input')).toBeNull();

    // Prove the observer has STOPPED while not_started.
    await settle(2000);
    expect(stateRequests).toBe(afterIdle);

    // NOW the user clicks start — the observer must re-arm and
    // the OTP input must appear WITHOUT any reload.
    await user.type(screen.getByTestId('max-phone-input'), '+79991234567');
    await user.click(screen.getByRole('button', { name: 'Получить код' }));

    // Backend flips to waiting_code 2s after start; the observer
    // polls at 1.5s — the input must appear by ~3.5s.
    await settle(3600);
    expect(screen.getByTestId('max-code-input')).toBeVisible();
    expect(screen.queryByTestId('max-connecting')).toBeNull();
    expect(stateRequests).toBeGreaterThan(afterIdle);
  });

  it('keeps observing through a slow VERIFYING_CODE until authenticated', { timeout: 40_000 }, async () => {
    const user = userEvent.setup();
    let backendState = 'not_started';
    const seen = new Set<string>();

    route = async (url, init) => {
      if (url.startsWith('GET /api/max/auth/state')) {
        seen.add(backendState);
        return snap(backendState);
      }
      if (url.startsWith('POST /api/max/auth/start')) {
        backendState = 'waiting_code';
        return snap(backendState);
      }
      if (url.startsWith('POST /api/max/auth/code')) {
        backendState = 'verifying_code';
        // Slow verify: several polls stay in verifying_code…
        setTimeout(() => {
          backendState = 'authenticated';
        }, 5000);
        return snap('verifying_code');
      }
      if (url.startsWith('GET /api/max/profile')) {
        return { user_id: 'acc-1', display_name: 'Owner', handle: null };
      }
      return snap(backendState);
    };

    renderMax();
    await settle(50);
    // Drive the real flow: phone → start lands in waiting_code →
    // the OTP form appears via the observer → submit.
    await user.type(screen.getByTestId('max-phone-input'), '+79991234567');
    await user.click(screen.getByRole('button', { name: 'Получить код' }));
    await settle(200);
    await user.type(screen.getByTestId('max-code-input'), '12345');
    await user.click(screen.getByRole('button', { name: 'Подтвердить' }));
    await settle(100);

    // Several polls pass while still verifying…
    await settle(3600);
    expect(screen.queryByText('MAX подключён')).toBeNull();
    // …then the authenticated card appears.
    await settle(3000);
    expect(screen.getByText('MAX подключён')).toBeVisible();
    expect(seen.has('verifying_code')).toBe(true);
  });

  it('keeps observing through a slow VERIFYING_PASSWORD until authenticated (slow 2FA)', { timeout: 45_000 }, async () => {
    const user = userEvent.setup();
    let backendState = 'not_started';
    const seen = new Set<string>();

    route = async (url) => {
      if (url.startsWith('GET /api/max/auth/state')) {
        seen.add(backendState);
        return snap(backendState);
      }
      if (url.startsWith('POST /api/max/auth/start')) {
        // MAX demands 2FA right away.
        backendState = 'waiting_password';
        return snap(backendState);
      }
      if (url.startsWith('POST /api/max/auth/password')) {
        backendState = 'verifying_password';
        // Slow verify: well over a second of observation only.
        setTimeout(() => {
          backendState = 'authenticated';
        }, 2500);
        return snap('verifying_password');
      }
      if (url.startsWith('GET /api/max/profile')) {
        return { user_id: 'acc-1', display_name: 'Owner', handle: null };
      }
      return snap(backendState);
    };

    renderMax();
    await settle(50);
    await user.type(screen.getByTestId('max-phone-input'), '+79991234567');
    await user.click(screen.getByRole('button', { name: 'Получить код' }));
    await settle(200);
    // waiting_password → the password form appears automatically.
    const pwd = await screen.findByTestId('max-password-input');
    await user.type(pwd, 'hunter22');
    await user.click(screen.getByRole('button', { name: 'Подтвердить' }));
    await settle(100);

    // >1s of verifying: still observing, not settled on a stable state.
    await settle(1200);
    expect(screen.queryByText('MAX подключён')).toBeNull();
    // …then the authenticated card appears without any reload.
    await settle(3500);
    expect(screen.getByText('MAX подключён')).toBeVisible();
    expect(seen.has('verifying_password')).toBe(true);
  });

  it('resend starts exactly one new round and observation resumes', { timeout: 30_000 }, async () => {
    const user = userEvent.setup();
    let backendState = 'waiting_code';
    let startCalls = 0;
    let resendCalls = 0;
    let stateCalls = 0;

    route = async (url) => {
      if (url.startsWith('GET /api/max/auth/state')) {
        stateCalls += 1;
        return snap(backendState);
      }
      if (url.startsWith('POST /api/max/auth/resend')) {
        resendCalls += 1;
        backendState = 'connecting';
        setTimeout(() => {
          backendState = 'waiting_code';
        }, 3200);
        return snap('connecting');
      }
      if (url.startsWith('POST /api/max/auth/start')) {
        startCalls += 1;
        return snap(backendState);
      }
      return snap(backendState);
    };

    renderMax();
    await settle(150);
    await user.click(screen.getByRole('button', { name: 'Отправить код ещё раз' }));
    await settle(100);

    expect(startCalls).toBe(0); // resend must NOT re-run start()
    expect(resendCalls).toBe(1);

    // New round lands in waiting_code ~3.2s later; the observer
    // picks it up on its next 1.5s tick (worst case ~5s).
    await settle(5500);
    expect(stateCalls).toBeGreaterThan(2);
    // The OTP field is the primary focus again.
    expect(screen.getByTestId('max-code-input')).toBeVisible();
  });
});
