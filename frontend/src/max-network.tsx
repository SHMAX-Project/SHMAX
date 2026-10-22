import React from 'react';
import {Link} from 'react-router-dom';
import {api} from './api';
import {useNavigationSafety} from './navigation';
import {StepIndicator} from './wizard';
import {WizardHelpCard} from './help';
import {PageHeader,Spinner} from './ui';
type Mode='direct'|'socks5'|'http'|'xray';
const modeLabels:Record<Mode,string>={direct:'Напрямую',socks5:'SOCKS5-прокси',http:'HTTP CONNECT-прокси',xray:'Xray'};
const xrayLabels:Record<string,string>={not_configured:'не настроен',running:'работает',stopped:'остановлен',starting:'запускается',ready:'готов',error:'ошибка',failed:'ошибка'};
type State={mode:Mode;host:string|null;port:number|null;username:string|null;password_saved:boolean;selected_node_id:string|null;nodes:Array<{node_id:string;name?:string;label?:string;host?:string;supported:boolean}>;xray:{state:string};last_probe:{ok:boolean;detail:string}|null};
export function MaxNetworkPage({onNext,onPrev}:{onNext?:()=>void;onPrev?:()=>void}={}){
 const [networkOk,setNetworkOk]=React.useState(false),[dirty,setDirty]=React.useState(false);
 const {markDirty,markSaved}=useNavigationSafety();
 function saved(){setDirty(false);markSaved();}
 const [state,setState]=React.useState<State|null>(null),[mode,setMode]=React.useState<Mode>('direct');
 const [host,setHost]=React.useState(''),[port,setPort]=React.useState(''),[username,setUsername]=React.useState(''),[password,setPassword]=React.useState('');
 const [url,setUrl]=React.useState(''),[node,setNode]=React.useState(''),[error,setError]=React.useState(''),[busy,setBusy]=React.useState(false),[result,setResult]=React.useState('');
 function accept(s:State){setState(s);setMode(s.mode);setHost(s.host||'');setPort(s.port?String(s.port):'');setUsername(s.username||'');setPassword('');setNode(s.selected_node_id||'');}
 React.useEffect(()=>{api<State>('/api/max/network').then(accept).catch(e=>setError(e.message));},[]);
 async function action(fn:()=>Promise<void>){setBusy(true);setError('');try{await fn();}catch(e:any){setError(e.message);}finally{setBusy(false);}}
 const proxy=mode==='socks5'||mode==='http';
 return <section onChangeCapture={()=>{setDirty(true);markDirty();setNetworkOk(false);}} className={onNext?'wizard-step settings-form':'settings-form'}>{onNext&&<><StepIndicator stepId="max-network"/><p className="eyebrow">Шаг 2 из 10</p></>}<PageHeader title="Подключение MAX">{!onNext&&<Link to="/max">Назад к MAX</Link>}</PageHeader>{onNext&&<WizardHelpCard stepId="max-network"/>}
 <p>Выберите путь к MAX независимо от Telegram. При смене сети SHMAX восстанавливает сохранённую сессию; код входа автоматически не запрашивается.</p>
 {error&&<div className="alert alert-error" role="alert">{error}</div>}
 {!state&&!error&&<Spinner label="Загрузка настроек MAX…"/>}
 {state&&<div className="card"><label>Режим подключения MAX<select aria-label="Режим подключения MAX" value={mode} disabled={busy} onChange={e=>{setMode(e.target.value as Mode);setNetworkOk(false);}}>
 <option value="direct">Напрямую</option><option value="socks5">SOCKS5-прокси</option><option value="http">HTTP CONNECT-прокси</option><option value="xray">Xray — подписка и узел</option></select></label>
 {proxy&&<><label>Хост прокси MAX<input value={host} onChange={e=>setHost(e.target.value)}/></label><label>Порт<input inputMode="numeric" value={port} onChange={e=>setPort(e.target.value)}/></label><label>Логин<input autoComplete="off" value={username} onChange={e=>setUsername(e.target.value)}/></label><label>Пароль<input type="password" autoComplete="new-password" value={password} placeholder={state.password_saved?'Сохранён; пусто — оставить для того же прокси':''} onChange={e=>setPassword(e.target.value)}/></label>{mode==='socks5'&&<p className="muted">Для передачи файлов через SOCKS5 используется локальный Xray. Убедитесь, что он установлен.</p>}</>}
 {mode==='xray'&&<><label>Подписка Xray для MAX<input type="password" autoComplete="new-password" value={url} onChange={e=>setUrl(e.target.value)}/></label>
 <button disabled={busy||!url.trim()} onClick={()=>action(async()=>{const s=await api<State>('/api/max/xray/subscription',{method:'POST',body:JSON.stringify({url:url.trim()})});setState(s);setUrl('');saved();})}>Загрузить подписку MAX</button>
 <label>Узел MAX<select aria-label="Узел MAX" value={node} onChange={e=>setNode(e.target.value)}><option value="">Выберите узел</option>{state.nodes.map(n=><option key={n.node_id} value={n.node_id} disabled={!n.supported}>{n.name||n.label||n.host||n.node_id}{!n.supported?' · не поддерживается':''}</option>)}</select></label>
 <button disabled={busy||!node} onClick={()=>action(async()=>{accept(await api<State>('/api/max/xray/select',{method:'POST',body:JSON.stringify({node_id:node})}));saved();setNetworkOk(true);setResult('Узел MAX применён и доступен.');})}>Применить узел MAX</button>
 <p>Если узел не проходит сетевую проверку MAX, сохраняется прежний выбор. Автоматического перехода на прямое подключение нет.</p></>}
 {mode!=='xray'&&<button disabled={busy||(proxy&&(!host||!port))} onClick={()=>action(async()=>{accept(await api<State>('/api/max/network',{method:'PUT',body:JSON.stringify({mode,host:proxy?host:null,port:proxy?Number(port):null,username:proxy?username:null,password:proxy?(password||null):null})}));saved();setNetworkOk(false);setResult('Настройки сохранены. Проверьте соединение перед входом в MAX.');})}>Сохранить подключение MAX</button>}
 <button disabled={busy} onClick={()=>action(async()=>{const r=await api<{ok:boolean;detail:string}>('/api/max/network/probe',{method:'POST'});setResult(r.detail);setNetworkOk(r.ok&&!dirty);if(!r.ok)setError(r.detail);})}>Проверить сохранённое подключение</button>
 <p>Сохранено: {modeLabels[state.mode]}{state.mode!=='direct'&&<> · Xray MAX: {xrayLabels[state.xray.state]||'состояние уточняется'}</>}</p>
 </div>}{result&&<p role="status">{result}</p>}
 {onNext&&<div className="wizard-actions"><button onClick={onPrev} disabled={busy}>Назад</button><button className="btn-primary" disabled={busy||dirty||!networkOk||mode!==state?.mode} onClick={onNext}>Далее: вход в MAX</button><p className="muted">Сначала сохраните выбранную сеть и проверьте подключение. Номер телефона на этом шаге не нужен.</p></div>}
 </section>;
}
