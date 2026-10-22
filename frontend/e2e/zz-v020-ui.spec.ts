import {test,expect} from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const here=path.dirname(fileURLToPath(import.meta.url));
test('v0.2.0 resource graphs and group/network controls fit desktop and mobile',async({page},info)=>{
 const states=JSON.parse(fs.readFileSync(path.join(here,'.e2e-state.json'),'utf8'));
 const backend=states.projects[info.project.name].backendURL;
 const groups=[1,2,3].map(id=>({id,chat_id:`-100100${id}`,title:id===3?'Администраторы':`Команда ${id===1?'А':'Б'}`,is_active:true,enabled:true,ready:true,validated:true,is_forum:true,is_admin_hub:id===3,admin_hub:id===3,auto_add_chats:id===3,show_source_chat:true,show_sender:true,routes:id===3?2:1,disabled_routes:0}));
 const routes=[1,2].map(id=>({id,max_chat_id:`max-${id}`,telegram_chat_id:groups[id-1].chat_id,telegram_thread_id:`${id}1`,label:`Клиенты команды ${id}`,enabled:true}));
 const network={mode:'xray',nodes:[{node_id:'demo-max',name:'Россия — MAX',supported:true}],selected_node_id:'demo-max',xray:{state:'running'},password_saved:false};
 const now=Math.floor(Date.now()/1000);
 const samples=Array.from({length:120},(_,i)=>({at:now-600+i*5,cpu_percent:24+10*Math.sin(i/9),memory_percent:43+i/30,memory_used:3.6*1024**3,memory_total:8*1024**3,disk_percent:31,disk_used:15.5*1024**3,disk_total:50*1024**3,load_1:0.42,container_memory_used:310*1024**2,container_memory_limit:2*1024**3,container_cpu_percent:8+3*Math.cos(i/7)}));
 const writes:string[]=[];
 await page.route('**/api/**',async route=>{
  const req=route.request(),key=new URL(req.url()).pathname;let body:any={ok:true};
  if(req.method()!=='GET'){writes.push(key);if(key==='/api/telegram/groups/2')Object.assign(groups[1],req.postDataJSON());}
  if(key==='/api/auth/session')body={authenticated:true,admin_exists:true,setup_complete:true,username:'demo-owner',csrf_token:'demo-csrf',mode:'AUTHENTICATED'};
  if(key==='/api/status')body={version:'0.2.0',uptime_seconds:1200,max:{state:'HEALTHY',configured:true,authenticated:true,connected:true,account_id:'demo-max'},telegram:{state:'HEALTHY',configured:true,bot_id:'demo-bot'},bridge:{state:'HEALTHY',worker_running:true},database:'ok',queue:{delivered:219,pending:2,failed:0}};
  if(key==='/api/dashboard')body={groups,resources:{scope:'linux_host',interval_seconds:5,history_since:samples[0].at,samples},max_network:network,telegram_network:{mode:'bot_api_socks5',xray:{state:'running'}}};
  if(key==='/api/telegram/groups')body={groups};if(key==='/api/routes')body=routes;
  if(key==='/api/max/chats')body={chats:[]};if(key==='/api/max/network')body=network;
  if(key==='/api/recent-activity')body={items:[]};
  await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});
 });
 const shots=path.join(here,'.shots',info.project.name);fs.mkdirSync(shots,{recursive:true});
 async function shot(name:string){
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=document.documentElement.clientWidth+1)).toBe(true);
  await page.evaluate(()=>{document.getElementById('demo-label')?.remove();const e=document.createElement('div');e.id='demo-label';e.textContent='Демонстрационные данные';Object.assign(e.style,{position:'fixed',bottom:'8px',right:'8px',background:'#101827',color:'#b8c7dd',padding:'6px 10px',border:'1px solid #40506a',fontSize:'12px',zIndex:'10000'});document.body.appendChild(e);});
  await page.evaluate(()=>window.scrollTo(0,0));
  await expect.poll(()=>page.evaluate(()=>window.scrollY)).toBe(0);
  await page.screenshot({path:path.join(shots,name+'.png'),fullPage:true});
 }
 await page.goto(backend+'/dashboard');await expect(page.getByRole('img',{name:/CPU сервера:/})).toBeVisible();
 await expect(page.getByRole('cell',{name:'Администраторы'})).toBeVisible();await shot('v020-dashboard');
 await page.goto(backend+'/telegram/group');await page.getByLabel('Telegram-группа').selectOption('2');
 await expect(page.getByLabel('Название max-2')).toBeVisible();await expect(page.getByLabel('Название max-1')).toHaveCount(0);
 await page.getByLabel('Имя отправителя, когда оно доступно').click();await expect.poll(()=>writes.includes('/api/telegram/groups/2')).toBe(true);
 await expect(page.getByLabel('Имя отправителя, когда оно доступно')).not.toBeChecked();
 expect(writes.includes('/api/telegram/groups/1')).toBe(false);await shot('v020-groups');
 await page.goto(backend+'/max/network');await expect(page.getByLabel('Узел MAX')).toHaveValue('demo-max');await shot('v020-max-network');
});
