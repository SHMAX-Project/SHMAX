import {test,expect} from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
// Actual production SPA / real backend. External services are the E2E mocks.
// No DOM replacement, no intercepted internal APIs, no production credentials.
const here=path.dirname(fileURLToPath(import.meta.url));
const out=path.resolve(here,'../../docs/assets/screenshots');
test('release demo screenshots and visible version',async({page},info)=>{
 const s=JSON.parse(fs.readFileSync(path.join(here,'.e2e-state.json'),'utf8')).projects[info.project.name];
 await page.goto(s.backendURL+'/login');
 await page.locator('input[name="username"]').fill('owner');
 await page.locator('input[name="password"]').fill('owner-pass-12345');
 await page.getByRole('button',{name:'Войти',exact:true}).click();
 await page.waitForURL(/\/dashboard$/);
 fs.mkdirSync(out,{recursive:true});
 const mobile=info.project.name.startsWith('mobile');
 const routes=mobile?[['dashboard','mobile-dashboard']]:[['dashboard','dashboard-desktop'],['max','max-settings'],['telegram','telegram-settings'],['chats','chat-routing'],['users','users'],['backups','backups'],['diagnostics','diagnostics']];
 for(const [route,name] of routes){
  await page.goto(s.backendURL+'/'+route);
  await expect(page.getByTestId('app-shell')).toBeVisible();
  await page.waitForTimeout(route==='diagnostics'?2500:800);
  await expect(page.locator('.spinner')).toHaveCount(0);
  await page.screenshot({path:path.join(out,name+'.png'),fullPage:!mobile});
 }
 await page.goto(s.backendURL+'/about');
 await expect(page.getByText('0.1.1',{exact:true})).toBeVisible();
 await expect(page.getByText('0.1.0-dev',{exact:true})).toHaveCount(0);
});
