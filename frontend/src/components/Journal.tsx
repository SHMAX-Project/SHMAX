import React from 'react';
import { api } from '../api';
import { PageHeader } from '../ui';
type Entry={id:number;timestamp:string;first_timestamp:string;severity:string;category:string;event_type:string;description:string;component:string|null;correlation_id:string|null;direction:string|null;attempt:number|null;error_class:string|null;result:string|null;occurrences:number};
type Page={events:Entry[];next_cursor:string|null;storage:{bytes:number;max_bytes:number;max_records:number;retention_days:number;dropped:number;write_failures:number}};
export function Journal(){
 const [page,setPage]=React.useState<Page|null>(null),[error,setError]=React.useState(''),[severity,setSeverity]=React.useState(''),[category,setCategory]=React.useState(''),[search,setSearch]=React.useState(''),[query,setQuery]=React.useState(''),[since,setSince]=React.useState(''),[until,setUntil]=React.useState(''),[cursor,setCursor]=React.useState(''),[history,setHistory]=React.useState<string[]>([]),[zone,setZone]=React.useState(Intl.DateTimeFormat().resolvedOptions().timeZone),[busy,setBusy]=React.useState(false);
 const sequence=React.useRef(0),inflight=React.useRef<AbortController|null>(null);
 const refresh=React.useCallback(async()=>{
  const seq=++sequence.current;inflight.current?.abort();const controller=new AbortController();inflight.current=controller;setBusy(true);
  const params=new URLSearchParams({limit:'50'});if(severity)params.set('severity',severity);if(category)params.set('category',category);if(query)params.set('search',query);if(cursor)params.set('before',cursor);if(since)params.set('since',new Date(since).toISOString());if(until)params.set('until',new Date(until).toISOString());
  try{const data=await api<Page>('/api/journal?'+params,{signal:controller.signal});if(seq===sequence.current){setPage(data);setError('');}}catch(e){if(!controller.signal.aborted)setError(e instanceof Error?e.message:'Журнал недоступен');}finally{if(seq===sequence.current)setBusy(false);}
 },[severity,category,query,cursor,since,until]);
 React.useEffect(()=>{const timer=setTimeout(()=>{setQuery(search);setCursor('');setHistory([]);},350);return()=>clearTimeout(timer);},[search]);
 React.useEffect(()=>{refresh();const timer=setInterval(()=>{if(document.visibilityState==='visible'&&!cursor)refresh();},10000);return()=>{clearInterval(timer);sequence.current++;inflight.current?.abort();};},[refresh,cursor]);
 const reset=()=>{setCursor('');setHistory([]);};
 const format=(value:string)=>new Intl.DateTimeFormat('ru-RU',{timeZone:zone,dateStyle:'short',timeStyle:'medium'}).format(new Date(value));
 return <><PageHeader title="Журнал"><button className="btn" onClick={refresh} disabled={busy}>Обновить</button></PageHeader>
 <p className="muted">События обработки и соединений. Тексты сообщений и секреты не сохраняются.</p>
 <div className="panel journal-filters">
 <label>Уровень <select aria-label="Уровень" value={severity} onChange={e=>{setSeverity(e.target.value);reset();}}><option value="">Все события</option>{[['ERROR','Ошибки'],['WARNING','Предупреждения'],['SUCCESS','Успешные операции'],['INFO','Информация'],['DEBUG','Отладка']].map(([v,l])=><option key={v} value={v}>{l}</option>)}</select></label>
 <label>Категория <select aria-label="Категория" value={category} onChange={e=>{setCategory(e.target.value);reset();}}><option value="">Все категории</option>{['MAX','TELEGRAM','XRAY','BRIDGE','DELIVERY','SYSTEM','SECURITY','UPDATE'].map(c=><option key={c}>{c}</option>)}</select></label>
 <label>Поиск <input type="text" aria-label="Поиск" maxLength={128} value={search} onChange={e=>setSearch(e.target.value)} placeholder="Событие или correlation ID"/></label>
 <label>С <input aria-label="С" type="datetime-local" value={since} onChange={e=>{setSince(e.target.value);reset();}}/></label>
 <label>По <input aria-label="По" type="datetime-local" value={until} onChange={e=>{setUntil(e.target.value);reset();}}/></label>
 <label>Часовой пояс <select aria-label="Часовой пояс" value={zone} onChange={e=>setZone(e.target.value)}>{Array.from(new Set([Intl.DateTimeFormat().resolvedOptions().timeZone,'Europe/Moscow','UTC'])).map(z=><option key={z}>{z}</option>)}</select></label>
 </div>
 {error&&<div className="alert alert-error" role="alert">{error}</div>}
 {page&&<><p className="muted">Хранение: {(page.storage.bytes/1048576).toFixed(2)} / {(page.storage.max_bytes/1048576).toFixed(0)} MiB · {page.storage.retention_days} дней · до {page.storage.max_records.toLocaleString('ru-RU')} записей. {page.storage.dropped>0&&`Пропущено при перегрузке: ${page.storage.dropped}.`} {page.storage.write_failures>0&&`Ошибки записи журнала: ${page.storage.write_failures}.`}</p>
 <div style={{overflowX:'auto'}}><table><thead><tr><th>Время</th><th>Уровень</th><th>Категория</th><th>Событие</th></tr></thead><tbody>{page.events.map(row=><tr key={row.id}><td style={{whiteSpace:'nowrap'}}>{format(row.timestamp)}</td><td>{row.severity}</td><td>{row.category}<br/><small>{row.direction==='max_to_telegram'?'MAX → Telegram':row.direction==='telegram_to_max'?'Telegram → MAX':''}</small></td><td><details><summary>{row.description}{row.occurrences>1&&` · повторено ${row.occurrences} раз`}</summary><dl>{[['Тип',row.event_type],['Correlation ID',row.correlation_id],['Попытка',row.attempt],['Результат',row.result],['Класс ошибки',row.error_class],['Первое событие',format(row.first_timestamp)]].filter(([,v])=>v!==null).map(([k,v])=><React.Fragment key={String(k)}><dt>{k}</dt><dd>{v}</dd></React.Fragment>)}</dl></details></td></tr>)}</tbody></table></div>
 {!page.events.length&&<p>Событий по выбранным фильтрам нет.</p>}
 <div style={{display:'flex',gap:12,marginTop:16}}><button className="btn" disabled={!history.length} onClick={()=>{setCursor(history[history.length-1]);setHistory(history.slice(0,-1));}}>Назад</button><button className="btn" disabled={!page.next_cursor} onClick={()=>{setHistory([...history,cursor]);setCursor(page.next_cursor||'');}}>Следующая страница</button></div></>}
 </>;
}
