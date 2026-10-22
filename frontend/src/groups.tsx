import React from 'react';
import {api} from './api';
import {PageHeader, Spinner} from './ui';

type Group = {id:number;chat_id:string;title:string|null;is_active:boolean;ready:boolean;is_forum:boolean;is_admin_hub:boolean;auto_add_chats:boolean;show_source_chat:boolean;show_sender:boolean};
type Route = {id:number;max_chat_id:string;telegram_chat_id:string;telegram_thread_id:string|null;label:string|null;enabled:boolean};
export function GroupsPage(){
 const [groups,setGroups]=React.useState<Group[]>([]),[routes,setRoutes]=React.useState<Route[]>([]);
 const [chats,setChats]=React.useState<Array<{chat_id:string;title:string|null}>>([]);
 const [selected,setSelected]=React.useState(''),[maxChat,setMaxChat]=React.useState('');
 const [thread,setThread]=React.useState('');
 const [code,setCode]=React.useState(''),[error,setError]=React.useState(''),[busy,setBusy]=React.useState(false),[loaded,setLoaded]=React.useState(false);
 const reload=React.useCallback(async()=>{
  const [g,r,c]=await Promise.all([api<{groups:Group[]}>('/api/telegram/groups'),api<Route[]>('/api/routes'),api<{chats:Array<{chat_id:string;title:string|null}>}>('/api/max/chats').catch(()=>({chats:[]}))]);
  setGroups(g.groups);setRoutes(r);setChats(c.chats);setSelected(old=>old||String(g.groups.find(x=>x.is_active)?.id||g.groups[0]?.id||''));setLoaded(true);
 },[]);
 React.useEffect(()=>{reload().catch(e=>setError(e.message));},[reload]);
 React.useEffect(()=>{if(!code)return;const timer=window.setInterval(()=>{reload().catch(e=>setError(e.message));},3000);return()=>clearInterval(timer);},[code,reload]);
 async function action(fn:()=>Promise<unknown>){setBusy(true);setError('');try{await fn();await reload();}catch(e:any){setError(e.message);}finally{setBusy(false);}}
 const group=groups.find(g=>String(g.id)===selected);
 async function update(values:Partial<Group>){if(group)await action(()=>api(`/api/telegram/groups/${group.id}`,{method:'PATCH',body:JSON.stringify(values)}));}
 return <section className="settings-form"><PageHeader title="Telegram-группы"><button disabled={busy} onClick={()=>action(reload)}>Обновить</button></PageHeader>
  {error&&<div role="alert" className="alert alert-error">{error}</div>}
  <p>Каждая закрытая группа получает только привязанные чаты MAX. Админский обзор получает переписку в обе стороны. Участников добавляйте и удаляйте средствами Telegram; право отправки в MAX задаётся отдельно на странице пользователей.</p>
  <div className="card"><h2>Подключить ещё одну группу</h2><p>Добавьте того же бота в форум-группу и дайте ему необходимые права. Подключённые группы сохраняются.</p>
   <button disabled={busy} onClick={()=>action(async()=>{const r=await api<{code:string}>('/api/telegram/group/pair-code',{method:'POST'});setCode(r.code);})}>Получить код подключения</button>
   {code&&<p>Отправьте в новой группе: <code>/shmax_connect {code}</code><button onClick={()=>setCode('')}>Завершить подключение</button></p>}
  </div>
  {!loaded&&!error&&<Spinner label="Загрузка групп…"/>}
  {!!groups.length&&<label>Группа<select aria-label="Telegram-группа" value={selected} onChange={e=>setSelected(e.target.value)}>{groups.map(g=><option key={g.id} value={g.id}>{g.title||g.chat_id}{!g.is_active?' · отключена':''}</option>)}</select></label>}
  {group&&<>
   <div className="card"><h2>{group.title||group.chat_id}</h2><p>ID {group.chat_id} · {group.ready?'подключена и проверена':'отключена или требует проверки'}</p>
    <label><input type="checkbox" checked={group.is_active} disabled={busy} onChange={e=>update({is_active:e.target.checked})}/> Группа включена</label><br/>
    <label><input type="checkbox" checked={group.is_admin_hub} disabled={busy||!group.is_forum} onChange={e=>update({is_admin_hub:e.target.checked,auto_add_chats:e.target.checked})}/> Админский обзор всех новых переписок MAX</label>
    {group.is_admin_hub&&<><p className="alert alert-warning">Все участники этой Telegram-группы будут видеть сообщения из всех чатов MAX, включая личные. Оставьте здесь только доверенных администраторов.</p>
     <label><input type="checkbox" checked={group.auto_add_chats} disabled={busy} onChange={e=>update({auto_add_chats:e.target.checked})}/> Автоматически создавать тему при первом новом сообщении чата MAX</label><p>Старая история автоматически не выгружается. При отключённом автодобавлении действуют существующие привязки.</p></>}
    <h3>Подписи входящих</h3><label><input type="checkbox" checked={group.show_source_chat} disabled={busy} onChange={e=>update({show_source_chat:e.target.checked})}/> Название чата MAX</label><br/>
    <label><input type="checkbox" checked={group.show_sender} disabled={busy} onChange={e=>update({show_sender:e.target.checked})}/> Имя отправителя, когда оно доступно</label>
   </div>
   <div className="card"><h2>Привязанные чаты MAX</h2>
    <ul>{routes.filter(r=>r.telegram_chat_id===group.chat_id&&r.label!=='system').map(r=><li key={r.id}><RouteEditor route={r} forum={group.is_forum} busy={busy} save={values=>action(()=>api(`/api/telegram/groups/${group.id}/routes/${r.id}`,{method:'PATCH',body:JSON.stringify(values)}))}/></li>)}</ul>
    <label>Добавить чат<select aria-label="Чат MAX" value={maxChat} onChange={e=>setMaxChat(e.target.value)}><option value="">Выберите чат</option>{chats.map(c=><option key={c.chat_id} value={c.chat_id}>{c.title||c.chat_id}</option>)}</select></label>
    {group.is_forum&&<label>ID существующей темы (пусто — создать новую)<input aria-label="ID темы для привязки" value={thread} onChange={e=>setThread(e.target.value)}/></label>}
    <button disabled={busy||!group.ready||!maxChat} onClick={()=>action(()=>api(`/api/telegram/groups/${group.id}/routes`,{method:'POST',body:JSON.stringify({max_chat_id:maxChat,thread_id:thread||null,title:chats.find(c=>c.chat_id===maxChat)?.title||maxChat})}))}>Привязать чат</button>
    <p>Один чат MAX можно привязать к нескольким группам. Чтобы передать его другой группе, добавьте привязку там и отключите прежнюю. Старые сообщения остаются в прежней Telegram-группе.</p>
   </div>
  </>}
 </section>;
}

function RouteEditor({route,forum,busy,save}:{route:Route;forum:boolean;busy:boolean;save:(values:Record<string,unknown>)=>Promise<void>}){
 const [title,setTitle]=React.useState(route.label||'');
 const [thread,setThread]=React.useState(route.telegram_thread_id||'');
 React.useEffect(()=>{setTitle(route.label||'');setThread(route.telegram_thread_id||'');},[route.label,route.telegram_thread_id]);
 return <div><p>{route.max_chat_id} · {route.enabled?'включён':'отключён'}</p>
  <label>Название<input aria-label={`Название ${route.max_chat_id}`} value={title} onChange={e=>setTitle(e.target.value)}/></label>
  {forum&&<label>ID темы<input aria-label={`Тема ${route.max_chat_id}`} value={thread} onChange={e=>setThread(e.target.value)}/></label>}
  <button disabled={busy||(forum&&!thread)} onClick={()=>save({title,thread_id:thread||null})}>Сохранить привязку</button>
  <button disabled={busy} onClick={()=>save({enabled:!route.enabled})}>{route.enabled?'Отключить':'Включить'}</button>
 </div>;
}
