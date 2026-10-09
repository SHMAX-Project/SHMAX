import type { StatusSnapshot, IntegrationState } from '../api';
const labels:Record<IntegrationState,string>={NOT_CONFIGURED:'Не настроен',CONFIGURED:'Настроен, не проверен',CONNECTING:'Подключаем…',HEALTHY:'Работает',DEGRADED:'Деградация',DOWN:'Недоступен',AUTH_REQUIRED:'Требуется вход',ERROR:'Ошибка'};
export function StatusGrid({status}:{status:StatusSnapshot|null}) {
  return <div className="status-grid">{(['max','telegram','bridge'] as const).map(name=><div className="status-card" key={name}><div className="label">{name==='max'?'MAX':name==='telegram'?'Telegram':'Bridge'}</div><div className={`value ${status?.[name].state==='HEALTHY'?'ok':'muted'}`}>{status?labels[status[name].state]:'Проверяем…'}</div></div>)}<div className="status-card"><div className="label">Очередь</div><div className="value">{status?`${status.queue.pending+status.queue.retry+status.queue.processing} сообщений`:'…'}</div></div></div>;
}
