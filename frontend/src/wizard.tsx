import { useTranslation } from 'react-i18next';
import { useConfigurationEditing } from './workflow';
import { useNavigationSafety } from './navigation';
import { UserSignature } from './user-signature';
import { MemberSync } from './member-sync';
// Canonical wizard step registry + step implementations.
//
// One source of truth for the wizard's order, completion
// predicate and route. Pages consume it through the helper
// components below.
//
// IMPORTANT: this is the production V1 wizard. It only knows
// about steps that the backend can actually back. Adding a new
// step requires both a backend handler and a registry entry
// here — we deliberately do not support phantom steps.

import { useCallback, useEffect, useRef, useState } from 'react';
import { WizardHelpCard } from './help';
import {
  ApiError,
  completeSetup,
  maxAuthState,
  maxAuthStart,
  maxAuthCode,
  maxAuthPassword,
  maxAuthResend,
  maxAuthCancel,
  maxChats,
  maxProfile,
  routeCreateFromChat,
  routesList,
  telegramGroupInfo,
  telegramPairCode,
  telegramSaveToken,
  telegramTransportGet,
  telegramTransportProbe,
  telegramTransportSet,
  xraySubscribe,
  xraySelect,
  xrayStatus,
  type XrayNode,
  telegramUsers,
  telegramUserSetApproval,
  telegramUserSetBlocked,
  telegramTokenPreview,
  telegramVerify,
  wizardReadiness,
  wizardTest,
  type MaxAuthSnapshot,
  type MaxChatRow,
  type PairCodeResponse,
  type TelegramGroupInfo,
  type TelegramTransportModeName,
  type TelegramTransportState,
  type TelegramUserRow,
  type TelegramUsersResponse,
  type MaxAuthStateName,
  type ReadinessMap,
  type WizardTestCheck,
  api,
} from './api';
import { EmptyState, Spinner, StatusBadge } from './ui';

// ---------------------------------------------------------------------
// Step registry
// ---------------------------------------------------------------------
export type WizardStepId =
  | 'admin'
  | 'max'
  | 'telegram-bot'
  | 'telegram-network'
  | 'telegram-group'
  | 'max-chats'
  | 'users'
  | 'test'
  | 'finish';

export interface WizardStep {
  id: WizardStepId;
  order: number;
  title: string;
  route: string;
}

export const wizardSteps: readonly WizardStep[] = [
  { id: 'admin', order: 1, title: 'Администратор', route: '/setup/admin' },
  { id: 'max', order: 2, title: 'MAX аккаунт', route: '/setup/max' },
  // Surgical acceptance: the NETWORK step comes FIRST — bot
  // validation needs Telegram access which may require the proxy
  // configured here. One source of truth for the order.
  { id: 'telegram-network', order: 3, title: 'Telegram сеть', route: '/setup/telegram/network' },
  { id: 'telegram-bot', order: 4, title: 'Telegram бот', route: '/setup/telegram' },
  { id: 'telegram-group', order: 5, title: 'Telegram группа', route: '/setup/telegram/group' },
  { id: 'max-chats', order: 6, title: 'MAX чаты', route: '/setup/chats' },
  { id: 'users', order: 7, title: 'Пользователи', route: '/setup/users' },
  { id: 'test', order: 8, title: 'Проверка', route: '/setup/test' },
  { id: 'finish', order: 9, title: 'Готово', route: '/setup/finish' },
];

export const wizardTotalSteps = wizardSteps.length;

export function stepIndex(id: WizardStepId): number {
  return wizardSteps.findIndex((s) => s.id === id) + 1;
}

// ---------------------------------------------------------------------
// Shared chrome
// ---------------------------------------------------------------------
function humanError(e: any, fallback: string): string {
  if (!e) return fallback;
  if (e instanceof ApiError) {
    // Never expose raw Pydantic/JSON to the operator.
    if (e.status === 400) return 'Неверные данные — проверьте поле';
    if (e.status === 401) return 'Требуется войти заново';
    if (e.status === 403) return 'Доступ запрещён';
    if (e.status === 404) return 'Не найдено';
    if (e.status === 409) return 'Конфликт — обновите страницу';
    if (e.status === 422) return 'Не заполнены обязательные поля';
    if (e.status === 429) return 'Слишком много попыток — подождите';
    if (e.status === 502 || e.status === 504) return 'Внешний сервис недоступен';
    if (typeof e.detail === 'string') return e.detail.slice(0, 200);
    return fallback;
  }
  if (typeof e.message === 'string') {
    const m = e.message;
    if (m.startsWith('[') || m.startsWith('{')) return fallback;
    return m.slice(0, 200);
  }
  return fallback;
}

/** The ONE canonical step indicator — every step renders it. */
export function StepIndicator({ stepId }: { stepId: WizardStepId }) {
  const idx = stepIndex(stepId);
  return (
    <ol className="wizard-steps" aria-label={`Шаг ${idx} из ${wizardTotalSteps}`}>
      {wizardSteps.map((s) => {
        const state = s.order < idx ? 'done' : s.order === idx ? 'current' : '';
        return (
          <li key={s.id} className={state} aria-current={state === 'current' ? 'step' : undefined}>
            {s.order > 1 && <span className="step-line" aria-hidden />}
            <span className="step-dot" aria-hidden>
              {state === 'done' ? '✓' : s.order}
            </span>
            <span className="step-label">{s.title}</span>
          </li>
        );
      })}
    </ol>
  );
}

function StepHeader({ stepId, subtitle }: { stepId: WizardStepId; subtitle?: string }) {
  const step = wizardSteps.find((s) => s.id === stepId);
  if (!step) return null;
  return (
    <div className="wizard-step-header">
      <div className="eyebrow">Шаг {stepIndex(stepId)} из {wizardTotalSteps}</div>
      <h2>{step.title}</h2>
      {subtitle && <p className="wizard-help">{subtitle}</p>}
    </div>
  );
}

function StepShell({ stepId, subtitle, children, footer, onDirty }: {
  stepId: WizardStepId;
  subtitle?: string;
  children: React.ReactNode;
  footer: React.ReactNode;
  onDirty?: () => void;
}) {
  const editing = useConfigurationEditing();
  const { t } = useTranslation();
  return (
    <section className="wizard-step" onChangeCapture={onDirty} onPasteCapture={onDirty} aria-label={`${editing ? t("setupUx.editing") : "Шаг"}: ${wizardSteps.find((s) => s.id === stepId)?.title}`}>
      {editing ? <div className="wizard-step-header"><h2>{wizardSteps.find(s => s.id === stepId)?.title}</h2><p>{t("setupUx.editing")}</p></div> : <><StepIndicator stepId={stepId} /><StepHeader stepId={stepId} subtitle={subtitle} /></>}
      <WizardHelpCard stepId={stepId} />
      {children}
      {footer}
    </section>
  );
}

// ---------------------------------------------------------------------
// Step: Admin (the existing /setup/admin flow remains.)
// ---------------------------------------------------------------------
export function StepAdmin(props: {
  onNext: () => void;
  onPrev: () => void;
  children: React.ReactNode;
}) {
  return (
    <StepShell
      stepId="admin"
      subtitle="Создайте учётную запись владельца SHMAX."
      footer={
        <div className="wizard-actions">
          <button type="button" className="btn-ghost" onClick={props.onPrev} disabled>Назад</button>
          <button type="button" className="btn-primary" onClick={props.onNext}>Далее</button>
        </div>
      }
    >
      {props.children}
    </StepShell>
  );
}

// ---------------------------------------------------------------------
// Step: MAX (real auth state machine).
// ---------------------------------------------------------------------
// States in which the backend is actively driving the MAX round
// and the UI MUST keep observing it. The audit (P0 #1) caught
// the wizard fetching the auth snapshot exactly once: the owner
// received three real SMS codes while SHMAX never left the
// "Запрашиваем код у MAX…" screen because nothing re-read the
// state that had already advanced to WAITING_CODE.
const MAX_AUTH_IN_FLIGHT = new Set<MaxAuthStateName>([
  'connecting',
  'requesting_code',
  'verifying_code',
  // P0 #1 (surgical review): the 2FA password verification phase is
  // in flight — the observer MUST keep polling here, otherwise a
  // late AUTHENTICATED is never surfaced to the operator.
  'verifying_password',
]);

const MAX_STATE_LABELS: Array<[MaxAuthStateName | 'not_started', string]> = [
  ['connecting', 'Подключение к MAX'],
  ['requesting_code', 'Запрос SMS-кода'],
  ['waiting_code', 'Ожидание кода из SMS'],
  ['verifying_code', 'Проверка кода'],
  ['waiting_password', 'Двухфакторная проверка'],
  ['verifying_password', 'Проверка пароля'],
  ['authenticated', 'Подключено'],
];

export function StepMax(props: { onNext: () => void; onPrev: () => void }) {
  const editing = useConfigurationEditing();
  const { t } = useTranslation();
  const { markDirty, markSaved } = useNavigationSafety();
  const [snap, setSnap] = useState<MaxAuthSnapshot | null>(null);
  const [phone, setPhone] = useState('');
  const [code, setCode] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [profile, setProfile] = useState<{ display_name: string; user_id: string } | null>(null);
  // Latest snapshot lives in a ref so the polling loop below
  // never closes over stale React state.
  const snapRef = useRef<MaxAuthSnapshot | null>(null);
  const codeInputRef = useRef<HTMLInputElement | null>(null);

  const applySnap = useCallback((s: MaxAuthSnapshot) => {
    snapRef.current = s;
    setSnap(s);
  }, []);

  // Initial load: restore whatever state the backend is in —
  // a page refresh must land back in the CURRENT step of the
  // round (including FAILED, which stays visible).
  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const s = await maxAuthState();
        if (cancelled) return;
        applySnap(s);
        if (s.state === 'authenticated') {
          try {
            const p = await maxProfile();
            if (!cancelled) setProfile(p);
          } catch (e) { /* ignore */ }
        }
      } catch (e: any) {
        if (!cancelled) setError(humanError(e, 'load failed'));
      }
    }
    load();
    return () => { cancelled = true; };
  }, [applySnap]);

  // P0 #1 repair (control-review round): a persistent OBSERVER
  // keyed on the observed state, not a mount-only loop. The
  // previous effect started polling once on mount; after the
  // initial not_started fetch stopped it, a later start() NEVER
  // restarted observation — the OTP stayed invisible forever.
  // Now:
  //   * watchingRef names the state currently being observed;
  //   * observe() (re)arms the poll loop whenever the state
  //     becomes in-flight, from ANY code path (mount, start,
  //     resend, submitCode, submitPassword, the poll itself);
  //   * polling stops only in stable / operator states
  //     (not_started, waiting_code, waiting_password,
  //     authenticated, failed, rate_limited);
  //   * every request reads/writes snapRef — no stale closures;
  //   * the loop is bounded (hard cap) and never overlaps.
  const POLL_MS = 1500;
  const MAX_POLLS = 120; // bounded: ~3 minutes per in-flight stretch
  const watchingRef = useRef<MaxAuthStateName | null>(null);
  const pollTimerRef = useRef<number | null>(null);
  const profileLoadedRef = useRef(false);

  const stopPolling = useCallback(() => {
    if (pollTimerRef.current !== null) {
      window.clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    watchingRef.current = null;
  }, []);

  const observe = useCallback((state: MaxAuthStateName, offset = 0) => {
    if (!MAX_AUTH_IN_FLIGHT.has(state)) {
      stopPolling();
      return;
    }
    // Already observing THIS state — do not stack a second loop.
    if (watchingRef.current === state && pollTimerRef.current !== null) return;
    stopPolling();
    watchingRef.current = state;
    let polls = offset;
    const tick = async () => {
      if (watchingRef.current === null) return;
      try {
        const s = await maxAuthState();
        if (watchingRef.current === null) return;
        applySnap(s);
        if (s.last_error && MAX_AUTH_IN_FLIGHT.has(s.state)) {
          setError(s.last_error);
        }
        if (s.state === 'waiting_code') {
          // The OTP input appears automatically, without a reload.
          codeInputRef.current?.focus();
        }
        if (s.state === 'authenticated' && !profileLoadedRef.current) {
          // The round completed while the operator waited — the
          // success card needs the profile; submitCode's own
          // response only carries it when IT observed the final
          // transition (P0 #1 control review).
          profileLoadedRef.current = true;
          try {
            setProfile(await maxProfile());
          } catch { /* card renders without profile details */ }
        }
        if (!MAX_AUTH_IN_FLIGHT.has(s.state)) {
          stopPolling();
          return;
        }
        // State may have moved WITHIN the in-flight set
        // (connecting → verifying_code) — keep observing under
        // the new name so the cap applies per state.
        if (watchingRef.current !== s.state) {
          observe(s.state, polls);
          return;
        }
      } catch {
        // Transient network errors do not kill observation; the
        // hard cap bounds it.
      }
      polls += 1;
      if (polls >= MAX_POLLS) {
        stopPolling();
        return;
      }
      pollTimerRef.current = window.setTimeout(tick, POLL_MS);
    };
    pollTimerRef.current = window.setTimeout(tick, POLL_MS);
  }, [applySnap, stopPolling]);

  // The observer follows the snapshot: whenever the state enters
  // an in-flight state, observation starts; whenever it settles,
  // it stops. This single effect is the lifecycle owner.
  useEffect(() => {
    const current = snap?.state || 'not_started';
    if (MAX_AUTH_IN_FLIGHT.has(current)) {
      observe(current);
    } else {
      stopPolling();
    }
    return () => { /* stop happens via unmount cleanup below */ };
  }, [snap?.state, observe, stopPolling]);

  // Unmount: stop everything.
  useEffect(() => stopPolling, [stopPolling]);

  async function start() {
    setBusy(true); setError(null);
    try {
      const s = await maxAuthStart(phone);
      markSaved();
      applySnap(s);
      // P0 #1: an explicit start ACTIVATES observation so the
      // OTP input appears as soon as the backend reaches
      // WAITING_CODE — even if the initial fetch long since
      // stopped the mount-time watcher.
      observe(s.state);
      if (s.last_error) setError(s.last_error);
    } catch (e: any) { setError(humanError(e, 'start failed')); }
    finally { setBusy(false); }
  }

  async function submitCode() {
    setBusy(true); setError(null);
    try {
      const s = await maxAuthCode(code);
      markSaved();
      applySnap(s);
      // Keep observing through VERIFYING_CODE until the backend
      // lands on authenticated / waiting_password / failed.
      observe(s.state);
      if (s.state === 'authenticated') {
        profileLoadedRef.current = true;
        const p = await maxProfile();
        setProfile(p);
      } else if (s.last_error) setError(s.last_error);
    } catch (e: any) { setError(humanError(e, 'submit failed')); }
    finally { setBusy(false); }
  }

  async function submitPassword() {
    setBusy(true); setError(null);
    try {
      const s = await maxAuthPassword(password);
      markSaved();
      applySnap(s);
      observe(s.state);
      if (s.state === 'authenticated') {
        profileLoadedRef.current = true;
        const p = await maxProfile();
        setProfile(p);
      } else if (s.last_error) setError(s.last_error);
    } catch (e: any) { setError(humanError(e, 'submit failed')); }
    finally { setBusy(false); }
  }

  async function resend() {
    setBusy(true); setError(null);
    try {
      const s = await maxAuthResend();
      applySnap(s);
      // P0 #1: resend re-arms observation for the new round —
      // exactly one new auth request (the backend busy guard
      // deduplicates the SMS).
      observe(s.state);
    } catch (e: any) { setError(humanError(e, 'resend failed')); }
    finally { setBusy(false); }
  }

  async function cancel() {
    setBusy(true); setError(null);
    try {
      const s = await maxAuthCancel();
      applySnap(s);
    } catch (e: any) { setError(humanError(e, 'cancel failed')); }
    finally { setBusy(false); }
  }

  const state = snap?.state || 'not_started';
  const showConnecting = state === 'connecting' || state === 'requesting_code';
  const showVerifying = state === 'verifying_code';
  const showCode = state === 'waiting_code' || state === 'sending_code';
  const showPassword = state === 'waiting_password';
  const showVerifyingPassword = state === 'verifying_password';
  const showAuthed = state === 'authenticated';
  const showFailed = state === 'failed' || state === 'rate_limited';
  const flowActive = !showAuthed;

  return (
    <StepShell
      onDirty={markDirty}
      stepId="max"
      subtitle="Введите номер телефона, привязанный к вашему MAX. SHMAX отправит SMS-код и при необходимости запросит пароль 2FA."
      footer={
        <div className="wizard-actions">
          <button type="button" className="btn-ghost" onClick={props.onPrev}>{editing ? t("setupUx.cancel") : "Назад"}</button>
          <button
            type="button"
            className="btn-primary"
            onClick={props.onNext}
            disabled={!showAuthed}
            title={showAuthed ? '' : 'Сначала подключите MAX'}
          >
            {editing ? t("setupUx.save") : "Далее"}
          </button>
        </div>
      }
    >
      {error && <div className="alert alert-error" role="alert">{error}</div>}

      {flowActive && (
        <div className="state-flow" aria-label="Прогресс подключения MAX">
          {MAX_STATE_LABELS.map(([s, label]) => {
            const order = MAX_STATE_LABELS.findIndex(([x]) => x === state);
            const cur = MAX_STATE_LABELS.findIndex(([x]) => x === s);
            let cls = '';
            if (state === s) cls = 'active';
            else if (cur < order) cls = 'done';
            if (showFailed && s === state) cls = 'failed';
            if (showFailed && state === 'rate_limited' && s === 'rate_limited') cls = 'failed';
            return (
              <div key={s} className={`state ${cls}`}>
                <span className="dot" aria-hidden />
                {label}
              </div>
            );
          })}
        </div>
      )}

      {!showAuthed && !showCode && !showPassword && !showConnecting && !showVerifying && !showVerifyingPassword && (
        <>
          <label className="form-row">
            <span>Номер телефона</span>
            <input
              type="tel"
              inputMode="tel"
              placeholder="+7 999 123 45 67"
              value={phone}
              onChange={(e) => setPhone(e.target.value)}
              onPaste={(e) => {
                // Allow pasted numbers with any formatting —
                // spaces, brackets, hyphens, dots. The backend
                // normalises them via libphonenumber before
                // handing the E.164 to MAX.
                e.preventDefault();
                const pasted = (e.clipboardData.getData('text') || '').trim();
                setPhone(pasted);
              }}
              disabled={busy}
              data-testid="max-phone-input"
              autoComplete="tel"
              aria-describedby="max-phone-help"
            />
            <small id="max-phone-help" className="form-hint">
              Примеры: +7 999 123 45 67, +49 30 12345678, 8 (999) 123-45-67.
              Любое форматирование нормализуется на сервере.
            </small>
            {snap?.phone_hint && <span className="hint">Сохранённый: {snap.phone_hint}</span>}
          </label>
          <div className="row">
            <button type="button" className="btn-primary" disabled={busy || !phone.trim()} onClick={start}>
              Получить код
            </button>
          </div>
        </>
      )}

      {showConnecting && (
        <div className="callout" data-testid="max-connecting" role="status">
          <strong>Запрашиваем код у MAX…</strong>
          <p className="muted small">
            Подключаемся к серверу MAX и запрашиваем SMS. Это занимает несколько секунд;
            никакого кода ещё не отправлено — мы ждём ответа MAX.
          </p>
          <button type="button" className="btn-ghost btn-sm" disabled={busy} onClick={cancel}>
            Отмена
          </button>
        </div>
      )}

      {showVerifying && (
        <div className="callout" data-testid="max-verifying" role="status">
          <strong>Отправляем код MAX…</strong>
          <p className="muted small">Проверяем введённый код на сервере MAX. Подождите.</p>
        </div>
      )}

      {showVerifyingPassword && (
        <div className="callout" data-testid="max-verifying-password" role="status">
          <strong>Проверяем пароль…</strong>
          <p className="muted small">Проверяем пароль 2FA на сервере MAX. Подождите.</p>
        </div>
      )}

      {showCode && (
        <>
          <label className="form-row">
            <span>Код из SMS / MAX</span>
            <input
              type="text"
              inputMode="numeric"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              disabled={busy}
              data-testid="max-code-input"
              maxLength={8}
              autoFocus
              ref={codeInputRef}
              aria-describedby="max-code-help"
            />
            <small id="max-code-help" className="form-hint">
              SMS отправлен. Введите код — SHMAX проверит его после подтверждения.
            </small>
          </label>
          <div className="row">
            <button type="button" className="btn-primary" disabled={busy || !code.trim()} onClick={submitCode}>
              Подтвердить
            </button>
            <button type="button" className="btn-ghost" disabled={busy} onClick={resend}>
              Отправить код ещё раз
            </button>
            <button type="button" className="btn-ghost" disabled={busy} onClick={cancel}>
              Отмена
            </button>
          </div>
        </>
      )}

      {showPassword && (
        <>
          <label className="form-row">
            <span>Дополнительный пароль (2FA)</span>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              disabled={busy}
              data-testid="max-password-input"
              autoFocus
            />
          </label>
          <div className="row">
            <button type="button" className="btn-primary" disabled={busy || !password} onClick={submitPassword}>
              Подтвердить
            </button>
            <button type="button" className="btn-ghost" disabled={busy} onClick={cancel}>
              Отмена
            </button>
          </div>
        </>
      )}

      {showFailed && (
        <div className="alert alert-warn" role="alert">
          <span>Подключение не завершено{snap?.last_error ? `: ${snap.last_error}` : ''}.</span>
          <span className="muted small">Исправьте проблему и запросите код заново — номер можно ввести выше.</span>
        </div>
      )}

      {showAuthed && profile && (
        <div className="success-card">
          <strong>MAX подключён</strong>
          <dl>
            <dt>Имя</dt><dd>{profile.display_name || '—'}</dd>
            <dt>Аккаунт</dt><dd>{profile.user_id}</dd>
            {snap?.phone_hint && (<><dt>Телефон</dt><dd>{snap.phone_hint}</dd></>)}
            <dt>Session</dt><dd>активна</dd>
          </dl>
        </div>
      )}
    </StepShell>
  );
}

// ---------------------------------------------------------------------
// Step: Telegram bot
// ---------------------------------------------------------------------
export function StepTelegramBot(props: { onNext: () => void; onPrev: () => void }) {
  const editing = useConfigurationEditing();
  const { t } = useTranslation();
  const { markDirty, markSaved } = useNavigationSafety();
  const [token, setToken] = useState('');
  const [showToken, setShowToken] = useState(false);
  const [saved, setSaved] = useState<{ preview: string } | null>(null);
  const [verifyResult, setVerifyResult] = useState<{ ok: boolean; username: string; bot_id: number; privacy_warning?: string | null } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    telegramTokenPreview().then((p) => { if (!cancelled) setSaved(p); }).catch(() => {});
    return () => { cancelled = true; };
  }, []);

  async function saveAndVerify() {
    if (!token.trim()) {
      setError('Введите токен');
      return;
    }
    setBusy(true); setError(null);
    try {
      const sv = await telegramSaveToken(token);
      setSaved(sv);
      markSaved();
      setToken('');
      try {
        const v = await telegramVerify(token);
        if (v.verified) { setVerifyResult({ ok: true, username: v.username, bot_id: v.bot_id, privacy_warning: v.privacy_warning }); if (editing) props.onNext(); }
      } catch (e: any) {
        setError(humanError(e, 'verify failed'));
      }
    } catch (e: any) {
      setError(humanError(e, 'save failed'));
    } finally {
      setBusy(false);
    }
  }

  const isVerified = !!verifyResult;

  return (
    <StepShell
      onDirty={markDirty}
      stepId="telegram-bot"
      subtitle="Создайте бота через @BotFather и вставьте его токен. SHMAX сохранит токен в защищённое хранилище и проверит подключение."
      footer={
        <div className="wizard-actions">
          <button type="button" className="btn-ghost" onClick={props.onPrev}>{editing ? t("setupUx.cancel") : "Назад"}</button>
          <button type="button" className="btn-primary" onClick={props.onNext} disabled={!isVerified && !(editing && saved?.preview)}
            title={isVerified ? '' : 'Сначала проверьте токен бота'}>
            {editing ? t("setupUx.done") : "Далее"}
          </button>
        </div>
      }
    >
      <div className="callout">
        <strong>Нет Telegram-бота?</strong>
        <ol>
          <li>Откройте <a href="https://t.me/BotFather" target="_blank" rel="noopener noreferrer">@BotFather</a></li>
          <li>Отправьте <code>/newbot</code></li>
          <li>Выберите имя и username</li>
          <li>Скопируйте токен и вставьте его сюда</li>
        </ol>
      </div>

      {error && <div className="alert alert-error" role="alert">{error}</div>}

      <label className="form-row">
        <span>Bot Token</span>
        <span className="token-field">
          <input
            type={showToken ? 'text' : 'password'}
            placeholder="123456:ABC..."
            value={token}
            onChange={(e) => setToken(e.target.value)}
            disabled={busy}
            data-testid="telegram-token-input"
            autoComplete="off"
          />
          <button
            type="button"
            className="show-btn"
            onClick={() => setShowToken((v) => !v)}
            aria-pressed={showToken}
          >
            {showToken ? 'Скрыть' : 'Показать'}
          </button>
        </span>
        {saved?.preview && <span className="hint">Сохранён: {saved.preview}</span>}
      </label>

      <div className="row">
        <button type="button" className="btn-primary" disabled={busy || !token.trim()} onClick={saveAndVerify}>
          {busy ? <><span className="spinner" aria-hidden /> Проверяем…</> : 'Проверить и сохранить'}
        </button>
      </div>

      {isVerified && verifyResult && (
        <div className="success-card">
          <strong>Бот найден</strong>
          {verifyResult.privacy_warning && <p role="status">{verifyResult.privacy_warning}</p>}
          <p className="muted small">Проверка API не подтверждает доставку обычных сообщений группы.</p>
          <dl>
            <dt>Username</dt><dd>@{verifyResult.username}</dd>
            <dt>Bot ID</dt><dd>{verifyResult.bot_id}</dd>
          </dl>
        </div>
      )}
    </StepShell>
  );
}

// ---------------------------------------------------------------------
// Step: Telegram network — FIRST network step (before the bot token).
// Tests Telegram HTTPS reachability through the selected transport
// WITHOUT any bot token. Modes: Direct / SOCKS5 / HTTP / Xray
// subscription. MTProto stays out of the V1 wizard UI.
// ---------------------------------------------------------------------
type NetworkMode = 'direct' | 'socks5' | 'http' | 'xray';

const NETWORK_MODES: Array<{ value: NetworkMode; label: string; help: string }> = [
  { value: 'direct', label: 'Напрямую', help: 'Подключаться к Telegram напрямую.' },
  { value: 'socks5', label: 'SOCKS5', help: 'Использовать внешний SOCKS5-прокси.' },
  { value: 'http', label: 'HTTP', help: 'Использовать HTTP CONNECT-прокси.' },
  { value: 'xray', label: 'Xray подписка', help: 'Использовать VLESS/Trojan из вашей подписки.' },
];

export function StepTelegramNetwork(props: { onNext: () => void; onPrev: () => void }) {
  const editing = useConfigurationEditing();
  const { t } = useTranslation();
  const { markDirty, markSaved } = useNavigationSafety();
  const [mode, setMode] = useState<NetworkMode>('direct');
  const [proxyHost, setProxyHost] = useState('');
  const [proxyPort, setProxyPort] = useState('');
  const [proxyUsername, setProxyUsername] = useState('');
  const [proxyPassword, setProxyPassword] = useState('');
  const [subUrl, setSubUrl] = useState('');
  const [nodes, setNodes] = useState<XrayNode[]>([]);
  const [selectedNode, setSelectedNode] = useState<string>('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<string>('Не настроено');
  const [networkOk, setNetworkOk] = useState(false);

  // Restore persisted state (mode/proxy/selected node) on mount.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [t, x] = await Promise.all([
          telegramTransportGet(),
          xrayStatus().catch(() => null),
        ]);
        if (cancelled) return;
        if (t.proxy_host && t.proxy_port) {
          setProxyHost(t.proxy_host);
          setProxyPort(String(t.proxy_port));
          setProxyUsername(t.proxy_username ?? '');
        }
        if (x) {
          setNodes(x.nodes);
          if (x.selected_node_id) setSelectedNode(x.selected_node_id);
          if (x.selected_node_disappeared) {
            setError('Выбранный узел исчез из подписки — выберите узел заново.');
            setStatus('Нода не поддерживается');
            return;
          }
          if (x.state === 'running' && x.selected_node_id) {
            setMode('xray');
            setStatus('Cached node active');
            setNetworkOk(true);
            return;
          }
        }
        if (t.mode === 'bot_api_socks5' && t.proxy_host) setMode('socks5');
        else if (t.mode === 'bot_api_http' && t.proxy_host) setMode('http');
      } catch {
        /* fresh install */
      }
    })();
    return () => { cancelled = true; };
  }, []);

  async function saveAndProbe(skipProbe = false): Promise<boolean> {
    setError(null);
    setBusy(true);
    setStatus('Проверяем');
    try {
      const result = await telegramTransportSet({
        mode: mode === 'xray' ? 'bot_api_socks5' : (mode === 'socks5' ? 'bot_api_socks5' : mode === 'http' ? 'bot_api_http' : 'bot_api_direct'),
        proxy_host: mode === 'direct' ? null : proxyHost || null,
        proxy_port: mode === 'direct' ? null : (proxyPort ? Number(proxyPort) : null),
        proxy_username: mode === 'direct' ? null : proxyUsername || null,
        proxy_password: mode === 'direct' ? null : proxyPassword || null,
        skip_bot_probe: true,
      });
      markSaved();
      if (!result.reachable && !skipProbe) {
        setStatus('Прокси недоступен');
        setError(result.detail || 'Telegram недоступен через выбранный транспорт');
        return false;
      }
      setStatus('Telegram доступен');
      setNetworkOk(true);
      return true;
    } catch (e: any) {
      setStatus('Прокси недоступен');
      setError(humanError(e, 'network failed'));
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function loadSubscription() {
    setError(null);
    setBusy(true);
    try {
      const r = await xraySubscribe(subUrl.trim());
      setNodes(r.nodes);
      markSaved();
      const supported = r.nodes.filter((n) => n.supported);
      if (!supported.length) {
        setStatus('Нода не поддерживается');
        setError('Формат подписки пока не поддерживается');
        return;
      }
      setStatus(`Подписка загружена: ${supported.length} узл(ов)`);
    } catch (e: any) {
      setStatus('Не настроено');
      setError(humanError(e, 'subscription failed'));
    } finally {
      setBusy(false);
    }
  }

  async function selectNode() {
    if (!selectedNode) return;
    const node = nodes.find((n) => n.id === selectedNode);
    if (node && !node.supported) {
      setStatus('Нода не поддерживается');
      setError(node.reason);
      return;
    }
    setError(null);
    setBusy(true);
    setStatus('Проверяем');
    try {
      const r = await xraySelect(selectedNode);
      markSaved();
      if (r.ok && r.reachable) {
        setStatus('Cached node active');
        setNetworkOk(true);
      } else {
        setNetworkOk(false);
        setStatus(r.rolled_back ? 'Предыдущий узел восстановлен' : r.reachable ? 'Сеть доступна; бот восстанавливается' : r.xray?.state === 'running' ? 'Прокси запущен; Telegram недоступен' : 'Прокси не готов');
        const current=await xrayStatus();
        setSelectedNode(current.selected_node_id || '');
        setError(r.detail || 'Прокси недоступен');
      }
    } catch (e: any) {
      setStatus('Не удалось проверить подключение через Xray');
      setError(humanError(e, 'Проверка Xray/Telegram не завершена'));
    } finally {
      setBusy(false);
    }
  }

  const proxyFieldsVisible = mode === 'socks5' || mode === 'http';
  const canProbe = mode === 'direct' || (proxyFieldsVisible && proxyHost.trim() && proxyPort.trim());

  return (
    <StepShell
      onDirty={markDirty}
      stepId="telegram-network"
      subtitle="Настройте доступ к Telegram. Токен бота вводится на следующем шаге — здесь достаточно проверить саму сеть."
      footer={
        <div className="wizard-actions">
          <button type="button" className="btn-ghost" onClick={props.onPrev}>{editing ? t("setupUx.cancel") : "Назад"}</button>
          <button type="button" className="btn-primary" onClick={props.onNext} disabled={!networkOk}
            title={networkOk ? '' : 'Сначала проверьте соединение с Telegram'}>
            {editing ? t("setupUx.done") : "Далее"}
          </button>
        </div>
      }
    >
      {error && <div className="alert alert-error" role="alert">{error}</div>}
      <p className="muted small" data-testid="network-status" role="status">Статус: {status}</p>

      <div className="cards-grid">
        {NETWORK_MODES.map((m) => (
          <button
            key={m.value}
            type="button"
            className={`card-radio ${mode === m.value ? 'selected' : ''}`}
            onClick={() => { markDirty(); setMode(m.value); setNetworkOk(false); setStatus('Не настроено'); }}
            aria-pressed={mode === m.value}
          >
            <strong>{m.label}</strong>
            <span className="help">{m.help}</span>
          </button>
        ))}
      </div>

      {proxyFieldsVisible && (
        <>
          <label className="form-row">
            <span>Host</span>
            <input value={proxyHost} onChange={(e) => setProxyHost(e.target.value)}
              placeholder="proxy.example.com" data-testid="proxy-host" />
          </label>
          <label className="form-row">
            <span>Port</span>
            <input value={proxyPort} onChange={(e) => setProxyPort(e.target.value)}
              inputMode="numeric" placeholder="1080" data-testid="proxy-port" />
          </label>
          <label className="form-row">
            <span>Username (опционально)</span>
            <input value={proxyUsername} onChange={(e) => setProxyUsername(e.target.value)}
              autoComplete="off" data-testid="proxy-username" />
          </label>
          <label className="form-row">
            <span>Password (опционально)</span>
            <input type="password" value={proxyPassword}
              onChange={(e) => setProxyPassword(e.target.value)}
              autoComplete="new-password" data-testid="proxy-password" />
            <small className="form-hint">Пароль хранится в защищённом хранилище и никогда не показывается повторно.</small>
          </label>
          <div className="row">
            <button type="button" className="btn-primary" disabled={busy || !canProbe}
              onClick={() => saveAndProbe()} data-testid="network-probe">
              {busy ? <><span className="spinner" aria-hidden /> Проверяем…</> : 'Проверить соединение'}
            </button>
          </div>
        </>
      )}

      {mode === 'xray' && (
        <>
          <label className="form-row">
            <span>URL подписки</span>
            <input value={subUrl} onChange={(e) => setSubUrl(e.target.value)}
              placeholder="https://…" data-testid="xray-sub-url" autoComplete="off" />
            <small className="form-hint">Поддерживаются VLESS/Trojan (обычный текст или Base64).</small>
          </label>
          <div className="row">
            <button type="button" className="btn-ghost" disabled={busy || !subUrl.trim()}
              onClick={loadSubscription} data-testid="xray-subscribe">
              Получить подписку
            </button>
          </div>
          {nodes.length > 0 && (
            <label className="form-row">
              <span>Ноды</span>
              <select value={selectedNode} onChange={(e) => setSelectedNode(e.target.value)}
                data-testid="xray-node-select">
                <option value="">— выберите узел —</option>
                {nodes.map((n) => (
                  <option key={n.id} value={n.id} disabled={!n.supported}>
                    {n.name} — {n.protocol} {n.security === 'reality' ? 'Reality' : n.security === 'tls' ? 'TLS' : ''} {n.supported ? '' : `(не поддерживается: ${n.reason})`}
                  </option>
                ))}
              </select>
            </label>
          )}
          <div className="row">
            <button type="button" className="btn-primary" disabled={busy || !selectedNode}
              onClick={selectNode} data-testid="xray-select">
              {busy ? <><span className="spinner" aria-hidden /> Проверяем…</> : 'Проверить соединение'}
            </button>
          </div>
        </>
      )}

      {mode === 'direct' && (
        <div className="row">
          <button type="button" className="btn-primary" disabled={busy}
            onClick={() => saveAndProbe()} data-testid="network-probe-direct">
            {busy ? <><span className="spinner" aria-hidden /> Проверяем…</> : 'Проверить соединение'}
          </button>
        </div>
      )}
    </StepShell>
  );
}

// ---------------------------------------------------------------------
// Step: Telegram group — beautifully guided pairing
// ---------------------------------------------------------------------
export function StepTelegramGroup(props: { onNext: () => void; onPrev: () => void }) {
  const editing = useConfigurationEditing();
  const { t } = useTranslation();
  const { markDirty } = useNavigationSafety();
  const [pair, setPair] = useState<PairCodeResponse | null>(null);
  const [info, setInfo] = useState<TelegramGroupInfo | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [waiting, setWaiting] = useState(false);

  const reloadInfo = useCallback(async () => {
    try {
      const i = await telegramGroupInfo();
      setInfo(i);
      return i;
    } catch {
      return null;
    }
  }, []);

  useEffect(() => {
    reloadInfo();
  }, [reloadInfo]);

  // While a code is outstanding, poll the group state so the
  // "connected" card appears the moment /shmax_connect lands —
  // no manual refresh.
  useEffect(() => {
    if (!pair) return;
    let cancelled = false;
    let timer: number | null = null;
    let polls = 0;
    async function tick() {
      if (cancelled) return;
      const i = await reloadInfo();
      if (cancelled) return;
      if (i?.paired) {
        setWaiting(false);
        return;
      }
      polls += 1;
      if (polls < 80) timer = window.setTimeout(tick, 2000);
      else setWaiting(false);
    }
    setWaiting(true);
    timer = window.setTimeout(tick, 2000);
    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [pair, reloadInfo]);

  async function issue() {
    setBusy(true); setError(null);
    try {
      const c = await telegramPairCode();
      setPair(c);
    } catch (e: any) {
      setError(humanError(e, 'issue failed'));
    } finally { setBusy(false); }
  }

  const paired = !!info?.paired;
  const missing = info?.missing_permissions || [];

  return (
    <StepShell
      onDirty={markDirty}
      stepId="telegram-group"
      subtitle="Подключите группу, куда SHMAX будет доставлять сообщения из MAX."
      footer={
        <div className="wizard-actions">
          <button type="button" className="btn-ghost" onClick={props.onPrev}>{editing ? t("setupUx.cancel") : "Назад"}</button>
          <button type="button" className="btn-primary" onClick={props.onNext} disabled={!paired}
            title={paired ? '' : 'Сначала подключите группу через /shmax_connect'}>
            {editing ? t("setupUx.save") : "Далее"}
          </button>
        </div>
      }
    >
      {error && <div className="alert alert-error" role="alert">{error}</div>}

      {!pair && !paired && (
        <div className="row">
          <button type="button" className="btn-primary" disabled={busy} onClick={issue}>
            Получить одноразовый код
          </button>
        </div>
      )}

      {pair && !paired && (
        <div className="callout">
          <strong>Код сопряжения</strong>
          <div className="pair-code" data-testid="telegram-pair-code">{pair.code}</div>
          <ol>
            <li>Добавьте бота в нужную группу.</li>
            <li>Преобразуйте её в супергруппу и включите «Темы» (Forum).</li>
            <li>Дайте боту права: отправка сообщений и управление темами.</li>
            <li>Отправьте в группе: <code>/shmax_connect {pair.code}</code></li>
          </ol>
          <small className="dim">Код одноразовый и действует ограниченное время.</small>
        </div>
      )}

      {waiting && !paired && (
        <div className="alert alert-info" role="status">
          <span><span className="spinner" aria-hidden /> Ожидаем команду /shmax_connect в группе…</span>
          <span className="muted small">Эта страница обновится автоматически.</span>
        </div>
      )}

      {!paired && missing.length > 0 && (
        <div className="alert alert-warn" role="alert">
          <span>Группа найдена, но у бота не хватает прав:</span>
          <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>
            {missing.map((m, i) => <li key={i}>{m}</li>)}
          </ul>
        </div>
      )}

      {paired && info && (
        <div className="success-card">
          <strong>Telegram подключён</strong>
          <dl>
            <dt>Группа</dt><dd>{info.title || info.chat_id}</dd>
            <dt>chat_id</dt><dd className="mono">{info.chat_id}</dd>
            <dt>Тип</dt><dd>{info.chat_type || '—'}</dd>
            <dt>Темы</dt><dd>{info.is_forum ? 'включены' : 'выключены'}</dd>
            <dt>Права бота</dt><dd>{info.bot_status || '—'}</dd>
            <dt>Бот</dt><dd>{info.bot_username ? `@${info.bot_username}` : info.bot_id}</dd>
            <dt>Маршрут</dt><dd>{info.transport_mode}</dd>
          </dl>
        </div>
      )}
    </StepShell>
  );
}

// ---------------------------------------------------------------------
// Step: MAX chats — search, select, create topics
// ---------------------------------------------------------------------
export function StepMaxChats(props: { onNext: () => void; onPrev: () => void }) {
  const editing = useConfigurationEditing();
  const { t } = useTranslation();
  const [chats, setChats] = useState<MaxChatRow[] | null>(null);
  const [groupInfo, setGroupInfo] = useState<TelegramGroupInfo | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<Array<{ id: number; label: string; thread_id: string }>>([]);
  const [filter, setFilter] = useState('');

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const [c, g] = await Promise.all([maxChats(), telegramGroupInfo()]);
        if (cancelled) return;
        setChats(c.chats);
        setGroupInfo(g);
      } catch (e: any) {
        if (!cancelled) setError(humanError(e, 'load failed'));
      }
    }
    load();
    return () => { cancelled = true; };
  }, []);

  async function create() {
    if (!groupInfo?.chat_id) {
      setError('Сначала подключите Telegram-группу.');
      return;
    }
    setBusy(true); setError(null);
    const createdRows: typeof created = [];
    try {
      for (const chat of chats || []) {
        if (!selected.has(chat.chat_id)) continue;
        const r = await routeCreateFromChat({
          max_chat_id: chat.chat_id,
          max_chat_title: chat.title || chat.chat_id,
          telegram_chat_id: groupInfo.chat_id,
        });
        createdRows.push({ id: r.id, label: r.label, thread_id: r.telegram_thread_id });
      }
      setCreated(createdRows);
    } catch (e: any) {
      setError(humanError(e, 'create failed'));
    } finally { setBusy(false); }
  }

  const canCreate = !!groupInfo?.chat_id && selected.size > 0;
  const visible = (chats || []).filter(
    (c) => !filter || (c.title || c.chat_id).toLowerCase().includes(filter.toLowerCase()),
  );

  return (
    <StepShell
      stepId="max-chats"
      subtitle="Отметьте чаты MAX, которые появятся в Telegram. Для каждого будет создана отдельная тема в группе."
      footer={
        <div className="wizard-actions">
          <button type="button" className="btn-ghost" onClick={props.onPrev}>{editing ? t("setupUx.cancel") : "Назад"}</button>
          <button type="button" className="btn-primary" onClick={props.onNext} disabled={created.length === 0}
            title={created.length ? '' : 'Создайте хотя бы одну тему'}>
            {editing ? t("setupUx.save") : "Далее"}
          </button>
        </div>
      }
    >
      {error && <div className="alert alert-error" role="alert">{error}</div>}

      {!chats && <Spinner label="Загружаем чаты MAX…" />}

      {chats && chats.length === 0 && (
        <EmptyState glyph="💬" title="В MAX пока нет чатов" hint="После появления новых сообщений список обновится." />
      )}

      {chats && chats.length > 0 && (
        <>
          <div className="search-row" style={{ margin: '10px 0' }}>
            <input
              type="search"
              placeholder="Поиск чата…"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              aria-label="Поиск чата"
            />
          </div>
          <ul className="chat-list">
            {visible.map((c) => (
              <li key={c.chat_id} className={selected.has(c.chat_id) ? 'selected' : ''}>
                <label>
                  <input
                    type="checkbox"
                    checked={selected.has(c.chat_id)}
                    onChange={(e) => {
                      const next = new Set(selected);
                      if (e.target.checked) next.add(c.chat_id);
                      else next.delete(c.chat_id);
                      setSelected(next);
                    }}
                  />
                  <strong>{c.title || c.chat_id}</strong>
                  <span className="muted small"> · {c.kind}</span>
                </label>
              </li>
            ))}
          </ul>
        </>
      )}

      <div className="row">
        <button type="button" className="btn-primary" disabled={!canCreate || busy} onClick={create}>
          {busy ? <><span className="spinner" aria-hidden /> Создаём темы…</> : `Создать темы для выбранных (${selected.size})`}
        </button>
      </div>

      {created.length > 0 && (
        <div className="success-card">
          <strong>Темы созданы</strong>
          <ul className="muted small" style={{ margin: '8px 0 0', paddingLeft: 18 }}>
            {created.map((c) => (
              <li key={c.id}>{c.label} → тема #{c.thread_id} (маршрут #{c.id})</li>
            ))}
          </ul>
        </div>
      )}
    </StepShell>
  );
}

// ---------------------------------------------------------------------
// Step: Users
// ---------------------------------------------------------------------
const ROLE_OPTIONS = ['read_only', 'operator', 'admin', 'owner'] as const;
type RoleName = typeof ROLE_OPTIONS[number];

function UsersRow(props: {
  user: TelegramUserRow;
  busy: boolean;
  onChange: () => void;
}) {
  const { user, onChange } = props;
  const [rowBusy, setRowBusy] = useState(false);
  const [rowError, setRowError] = useState<string | null>(null);
  const busy = props.busy || rowBusy;

  async function patch(payload: { approved?: boolean; blocked?: boolean; role?: RoleName }) {
    setRowBusy(true);
    setRowError(null);
    try {
      if (payload.approved !== undefined || payload.role !== undefined) {
        await telegramUserSetApproval(user.user_id, {
          approved: payload.approved ?? user.approved,
          role: (payload.role ?? (user.role as RoleName)),
        });
      }
      if (payload.blocked !== undefined) {
        await telegramUserSetBlocked(user.user_id, { blocked: payload.blocked });
      }
      onChange();
    } catch (e) {
      setRowError(humanError(e, 'save failed'));
    } finally {
      setRowBusy(false);
    }
  }

  const initial = (user.display_name || user.username || user.user_id).slice(0, 1).toUpperCase();
  return (
    <li className="user-row">
      <div className="user-row-main">
        <span className="avatar" aria-hidden>{initial}</span>
        <strong>{user.display_name || user.username || user.user_id}</strong>
        {user.username && <span className="muted small">@{user.username}</span>}
        <span className="muted small">ID {user.user_id}</span>
        {user.membership && <span className="muted small">Telegram: {user.membership}{user.is_admin ? " · администратор группы" : ""}</span>}
        {user.blocked
          ? <span className="badge error">заблокирован</span>
          : user.approved
            ? <span className="badge ok">одобрен · {user.role}</span>
            : <span className="badge warn">ожидает подтверждения</span>}
        {user.nickname && <span className="muted small">псевдоним «{user.nickname}»</span>}
      </div>
      <UserSignature user={user} onSaved={onChange} />
      <div className="user-row-controls">
        <label>
          <span>Роль</span>
          <select
            value={user.role}
            disabled={busy}
            onChange={(e) => patch({ role: e.target.value as RoleName })}
          >
            {ROLE_OPTIONS.map((r) => (
              <option key={r} value={r}>{r}</option>
            ))}
          </select>
        </label>
        <label className="row-check">
          <input
            type="checkbox"
            checked={user.approved}
            disabled={busy}
            onChange={(e) =>
              patch({
                approved: e.target.checked,
                // Approving a read_only user would leave them
                // unable to send — grant a send-capable role.
                role:
                  e.target.checked && (user.role || 'read_only') === 'read_only'
                    ? 'operator'
                    : undefined,
              })
            }
          />
          <span>Одобрить</span>
        </label>
        <label className="row-check">
          <input
            type="checkbox"
            checked={user.blocked}
            disabled={busy}
            onChange={(e) => patch({ blocked: e.target.checked })}
          />
          <span>Заблокировать</span>
        </label>
      </div>
      {rowError && <div className="alert alert-error" role="alert">{rowError}</div>}
    </li>
  );
}

export function StepUsers(props: { onNext: () => void; onPrev: () => void }) {
  const [data, setData] = useState<TelegramUsersResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function reload() {
    setBusy(true);
    try {
      const d = await telegramUsers();
      setData(d);
    } catch (e) {
      setError(humanError(e, 'load failed'));
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    reload();
  }, []);

  const pending = (data?.users || []).filter((u) => !u.approved && !u.blocked);
  const active = (data?.users || []).filter((u) => u.approved || u.blocked);

  return (
    <StepShell
      stepId="users"
      subtitle="По умолчанию писать из Telegram в MAX может только владелец. Подтвердите тех, кому разрешено."
      footer={
        <div className="wizard-actions">
          <button type="button" className="btn-ghost" onClick={props.onPrev}>Назад</button>
          <button type="button" className="btn-primary" onClick={props.onNext}>Далее</button>
        </div>
      }
    >
      <MemberSync onSync={reload} />
      {error && <div className="alert alert-error" role="alert">{error}</div>}

      <div className="wizard-actions sub">
        <button type="button" className="btn-ghost btn-sm" onClick={reload} disabled={busy}>
          Обновить
        </button>
      </div>

      {!data && <Spinner label="Загружаем пользователей…" />}

      {data && data.users.length === 0 && (
        <EmptyState
          glyph="☺"
          title="Пока никого"
          hint="Пользователи появятся после синхронизации, событий вступления или видимых боту сообщений."
        />
      )}

      {data && data.users.length > 0 && (
        <>
          {pending.length > 0 && (
            <>
              <h3>Ожидают подтверждения</h3>
              <ul className="user-list">
                {pending.map((u) => <UsersRow key={u.user_id} user={u} busy={busy} onChange={reload} />)}
              </ul>
            </>
          )}
          {active.length > 0 && (
            <>
              <h3>Активные</h3>
              <ul className="user-list">
                {active.map((u) => <UsersRow key={u.user_id} user={u} busy={busy} onChange={reload} />)}
              </ul>
            </>
          )}
        </>
      )}
    </StepShell>
  );
}

// ---------------------------------------------------------------------
// Step: Test — every Next is gated on report.ok (P0 #13)
// ---------------------------------------------------------------------
const KIND_LABEL: Record<string, string> = {
  active_external: 'внешняя проверка',
  runtime: 'рантайм',
  persistence: 'состояние',
};

export function StepTest(props: { onNext: () => void; onPrev: () => void }) {
  const [report, setReport] = useState<{ ok: boolean; summary: string; checks: WizardTestCheck[] } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run() {
    setBusy(true);
    setError(null);
    try {
      const r = await wizardTest();
      setReport(r);
    } catch (e: any) {
      setError(humanError(e, 'test failed'));
    } finally {
      setBusy(false);
    }
  }

  return (
    <StepShell
      stepId="test"
      subtitle="Запустите проверку: MAX, Telegram, транспорт, группа и очередь должны быть реально готовы к работе."
      footer={
        <div className="wizard-actions">
          <button type="button" className="btn-ghost" onClick={props.onPrev}>Назад</button>
          <button type="button" className="btn-primary" onClick={props.onNext} disabled={!report || !report.ok}
            title={report?.ok ? '' : 'Все проверки должны пройти успешно'}>
            Далее
          </button>
        </div>
      }
    >
      <div className="row">
        <button type="button" className="btn-primary" disabled={busy} onClick={run}>
          {busy ? <><span className="spinner" aria-hidden /> Проверяем…</> : 'Проверить'}
        </button>
      </div>
      {error && <div className="alert alert-error" role="alert">{error}</div>}
      {report && (
        <>
          <p className={report.ok ? 'ok' : 'fail'} style={{ fontWeight: 650 }}>{report.summary}</p>
          <ul className="check-list">
            {report.checks.map((c) => (
              <li key={c.id}>
                <span className={`mark ${c.ok ? 'ok' : 'fail'}`}>{c.ok ? '✓' : '✗'}</span>
                <strong>{c.label}</strong>
                {c.detail && <span className="muted small"> · {c.detail}</span>}
                {!c.ok && c.fix_step && <span className="muted small"> · исправить на шаге «{c.fix_step}»</span>}
                {c.kind && <span className="kind">{KIND_LABEL[c.kind] || c.kind}</span>}
              </li>
            ))}
          </ul>
        </>
      )}
    </StepShell>
  );
}

// ---------------------------------------------------------------------
// Step: Finish — backend-authoritative global readiness (P0 #13)
// ---------------------------------------------------------------------
export function StepFinish(props: { onPrev: () => void }) {
  const [readiness, setReadiness] = useState<ReadinessMap | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const [ack, setAck] = useState(false);

  useEffect(() => {
    let cancelled = false;
    wizardReadiness().then((r) => { if (!cancelled) setReadiness(r); }).catch(() => {
      if (!cancelled) setError('Не удалось получить состояние готовности.');
    });
    return () => { cancelled = true; };
  }, []);

  const finish = readiness?.finish;
  const ready = !!finish?.ready;
  const blocking = Object.entries(readiness || {})
    .filter(([k, v]) => k !== 'finish' && !v.ready)
    .map(([k, v]) => ({ step: k, reason: v.reason }));

  async function complete() {
    setBusy(true); setError(null);
    try {
      await completeSetup(!ready && ack);
      setDone(true);
    } catch (e: any) {
      setError(humanError(e, 'Не удалось завершить настройку.'));
    } finally {
      setBusy(false);
    }
  }

  return (
    <StepShell
      stepId="finish"
      footer={
        <div className="wizard-actions">
          <button type="button" className="btn-ghost" onClick={props.onPrev}>Назад</button>
          <button
            type="button"
            className="btn-primary"
            onClick={complete}
            disabled={busy || done || (!ready && !ack)}
            title={ready || ack ? '' : 'Готовность ещё не подтверждена — отметьте осознанное завершение'}
          >
            Завершить настройку
          </button>
        </div>
      }
    >
      {!readiness && <Spinner label="Проверяем готовность…" />}
      {error && <div className="alert alert-error" role="alert">{error}</div>}

      {ready && (
        <div className="success-card">
          <strong>Всё готово</strong>
          <p className="muted small" style={{ margin: '6px 0 0' }}>
            {finish?.reason || 'Все обязательные проверки пройдены.'}
          </p>
        </div>
      )}

      {readiness && !ready && (
        <>
          <div className="alert alert-warn" role="alert">
            <span>Настройка ещё не полностью завершена:</span>
            <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>
              {blocking.map((b) => (
                <li key={b.step}>{b.reason}</li>
              ))}
            </ul>
          </div>
          <label className="row-check" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
            <span className="muted small">
              Я понимаю, что мост может работать не полностью, и хочу завершить настройку сейчас.
            </span>
          </label>
        </>
      )}

      {done && (
        <div className="success-card">
          <strong>Настройка завершена</strong>
          <p className="muted small" style={{ margin: '6px 0 0' }}>
            Добро пожаловать в SHMAX. <a href="/dashboard">Открыть панель управления →</a>
          </p>
        </div>
      )}
    </StepShell>
  );
}

// The pages module imports StepIndicator for its own uses; keep
// the re-export explicit so the registry stays the single source.
export { StatusBadge };
