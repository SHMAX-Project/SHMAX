import {test,expect} from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const here=path.dirname(fileURLToPath(import.meta.url));
test('all configured application screens fit desktop and mobile',async({page},info)=>{
 const state=JSON.parse(fs.readFileSync(path.join(here,'.e2e-state.json'),'utf8')).projects[info.project.name];
 await page.goto(state.backendURL+'/login');
 await page.locator('input[name="username"]').fill('owner');
 await page.locator('input[name="password"]').fill('owner-pass-12345');
 await page.getByRole('button',{name:'Войти',exact:true}).click();await page.waitForURL(/\/dashboard$/);
 const output=path.join(here,'.shots',info.project.name);fs.mkdirSync(output,{recursive:true});
 const pages=['dashboard','max','max/settings','max/network','telegram','telegram/settings','telegram/network','telegram/group','chats','chats/settings','users','diagnostics','logs','backups','updates','settings','help','about'];
 for(const route of pages){
  await page.goto(state.backendURL+'/'+route);await expect(page.getByTestId('app-shell')).toBeVisible();
  await expect(page.locator('.spinner')).toHaveCount(0);
  await expect.soft.poll(()=>page.evaluate(()=>document.documentElement.scrollWidth<=document.documentElement.clientWidth+1),{message:route+' must fit the viewport'}).toBe(true);
  await expect(page.getByRole('heading').first()).toBeVisible();
  await page.evaluate(()=>window.scrollTo(0,0));
  await page.screenshot({path:path.join(output,'audit-'+route.replaceAll('/','-')+'.png'),fullPage:true});
 }
});
