import React from 'react';
import {api} from './api';
import {PageHeader,Spinner} from './ui';
import {UserSignature} from './user-signature';
import {MemberSync} from './member-sync';
type Group={id:number;chat_id:string;title:string|null;is_active:boolean};
type User={user_id:string;display_name:string|null;username:string|null;nickname:string|null;role:string;approved:boolean;blocked:boolean;globally_blocked:boolean;membership:string|null;is_admin:boolean;in_group:boolean};
export function GroupUsersPage(){
 const [groups,setGroups]=React.useState<Group[]>([]),[group,setGroup]=React.useState('');
 const selectedGroup=React.useRef(group);selectedGroup.current=group;
 const [users,setUsers]=React.useState<User[]|null>(null),[error,setError]=React.useState(''),[busy,setBusy]=React.useState(false);
 React.useEffect(()=>{api<{groups:Group[]}>('/api/telegram/groups').then(r=>{setGroups(r.groups);setGroup(String(r.groups.find(g=>g.is_active)?.id||r.groups[0]?.id||''));}).catch(e=>setError(e.message));},[]);
 const reload=React.useCallback(async()=>{if(!group)return;const requested=group;setBusy(true);setError('');try{const r=await api<{users:User[]}>(`/api/telegram/groups/${requested}/users`);if(selectedGroup.current===requested)setUsers(r.users);}catch(e:any){if(selectedGroup.current===requested)setError(e.message);}finally{if(selectedGroup.current===requested)setBusy(false);}},[group]);
 React.useEffect(()=>{setUsers(null);reload();},[reload]);
 async function grant(user:User,changes:Partial<User>){setBusy(true);setError('');try{await api(`/api/telegram/groups/${group}/users/${encodeURIComponent(user.user_id)}`,{method:'PUT',body:JSON.stringify({approved:user.approved,role:user.role,blocked:user.blocked,...changes})});await reload();}catch(e:any){setError(e.message);}finally{setBusy(false);}}
 const selected=groups.find(g=>String(g.id)===group);
 return <section className="settings-form"><PageHeader title="Пользователи и доступ к группам"><button disabled={busy} onClick={reload}>Обновить</button></PageHeader>
  {error&&<div role="alert" className="alert alert-error">{error}</div>}
  <p>Права отправки в MAX действуют только в выбранной группе. Для закрытия просмотра удалите человека из самой Telegram-группы. Статус администратора Telegram не выдаёт права SHMAX автоматически.</p>
  <label>Настраиваемая группа<select aria-label="Группа пользователя" value={group} onChange={e=>{selectedGroup.current=e.target.value;setUsers(null);setGroup(e.target.value);}}><option value="">Выберите группу</option>{groups.map(g=><option key={g.id} value={g.id}>{g.title||g.chat_id}</option>)}</select></label>
  {selected&&<MemberSync key={selected.chat_id} groupId={selected.chat_id} onSync={reload}/>}
  {!groups.length&&!error&&<p>Сначала подключите Telegram-группу.</p>}
  {group&&!users&&!error&&<Spinner label="Загружаем пользователей…"/>}
  <ul className="user-list">{users?.map(u=><li className="user-row" key={u.user_id}><div className="user-row-main"><strong>{u.display_name||u.username||u.user_id}</strong><span>ID {u.user_id}</span><span>{u.membership||'Членство в Telegram не подтверждено'}{u.is_admin?' · администратор Telegram':''}</span>{u.globally_blocked&&<span className="badge error">Общая блокировка SHMAX</span>}</div>
   <UserSignature user={u} onSaved={reload}/>
   <div className="user-row-controls"><label className="row-check"><input type="checkbox" disabled={busy||u.globally_blocked} checked={u.approved} onChange={e=>grant(u,{approved:e.target.checked,role:e.target.checked&&u.role==='read_only'?'operator':u.role})}/> Разрешён в этой группе</label>
    <label className="row-check"><input type="checkbox" disabled={busy||u.globally_blocked} checked={u.blocked} onChange={e=>grant(u,{blocked:e.target.checked})}/> Запрет отправки в этой группе</label>
    <label>Роль<select disabled={busy||u.globally_blocked} value={u.role} onChange={e=>grant(u,{role:e.target.value})}>{['read_only','operator','admin','owner'].map(r=><option key={r} value={r}>{r==='read_only'?'Только чтение':r==='operator'?'Отправка в MAX':r}</option>)}</select></label>
   </div></li>)}</ul>
  {users?.length===0&&<p>Пользователи появятся после /start, сообщения в группе или синхронизации участников. Синхронизация не выдаёт права отправки.</p>}
 </section>;
}
