import React from 'react';
import {it,expect,vi,beforeEach} from 'vitest';
import {render,screen,cleanup,fireEvent} from '@testing-library/react';
import {MemoryRouter} from 'react-router-dom';
import '@testing-library/jest-dom/vitest';
import {DashboardOverview} from './dashboard-overview';
import {api} from './api';
vi.mock('./api',()=>({api:vi.fn()}));
const mock=vi.mocked(api);
beforeEach(()=>{cleanup();mock.mockReset();});
it('shows resource charts and distinguishes validated groups from a live bot connection',async()=>{
 const samples=[100,105].map(at=>({at,cpu_percent:at===100?null:25,memory_percent:50,memory_used:1024**3,memory_total:2*1024**3,disk_percent:30,disk_used:3*1024**3,disk_total:10*1024**3,load_1:0.2,container_memory_used:null,container_memory_limit:null,container_cpu_percent:null}));
 mock.mockResolvedValue({resources:{samples,scope:'linux_host',interval_seconds:5,history_since:100},groups:[{id:1,chat_id:'-1',title:'Команда A',enabled:true,validated:true,admin_hub:false,auto_add_chats:false,routes:2,disabled_routes:0}],queue:{},max_network:{mode:'xray',xray:{state:'running'}}});
 const {container}=render(<MemoryRouter><DashboardOverview/></MemoryRouter>);
 expect(await screen.findByText('Команда A')).toBeInTheDocument();
 expect(screen.getByRole('img',{name:'CPU сервера: 25.0 процентов'})).toBeInTheDocument();
 expect(screen.getByText(/Текущее соединение Telegram показано выше/)).toBeInTheDocument();
 expect(screen.getByRole('link',{name:'Настроить подключение MAX'})).toHaveAttribute('href','/max/network');
 expect(container.innerHTML).not.toContain('NaN');
});

it('keeps sparse samples at their true positions when the time range changes', async () => {
 const last = 1795442400;
 const samples = [last - 300, last].map(at => ({
  at, cpu_percent: 25, memory_percent: 50, disk_percent: 30,
  memory_used: 1, memory_total: 2, disk_used: 3, disk_total: 10,
  load_1: 0.2, container_memory_used: null, container_memory_limit: null, container_cpu_percent: null
 }));
 mock.mockResolvedValue({
  resources: { samples, scope: 'linux_host', interval_seconds: 5, history_since: samples[0].at },
  groups: [], queue: {}, max_network: { mode: 'direct', xray: { state: 'not_configured' } }
 });
 render(<MemoryRouter><DashboardOverview/></MemoryRouter>);
 const chart = await screen.findByRole('img', {name: 'CPU сервера: 25.0 процентов'});
 const xs = () => chart.querySelector('polyline')!.getAttribute('points')!
  .split(' ').map(point => Number(point.split(',')[0]));
 const minutes = () => [...chart.querySelectorAll('text')].slice(-4).map(label => label.textContent);
 expect(xs()[0]).toBeCloseTo(10 + 280 * 600 / 900);
 expect(xs()[1]).toBeCloseTo(290);
 const fifteenMinutes = minutes();
 fireEvent.change(screen.getByLabelText('Период графиков'), {target: {value: '3600'}});
 expect(xs()[0]).toBeCloseTo(10 + 280 * 3300 / 3600);
 expect(xs()[1]).toBeCloseTo(290);
 expect(minutes()[0]).not.toBe(fifteenMinutes[0]);
 expect(minutes()[3]).toBe(fifteenMinutes[3]);
});
