import { useTranslation } from 'react-i18next';
import { ConfigurationEditor } from './configuration-editor';
import { SetupChoice, PortableRestore } from "./portable-backup";
import { useCallback, useEffect, useState } from 'react';
import { Link, Navigate, NavLink, Outlet, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import { api, login, logout, SessionStatus, setUnauthorizedObserver, StatusSnapshot } from './api';
import { setCsrfToken } from './csrf';
import { Admin, Dashboard, Diagnostics, Logs, Backups, Updates, Settings, Chats, MaxPage, TelegramPage, UsersPage } from './pages';
import { HelpPage, AboutPage } from './help';
import { StatusBadge } from './ui';
import {
  wizardSteps,
  StepMax,
  StepTelegramBot,
  StepTelegramNetwork,
  StepTelegramGroup,
  StepMaxChats,
  StepUsers,
  StepTest,
  StepFinish,
  StepIndicator,
  type WizardStepId,
} from './wizard';

const NAV: Array<[string, string, string]> = [
  ['/dashboard', 'Главная', '◉'],
  ['/max', 'MAX', '⇄'],
  ['/telegram', 'Telegram', '✈'],
  ['/chats', 'Чаты', '☰'],
  ['/users', 'Пользователи', '☺'],
  ['/diagnostics', 'Диагностика', '⚙'],
  ['/logs', 'Журнал', '❢'],
  ['/backups', 'Резервные копии', '⌸'],
  ['/updates', 'Обновления', '↻'],
  ['/settings', 'Настройки', '✳'],
  ['/about', 'О SHMAX', 'ℹ'],
  ['/help', 'Инструкция', '?'],
];

function Brand({ compact, onNavigate }: { compact?: boolean; onNavigate?: () => void }) {
  const { t } = useTranslation();
  const location = useLocation();
  return (
    <Link className="brand" to={`/dashboard${location.search}`} aria-label={`SHMAX — ${t("setupUx.home")}`} onClick={onNavigate}>
      <img className="logo" src="/shmax-icon-256.png" alt="" aria-hidden width={28} height={28} style={{ borderRadius: 7 }} />
      <span>
        SHMAX
        {!compact && <span className="sub">MAX ↔ TELEGRAM</span>}
      </span>
    </Link>
  );
}

function NavLinks({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <nav aria-label="Основная навигация">
      {NAV.map(([path, label, ico]) => (
        <NavLink key={path} to={path} className={({ isActive }) => (isActive ? 'active' : '')} onClick={onNavigate}>
          <span className="ico" aria-hidden>{ico}</span>
          {label}
        </NavLink>
      ))}
    </nav>
  );
}

function BridgeChip({ status }: { status: StatusSnapshot | null }) {
  if (!status) return <span className="badge idle">…</span>;
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
      <StatusBadge state={status.bridge.state} />
    </span>
  );
}

function AppShell({ status, username, onLogout }: { status: StatusSnapshot | null; username: string | null; onLogout: () => void }) {
  const [drawer, setDrawer] = useState(false);
  return (
    <div className="app-shell" data-testid="app-shell">
      <aside className="sidebar" data-testid="sidebar">
        <Brand />
        <NavLinks />
        <div className="sidebar-footer">
          <div className="row" style={{ margin: 0, justifyContent: 'space-between' }}>
            <small className="dim">Мост</small>
            <BridgeChip status={status} />
          </div>
          <div className="account">
            <span className="avatar" aria-hidden>{(username || '?').slice(0, 1).toUpperCase()}</span>
            <span>{username || '—'}</span>
          </div>
          <button type="button" className="btn-ghost btn-sm" onClick={onLogout}>Выйти</button>
        </div>
      </aside>
      <div className="main">
        <header className="topbar">
          <button type="button" className="nav-burger" aria-label="Открыть меню" onClick={() => setDrawer(true)}>☰</button>
          <Brand compact />
          <BridgeChip status={status} />
        </header>
        {drawer && (
          <div className="mobile-drawer" onClick={() => setDrawer(false)}>
            <div className="sheet" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Меню">
              <Brand onNavigate={() => setDrawer(false)} />
              <NavLinks onNavigate={() => setDrawer(false)} />
              <div className="sidebar-footer">
                <div className="account">
                  <span className="avatar" aria-hidden>{(username || '?').slice(0, 1).toUpperCase()}</span>
                  <span>{username || '—'}</span>
                </div>
                <button type="button" className="btn-ghost btn-sm" onClick={onLogout}>Выйти</button>
              </div>
            </div>
          </div>
        )}
        <div className="page">
          <Outlet />
        </div>
      </div>
    </div>
  );
}

// Setup layout: chrome only — step numbering is owned by the
// canonical StepIndicator inside each step; Back / Next live in
// each step's footer so they never overlap or duplicate.
function SetupLayout({ authenticated }: { authenticated: boolean }) {
  const { t } = useTranslation();
  return (
    <main className="wizard-page" data-testid="setup-layout">
      <div className="wizard-frame">
        <Brand />
        {authenticated && <nav className="wizard-exit" aria-label={t("setupUx.home")}><Link className="btn-ghost btn-sm" to="/dashboard">{t("setupUx.home")}</Link><Link className="btn-ghost btn-sm" to="/dashboard">{t("setupUx.cancel")}</Link></nav>}
        <Outlet />
      </div>
    </main>
  );
}

function Login({ onLogin }: { onLogin: () => Promise<void> }) {
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  return (
    <main className="wizard-page">
      <div className="wizard-frame">
        <Brand />
        <section className="wizard-step">
          <div className="wizard-step-header">
            <div className="eyebrow">SHMAX</div>
            <h2>Вход</h2>
          </div>
          <form className="col" onSubmit={async e => {
            e.preventDefault(); setBusy(true); setError('');
            const data = new FormData(e.currentTarget);
            try {
              await login(String(data.get('username')), String(data.get('password')));
              await onLogin();
            } catch (e: any) { setError(e.message); } finally { setBusy(false); }
          }}>
            <label className="form-row">
              <span>Имя администратора</span>
              <input name="username" autoComplete="username" required />
            </label>
            <label className="form-row">
              <span>Пароль</span>
              <input name="password" type="password" autoComplete="current-password" required />
            </label>
            {error && <p role="alert" className="alert alert-error">{error}</p>}
            <div className="row">
              <button className="btn-primary" disabled={busy}>Войти</button>
            </div>
          </form>
        </section>
      </div>
    </main>
  );
}

// Routing helper — canonical wizard order means Back/Next go to
// the previous/next route in the registry.
function useWizardNav() {
  const navigate = useNavigate();
  return useCallback((id: WizardStepId | 'next' | 'prev') => {
    const cur = wizardSteps.findIndex((s) => window.location.pathname === s.route);
    if (cur < 0) return;
    if (id === 'next') {
      const next = wizardSteps[cur + 1];
      if (next) navigate(next.route);
    } else if (id === 'prev') {
      const prev = wizardSteps[cur - 1];
      if (prev) navigate(prev.id === "admin" ? "/setup/choice" : prev.route);
    } else {
      const target = wizardSteps.find((s) => s.id === id);
      if (target) navigate(target.route);
    }
  }, [navigate]);
}

export default function App() {
  const [session, setSession] = useState<SessionStatus | null>(null);
  const [error, setError] = useState('');
  const [status, setStatus] = useState<StatusSnapshot | null>(null);
  const location = useLocation();
  const nav = useWizardNav();
  const refreshSession = useCallback(async () => {
    const s = await api<SessionStatus>('/api/auth/session');
    setCsrfToken(s.csrf_token);
    setSession(s);
  }, []);
  const reloadStatus = useCallback(async () => {
    try { setStatus(await api<StatusSnapshot>('/api/status')); } catch { setStatus(null); }
  }, []);
  useEffect(() => {
    refreshSession().catch(e => setError(e.message));
    setUnauthorizedObserver(path => {
      if (!path.startsWith('/api/auth/')) {
        setSession(s => s ? { ...s, authenticated: false, mode: 'AUTH_REQUIRED' } : null);
        setStatus(null);
      }
    });
    return () => setUnauthorizedObserver(null);
  }, [refreshSession]);
  useEffect(() => {
    if (!session?.authenticated) return;
    reloadStatus();
    const id = window.setInterval(reloadStatus, 10000);
    return () => window.clearInterval(id);
  }, [session?.authenticated, reloadStatus]);

  if (!session) {
    return (
      <main className="wizard-page">
        <div className="wizard-frame">
          <Brand />
          <section className="wizard-step">
            <p>{error ? `Не удалось проверить доступ: ${error}` : 'Проверяем доступ…'}</p>
            {error && <button onClick={() => { setError(''); refreshSession().catch(e => setError(e.message)); }}>Повторить</button>}
          </section>
        </div>
      </main>
    );
  }
  const bootstrap = !session.admin_exists;
  if (bootstrap && !['/setup', '/setup/admin', '/setup/welcome'].includes(location.pathname)) return <Navigate to={`/setup${location.search}`} replace />;
  if (!bootstrap && !session.authenticated && location.pathname !== '/login') return <Navigate to="/login" replace />;
  if (!bootstrap && !session.authenticated) return <Login onLogin={refreshSession} />;
  if (session.authenticated && location.pathname === '/login') return <Navigate to="/dashboard" replace />;
  return (
    <Routes>
      <Route path="/setup" element={<SetupLayout authenticated={session.authenticated} />}>
        <Route index element={bootstrap ? <Admin onCreated={refreshSession} /> : <Navigate to="choice" replace />} />
        <Route path="welcome" element={bootstrap ? <Admin onCreated={refreshSession} /> : <Navigate to="/setup/max" replace />} />
        <Route path="admin" element={bootstrap ? <Admin onCreated={refreshSession} /> : <Navigate to="/setup/max" replace />} />
        {!bootstrap && (
          <>
            <Route path="choice" element={<SetupChoice onDismissed={refreshSession} />} />
            <Route path="restore" element={<PortableRestore />} />
            <Route path="max" element={<StepMax onNext={() => nav('next')} onPrev={() => nav('prev')} />} />
            <Route path="telegram" element={<StepTelegramBot onNext={() => nav('next')} onPrev={() => nav('prev')} />} />
            <Route path="telegram/network" element={<StepTelegramNetwork onNext={() => nav('next')} onPrev={() => nav('prev')} />} />
            <Route path="telegram/group" element={<StepTelegramGroup onNext={() => nav('next')} onPrev={() => nav('prev')} />} />
            <Route path="chats" element={<StepMaxChats onNext={() => nav('next')} onPrev={() => nav('prev')} />} />
            <Route path="users" element={<StepUsers onNext={() => nav('next')} onPrev={() => nav('prev')} />} />
            <Route path="test" element={<StepTest onNext={() => nav('next')} onPrev={() => nav('prev')} />} />
            <Route path="finish" element={<StepFinish onPrev={() => nav('prev')} />} />
          </>
        )}
        <Route path="*" element={<Navigate to={bootstrap ? '/setup' : '/setup/max'} replace />} />
      </Route>
      {!bootstrap && session.authenticated && (
        <Route element={<AppShell status={status} username={session.username} onLogout={async () => { await logout(); window.location.assign('/login'); }} />}>
          <Route index element={<Navigate to="/dashboard" replace />} />
          <Route path="dashboard" element={<Dashboard status={status} reload={reloadStatus} setupComplete={session.setup_complete} />} />
          <Route path="max" element={<MaxPage status={status?.max ?? null} reload={reloadStatus} />} />
          <Route path="max/settings" element={<ConfigurationEditor component="max" reload={reloadStatus} />} />
          <Route path="telegram/settings" element={<ConfigurationEditor component="telegram-bot" reload={reloadStatus} />} />
          <Route path="telegram/network" element={<ConfigurationEditor component="telegram-network" reload={reloadStatus} />} />
          <Route path="telegram/group" element={<ConfigurationEditor component="telegram-group" reload={reloadStatus} />} />
          <Route path="telegram" element={<TelegramPage status={status?.telegram ?? null} reload={reloadStatus} />} />
          <Route path="chats/settings" element={<ConfigurationEditor component="max-chats" reload={reloadStatus} />} />
          <Route path="chats" element={<Chats routes={null} />} />
          <Route path="users" element={<UsersPage />} />
          <Route path="diagnostics" element={<Diagnostics />} />
          <Route path="logs" element={<Logs />} />
          <Route path="backups" element={<Backups backups={null} setBackups={() => {}} />} />
          <Route path="updates" element={<Updates />} />
          <Route path="settings" element={<Settings />} />
          <Route path="help" element={<HelpPage />} />
          <Route path="about" element={<AboutPage version={status?.version ?? null} />} />
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Route>
      )}
    </Routes>
  );
}
