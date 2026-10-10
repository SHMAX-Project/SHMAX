// Tiny fetch wrapper. Returns parsed JSON or throws.

import { getCsrfToken, setCsrfToken, clearCsrfToken } from './csrf';

// API error class so callers can branch on status codes
// (BLOCKER 24 — 401 → redirect to /login).
export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, message: string, detail: unknown) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

// The SPA tracks a single 401 observer so the entire app
// reacts to session expiry without each component wiring its
// own fetch error handler.
type UnauthorizedObserver = (path: string) => void;
let _onUnauthorized: UnauthorizedObserver | null = null;

export function setUnauthorizedObserver(fn: UnauthorizedObserver | null) {
  _onUnauthorized = fn;
}

async function apiResponse(path: string, init: RequestInit = {}): Promise<Response> {
  // CSRF (BLOCKER 25) — for any mutating verb we attach the
  // token via a header so the backend can verify it.
  const method = (init.method || 'GET').toUpperCase();
  const isMutating = method !== 'GET' && method !== 'HEAD' && method !== 'OPTIONS';
  const csrfToken = isMutating ? getCsrfToken() : null;
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(init.headers as Record<string, string> | undefined),
  };
  if (csrfToken) {
    headers['X-CSRF-Token'] = csrfToken;
  }
  const res = await fetch(path, {
    credentials: 'include',
    ...init,
    headers,
  });
  if (!res.ok) {
    let detail: any = null;
    try { detail = await res.json(); } catch { /* */ }
    const msg = detail?.detail || `${res.status} ${res.statusText}`;
    if (res.status === 401 && _onUnauthorized) {
      // Session expired / not authenticated — bounce to login.
      _onUnauthorized(path);
    }
    throw new ApiError(res.status, typeof msg === 'string' ? msg : JSON.stringify(msg), detail);
  }
  return res;
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await apiResponse(path, init);
  if (res.status === 204) return null as T;
  return res.json() as Promise<T>;
}

export async function apiBlob(path: string, init: RequestInit = {}): Promise<Blob> {
  return (await apiResponse(path, init)).blob();
}

// Auth helpers — real backend session flow, NOT local state.
export interface LoginResponse {
  ok: boolean;
  csrf_token: string;
  expires_at: number;
  username: string;
}

export async function login(username: string, password: string): Promise<LoginResponse> {
  const result = await api<LoginResponse>('/api/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  });
  // Pin the CSRF token so subsequent mutating calls can read it.
  setCsrfToken(result.csrf_token);
  return result;
}

export async function logout(): Promise<void> {
  try {
    await api<{ ok: boolean }>('/api/auth/logout', { method: 'POST' });
  } finally {
    clearCsrfToken();
  }
}

export interface SessionStatus {
  authenticated: boolean;
  admin_exists: boolean;
  mode: 'BOOTSTRAP' | 'AUTH_REQUIRED' | 'AUTHENTICATED';
  setup_complete: boolean;
  setup_dismissed: boolean;
  username: string | null;
  csrf_token: string | null;
  expires_at: number | null;
}

export async function session(): Promise<SessionStatus> {
  return api<SessionStatus>('/api/auth/session');
}

export type IntegrationState = 'NOT_CONFIGURED' | 'CONFIGURED' | 'CONNECTING' | 'HEALTHY' | 'DEGRADED' | 'DOWN' | 'AUTH_REQUIRED' | 'ERROR';
export interface IntegrationStatus { recovery?: { state: IntegrationState; attempts: number; failure: string | null; retry_in_seconds: number | null }; configured: boolean; state: IntegrationState; authenticated: boolean; connected: boolean; account_id: string | null; bot_id: string | null; last_probe_at: number | null; }
export interface StatusSnapshot {
  service: string;
  version: string;
  data_dir: string;
  uptime_seconds: number;
  max: IntegrationStatus;
  telegram: IntegrationStatus;
  bridge: { state: IntegrationState; worker_running: boolean };
  queue: { pending: number; processing: number; retry: number; delivered: number; failed: number };
  database: string;
  storage: { db_bytes: number; sessions_bytes: number; backups_bytes: number; cache_bytes: number; logs_bytes: number };
}

export interface DoctorResult {
  name: string;
  status: 'ok' | 'warning' | 'error' | 'not_configured' | 'not_tested';
  detail: string;
  technical?: string | null;
}

export interface RouteRow {
  id: number;
  max_account_id: number;
  max_chat_id: string;
  telegram_account_id: number;
  telegram_chat_id: string;
  telegram_thread_id: string | null;
  enabled: boolean;
  label: string | null;
}

export interface BackupRow {
  name: string;
  path: string;
  size: number;
  created_at: number;
  format: string;
  entries: number;
  error?: string;
}

// ---------------------------------------------------------------------
// MAX auth state machine
// ---------------------------------------------------------------------
export type MaxAuthStateName =
  | 'not_started'
  | 'connecting'
  | 'requesting_code'
  | 'verifying_code'
  | 'sending_code'  // legacy alias kept for backward compatibility
  | 'waiting_code'
  | 'waiting_password'
  | 'verifying_password'
  | 'authenticated'
  | 'rate_limited'
  | 'failed';

export interface MaxAuthSnapshot {
  state: MaxAuthStateName;
  phone_hint: string | null;
  account_id: string | null;
  display_name: string | null;
  handle: string | null;
  last_error: string | null;
  last_error_class: string | null;
  next_action: 'start' | 'wait' | 'submit_code' | 'submit_password' | 'done' | 'retry';
  updated_at: number;
}

export interface MaxProfile {
  user_id: string;
  display_name: string;
  handle: string | null;
}

export interface MaxChatRow {
  chat_id: string;
  title: string;
  kind: 'chat' | 'group' | 'channel' | 'dialog' | string;
}

// ---------------------------------------------------------------------
// Telegram
// ---------------------------------------------------------------------
export interface TelegramVerifyResult {
  privacy_warning?: string | null;
  ok: boolean;
  verified: boolean;
  bot_id: number;
  username: string;
  can_join_groups: boolean;
  preview: string;
}

export interface TelegramGroupInfo {
  paired: boolean;
  validated: boolean;
  missing_permissions: string[] | null;
  bot_id: number | null;
  bot_username: string | null;
  chat_id: string | null;
  title: string | null;
  is_forum: boolean;
  chat_type: string | null;
  bot_status: string | null;
  transport_mode: string;
}

export interface PairCodeResponse {
  code: string;
  expires_at: number | string;
  max_chat_id?: string;
}

export interface TelegramPairCode {
  code: string;
  purpose: string;
  expires_at: string;
}

export interface TelegramUserRow {
  membership?: string | null;
  is_admin?: boolean;
  source?: string | null;
  user_id: string;
  display_name: string | null;
  username: string | null;
  nickname: string | null;
  role: string;
  approved: boolean;
  blocked: boolean;
  first_seen_at: number | null;
  last_seen_at: number | null;
}

export interface TelegramUsersResponse {
  users: TelegramUserRow[];
  phone_available: boolean;
  phone_note: string;
}

export interface UserApprovalPayload {
  approved: boolean;
  role: 'owner' | 'admin' | 'operator' | 'read_only';
}

export interface UserBlockedPayload {
  blocked: boolean;
}

export interface UserNicknamePayload {
  nickname: string | null;
}

export type TelegramTransportModeName =
  | 'auto'
  | 'bot_api_direct'
  | 'bot_api_socks5'
  | 'bot_api_http'
  | 'mtproto_direct'
  | 'mtproto_socks'
  | 'mtproto_proxy';

export interface TelegramTransportState {
  mode: TelegramTransportModeName;
  proxy_url: string | null;
  proxy_host: string | null;
  proxy_port: number | null;
  proxy_username: string | null;
  proxy_has_password: boolean;
  api_id: number | null;
  api_hash_set: boolean;
  mtproxy_host: string | null;
  mtproxy_port: number | null;
  mtproxy_secret_set: boolean;
}

export interface TelegramTransportProbe {
  ok: boolean;
  bot_id?: number;
  username?: string;
  transport?: string;
  error_class?: string;
  detail?: string;
}

// ---------------------------------------------------------------------
// Wizard step / progress
// ---------------------------------------------------------------------
export interface WizardProgress {
  completed: string[];
  current: string;
}

// ---------------------------------------------------------------------
// API client helpers (typed wrappers)
// ---------------------------------------------------------------------
export async function maxAuthState(): Promise<MaxAuthSnapshot> {
  return api<MaxAuthSnapshot>('/api/max/auth/state');
}
export async function maxAuthStart(phone: string): Promise<MaxAuthSnapshot> {
  return api<MaxAuthSnapshot>('/api/max/auth/start', {
    method: 'POST',
    body: JSON.stringify({ phone }),
  });
}
export async function maxAuthCode(code: string): Promise<MaxAuthSnapshot> {
  return api<MaxAuthSnapshot>('/api/max/auth/code', {
    method: 'POST',
    body: JSON.stringify({ code }),
  });
}
export async function maxAuthPassword(password: string): Promise<MaxAuthSnapshot> {
  return api<MaxAuthSnapshot>('/api/max/auth/password', {
    method: 'POST',
    body: JSON.stringify({ password }),
  });
}
export async function maxAuthResend(): Promise<MaxAuthSnapshot> {
  return api<MaxAuthSnapshot>('/api/max/auth/resend', { method: 'POST' });
}
export async function maxAuthCancel(): Promise<MaxAuthSnapshot> {
  return api<MaxAuthSnapshot>('/api/max/auth/cancel', { method: 'POST' });
}
export async function maxProfile(): Promise<MaxProfile> {
  return api<MaxProfile>('/api/max/profile');
}
export async function maxChats(): Promise<{ configured: boolean; chats: MaxChatRow[] }> {
  return api<{ configured: boolean; chats: MaxChatRow[] }>('/api/max/chats');
}

export async function telegramSaveToken(token: string): Promise<{ ok: boolean; preview: string }> {
  return api<{ ok: boolean; preview: string }>('/api/telegram/token', {
    method: 'POST',
    body: JSON.stringify({ token }),
  });
}
export async function telegramTokenPreview(): Promise<{ saved: boolean; preview: string }> {
  return api<{ saved: boolean; preview: string }>('/api/telegram/token/preview');
}
export async function telegramVerify(token: string): Promise<TelegramVerifyResult> {
  return api<TelegramVerifyResult>('/api/telegram/verify', {
    method: 'POST',
    body: JSON.stringify({ token }),
  });
}
export async function telegramSystemTopic(chatId: string): Promise<{ ok: boolean; chat_id: string; topic_id: string; title: string }> {
  return api<{ ok: boolean; chat_id: string; topic_id: string; title: string }>(
    '/api/telegram/system-topic',
    { method: 'POST', body: JSON.stringify({ chat_id: chatId }) },
  );
}
export async function telegramGroupInfo(): Promise<TelegramGroupInfo> {
  return api<TelegramGroupInfo>('/api/telegram/group');
}
export async function telegramGroupPair(chatId: string): Promise<{ ok: boolean; chat_id: string; is_forum: boolean }> {
  return api<{ ok: boolean; chat_id: string; is_forum: boolean }>('/api/telegram/group/pair', {
    method: 'POST',
    body: JSON.stringify({ chat_id: chatId, code: '' }),
  });
}
export async function telegramPairCode(): Promise<TelegramPairCode> {
  return api<TelegramPairCode>('/api/telegram/group/pair-code', { method: 'POST' });
}
export async function telegramUsers(): Promise<TelegramUsersResponse> {
  return api<TelegramUsersResponse>('/api/telegram/users');
}

export async function telegramUserSetApproval(
  userId: string,
  payload: UserApprovalPayload,
): Promise<{ ok: boolean; user_id: string; approved: boolean; role: string }> {
  return api(`/api/telegram/users/${encodeURIComponent(userId)}/approval`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  });
}

export async function telegramUserSetBlocked(
  userId: string,
  payload: UserBlockedPayload,
): Promise<{ ok: boolean; user_id: string; blocked: boolean }> {
  return api(`/api/telegram/users/${encodeURIComponent(userId)}/blocked`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  });
}

export async function telegramUserSetNickname(
  userId: string,
  payload: UserNicknamePayload,
): Promise<{ ok: boolean; user_id: string; nickname: string | null }> {
  return api(`/api/telegram/users/${encodeURIComponent(userId)}/nickname`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  });
}

export interface WizardTestCheck {
  id: string;
  label: string;
  ok: boolean;
  kind?: 'active_external' | 'runtime' | 'persistence';
  detail?: string;
  fix_step?: string;
}

export interface WizardTestResponse {
  ok: boolean;
  checks: WizardTestCheck[];
  summary: string;
}

export async function wizardTest(): Promise<WizardTestResponse> {
  return api<WizardTestResponse>('/api/wizard/test');
}
export async function telegramTransportGet(): Promise<TelegramTransportState> {
  return api<TelegramTransportState>('/api/telegram/transport');
}
export interface TelegramTransportSetResult {
  ok: boolean;
  reachable: boolean;
  detail: string;
  mode: string;
  proxy_set: boolean;
  mtproxy_set: boolean;
  api_id_set: boolean;
}

export async function telegramTransportSet(req: {
  mode: TelegramTransportModeName;
  proxy_url?: string | null;
  proxy_host?: string | null;
  proxy_port?: number | null;
  proxy_username?: string | null;
  proxy_password?: string | null;
  api_id?: number | null;
  api_hash?: string | null;
  mtproxy_host?: string | null;
  mtproxy_port?: number | null;
  mtproxy_secret?: string | null;
  skip_bot_probe?: boolean;
}): Promise<TelegramTransportSetResult> {
  return api<TelegramTransportSetResult>(
    '/api/telegram/transport',
    { method: 'POST', body: JSON.stringify(req) },
  );
}

// ---------------------------------------------------------------------
// Xray subscription (network bootstrap)
// ---------------------------------------------------------------------
export interface XrayNode {
  id: string;
  name: string;
  protocol: string;
  transport: string;
  security: string;
  address: string;
  port: number;
  supported: boolean;
  reason: string;
  sni: string;
  fingerprint: string;
  flow: string;
}

export interface XrayStatusResponse {
  state: 'not_configured' | 'starting' | 'running' | 'stopped' | 'failed';
  detail: string;
  socks_port: number | null;
  pid: number | null;
  nodes: XrayNode[];
  selected_node_id: string | null;
  subscription_loaded: boolean;
  subscription_url_set: boolean;
  selected_node_disappeared: boolean;
}

export async function xraySubscribe(url: string): Promise<{ ok: boolean; nodes: XrayNode[] }> {
  return api('/api/telegram/xray/subscription', {
    method: 'POST',
    body: JSON.stringify({ url }),
  });
}

export async function xraySelect(nodeId: string): Promise<{
  ok: boolean;
  xray: { state: string; detail: string; socks_port: number | null; pid: number | null };
  reachable: boolean;
  detail: string;
  bot_error?: string;
}> {
  return api('/api/telegram/xray/select', {
    method: 'POST',
    body: JSON.stringify({ node_id: nodeId }),
  });
}

export async function xrayStatus(): Promise<XrayStatusResponse> {
  return api('/api/telegram/xray/status');
}

export async function telegramTransportGetState(): Promise<TelegramTransportState> {
  return telegramTransportGet();
}
export async function telegramTransportProbe(): Promise<TelegramTransportProbe> {
  return api<TelegramTransportProbe>('/api/telegram/transport/probe', { method: 'POST' });
}
export async function routesList(): Promise<RouteRow[]> {
  return api<RouteRow[]>('/api/routes');
}
export async function routeCreateFromChat(req: {
  max_chat_id: string;
  max_chat_title: string;
  telegram_chat_id: string;
}): Promise<{ id: number; telegram_thread_id: string; telegram_chat_id: string; label: string }> {
  return api<{ id: number; telegram_thread_id: string; telegram_chat_id: string; label: string }>(
    '/api/routes/from-chat',
    { method: 'POST', body: JSON.stringify(req) },
  );
}
export async function routeRecreateTopic(routeId: number): Promise<{ ok: boolean; route_id: number; telegram_chat_id: string; telegram_thread_id: string; title: string }> {
  return api<{ ok: boolean; route_id: number; telegram_chat_id: string; telegram_thread_id: string; title: string }>(
    `/api/routes/${routeId}/recreate-topic`,
    { method: 'POST' },
  );
}
// ---------------------------------------------------------------------
// Wizard readiness / setup completion (P0 #13)
// ---------------------------------------------------------------------
export interface StepReadiness {
  ready: boolean;
  reason: string;
  blocking_check: string | null;
}

export type ReadinessMap = Record<string, StepReadiness>;

export async function wizardReadiness(): Promise<ReadinessMap> {
  return api<ReadinessMap>('/api/wizard/readiness');
}

export async function completeSetup(acknowledgeUnconfigured = false): Promise<{ ok: boolean; setup_complete: boolean }> {
  return api<{ ok: boolean; setup_complete: boolean }>('/api/setup/complete', {
    method: 'POST',
    body: JSON.stringify({ acknowledge_unconfigured: acknowledgeUnconfigured }),
  });
}
