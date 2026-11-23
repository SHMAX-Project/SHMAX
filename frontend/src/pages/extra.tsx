import React from 'react';
import { useTranslation } from 'react-i18next';
import { api, IntegrationStatus } from '../api';
import { Link } from 'react-router-dom';
import { StatusBadge } from '../ui';

/**
 * WatchdogCard — renders one adapter's health state plus
 * the operator actions (probe / self-heal / re-login). The
 * backend endpoint is /api/watchdogs/{adapter} for read and
 * /api/watchdogs/{adapter}/{probe,self-heal} for actions.
 */
export interface WatchdogSnapshot {
  adapter: string;
  state: 'healthy' | 'degraded' | 'down' | 'auth_required';
  last_success_at: number | null;
  last_probe_at: number | null;
  down_since: number | null;
  consecutive_failures: number;
  reconnect_attempts: number;
  last_error_class: string | null;
  last_error_detail: string | null;
}

export function WatchdogCard({ adapter, label, canonical, reload }: {
  adapter: 'max' | 'telegram';
  label: string;
  canonical: IntegrationStatus | null;
  reload?: () => Promise<void> | void;
}) {
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState('');
  const state = canonical?.state;

  async function probe() {
    setBusy(true);
    setError('');
    try {
      await api(`/api/watchdogs/${adapter}/probe`, { method: 'POST' });
      await reload?.();
    } catch (error) {
      setError(error instanceof Error ? error.message : 'Не удалось проверить соединение.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card watchdog-card" data-testid={`${adapter}-status`}>
      <h3>{label}</h3>
      <div className="watchdog-controls">
        <span className="watchdog-status" role="status">
          {state ? <StatusBadge state={state} /> : <span className="badge idle">Проверяем…</span>}
        </span>
        {state === 'NOT_CONFIGURED' ? (
          <Link className="btn-ghost" to={adapter === 'max' ? '/max/settings' : '/telegram/network'}>
            Настроить {label}
          </Link>
        ) : state && (
          <button className="btn-ghost watchdog-probe" type="button" disabled={busy} onClick={probe}>
            {busy ? 'Проверяем…' : 'Проверить сейчас'}
          </button>
        )}
      </div>
      {error && <p className="alert alert-error" role="alert">{error}</p>}
    </div>
  );
}

/**
 * StorageManager — shows DB / cache / logs size and offers
 * "clear cache" action. Sessions + secrets are NEVER cleared
 * here.
 */
export interface StorageInfo {
  db_bytes: number;
  sessions_bytes: number;
  backups_bytes: number;
  cache_bytes: number;
  logs_bytes: number;
}

export function StorageManager() {
  const { t } = useTranslation();
  const [info, setInfo] = React.useState<StorageInfo | null>(null);
  const [err, setErr] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  const reload = React.useCallback(async () => {
    try {
      setInfo(await api<StorageInfo>('/api/storage'));
    } catch (e: any) { setErr(e.message); }
  }, []);
  React.useEffect(() => { reload(); }, [reload]);

  const clearCache = async () => {
    setBusy(true); setErr(null);
    try {
      await api('/api/storage/clear-cache', { method: 'POST' });
      await reload();
    } catch (e: any) { setErr(e.message); }
    finally { setBusy(false); }
  };

  const fmt = (b: number) => `${(b / 1024 ** 2).toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 })} МБ`;
  const total = info ? info.db_bytes + info.sessions_bytes + info.backups_bytes + info.cache_bytes + info.logs_bytes : 0;
  return (
    <div className="card">
      <h3>{t('storage.title', 'Хранилище')}</h3>
      {info ? (
        <div className="table-scroll" role="region" aria-label="Таблица данных" tabIndex={0}><table>
          <thead><tr><th>Подсистема</th><th>Размер</th></tr></thead>
          <tbody>
            <tr><td>База данных</td><td>{fmt(info.db_bytes)}</td></tr>
            <tr><td>Сессии</td><td>{fmt(info.sessions_bytes)}</td></tr>
            <tr><td>Резервные копии</td><td>{fmt(info.backups_bytes)}</td></tr>
            <tr><td>Кэш</td><td>{fmt(info.cache_bytes)}</td></tr>
            <tr><td>Технические логи</td><td>{fmt(info.logs_bytes)}</td></tr>
            <tr><td><strong>Всего</strong></td><td><strong>{fmt(total)}</strong></td></tr>
          </tbody>
        </table></div>
      ) : !err && <div className="muted">Загрузка…</div>}
      <div className="row" style={{ marginTop: '0.75rem' }}>
        <button onClick={clearCache} disabled={busy || !info}>{t('storage.clearCache', 'Очистить кэш')}</button>
        <span className="muted">Не затрагивает сессии и базу данных.</span>
      </div>
      {err && <div className="alert alert-error" role="alert">{err}</div>}
    </div>
  );
}

/**
 * DangerZone — purge SHMAX data. Requires admin password
 * AND typed "SHMAX" confirmation.
 */
export function DangerZone() {
  const { t } = useTranslation();
  const [open, setOpen] = React.useState(false);
  const [password, setPassword] = React.useState('');
  const [confirm, setConfirm] = React.useState('');
  const [err, setErr] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  const purge = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    if (confirm !== 'SHMAX') { setErr('Введите SHMAX'); return; }
    setBusy(true);
    try {
      await api('/api/danger-zone/purge', {
        method: 'POST',
        body: JSON.stringify({ password, confirm }),
      });
      setOpen(false);
      alert('Готово. Контейнер можно перезапускать.');
    } catch (e: any) { setErr(e.message); }
    finally { setBusy(false); }
  };

  if (!open) {
    return (
      <div className="card danger">
        <h3>{t('danger.title', 'Опасная зона')}</h3>
        <p>Полное удаление данных SHMAX необратимо.</p>
        <button className="danger" onClick={() => setOpen(true)}>{t('danger.open', 'Открыть')}</button>
      </div>
    );
  }
  return (
    <div className="card danger">
      <h3>{t('danger.title', 'Опасная зона')}</h3>
      <form onSubmit={purge} className="col">
        <label>Пароль администратора
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        </label>
        <label>Введите SHMAX для подтверждения
          <input value={confirm} onChange={(e) => setConfirm(e.target.value)} required />
        </label>
        {err && <div className="tag error">{err}</div>}
        <div className="row">
          <button type="submit" className="danger" disabled={busy}>{busy ? '…' : 'Удалить данные'}</button>
          <button type="button" className="secondary" onClick={() => setOpen(false)}>Отмена</button>
        </div>
      </form>
    </div>
  );
}

/**
 * SupportBundle — downloads a sanitized JSON support bundle
 * for sharing with maintainers. Secrets are redacted.
 */
export function SupportBundle() {
  const { t } = useTranslation();
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState<string | null>(null);
  const dl = async () => {
    setBusy(true); setErr(null);
    try {
      const r = await fetch('/api/support-bundle', { credentials: 'include' });
      if (!r.ok) { setErr(`${r.status}`); return; }
      const j = await r.json();
      const blob = new Blob([JSON.stringify(j, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `shmax-support-${Date.now()}.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e: any) { setErr(e.message); }
    finally { setBusy(false); }
  };
  return (
    <div className="card">
      <h3>{t('support.title', 'Поддержка')}</h3>
      <p>Скачайте диагностический пакет: версия, состояние, логи без секретов.</p>
      <div className="row">
        <button onClick={dl} disabled={busy}>{busy ? 'Готовлю…' : 'Скачать support bundle'}</button>
        {err && <span className="tag warn">{err}</span>}
      </div>
    </div>
  );
}

/**
 * RecentActivity — last admin actions. Bodies / contents
 * are NOT returned; only metadata.
 */
export interface ActivityRow {
  ts: number;
  actor: string;
  action: string;
  target?: string;
}

export function RecentActivity() {
  const [rows, setRows] = React.useState<ActivityRow[] | null>(null);
  const [err, setErr] = React.useState<string | null>(null);
  React.useEffect(() => {
    (async () => {
      try {
        // The backend wraps the list: {"items": [...]} — only
        // metadata (actor/event/target/timestamp), never bodies.
        const j = await api<{ items: Array<{ actor: string; event: string; target: string | null; created_at: number }> }>('/api/recent-activity');
        setRows((j.items || []).map((i) => ({
          ts: i.created_at,
          actor: i.actor,
          action: i.event,
          target: i.target ?? undefined,
        })));
      }
      catch (e: any) { setErr(e.message); }
    })();
  }, []);
  return (
    <div className="card">
      <h3>Последние действия</h3>
      {err && <div className="tag warn">{err}</div>}
      {rows && rows.length === 0 && <div className="muted">Пока нет действий.</div>}
      {rows && rows.length > 0 && (
        <div className="table-scroll" role="region" aria-label="Таблица данных" tabIndex={0}><table>
          <thead><tr><th>Когда</th><th>Кто</th><th>Действие</th><th>Цель</th></tr></thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i}>
                <td className="muted">{new Date(r.ts * 1000).toLocaleString()}</td>
                <td>{r.actor}</td>
                <td>{r.action}</td>
                <td className="muted">{r.target ?? '—'}</td>
              </tr>
            ))}
          </tbody>
        </table></div>
      )}
    </div>
  );
}
