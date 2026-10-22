import React from 'react';
import { useTranslation } from 'react-i18next';
import { api, IntegrationStatus } from '../api';
import { Link } from 'react-router-dom';

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

export function WatchdogCard({adapter,label,canonical,reload}: {adapter:'max'|'telegram';label:string;canonical:IntegrationStatus|null;reload?:()=>void}) {
  const [busy,setBusy]=React.useState(false);
  const [error,setError]=React.useState('');
  const state=canonical?.state;
  const labels:Record<string,string>={RECONNECTING:'Переподключаем…',VERIFICATION_UNAVAILABLE:'Проверка сессии временно недоступна',NOT_CONFIGURED:'⚪ Не настроен',CONFIGURED:'Настроен, не проверен',CONNECTING:'Подключаем…',HEALTHY:'🟢 Работает',DEGRADED:'🟡 Деградация',DOWN:'🔴 Недоступен',AUTH_REQUIRED:'Требуется вход',ERROR:'Ошибка'};
  return <div className="card" data-testid={`${adapter}-status`}><h3>{label}</h3><span className={`tag ${state==='HEALTHY'?'healthy':state==='NOT_CONFIGURED'?'not-configured':'not-tested'}`}>{state?labels[state]:'Проверяем…'}</span>{state==='NOT_CONFIGURED'?<p><Link to={adapter === "max" ? "/max/settings" : "/telegram/network"}>Настроить {label}</Link></p>:state&&<button disabled={busy} onClick={async()=>{setBusy(true);setError('');try{await api(`/api/watchdogs/${adapter}/probe`,{method:'POST'});reload?.();}catch(e:any){setError(e.message);}finally{setBusy(false);}}}>Проверить сейчас</button>}{error&&<p role="alert">{error}</p>}</div>;
}

/**
 * StorageManager — shows DB / cache / logs size and offers
 * "clear cache" action. Sessions + secrets are NEVER cleared
 * here.
 */
export interface StorageInfo {
  database_bytes: number;
  sessions_bytes: number;
  backups_bytes: number;
  cache_bytes: number;
  logs_bytes: number;
  total_bytes: number;
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

  const fmt = (b: number | undefined) => b == null ? '—' : `${(b / 1024 / 1024).toFixed(2)} MB`;
  return (
    <div className="card">
      <h3>{t('storage.title', 'Хранилище')}</h3>
      {info ? (
        <div className="table-scroll" role="region" aria-label="Таблица данных" tabIndex={0}><table>
          <thead><tr><th>Подсистема</th><th>Размер</th></tr></thead>
          <tbody>
            <tr><td>Database</td><td>{fmt(info.database_bytes)}</td></tr>
            <tr><td>Sessions</td><td>{fmt(info.sessions_bytes)}</td></tr>
            <tr><td>Backups</td><td>{fmt(info.backups_bytes)}</td></tr>
            <tr><td>Cache</td><td>{fmt(info.cache_bytes)}</td></tr>
            <tr><td>Logs</td><td>{fmt(info.logs_bytes)}</td></tr>
            <tr><td><strong>Total</strong></td><td><strong>{fmt(info.total_bytes)}</strong></td></tr>
          </tbody>
        </table></div>
      ) : <div className="muted">Загрузка…</div>}
      <div className="row" style={{ marginTop: '0.75rem' }}>
        <button onClick={clearCache} disabled={busy}>{t('storage.clearCache', 'Очистить кэш')}</button>
        <span className="muted">Не затрагивает сессии и базу данных.</span>
      </div>
      {err && <div className="tag warn" style={{ marginTop: '0.5rem' }}>{err}</div>}
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
