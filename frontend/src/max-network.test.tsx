import React from 'react';
import {it,expect,vi,beforeEach} from 'vitest';
import {render,screen,fireEvent,waitFor,cleanup} from '@testing-library/react';
import {MemoryRouter} from 'react-router-dom';
import '@testing-library/jest-dom/vitest';
import {MaxNetworkPage} from './max-network';
import {api} from './api';
vi.mock('./api',()=>({api:vi.fn()}));
const mock=vi.mocked(api);
const initial={mode:'direct',host:null,port:null,username:null,password_saved:false,nodes:[],selected_node_id:null,xray:{state:'not_configured'},last_probe:null};
beforeEach(()=>{cleanup();mock.mockReset();mock.mockImplementation(async(path)=>path==='/api/max/network/probe'?{ok:true,detail:'MAX доступен'}:initial);});
it('keeps onboarding next disabled until saved network passes a probe',async()=>{
 const next=vi.fn();render(<MemoryRouter><MaxNetworkPage onNext={next} onPrev={()=>{}}/></MemoryRouter>);
 await screen.findByLabelText('Режим подключения MAX');
 expect(screen.getByRole('button',{name:'Далее: вход в MAX'})).toBeDisabled();
 fireEvent.click(screen.getByRole('button',{name:'Проверить сохранённое подключение'}));
 await waitFor(()=>expect(screen.getByRole('button',{name:'Далее: вход в MAX'})).toBeEnabled());
 fireEvent.click(screen.getByRole('button',{name:'Далее: вход в MAX'}));expect(next).toHaveBeenCalledOnce();
 fireEvent.change(screen.getByLabelText('Режим подключения MAX'),{target:{value:'http'}});
 expect(screen.getByRole('button',{name:'Далее: вход в MAX'})).toBeDisabled();
 expect(mock.mock.calls.every(([url])=>!url.includes('/api/telegram'))).toBe(true);
});
it('does not expose a saved proxy password in the form',async()=>{
 mock.mockResolvedValue({...initial,mode:'http',host:'proxy.example',port:3128,username:'u',password_saved:true});
 render(<MemoryRouter><MaxNetworkPage/></MemoryRouter>);
 expect(await screen.findByLabelText('Пароль')).toHaveValue('');
 expect(screen.getByLabelText('Пароль')).toHaveAttribute('type','password');
});
