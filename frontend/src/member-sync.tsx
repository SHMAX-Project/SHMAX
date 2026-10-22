import { useNavigationSafety } from './navigation';
import React from 'react';
import { Link } from 'react-router-dom';
import { api } from './api';

interface State { configured: boolean; running: boolean; api_id: number | null; api_hash_saved: boolean; status: string; last_sync_at: number; found: number; new: number; updated: number }
export function MemberSync({ settings = false, onSync, groupId }: { settings?: boolean; onSync?: () => void; groupId?: string }) {
  const { markDirty, markSaved } = useNavigationSafety();
  const [state, setState] = React.useState<State | null>(null);
  const [id, setId] = React.useState('');
  const [hash, setHash] = React.useState('');
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState('');
  const statusPath='/api/telegram/member-sync'+(groupId?'?group_id='+encodeURIComponent(groupId):'');
  React.useEffect(() => { api<State>(statusPath).then(s => { setState(s); setId(s.api_id ? String(s.api_id) : ''); }).catch(e => setError(e.message)); }, [statusPath]);
  React.useEffect(() => {
    if (!state?.running) return;
    const timer = window.setTimeout(() => {
      api<State>(statusPath).then(s => {
        setState(s);
        if (!s.running) onSync?.();
      }).catch(e => setError(e.message));
    }, 2000);
    return () => window.clearTimeout(timer);
  }, [state, onSync,statusPath]);
  async function run(save: boolean) {
    setBusy(true); setError('');
    try {
      if (save) {
        setState(await api<State>('/api/telegram/member-sync', { method: 'PUT', body: JSON.stringify({ api_id: Number(id), api_hash: hash }) }));
        markSaved();
        setHash('');
      } else {
        await api('/api/telegram/member-sync'+(groupId?'?group_id='+encodeURIComponent(groupId):''), { method: 'POST' });
        setState(await api<State>(statusPath));
        onSync?.();
      }
    } catch (e: any) { setError(e.message); } finally { setBusy(false); }
  }
  return <div className="card member-sync" onChangeCapture={markDirty}>
    {settings && <>
      <h3>Синхронизация участников · необязательно</h3>
      <p>SHMAX может автоматически синхронизировать участников Telegram-группы, чтобы им не приходилось писать боту для появления в разделе «Пользователи».</p>
      <p>API ID и API Hash доступны на <a href="https://my.telegram.org" target="_blank" rel="noreferrer">my.telegram.org</a>. Используется существующий бот, без userbot и входа в человеческий Telegram-аккаунт. Мост работает и без этих настроек; ошибка синхронизации его не останавливает. API Hash — секрет: не отправляйте его в поддержку или логи.</p>
      <label>Telegram API ID<input type="number" min="1" value={id} onChange={e => setId(e.target.value)} /></label>
      <label>Telegram API Hash<input type="password" autoComplete="new-password" value={hash} placeholder={state?.api_hash_saved ? 'Сохранён; оставьте пустым для сохранения' : ''} onChange={e => setHash(e.target.value)} /></label>
      <button disabled={busy || !id} onClick={() => run(true)}>Сохранить настройки синхронизации</button>
    </>}
    {state && !state.configured && <p>Полная синхронизация участников не настроена. <Link to="/telegram">Настройки Telegram</Link></p>}
    {state?.configured && <button disabled={busy || state.running} onClick={() => run(false)}>{busy ? 'Синхронизируем…' : 'Синхронизировать с Telegram'}</button>}
    {!!state?.last_sync_at && <p>Последняя синхронизация: {new Date(state.last_sync_at * 1000).toLocaleString()}<br />Найдено: {state.found} · Новых: {state.new} · Обновлено: {state.updated}</p>}
    {state && !['ok', 'not_checked', 'not_configured'].includes(state.status) && <p role="status">{state.status === 'not_paired' ? 'Сначала подключите группу Telegram.' : state.status === 'running' ? 'Синхронизация уже выполняется.' : 'Синхронизация не завершена. Проверьте подключение и права бота; повторите позже.'}</p>}
    <p className="muted small">Обнаружение не даёт доступ: новые участники не одобрены и имеют роль read_only. Telegram admin не даёт SHMAX admin; доступом управляет администратор SHMAX. Будущие изменения членства отслеживаются через chat_member, видимые сообщения остаются запасным способом обнаружения.</p>
    {error && <p role="alert">{error}</p>}
  </div>;
}
