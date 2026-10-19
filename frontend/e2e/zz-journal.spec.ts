import {test,expect} from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const here=path.dirname(fileURLToPath(import.meta.url));
test('persistent journal from actual backend operations; refresh and filters',async({page,request},info)=>{
 const s=JSON.parse(fs.readFileSync(path.join(here,'.e2e-state.json'),'utf8')).projects[info.project.name];
 const state=await (await request.get(s.backendURL+'/api/auth/session')).json();
 if(!state.admin_exists){const created=await request.post(s.backendURL+'/api/setup/admin',{data:{token:s.setupToken,username:'owner',password:'owner-pass-12345'}});expect(created.ok()).toBe(true);}
 await page.goto(s.backendURL+'/login');await page.locator('input[name="username"]').fill('owner');await page.locator('input[name="password"]').fill('owner-pass-12345');await page.getByRole('button',{name:'Войти',exact:true}).click();await page.waitForURL(/\/dashboard$/);
 await page.goto(s.backendURL+'/logs');await expect(page.getByRole('heading',{name:'Журнал',exact:true})).toBeVisible();
 const response=await page.request.get(s.backendURL+'/api/journal?limit=50');expect(response.ok()).toBe(true);const baseline=await response.json();expect(baseline.events.length).toBeGreaterThan(0);
 const auto=page.waitForResponse(r=>r.url().includes('/api/journal?')&&r.ok(),{timeout:15000});
 await page.request.post(s.backendURL+'/api/auth/login',{data:{username:'owner',password:'owner-pass-12345'}});
 const updated=await (await auto).json();expect(updated.events[0].id).toBeGreaterThan(baseline.events[0].id);
 await page.getByLabel('Категория',{exact:true}).selectOption('SYSTEM');await expect(page.getByText('SHMAX запущен',{exact:true})).toBeVisible();
 await page.getByLabel('Поиск',{exact:true}).fill('system.started');await expect(page.locator('tbody tr')).toHaveCount(1);
 await page.getByRole('button',{name:'Обновить',exact:true}).click();await expect(page.locator('tbody tr')).toHaveCount(1);
 await page.reload();await page.getByLabel('Категория',{exact:true}).selectOption('SYSTEM');await expect(page.getByText('SHMAX запущен',{exact:true})).toBeVisible();
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
 const out=process.env.SHMAX_SCREENSHOT_DIR;if(out){fs.mkdirSync(out,{recursive:true});await page.screenshot({path:path.join(out,`journal-${info.project.name}.png`),fullPage:true});}
});
