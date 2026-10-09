import { useNavigationSafety } from './navigation';
import React from 'react';
import { api } from './api';

export interface SignatureUser {
  user_id: string;
  nickname?: string | null;
  display_name?: string | null;
  username?: string | null;
}

export function UserSignature({ user, onSaved }: { user: SignatureUser; onSaved: () => void | Promise<void> }) {
  const { markDirty, markSaved } = useNavigationSafety();
  const [draft, setDraft] = React.useState(user.nickname || '');
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState('');
  React.useEffect(() => { setDraft(user.nickname || ''); }, [user.user_id, user.nickname]);
  const fallback = user.display_name || (user.username ? `@${user.username.replace(/^@/, '')}` : 'Участник');
  const label = (draft.trim() || fallback).replace(/[\r\n\t]/g, ' ').trim();
  const chars = [...label];
  const preview = chars.length > 64 ? chars.slice(0, 63).join('') + '…' : label;

  async function save(value: string) {
    const nickname = value.trim() || null;
    if (/\p{Cc}|\p{Zl}|\p{Zp}/u.test(value) || (nickname && [...nickname].length > 64)) {
      setError('До 64 символов, без переносов строк и управляющих символов.');
      return;
    }
    setBusy(true); setError('');
    try {
      const result = await api<{ nickname: string | null }>(`/api/telegram/users/${encodeURIComponent(user.user_id)}/nickname`, {
        method: 'PATCH', body: JSON.stringify({ nickname }),
      });
      markSaved();
      setDraft(result.nickname || '');
      await onSaved();
    } catch (e: any) { setError(e.message); }
    finally { setBusy(false); }
  }

  return <div className="user-signature" onChangeCapture={markDirty}>
    <label htmlFor={`nickname-${user.user_id}`}>Подпись в MAX</label>
    <div className="user-signature-edit">
      <input id={`nickname-${user.user_id}`} type="text" value={draft} maxLength={128}
        placeholder="Использовать имя Telegram" disabled={busy}
        onChange={e => setDraft(e.target.value)} />
      <button type="button" className="btn-ghost btn-sm" disabled={busy} onClick={() => save(draft)}>Сохранить подпись</button>
      <button type="button" className="btn-ghost btn-sm" disabled={busy || (!draft && !user.nickname)} onClick={() => save('')}>Очистить</button>
    </div>
    <p className="muted small">Предпросмотр: [{preview}]: Текст сообщения</p>
    <p className="muted small">{draft.trim() ? 'Локальная подпись SHMAX; профиль Telegram не меняется.' : 'Использовать имя Telegram. Пустая подпись возвращает обычное имя.'} Синхронизация участников эту подпись не меняет.</p>
    {error && <p role="alert" className="alert alert-error">{error}</p>}
  </div>;
}
