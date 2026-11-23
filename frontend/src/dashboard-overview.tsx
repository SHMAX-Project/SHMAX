import React from 'react';
import {Link} from 'react-router-dom';
import {api} from './api';
type Sample={at:number;cpu_percent:number|null;memory_percent:number|null;memory_used:number|null;memory_total:number|null;disk_percent:number|null;disk_used:number|null;disk_total:number|null;load_1:number|null;container_memory_used:number|null;container_memory_limit:number|null;container_cpu_percent:number|null};
type Overview={resources:{samples:Sample[];scope:string;interval_seconds:number;history_since:number|null};groups:Array<{id:number;chat_id:string;title:string|null;enabled:boolean;validated:boolean;admin_hub:boolean;auto_add_chats:boolean;routes:number;disabled_routes:number}>;max_network:{mode:string;xray:{state:string}};telegram_network?:{mode:string;xray:{state:string}};queue:Record<string,number>};
const connectionLabels: Record<string, string> = {
  direct: 'Напрямую', bot_api_direct: 'Напрямую', mtproto_direct: 'MTProto напрямую',
  socks5: 'SOCKS5', bot_api_socks5: 'SOCKS5', mtproto_socks: 'MTProto через SOCKS5',
  http: 'HTTP-прокси', bot_api_http: 'HTTP-прокси', mtproto_proxy: 'MTProto-прокси',
  xray: 'Xray', xray_subscription: 'Подписка Xray', auto: 'Автоматически',
  not_configured: 'Не настроена',
};
const xrayLabels: Record<string, string> = {
  running: 'работает', failed: 'ошибка', stopped: 'остановлен', not_configured: 'не настроен',
};
function networkSummary(network?: Overview['max_network']) {
  if (!network) return 'Не настроена';
  return `${connectionLabels[network.mode] ?? 'Режим не определён'} · Xray: ${xrayLabels[network.xray.state] ?? 'состояние неизвестно'}`;
}
function bytes(value:number|null|undefined){if(value==null)return '—';return `${(value/1024**3).toFixed(2)} ГБ`;}
type TimeRange = { start: number; end: number };
const chartTime = new Intl.DateTimeFormat('ru-RU', { hour: '2-digit', minute: '2-digit' });

function Chart({ title, points, range, field, detail, unavailable = false }: {
  title: string;
  points: Sample[];
  range: TimeRange;
  field: 'cpu_percent' | 'memory_percent' | 'disk_percent' | 'container_cpu_percent';
  detail: string;
  unavailable?: boolean;
}) {
  const available = points.filter(point => point[field] != null);
  const last = available.at(-1)?.[field];
  const duration = range.end - range.start;
  const lines: string[] = [];
  let current: string[] = [];
  for (const point of points) {
    const value = point[field];
    if (value == null) {
      if (current.length) lines.push(current.join(' '));
      current = [];
      continue;
    }
    const x = 10 + 280 * (point.at - range.start) / duration;
    const y = 120 - Math.max(0, Math.min(100, value));
    current.push(`${x},${y}`);
  }
  if (current.length) lines.push(current.join(' '));

  return (
    <div className="card">
      <h3>{title} <span className="muted">{last == null ? '—' : `${last.toFixed(1)}%`}</span></h3>
      <svg
        role="img" aria-label={`${title}: ${last == null ? 'нет данных' : `${last.toFixed(1)} процентов`}`}
        viewBox="0 0 300 145" className="resource-chart"
      >
        {[0, 50, 100].map(value => (
          <g key={value}>
            <line x1="10" x2="290" y1={120 - value} y2={120 - value} stroke="currentColor" opacity="0.12" />
            <text x="12" y={116 - value} fill="currentColor" fontSize="10" opacity="0.6">{value}%</text>
          </g>
        ))}
        {lines.map((line, index) => (
          <polyline key={index} points={line} fill="none" stroke="#5195ff" strokeWidth="2.5" />
        ))}
        {[0, 1 / 3, 2 / 3, 1].map(fraction => (
          <text key={fraction} x={10 + 280 * fraction} y="140"
            textAnchor={fraction === 0 ? 'start' : fraction === 1 ? 'end' : 'middle'}
            fill="currentColor" fontSize="11">
            {chartTime.format((range.start + duration * fraction) * 1000)}
          </text>
        ))}
      </svg>
      <p className="muted small">{detail}</p>
      {unavailable ? <p className="muted">Измерение недоступно в этой среде.</p>
        : available.length < 2 && <p className="muted">График появится по мере накопления измерений.</p>}
    </div>
  );
}
export function DashboardOverview(){
 const [data,setData]=React.useState<Overview|null>(null),[error,setError]=React.useState(''),[periodSeconds,setPeriodSeconds]=React.useState(900);
 React.useEffect(()=>{let alive=true,running=false;async function load(){if(running)return;running=true;try{const value=await api<Overview>('/api/dashboard');if(alive){setData(value);setError('');}}catch(e:any){if(alive)setError(e.message);}finally{running=false;}}load();const timer=globalThis.setInterval(load,5000);return()=>{alive=false;clearInterval(timer);};},[]);
 if(error&&!data)return <div className="alert alert-warning" role="status">Статистика недоступна: {error}</div>;
 if(!data?.resources)return null;
 const samples = data.resources.samples;
 const latest = samples.at(-1);
 const end = latest?.at ?? Date.now() / 1000;
 const range = { start: end - periodSeconds, end };
 const points = samples.filter(sample => sample.at >= range.start && sample.at <= range.end);
 return <div className="resource-overview">
 <div className="resource-header">
  <h2>Ресурсы и нагрузка</h2>
  <label className="resource-period">
   <span>Период графиков</span>
   <select value={periodSeconds} onChange={event => setPeriodSeconds(Number(event.target.value))}>
    <option value="900">15 минут</option>
    <option value="3600">1 час</option>
   </select>
  </label>
 </div>
 <p className="muted">Измерение каждые {data.resources.interval_seconds} с. История хранится в памяти с запуска SHMAX. {data.resources.scope==='linux_host'?'CPU и память — сервер Linux; диск — файловая система данных SHMAX.':'Показатели CPU и памяти сервера недоступны в этой среде.'}</p>
 {error&&<p role="status" className="alert alert-warning">Обновление недоступно; показаны последние данные.</p>}
 <div className="grid"><Chart title="CPU сервера" points={points} range={range} field="cpu_percent" unavailable={data.resources.scope!=='linux_host'} detail={`Средняя нагрузка за минуту: ${latest?.load_1?.toFixed(2)||'—'}`}/>
 <Chart title="Память сервера" points={points} range={range} field="memory_percent" unavailable={data.resources.scope!=='linux_host'} detail={`${bytes(latest?.memory_used)} / ${bytes(latest?.memory_total)}`}/>
 <Chart title="Диск данных" points={points} range={range} field="disk_percent" detail={`${bytes(latest?.disk_used)} / ${bytes(latest?.disk_total)}`}/>
 {latest?.container_memory_used!=null&&<Chart title="CPU контейнера SHMAX" points={points} range={range} field="container_cpu_percent" detail={`Память контейнера: ${bytes(latest.container_memory_used)}; лимит: ${latest.container_memory_limit==null?'не задан':bytes(latest.container_memory_limit)}`}/>}</div>
 <div className="card"><h2>Подключённые группы</h2><p>Всего: {data.groups.length} · включено: {data.groups.filter(g=>g.enabled).length} · проверено: {data.groups.filter(g=>g.validated).length}</p>
 <p className="muted">Проверка группы подтверждает настройки и права бота. Текущее соединение Telegram показано выше.</p>
 {data.groups.length?<div style={{overflowX:'auto'}}><table><thead><tr><th>Группа</th><th>Состояние</th><th>Чаты MAX</th><th>Режим</th></tr></thead><tbody>{data.groups.map(g=><tr key={g.id}><td>{g.title||g.chat_id}<br/><span className="muted small">{g.chat_id}</span></td><td>{!g.enabled?'Отключена':g.validated?'Проверена':'Требует проверки'}</td><td>{g.routes}{g.disabled_routes?` · отключено ${g.disabled_routes}`:''}</td><td>{g.admin_hub?`Админский обзор${g.auto_add_chats?' · автодобавление':''}`:'Привязанные чаты'}</td></tr>)}</tbody></table></div>:<p>Группы ещё не подключены.</p>}
 <div className="row"><Link to="/telegram/group">Настроить группы и привязки</Link><Link to="/users">Права пользователей</Link></div></div>
 <div className="grid"><div className="card"><h2>Сеть MAX</h2><p>{networkSummary(data.max_network)}</p><Link to="/max/network">Настроить подключение MAX</Link></div><div className="card"><h2>Сеть Telegram</h2><p>{networkSummary(data.telegram_network)}</p><Link to="/telegram/network">Настроить подключение Telegram</Link></div></div>
 </div>;
}
