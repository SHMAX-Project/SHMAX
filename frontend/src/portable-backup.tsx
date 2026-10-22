import { useTranslation } from 'react-i18next';
import { useNavigationSafety } from './navigation';
import React from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { api, apiBlob, ApiError } from './api';

export function SetupChoice({ onDismissed }: { onDismissed?: () => Promise<void> }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [error, setError] = React.useState('');
  const [busy, setBusy] = React.useState(false);
  async function skip() {
    setBusy(true); setError('');
    try { await api('/api/setup/dismiss', { method: 'POST' }); await onDismissed?.(); navigate('/dashboard'); }
    catch (e: any) { setError(e.message); }
    finally { setBusy(false); }
  }
  return <section className="card stack">
    <h1>{t('setupUx.welcome')}</h1>
    <p>{t('setupUx.choice')}</p>
    <Link className="btn-primary" to="/setup/max/network">{t('setupUx.start')}</Link>
    <Link className="btn-ghost" to="/setup/restore">{t('setupUx.restore')}</Link>
    <button className="btn-ghost" disabled={busy} onClick={skip}>{t('setupUx.skip')}</button>
    {error && <p role="alert">{error}</p>}
  </section>;
}

export function PortableExport() {
  const { markDirty, markSaved } = useNavigationSafety();
  const [password,setPassword]=React.useState('');
  const [confirmation,setConfirmation]=React.useState('');
  const [busy,setBusy]=React.useState(false);const [message,setMessage]=React.useState('');
  async function submit(e:React.FormEvent) {
    e.preventDefault();setMessage('');
    if(password!==confirmation) {setMessage('Пароли не совпадают.');return;}
    setBusy(true);setMessage('Создание согласованного снимка, шифрование и проверка…');
    try {
      const blob=await apiBlob('/api/portable/export',{method:'POST',body:JSON.stringify({password,confirmation})});
      const url=URL.createObjectURL(blob);const link=document.createElement('a');
      link.href=url;link.download=`shmax-backup-${new Date().toISOString().slice(0,10)}.shmax`;
      document.body.appendChild(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);
      setMessage('Готово. Зашифрованный файл скачан. Сохраните пароль отдельно.');
    } catch(e:any) {setMessage(e.message);}
    finally {markSaved();setPassword('');setConfirmation('');setBusy(false);}
  }
  return <section className="card stack portable-backup">
    <h2>Перенос на другой сервер</h2>
    <p>Скачайте один зашифрованный файл .shmax с конфигурацией, базой данных, секретами и доступными сеансами MAX/Telegram. Локальные копии для восстановления на этом устройстве могут быть незашифрованными.</p>
    <p>Этот файл содержит данные авторизации. Храните его и пароль в безопасном месте. Без пароля восстановление невозможно.</p>
    <form onSubmit={submit} className="stack" onChangeCapture={markDirty}>
      <label>Пароль резервной копии<input type="password" autoComplete="new-password" value={password} onChange={e=>setPassword(e.target.value)} minLength={10} maxLength={256} required disabled={busy}/></label>
      <label>Повторите пароль<input type="password" autoComplete="new-password" value={confirmation} onChange={e=>setConfirmation(e.target.value)} required disabled={busy}/></label>
      <button className="btn-primary" disabled={busy}>{busy?'Подготовка экспорта…':'Экспортировать для переноса'}</button>
    </form>
    {message&&<p role="status">{message}</p>}
    <p>Перед запуском перенесённого SHMAX остановите старый мост. Два экземпляра не должны одновременно использовать один Telegram Bot или MAX аккаунт.</p>
  </section>;
}

const phases:Record<string,string>={queued:'Подготовка',validating:'Проверка',migrating:'Миграции',restoring:'Восстановление',restarting:'Перезапуск',checking:'Проверка готовности',completed:'Завершено',failed:'Восстановление отменено',rollback_failed:'Требуется восстановление владельцем'};
export function PortableRestore() {
  const { markDirty, markSaved } = useNavigationSafety();
  const navigate=useNavigate();const [file,setFile]=React.useState<File|null>(null);
  const [password,setPassword]=React.useState('');const [preview,setPreview]=React.useState<any>(null);
  const [busy,setBusy]=React.useState(false);const [message,setMessage]=React.useState('');
  const [sourceStopped,setSourceStopped]=React.useState(false);
  const alive=React.useRef(true);React.useEffect(()=>()=>{alive.current=false;},[]);
  async function inspect(e:React.FormEvent) {
    e.preventDefault();if(!file)return;
    if(file.size>513*1024*1024){setMessage('Резервная копия слишком велика.');return;}
    setBusy(true);setPreview(null);setSourceStopped(false);setMessage('Загрузка…');
    try {
      const uploaded=await api<{job_id:string}>('/api/portable/import',{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file});
      setMessage('Расшифровка и проверка содержимого…');
      setPreview(await api(`/api/portable/${uploaded.job_id}/inspect`,{method:'POST',body:JSON.stringify({password})}));
      setMessage('Проверено. Данные ещё не изменены.');
    } catch(e:any){setMessage(e.message);}
    finally {setPassword('');setBusy(false);}
  }
  async function restore() {
    markSaved();
    setBusy(true);setMessage('Подготовка восстановления…');
    try {
      await api(`/api/portable/${preview.job_id}/restore`,{method:'POST',body:'{}'});
      const deadline=Date.now()+10*60*1000;
      while(alive.current && Date.now()<deadline) {
        await new Promise(resolve=>setTimeout(resolve,1500));
        try {
          const status=await api<any>(`/api/portable/${preview.job_id}/status`);
          setMessage(phases[status.phase]||'Восстановление…');
          if(['completed','failed','rollback_failed'].includes(status.phase)) {
            setMessage(status.detail||'Восстановление завершено. Войдите с логином и паролем нового владельца.');break;
          }
        } catch(e) {
          if(e instanceof ApiError && e.status===401){navigate('/login',{replace:true});break;}
          setMessage('Перезапуск SHMAX… После восстановления войдите с логином и паролем нового владельца.');
        }
      }
      if(alive.current && Date.now()>=deadline)setMessage('Ожидание завершено. Проверьте доступность SHMAX и статус восстановления перед повторной попыткой.');
    } catch(e:any){setMessage(e.message);}
    finally {if(alive.current)setBusy(false);}
  }
  return <section className="card stack portable-backup">
    <h1>Восстановление SHMAX</h1>
    <p>После создания владельца можно восстановить готовую конфигурацию и перейти к панели управления, пропустив остальные шаги мастера. Внешние службы могут потребовать повторную авторизацию.</p>
    <p>Логин и пароль нового владельца сохранятся; прежние веб-администраторы и сеансы не импортируются. Привязка к IP нового сервера сохраняется.</p>
    <form className="stack" onSubmit={inspect} onChangeCapture={markDirty}>
      <label>Выберите файл .shmax<input type="file" accept=".shmax,.enc,.gz,.tar" disabled={busy} onChange={e=>{setFile(e.target.files?.[0]||null);setPreview(null);setSourceStopped(false);}} required/></label>
      <label>Пароль резервной копии<input type="password" autoComplete="off" maxLength={256} disabled={busy} value={password} onChange={e=>setPassword(e.target.value)}/></label>
      <p className="muted small">Поддерживаются переносимые .shmax и старые локальные копии v1 (.shmax, .shmax.enc, .tar.gz). Введите исходный пароль; для незашифрованной старой копии оставьте поле пустым. Старые пароли от 8 символов принимаются.</p>
      <button className="btn-primary" disabled={busy||!file}>Проверить резервную копию</button>
    </form>
    {preview&&<div className="stack">
      {preview.source_format==='legacy-v1'&&<p>Старая локальная копия проверена и подготовлена для восстановления. Рабочие данные ещё не изменены.</p>}
      <p>Версия SHMAX: {preview.application_version}. Создана: {new Date(preview.created_at*1000).toLocaleString()}.</p>
      <p>Содержимое: {preview.contents.join(', ')}. Совместимость проверена.</p>
      {preview.warnings.map((warning:string)=><p key={warning}>{warning}</p>)}
      <label><input type="checkbox" checked={sourceStopped} disabled={busy} onChange={e=>setSourceStopped(e.target.checked)}/> Старый SHMAX остановлен; начать восстановление на этом сервере.</label>
      <button className="btn-primary" disabled={busy||!sourceStopped} onClick={restore}>Восстановить SHMAX</button>
    </div>}
    {message&&<p role="status">{message}</p>}
    <Link to="/setup/choice">Вернуться к выбору настройки</Link>
  </section>;
}
