// SHMAX shared UI primitives (V1 product pass).
// Deliberately dependency-free — small, typed, reusable so pages
// stop scattering one-off styling.

import React from 'react';
import type { IntegrationState, IntegrationStatus } from './api';

/** Status chip for the canonical integration states. */
const STATE_TONE: Record<string, { cls: string; label: string }> = {
  HEALTHY: { cls: 'ok', label: 'Здоров' },
  DEGRADED: { cls: 'warn', label: 'Деградация' },
  DOWN: { cls: 'error', label: 'Недоступен' },
  AUTH_REQUIRED: { cls: 'warn', label: 'Требуется вход' },
  NOT_CONFIGURED: { cls: 'idle', label: 'Не настроен' },
  CONFIGURED: { cls: 'info', label: 'Настроен' },
  CONNECTING: { cls: 'info', label: 'Подключение…' },
  RECONNECTING: { cls:'info',label:'Переподключаем…' },
  VERIFICATION_UNAVAILABLE: { cls:'warn',label:'Проверка сессии временно недоступна' },
  ERROR: { cls: 'error', label: 'Ошибка' },
};

export function StatusBadge({ state }: { state: IntegrationState | string | undefined }) {
  if (!state) return <span className="badge idle">…</span>;
  const tone = STATE_TONE[state] || { cls: 'idle', label: state };
  return <span className={`badge ${tone.cls}`}>{tone.label}</span>;
}

export function Spinner({ label }: { label?: string }) {
  return (
    <span role="status" aria-live="polite">
      <span className="spinner" aria-hidden />
      {label ? <span className="muted small" style={{ marginLeft: 8 }}>{label}</span> : null}
    </span>
  );
}

export function EmptyState({ glyph, title, hint }: { glyph?: string; title: string; hint?: string }) {
  return (
    <div className="empty">
      {glyph ? <div className="glyph" aria-hidden>{glyph}</div> : null}
      <strong>{title}</strong>
      {hint ? <span className="muted small">{hint}</span> : null}
    </div>
  );
}

export function ErrorAlert({ error, onRetry, retryLabel }: { error: string | null; onRetry?: () => void; retryLabel?: string }) {
  if (!error) return null;
  return (
    <div className="alert alert-error" role="alert">
      <span>{error}</span>
      {onRetry ? (
        <span>
          <button type="button" className="btn-ghost btn-sm" onClick={onRetry}>
            {retryLabel || 'Повторить'}
          </button>
        </span>
      ) : null}
    </div>
  );
}

export function FieldHelp({ children }: { children: React.ReactNode }) {
  return <small className="form-hint">{children}</small>;
}

export function PageHeader({ title, children }: { title: string; children?: React.ReactNode }) {
  return (
    <div className="page-header">
      <h1>{title}</h1>
      {children ? <div className="row" style={{ margin: 0 }}>{children}</div> : null}
    </div>
  );
}

/** Session validity and transport verification are separate facts. */
export function maxSessionLabel(status: IntegrationStatus): string {
  if (!status.configured) return 'Сессия не настроена';
  if (status.state === 'RECONNECTING') return 'Соединение потеряно; выполняется автоматическое восстановление';
  if (status.state === 'VERIFICATION_UNAVAILABLE') return 'Проверка сессии временно недоступна из-за сети или API';
  if (status.state === 'AUTH_REQUIRED') return 'Сессия отклонена или отсутствует; требуется вход';
  if (status.state === 'HEALTHY') return 'Сессия проверена';
  if (status.state === 'ERROR') return 'Ошибка проверки; подробности в журнале';
  return status.authenticated ? 'Сессия активна' : 'Проверка сессии ещё не завершена';
}
