import React from 'react';
import { beforeEach, afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import './i18n';
import i18n from './i18n';
import App from './App';
import { NavigationSafetyProvider } from './navigation';

let session: any;
let authenticatedMax = false;
let calls: Array<[string, RequestInit | undefined]>;
const component = { configured: false, state: 'NOT_CONFIGURED', authenticated: false, connected: false, account_id: null, bot_id: null };
function mount(path: string) {
  const router = createMemoryRouter([{ path: '*', element: <NavigationSafetyProvider><App /></NavigationSafetyProvider> }], { initialEntries: [path] });
  render(<RouterProvider router={router} />);
  return router;
}
beforeEach(() => {
  // jsdom AbortSignal and Node's native fetch Request are different realms.
  // There are no loaders in these route tests; browser E2E covers native signals.
  const NativeRequest = Request;
  vi.stubGlobal('Request', class extends NativeRequest {
    constructor(input: RequestInfo | URL, init?: RequestInit) { super(input, { ...init, signal: undefined }); }
  });
  session = { authenticated: true, admin_exists: true, setup_complete: false, setup_dismissed: false, username: 'owner', csrf_token: 'csrf', mode: 'AUTHENTICATED' };
  authenticatedMax = false; calls = [];
  i18n.changeLanguage('ru');
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    calls.push([path, init]);
    let body: any = {};
    if (path === '/api/auth/session') body = session;
    if (path === '/api/setup/dismiss') { session = { ...session, setup_dismissed: true }; body = { ok: true }; }
    if (path === '/api/status') body = { max: component, telegram: component, bridge: { state: 'NOT_CONFIGURED', worker_running: false }, queue: {}, database: 'ok', version: 'test' };
    if (path === '/api/max/auth/state') body = { state: authenticatedMax ? 'authenticated' : 'not_started' };
    if (path === '/api/max/profile') body = { display_name: 'Existing MAX', user_id: '42' };
    if (path === '/api/telegram/token/preview') body = { saved: true, preview: '123…' };
    if (path === '/api/telegram/member-sync') body = { configured: false, running: false, status: 'not_configured' };
    if (path === '/api/recent-activity') body = { items: [] };
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

it.each(['/dashboard', '/max', '/telegram', '/max/settings', '/telegram/settings', '/chats/settings', '/setup/max'])('logo returns to dashboard from %s without resetting sessions', async path => {
  const router = mount(path);
  if (path === '/setup/max') await screen.findByTestId('max-phone-input');
  else await screen.findByTestId('app-shell');
  const brands = screen.getAllByRole('link', { name: 'SHMAX — На главную' });
  await userEvent.click(brands[0]);
  await waitFor(() => expect(router.state.location.pathname).toBe('/dashboard'));
  expect(await screen.findByTestId('incomplete-setup')).toBeVisible();
  expect(calls.filter(([, init]) => init?.method && init.method !== 'GET')).toHaveLength(0);
  expect(session.authenticated).toBe(true);
});
it('blocks unsaved MAX edits, then permits confirmed discard without an API write', async () => {
  const router = mount('/max/settings');
  fireEvent.change(await screen.findByTestId('max-phone-input'), { target: { value: '+79991234567' } });
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
  await userEvent.click(screen.getAllByRole('link', { name: 'SHMAX — На главную' })[0]);
  await waitFor(() => expect(confirm).toHaveBeenCalledOnce());
  expect(router.state.location.pathname).toBe('/max/settings');
  expect(screen.getByTestId('max-phone-input')).toHaveValue('+79991234567');
  confirm.mockReturnValue(true);
  await userEvent.click(screen.getAllByRole('link', { name: 'SHMAX — На главную' })[0]);
  await waitFor(() => expect(router.state.location.pathname).toBe('/dashboard'));
  expect(calls.some(([path]) => path === '/api/max/auth/start')).toBe(false);
});
it('MAX settings Save returns to MAX and does not restart onboarding or revoke auth', async () => {
  authenticatedMax = true;
  const router = mount('/max/settings');
  const save = await screen.findByRole('button', { name: 'Сохранить' });
  await waitFor(() => expect(save).toBeEnabled());
  expect(screen.queryByRole('list', { name: /Шаг/ })).not.toBeInTheDocument();
  await userEvent.click(save);
  await waitFor(() => expect(router.state.location.pathname).toBe('/max'));
  expect(calls.some(([path]) => path === '/api/max/auth/cancel' || path === '/api/setup/complete')).toBe(false);
});
it('cancel focused MAX editor returns to MAX without mutations', async () => {
  const router = mount('/max/settings');
  await userEvent.click(await screen.findByRole('button', { name: 'Отмена / Назад' }));
  await waitFor(() => expect(router.state.location.pathname).toBe('/max'));
  expect(calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(0);
});
it('skip persists dismissal independently of completion, opens dashboard and permits continuing', async () => {
  const router = mount('/setup/choice');
  expect(await screen.findByRole('link', { name: 'Восстановить из резервной копии' })).toHaveAttribute('href', '/setup/restore');
  await userEvent.click(screen.getByRole('button', { name: 'Пропустить настройку' }));
  await waitFor(() => expect(router.state.location.pathname).toBe('/dashboard'));
  expect(session.setup_dismissed).toBe(true); expect(session.setup_complete).toBe(false);
  expect(screen.getByTestId('dashboard-max')).toHaveTextContent('Не настроен');
  expect(screen.getByTestId('dashboard-telegram')).toHaveTextContent('Не настроен');
  expect(screen.getByText('Ожидает настройки')).toBeVisible();
  await userEvent.click(screen.getByRole('link', { name: 'Продолжить первоначальную настройку' }));
  await waitFor(() => expect(router.state.location.pathname).toBe('/setup/max'));
});
it('unauthenticated visitors still require login even with dismissal recorded', async () => {
  session = { ...session, authenticated: false, setup_dismissed: true, mode: 'AUTH_REQUIRED' };
  const router = mount('/dashboard');
  await screen.findByRole('heading', { name: 'Вход' });
  expect(router.state.location.pathname).toBe('/login');
  expect(calls.some(([path]) => path === '/api/status')).toBe(false);
});
it('administrator creation cannot be skipped and the bootstrap token survives logo navigation', async () => {
  session = { ...session, authenticated: false, admin_exists: false, mode: 'BOOTSTRAP' };
  const router = mount('/setup?t=owner-token');
  await screen.findByRole('heading', { name: 'Создайте администратора' });
  expect(screen.queryByRole('button', { name: 'Пропустить настройку' })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole('link', { name: 'SHMAX — На главную' }));
  await waitFor(() => expect(router.state.location.pathname).toBe('/setup'));
  expect(router.state.location.search).toBe('?t=owner-token');
});
it('completed but disconnected installation stays on the requested application page', async () => {
  session.setup_complete = true;
  const router = mount('/max');
  await screen.findByRole('link', { name: 'Настройки MAX' });
  expect(router.state.location.pathname).toBe('/max');
});
it('provides English setup choices and navigation', async () => {
  await i18n.changeLanguage('en');
  mount('/setup/choice');
  expect(await screen.findByRole('heading', { name: 'Welcome to SHMAX!' })).toBeVisible();
  expect(screen.getByRole('button', { name: 'Skip setup' })).toBeVisible();
  expect(screen.getByRole('link', { name: 'Dashboard' })).toBeVisible();
});
it('background MAX status loading cannot clear a draft typed before the response', async () => {
  const originalFetch = fetch;
  let finishLoad!: (response: Response) => void;
  vi.stubGlobal('fetch', (path: string, init?: RequestInit) => path === '/api/max/auth/state'
    ? new Promise<Response>(resolve => { finishLoad = resolve; }) : originalFetch(path, init));
  const router = mount('/max/settings');
  fireEvent.change(await screen.findByTestId('max-phone-input'), { target: { value: '+79991234567' } });
  finishLoad(new Response(JSON.stringify({ state: 'not_started', phone_hint: '+7•••88' }), { status: 200 }));
  await screen.findByText('Сохранённый: +7•••88');
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
  await userEvent.click(screen.getAllByRole('link', { name: 'SHMAX — На главную' })[0]);
  await waitFor(() => expect(confirm).toHaveBeenCalledOnce());
  expect(router.state.location.pathname).toBe('/max/settings');
});
it('Telegram token editor saves through the real API client and returns to Telegram', async () => {
  const originalFetch = fetch;
  vi.stubGlobal('fetch', (path: string, init?: RequestInit) => {
    if (path === '/api/telegram/token' || path === '/api/telegram/verify') {
      calls.push([path, init]);
      return Promise.resolve(new Response(JSON.stringify(path.endsWith('/verify') ? { verified: true, username: 'bot', bot_id: 123 } : { ok: true, preview: '123…' }), { status: 200 }));
    }
    return originalFetch(path, init);
  });
  const router = mount('/telegram/settings');
  fireEvent.change(await screen.findByTestId('telegram-token-input'), { target: { value: '123:replacement' } });
  await userEvent.click(screen.getByRole('button', { name: 'Проверить и сохранить' }));
  await waitFor(() => expect(router.state.location.pathname).toBe('/telegram'));
  expect(calls.find(([path]) => path === '/api/telegram/token')?.[1]?.headers).toMatchObject({ 'X-CSRF-Token': 'csrf' });
  expect(calls.some(([path]) => path === '/api/setup/complete')).toBe(false);
});
it('Telegram cancel warns before discarding a draft and retains stored credentials', async () => {
  const router = mount('/telegram/settings');
  fireEvent.change(await screen.findByTestId('telegram-token-input'), { target: { value: '123:unsaved' } });
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
  await userEvent.click(screen.getByRole('button', { name: 'Отмена / Назад' }));
  await waitFor(() => expect(router.state.location.pathname).toBe('/telegram'));
  expect(confirm).toHaveBeenCalledOnce();
  expect(calls.some(([path]) => path === '/api/telegram/token')).toBe(false);
});
it('reload is guarded when configuration edits are unsaved', async () => {
  mount('/max/settings');
  fireEvent.change(await screen.findByTestId('max-phone-input'), { target: { value: '+79991234567' } });
  const event = new Event('beforeunload', { cancelable: true });
  window.dispatchEvent(event);
  expect(event.defaultPrevented).toBe(true);
});

it('route configuration opens its focused editor instead of onboarding', async () => {
  const router = mount('/chats/settings');
  expect(await screen.findByRole('heading', { name: 'MAX чаты' })).toBeVisible();
  expect(router.state.location.pathname).toBe('/chats/settings');
  expect(screen.queryByRole('list', { name: /Шаг/ })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: 'Отмена / Назад' }));
  await waitFor(() => expect(router.state.location.pathname).toBe('/chats'));
});
it('English in-product Help explains skip, resume and independent editing', async () => {
  await i18n.changeLanguage('en');
  mount('/help');
  expect(await screen.findByTestId('help-section-navigation')).toHaveTextContent('After mandatory administrator creation');
  expect(screen.getByTestId('help-section-navigation')).toHaveTextContent('Continue initial setup');
});

it('shows sanitized Telegram recovery progress without claiming Xray is stopped', async () => {
  const original = globalThis.fetch;
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path === '/api/status') return new Response(JSON.stringify({ max: component,
      telegram: { ...component, configured: true, state: 'DEGRADED', recovery: {
        state: 'DEGRADED', attempts: 2, failure: 'TimeoutError', retry_in_seconds: 4,
      } }, bridge: { state: 'DEGRADED', worker_running: true }, queue: {}, database: 'ok', version: '0.1.0',
    }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    return original(path, init);
  }));
  mount('/telegram');
  expect(await screen.findByText(/Восстановление Telegram/)).toHaveTextContent('TimeoutError');
  expect(screen.getByText(/Восстановление Telegram/)).toHaveTextContent('Попыток: 2');
  expect(screen.queryByText('Xray не запущен')).not.toBeInTheDocument();
});
