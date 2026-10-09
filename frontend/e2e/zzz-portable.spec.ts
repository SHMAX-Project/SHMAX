import {test,expect} from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import net from 'node:net';
import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';
const here=path.dirname(fileURLToPath(import.meta.url));
const repo=path.resolve(here,'../..');

async function unusedPort():Promise<number>{
 const server=net.createServer();await new Promise<void>(resolve=>server.listen(0,'127.0.0.1',resolve));
 const port=(server.address() as net.AddressInfo).port;await new Promise<void>(resolve=>server.close(()=>resolve()));return port;
}

test('one encrypted file → fresh owner → restore → new login → restored dashboard',async({page,request},testInfo)=>{
 test.setTimeout(150000);
 const state=JSON.parse(fs.readFileSync(path.join(here,'.e2e-state.json'),'utf8'));
 const source=state.projects[testInfo.project.name];const sourcePid=state._backends[testInfo.project.name].pid;
 const work=fs.mkdtempSync(path.join(os.tmpdir(),'shmax-portable-e2e-'));
 const socket=path.join('/tmp',`shp-${Date.now()}.sock`);let host:ReturnType<typeof spawn>|null=null;
 try{
  await page.goto(source.backendURL+'/login');
  await page.locator('input[autocomplete="username"]').fill('owner');
  await page.locator('input[autocomplete="current-password"]').fill('owner-pass-12345');
  await page.getByRole('button',{name:'Войти',exact:true}).click();
  await page.waitForURL(/\/dashboard$/);
  await page.goto(source.backendURL+'/users');
  const row=page.locator('.user-row:has(#nickname-888001)');
  await row.getByLabel('Подпись в MAX').fill('Роман');
  const saved=page.waitForResponse(r=>r.url().endsWith('/888001/nickname')&&r.request().method()==='PATCH');
  await row.getByRole('button',{name:'Сохранить подпись'}).click();expect((await saved).status()).toBe(200);
  await page.goto(source.backendURL+'/backups');
  await page.getByLabel('Пароль резервной копии').fill('portable-password-123');
  await page.getByLabel('Повторите пароль').fill('portable-password-123');
  const downloadPromise=page.waitForEvent('download');
  await page.getByRole('button',{name:'Экспортировать для переноса'}).click();
  const downloaded=await downloadPromise;const archive=path.join(work,'migration.shmax');await downloaded.saveAs(archive);
  expect(fs.readFileSync(archive).subarray(0,6).toString()).toBe('SHMAXP');
  expect(fs.readFileSync(archive).includes(Buffer.from('Роман'))).toBe(false);
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  // No dual Telegram pollers: stop the source appliance before restoring.
  process.kill(sourcePid,'SIGTERM');
  await expect.poll(async()=>{try{return (await request.get(source.backendURL+'/health/live',{timeout:1000})).status();}catch{return 0;}}).toBe(0);
  await expect.poll(()=>{try{process.kill(sourcePid,0);return false;}catch{return true;}},{timeout:15000}).toBe(true);
  const port=await unusedPort();const destination=`http://127.0.0.1:${port}`;
  const backendDir=path.join(repo,'backend');
  host=spawn(path.join(backendDir,'.venv/bin/python'),[path.join(here,'portable-host.py'),'--data',path.join(work,'data'),'--port',String(port),'--socket',socket],{
    cwd:backendDir,stdio:'ignore',env:{...process.env,PYTHONPATH:backendDir,
      SHMAX_TELEGRAM_API_BASE:`${source.mockURL}/bot{token}/{method}`,
      SHMAX_TELEGRAM_REACHABILITY_URL:'http://127.0.0.1:59999',SHMAX_XRAY_BINARY:path.join(here,'stub-xray.py'),
      SHMAX_XRAY_SOCKS_PORT:String(source.xraySocksPort),SHMAX_E2E_FORWARD:source.mockURL.replace('http://',''),SHMAX_E2E_OTP_DELAY:'0',SHMAX_E2E_2FA:'1'}});
  await expect.poll(async()=>{try{return (await request.get(destination+'/health/ready',{timeout:1000})).status();}catch{return 0;}},{timeout:40000}).toBe(200);
  const token=(await (await request.post(destination+'/api/setup/token')).json()).token;
  await page.goto(`${destination}/setup?t=${token}`);
  await page.locator('input[autocomplete="username"]').fill('destination-owner');
  await page.locator('input[autocomplete="new-password"]').first().fill('destination-pass-12345');
  await page.locator('input[autocomplete="new-password"]').nth(1).fill('destination-pass-12345');
  await page.getByRole('button',{name:'Создать администратора'}).click();
  await page.waitForURL(/\/setup\/choice$/);
  if(testInfo.project.name.startsWith('desktop')) await page.screenshot({path:path.join(repo,'docs/assets/screenshots/initial-setup.png'),fullPage:true});
  await page.getByRole('link',{name:'Восстановить из резервной копии'}).click();
  if(testInfo.project.name.startsWith('desktop')) await page.screenshot({path:path.join(repo,'docs/assets/screenshots/restore.png'),fullPage:true});
  await page.getByLabel('Выберите файл .shmax').setInputFiles(archive);
  await page.getByLabel('Пароль резервной копии').fill('portable-password-123');
  await page.getByRole('button',{name:'Проверить резервную копию'}).click();
  await expect(page.getByText('Проверено. Данные ещё не изменены.')).toBeVisible();
  await expect(page.getByRole('button',{name:'Восстановить SHMAX'})).toBeDisabled();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  await page.getByRole('checkbox').check();
  await page.getByRole('button',{name:'Восстановить SHMAX'}).click();
  await expect.poll(async()=>{
    try{const response=await request.get(destination+'/api/auth/session',{timeout:1000});return response.ok()?(await response.json()).setup_complete:false;}catch{return false;}
  },{timeout:45000}).toBe(true);
  await page.goto(destination+'/login');
  await page.locator('input[autocomplete="username"]').fill('destination-owner');
  await page.locator('input[autocomplete="current-password"]').fill('destination-pass-12345');
  await page.getByRole('button',{name:'Войти',exact:true}).click();
  await page.waitForURL(/\/dashboard$/);
  await page.goto(destination+'/users');
  const restored=page.locator('.user-row:has(#nickname-888001)');
  await expect(restored.getByLabel('Подпись в MAX')).toHaveValue('Роман');
  await expect(restored.getByRole('checkbox').first()).not.toBeChecked();
  await expect(restored.getByRole('combobox')).toHaveValue('read_only');
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
 }finally{
  if(host && host.exitCode===null){host.kill('SIGTERM');await new Promise<void>(resolve=>host!.once('exit',()=>resolve()));}
  fs.rmSync(work,{recursive:true,force:true});fs.rmSync(socket,{force:true});fs.rmSync(path.join('/tmp','.'+path.basename(socket)+'.lock'),{force:true});
 }
});
