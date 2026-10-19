import React from 'react';
import {afterEach,expect,it,vi} from 'vitest';
import {render,screen,waitFor,fireEvent,cleanup} from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import {Journal} from './components/Journal';
const page={events:[{id:1,timestamp:'2026-10-19T10:00:00Z',first_timestamp:'2026-10-19T10:00:00Z',severity:'SUCCESS',category:'MAX',description:'Соединение восстановлено',event_type:'max.reconnect_success',occurrences:1,correlation_id:'a'.repeat(24),direction:null,attempt:1,error_class:null,result:'success'}],next_cursor:'123:1',storage:{bytes:40960,max_bytes:134217728,max_records:100000,retention_days:90,dropped:0}};
afterEach(()=>{cleanup();vi.unstubAllGlobals();vi.useRealTimers();});
it('renders metadata, filters and server pagination without accumulating rows',async()=>{
 const fetch=vi.fn(async()=>({ok:true,json:async()=>page}));vi.stubGlobal('fetch',fetch);render(<Journal/>);
 await screen.findByText('Соединение восстановлено');fireEvent.change(screen.getByLabelText('Категория'),{target:{value:'MAX'}});
 await waitFor(()=>expect(fetch.mock.calls.some((call:any)=>String(call[0]).includes('category=MAX'))).toBe(true));
 fireEvent.click(screen.getByText('Следующая страница'));
 await waitFor(()=>expect(fetch.mock.calls.some((call:any)=>String(call[0]).includes('before=123%3A1'))).toBe(true));
 expect(screen.getAllByText('Соединение восстановлено')).toHaveLength(1);
});
it('refreshes only while mounted and aborts on unmount',async()=>{
 const fetch=vi.fn(async()=>({ok:true,json:async()=>page}));vi.stubGlobal('fetch',fetch);const view=render(<Journal/>);
 await screen.findByText('Соединение восстановлено');const n=fetch.mock.calls.length;fireEvent.click(screen.getByText('Обновить'));await waitFor(()=>expect(fetch.mock.calls.length).toBe(n+1));
 const signal=(fetch.mock.calls.at(-1) as any)[1].signal;view.unmount();expect(signal.aborted).toBe(true);
});
