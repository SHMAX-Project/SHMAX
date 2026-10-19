import React from 'react';
import {it,expect,afterEach} from 'vitest';
import {render,screen,cleanup} from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import {MemoryRouter} from 'react-router-dom';
import './i18n';
import {Dashboard} from './pages';
import {maxSessionLabel} from './ui';
afterEach(cleanup);
it('dashboard explains unavailable verification and displays real recovery metadata',()=>{
 const status:any={version:'0.1.2',uptime_seconds:1,queue:{},database:'healthy',max:{configured:true,state:'VERIFICATION_UNAVAILABLE',authenticated:false,account_id:null,recovery:{state:'VERIFICATION_UNAVAILABLE',attempts:4,retry_in_seconds:8,last_success_at:1792405707}},telegram:{state:'HEALTHY'},bridge:{state:'DEGRADED',worker_running:true}};
 render(<MemoryRouter><Dashboard status={status}/></MemoryRouter>);
 expect(screen.getByText(/Проверка сессии временно недоступна из-за сети или API/)).toBeVisible();
 expect(screen.queryByText(/сессия не подтверждена/)).not.toBeInTheDocument();
 expect(screen.getByText(/Попытка восстановления: 4/)).toBeVisible();expect(screen.getByText(/Последнее успешное соединение:/)).toBeVisible();
});
it('only explicit auth rejection asks the owner to sign in',()=>{
 expect(maxSessionLabel({configured:true,state:'RECONNECTING'} as any)).toContain('автоматическое восстановление');
 expect(maxSessionLabel({configured:true,state:'AUTH_REQUIRED'} as any)).toContain('требуется вход');
 expect(maxSessionLabel({configured:true,state:'VERIFICATION_UNAVAILABLE'} as any)).not.toContain('требуется вход');
});
