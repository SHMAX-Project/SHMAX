import { useTranslation } from 'react-i18next';
import { useNavigationSafety } from '../navigation';
import { PortableExport } from "../portable-backup";
import { UserSignature } from '../user-signature';
import { MemberSync } from '../member-sync';
import React from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { Link } from 'react-router-dom';
import { api, type StatusSnapshot, type RouteRow, type DoctorResult } from '../api';
import { routesList } from '../api';
import { EmptyState, PageHeader, Spinner, StatusBadge } from '../ui';
import { StepIndicator } from '../wizard';
import {
  WatchdogCard,
  StorageManager,
  DangerZone,
  SupportBundle,
  RecentActivity,
} from './extra';

export function Admin({ onCreated }: { onCreated?: () => Promise<void> }) {
  const { markDirty, markSaved } = useNavigationSafety();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const [token] = React.useState(() => params.get('t') || '');
  const [username, setUsername] = React.useState('');
  const [password, setPassword] = React.useState('');
  const [password2, setPassword2] = React.useState('');
  const [error, setError] = React.useState('');
  const [busy, setBusy] = React.useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault(); setError('');
    if (password !== password2) { setError('Пароли не совпадают'); return; }
    if (!token) { setError('Откройте действующую setup-ссылку владельца.'); return; }
    setBusy(true);
    try {
      await api('/api/setup/admin', { method: 'POST', body: JSON.stringify({ username, password, token }) });
      markSaved();
      await onCreated?.();
      navigate('/setup/choice', { replace: true });
    } catch (e: any) { setError(e.message); } finally { setBusy(false); }
  }

  return (
    <section className="wizard-step" aria-label="Шаг: Администратор">
      <StepIndicator stepId="admin" />
      <div className="wizard-step-header">
        <div className="eyebrow">Шаг 1 из 9</div>
        <h2>Создайте администратора</h2>
        <p className="wizard-help">Эта учётная запись управляет SHMAX.</p>
      </div>
      <form onSubmit={submit} onChangeCapture={markDirty}>
        <label className="form-row">
          <span>Имя администратора</span>
          <input autoComplete="username" value={username} onChange={e => setUsername(e.target.value)} required />
        </label>
        <label className="form-row">
          <span>Пароль</span>
          <input autoComplete="new-password" type="password" value={password} onChange={e => setPassword(e.target.value)} minLength={8} required />
        </label>
        <label className="form-row">
          <span>Повторите пароль</span>
          <input autoComplete="new-password" type="password" value={password2} onChange={e => setPassword2(e.target.value)} required />
        </label>
        {error && <div className="alert alert-error" role="alert">{error}</div>}
        <div className="row">
          <button className="btn-primary" disabled={busy}>
            {busy ? <><span className="spinner" aria-hidden /> Создаём…</> : 'Создать администратора'}
          </button>
        </div>
      </form>
    </section>
  );
}

export interface DashboardProps {
  status: StatusSnapshot | null;
  routes?: RouteRow[] | null;
  doctor?: DoctorResult[] | null;
}

const fmtBytes = (b: number | undefined) =>
  b == null ? '—' : b > 1024 * 1024 ? `${(b / 1024 / 1024).toFixed(1)} MB` : `${(b / 1024).toFixed(0)} KB`;

export function Dashboard({ status, reload, setupComplete = true }: { status: StatusSnapshot | null; reload?: () => void; setupComplete?: boolean }) {
  const { t } = useTranslation();
  if (!status) {
    return (
      <section>
        <PageHeader title="Главная" />
        <Spinner label="Загружаем состояние SHMAX…" />
      </section>
    );
  }
  const queue = status.queue || {} as Record<string, number>;
  return (
    <section>
      <PageHeader title="Главная">
        <span className="muted small">
          аптайм {Math.floor((status.uptime_seconds || 0) / 60)} мин · v{status.version}
        </span>
      </PageHeader>

      {!setupComplete && <div className="card" data-testid="incomplete-setup"><h2>{t('setupUx.incomplete')}</h2><div className="row"><Link className="btn-ghost" to="/max/settings">{t('setupUx.max')}</Link><Link className="btn-ghost" to="/telegram/network">{t('setupUx.telegram')}</Link><Link className="btn-ghost" to="/setup/max">{t('setupUx.continue')}</Link></div></div>}
      <div className="status-grid">
        <div className="status-cell" data-testid="dashboard-max">
          <span className="name">MAX</span>
          <span className="val"><StatusBadge state={status.max.state} /></span>
          <span className="meta">{status.max.account_id ? `аккаунт ${status.max.account_id}` : status.max.authenticated ? 'сессия активна' : 'сессия не подтверждена'}</span>
        </div>
        <div className="status-cell" data-testid="dashboard-telegram">
          <span className="name">Telegram</span>
          <span className="val"><StatusBadge state={status.telegram.state} /></span>
          <span className="meta">{status.telegram.bot_id ? `бот ${status.telegram.bot_id}` : 'бот не подключён'}</span>
        </div>
        <div className="status-cell">
          <span className="name">Мост</span>
          <span className="val">{(setupComplete || (status.max.configured && status.telegram.configured)) ? <StatusBadge state={status.bridge.state} /> : <span className="badge idle">{t("setupUx.waiting")}</span>}</span>
          <span className="meta">{status.bridge.worker_running ? 'обработчики работают' : 'обработчики не запущены'}</span>
        </div>
        <div className="status-cell">
          <span className="name">Очередь</span>
          <span className="val">{(queue.pending || 0) + (queue.processing || 0)} в работе</span>
          <span className="meta">доставлено {queue.delivered || 0} · ошибок {queue.failed || 0}</span>
        </div>
        <div className="status-cell">
          <span className="name">База данных</span>
          <span className="val">{status.database === 'ok' ? 'OK' : status.database}</span>
          <span className="meta">{fmtBytes(status.storage?.db_bytes)}</span>
        </div>
      </div>

      <div className="grid">
        <WatchdogCard adapter="max" label="MAX" canonical={status.max} reload={reload} />
        <WatchdogCard adapter="telegram" label="Telegram" canonical={status.telegram} reload={reload} />
      </div>
      <RecentActivity />
    </section>
  );
}

/** MAX integration page — status + reconnect + auth state. */
export function MaxPage({ status, reload }: { status: StatusSnapshot['max'] | null; reload?: () => void }) {
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  async function reconnect() {
    setBusy(true); setError(null);
    try {
      await api('/api/max/reconnect', { method: 'POST' });
      await api('/api/max/probe', { method: 'POST' });
      reload?.();
    } catch (e: any) { setError(e.message); } finally { setBusy(false); }
  }

  return (
    <section>
      <PageHeader title="MAX">
        <StatusBadge state={status?.state} />
      </PageHeader>
      {error && <div className="alert alert-error" role="alert">{error}</div>}
      <div className="card">
        {status ? (
          <div className="kv">
            <span className="k">Состояние</span><span className="v"><StatusBadge state={status.state} /></span>
            <span className="k">Аккаунт</span><span className="v">{status.account_id || '—'}</span>
            <span className="k">Сессия</span><span className="v">{status.authenticated ? 'авторизована' : status.configured ? 'проверяется' : 'нет'}</span>
            <span className="k">Последняя проверка</span><span className="v">{status.last_probe_at ? new Date(status.last_probe_at * 1000).toLocaleString() : '—'}</span>
          </div>
        ) : (
          <Spinner label="Загружаем…" />
        )}
        <div className="row">
          <button className="btn-ghost" onClick={reconnect} disabled={busy}>
            {busy ? <><span className="spinner" aria-hidden /> Переподключаем…</> : 'Переподключить'}
          </button>
          <Link className="btn-ghost" to="/max/settings">Настройки MAX</Link>
        </div>
      </div>
    </section>
  );
}

/** Telegram integration page — status + transport probe. */
export function TelegramPage({ status, reload }: { status: StatusSnapshot['telegram'] | null; reload?: () => void }) {
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [result, setResult] = React.useState<string | null>(null);

  async function probe() {
    setBusy(true); setError(null); setResult(null);
    try {
      const r = await api<{ ok: boolean; username?: string; error_class?: string; detail?: string }>('/api/telegram/transport/probe', { method: 'POST' });
      setResult(r.ok ? `Соединение в порядке: @${r.username}` : `Проблема: ${r.error_class} ${r.detail || ''}`);
      reload?.();
    } catch (e: any) { setError(e.message); } finally { setBusy(false); }
  }

  return (
    <section>
      <PageHeader title="Telegram">
        <StatusBadge state={status?.state} />
      </PageHeader>
      {error && <div className="alert alert-error" role="alert">{error}</div>}
      <div className="card">
        {status ? (
          <div className="kv">
            <span className="k">Состояние</span><span className="v"><StatusBadge state={status.state} /></span>
            <span className="k">Бот</span><span className="v">{status.bot_id || '—'}</span>
            <span className="k">Подключение</span><span className="v">{status.connected ? 'активно' : 'нет'}</span>
            <span className="k">Последняя проверка</span><span className="v">{status.last_probe_at ? new Date(status.last_probe_at * 1000).toLocaleString() : '—'}</span>
          </div>
        ) : (
          <Spinner label="Загружаем…" />
        )}
        <div className="row">
          <button className="btn-ghost" onClick={probe} disabled={busy}>
            {busy ? <><span className="spinner" aria-hidden /> Проверяем…</> : 'Проверить соединение'}
          </button>
          <Link className="btn-ghost" to="/telegram/settings">Настройки бота</Link>
          <Link className="btn-ghost" to="/telegram/network">Настройки сети</Link>
          <Link className="btn-ghost" to="/telegram/group">Настройки группы</Link>
        </div>
        {result && <div className={`alert ${result.startsWith('Соединение') ? 'alert-success' : 'alert-error'}`}>{result}</div>}
      </div>
      {status?.recovery && status.recovery.state !== 'HEALTHY' && status.recovery.state !== 'NOT_CONFIGURED' && (
        <div className="alert" role="status">
          Восстановление Telegram: {status.recovery.state}. Попыток: {status.recovery.attempts}.
          {status.recovery.failure && <> Причина: {status.recovery.failure}.</>}
          {status.recovery.retry_in_seconds != null && <> Следующая проверка не позднее чем через {Math.ceil(status.recovery.retry_in_seconds)} с.</>}
        </div>
      )}
      <MemberSync settings />
      <p className="muted">Проверка соединения не подтверждает получение обычных сообщений группы. Если SHMAX их не видит, проверьте права бота и /setprivacy → Disable в BotFather.</p>
    </section>
  );
}

/** Telegram users management (shell page, mirrors wizard step). */
export function UsersPage() {
  const [data, setData] = React.useState<{ users: Array<{
    user_id: string; display_name: string | null; username: string | null;
    nickname: string | null; role: string; approved: boolean; blocked: boolean;
    membership?: string | null; is_admin?: boolean;
  }> } | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  const reload = React.useCallback(async () => {
    setBusy(true);
    try { setData(await api('/api/telegram/users')); }
    catch (e: any) { setError(e.message); }
    finally { setBusy(false); }
  }, []);
  React.useEffect(() => { reload(); }, [reload]);

  async function setField(userId: string, action: 'approval' | 'blocked', body: Record<string, unknown>) {
    setError(null);
    try {
      await api(`/api/telegram/users/${encodeURIComponent(userId)}/${action}`, {
        method: 'PATCH', body: JSON.stringify(body),
      });
      await reload();
    } catch (e: any) { setError(e.message); }
  }

  return (
    <section>
      <PageHeader title="Пользователи">
        <button className="btn-ghost btn-sm" onClick={reload} disabled={busy}>Обновить</button>
      </PageHeader>
      {error && <div className="alert alert-error" role="alert">{error}</div>}
      <MemberSync onSync={reload} />
      {!data && <Spinner label="Загружаем…" />}
      {data && data.users.length === 0 && (
        <EmptyState glyph="☺" title="Пользователей пока нет"
          hint="Пользователи появятся после синхронизации, событий вступления или видимых боту сообщений." />
      )}
      {data && data.users.length > 0 && (
        <ul className="user-list">
          {data.users.map((u) => (
            <li className="user-row" key={u.user_id}>
              <div className="user-row-main">
                <strong>{u.display_name || u.username || u.user_id}</strong>
                {u.username && <span className="muted small">@{u.username}</span>}
                <span className="muted small">ID {u.user_id}</span>
                {u.membership && <span className="muted small">Telegram: {u.membership}{u.is_admin ? " · администратор группы" : ""}</span>}
                {u.blocked
                  ? <span className="badge error">заблокирован</span>
                  : u.approved
                    ? <span className="badge ok">одобрен · {u.role}</span>
                    : <span className="badge warn">ожидает подтверждения</span>}
              </div>
              <UserSignature user={u} onSaved={reload} />
              <div className="user-row-controls">
                <label className="row-check">
                  <input type="checkbox" checked={u.approved} disabled={busy}
                    onChange={(e) => setField(u.user_id, 'approval', {
                      approved: e.target.checked,
                      role: e.target.checked && (u.role || 'read_only') === 'read_only'
                        ? 'operator'
                        : (u.role || 'read_only'),
                    })} />
                  <span>Одобрить</span>
                </label>
                <label className="row-check">
                  <input type="checkbox" checked={u.blocked} disabled={busy}
                    onChange={(e) => setField(u.user_id, 'blocked', { blocked: e.target.checked })} />
                  <span>Заблокировать</span>
                </label>
                <label>
                  <span>Роль</span>
                  <select value={u.role || 'read_only'} disabled={busy}
                    onChange={(e) => setField(u.user_id, 'approval', { approved: u.approved, role: e.target.value })}>
                    {['read_only', 'operator', 'admin', 'owner'].map((r) => <option key={r} value={r}>{r}</option>)}
                  </select>
                </label>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export function Diagnostics() {
  const [items, setItems] = React.useState<DoctorResult[] | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  React.useEffect(() => {
    (async () => {
      try {
        const r = await api<{ results: DoctorResult[] }>('/api/doctor');
        setItems(r.results);
      } catch (e: any) { setError(e.message); }
    })();
  }, []);
  const tone: Record<string, string> = {
    ok: 'ok', warning: 'warn', error: 'error',
    not_configured: 'not-configured', not_tested: 'not-configured',
  };
  return (
    <>
      <PageHeader title="Диагностика" />
      {error && <div className="alert alert-error" role="alert">{error}</div>}
      {!items && <Spinner label="Запускаем проверки…" />}
      {items && (
        <table>
          <thead><tr><th>Проверка</th><th>Статус</th><th>Детали</th></tr></thead>
          <tbody>
            {items.map((it, i) => (
              <tr key={i}>
                <td>{it.name}</td>
                <td><span className={`tag ${tone[it.status] || ''}`}>{it.status}</span></td>
                <td className="muted">{it.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <SupportBundle />
    </>
  );
}

export { Journal as Logs } from "../components/Journal";

export function Backups({ backups, setBackups }: { backups: any[] | null; setBackups: (b: any[]) => void }) {
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const refresh = async () => {
    try {
      setBackups(await api<any[]>('/api/backups'));
      setError(null);
    } catch (e: any) { setError(e.message); }
  };
  React.useEffect(() => { refresh(); }, []);
  const create = async () => {
    setError(null); setBusy(true);
    try {
      // api() attaches the CSRF header — plain fetch caused HTTP 403.
      await api('/api/backups', { method: 'POST', body: JSON.stringify({}) });
      await refresh();
    } catch (e: any) { setError(e.message); } finally { setBusy(false); }
  };
  const fmt = (b: any) => b == null ? '—' : `${(b / 1024 / 1024).toFixed(2)} MB`;
  return (
    <>
      <PageHeader title="Резервные копии">
        <button className="btn-primary" onClick={create} disabled={busy}>
          {busy ? <><span className="spinner" aria-hidden /> Создаём…</> : 'Создать локальную копию'}
        </button>
      </PageHeader>
      <PortableExport />
      <h2>Локальные копии</h2>
      <p>Для восстановления на этом устройстве. Эти копии могут содержать незашифрованный ключ секретов; не используйте их как защищённый переносимый экспорт.</p>
      {error && <div className="alert alert-error" role="alert">{error}</div>}
      {backups && backups.length === 0 && (
        <EmptyState glyph="⌸" title="Резервных копий пока нет." hint="Создайте первую копию перед важными изменениями." />
      )}
{backups && backups.length > 0 && (
        <table>
          <thead><tr><th>Копия</th><th>Версия</th><th>Создана</th><th>Размер</th><th>Формат</th></tr></thead>
          <tbody>
            {backups.map((b) => (
              <tr key={b.name}>
                <td className="mono small">{b.name}<br /><span className="muted">{b.id ?? ''}</span></td>
                <td>{b.version || b.shmax_version || '—'}</td>
                <td className="muted">{b.created_at ? new Date(b.created_at * 1000).toLocaleString() : '—'}</td>
                <td>{fmt(b.size)}</td>
                <td><span className="tag">{b.encrypted ? 'шифрованная' : (b.format || b.error)}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="muted small"><a href="/help">Нужна помощь?</a></p>
    </>
  );
}

const UPDATE_STATUS_RU: Record<string, string> = {
  up_to_date: 'Установлена последняя версия.',
  update_available: 'Обновление доступно',
  not_checked: 'Ещё не проверялось',
  not_configured: 'Источник обновлений не настроен',
  timeout: 'Не удалось проверить обновления.',
  network_error: 'Не удалось проверить обновления.',
  http_error: 'Не удалось проверить обновления.',
  rate_limited: 'Не удалось проверить обновления.',
  bad_manifest: 'Не удалось проверить обновления.',
  oversized: 'Не удалось проверить обновления.',
};

const CHANNEL_RU: Record<string, string> = { stable: 'Stable', beta: 'Beta' };

export function Updates() {
  const [status, setStatus] = React.useState<any>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [showNotes, setShowNotes] = React.useState(false);
  const load = React.useCallback(async () => {
    try { setStatus(await api<any>('/api/update/status')); setError(null); }
    catch (e: any) { setError(e.message); }
  }, []);
  React.useEffect(() => { load(); }, [load]);
  const check = async () => {
    setBusy(true); setError(null);
    try { setStatus(await api('/api/update/check', { method: 'POST' })); }
    catch (e: any) { setError(e.message); }
    finally { setBusy(false); }
  };
  const apply = async () => {
    if (!window.confirm('Установить обновление? SHMAX создаст резервную копию и перезапустится.')) return;
    setBusy(true);
    try {
      await api('/api/update/apply', { method: 'POST', body: JSON.stringify({ channel: status?.channel || 'stable' }) });
    } catch (e: any) { setError(e.message); }
    finally { setBusy(false); }
  };
  const available = status?.available ?? null;
  const statusText = UPDATE_STATUS_RU[status?.status] ?? status?.status ?? '';
  return (
    <>
      <PageHeader title="Обновления">
        <button onClick={check} disabled={busy}>{busy ? 'Проверяем…' : 'Проверить обновления'}</button>
      </PageHeader>
      {error && <div className="alert alert-error" role="alert">{error}</div>}
      {status && (
        <div className="card">
          <div className="kv">
            <span className="k">Текущая версия</span><span className="v">{status.current_version}</span>
            <span className="k">Канал</span><span className="v">{CHANNEL_RU[status.channel] ?? status.channel}</span>
            <span className="k">Последняя проверка</span>
            <span className="v">{status.last_checked_at ? new Date(status.last_checked_at * 1000).toLocaleString() : '—'}</span>
            <span className="k">Доступна версия</span><span className="v">{status.latest_known_version ?? '—'}</span>
            <span className="k">Статус</span><span className="v" data-testid="update-status">{statusText}</span>
          </div>
          {status.status === 'not_configured' && (
            <p className="muted small">Укажите репозиторий релизов через переменную SHMAX_UPDATE_REPO_URL.</p>
          )}
          {available && status.status === 'update_available' && !status.hostd_available && (
            <p className="muted small" data-testid="apply-unavailable">
              Обновление доступно, но хост-компонент shmax-hostd недоступен — установка через веб-интерфейс невозможна.
            </p>
          )}
          {available && status.status === 'update_available' && status.hostd_available && (
            <>
              <div className="row" style={{ marginTop: '0.75rem' }}>
                <button className="btn-primary" onClick={apply} disabled={busy}>Обновить</button>
                <button onClick={() => setShowNotes((v) => !v)}>Что нового</button>
              </div>
              {showNotes && (
                <pre className="mono small" style={{ whiteSpace: 'pre-wrap', marginTop: '0.5rem' }} data-testid="release-notes">
                  {available.notes || 'Без описания.'}
                </pre>
              )}
            </>
          )}
        </div>
      )}
      <p className="muted small"><a href="/help">Нужна помощь?</a></p>
    </>
  );
}

export function Settings() {
  return (
    <>
      <PageHeader title="Настройки" />
      <div className="card muted small">
        <div className="row"><Link to="/max/settings">Настройки MAX</Link><Link to="/telegram/settings">Настройки бота</Link><Link to="/telegram/network">Настройки сети</Link><Link to="/telegram/group">Настройки группы</Link><Link to="/chats/settings">Маршруты</Link><Link to="/users">Пользователи и ACL</Link></div>
      </div>
      <StorageManager />
      <DangerZone />
    </>
  );
}

export function Chats({ routes }: { routes: RouteRow[] | null }) {
  const [rows, setRows] = React.useState<RouteRow[] | null>(routes);
  const [error, setError] = React.useState<string | null>(null);
  React.useEffect(() => {
    if (routes !== null) { setRows(routes); return; }
    (async () => {
      try { setRows(await routesList()); }
      catch (e: any) { setError(e.message); }
    })();
  }, [routes]);

  async function toggle(r: RouteRow) {
    if (rows === null) return;
    setError(null);
    try {
      await api(`/api/routes/${r.id}/toggle`, { method: 'POST', body: JSON.stringify({ enabled: !r.enabled }) });
      setRows(await routesList());
    } catch (e: any) { setError(e.message); }
  }

  return (
    <>
      <PageHeader title="Чаты и маршруты"><Link className="btn-ghost" to="/chats/settings">Настроить маршруты</Link></PageHeader>
      {error && <div className="alert alert-error" role="alert">{error}</div>}
      {rows === null && <Spinner label="Загружаем маршруты…" />}
      {rows && rows.length === 0 && (
        <EmptyState glyph="☰" title="Маршруты ещё не настроены"
          hint="Подключите MAX и Telegram-группу — темы создадутся автоматически." />
      )}
      {rows && rows.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>MAX чат</th>
              <th>Telegram</th>
              <th>Тема</th>
              <th>Мост</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td><strong>{r.label || r.max_chat_id}</strong></td>
                <td className="mono small">{r.telegram_chat_id}</td>
                <td>{r.telegram_thread_id ? `#${r.telegram_thread_id}` : '—'}</td>
                <td><span className={`tag ${r.enabled ? 'ok' : 'not-configured'}`}>{r.enabled ? 'ON' : 'OFF'}</span></td>
                <td>
                  <button className="btn-ghost btn-sm" onClick={() => toggle(r)}>
                    {r.enabled ? 'Выключить' : 'Включить'}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
