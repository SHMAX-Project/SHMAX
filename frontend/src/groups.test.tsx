import React from 'react';
import {it,expect,vi,beforeEach} from 'vitest';
import {render,screen,fireEvent,waitFor,cleanup} from '@testing-library/react';
import {MemoryRouter} from 'react-router-dom';
import '@testing-library/jest-dom/vitest';
import {GroupsPage} from './groups';
import {api} from './api';
vi.mock('./api',()=>({api:vi.fn()}));
const mock=vi.mocked(api);
const groups=[1,2].map(id=>({id,chat_id:`-${id}`,title:`Группа ${id}`,is_active:true,ready:true,is_forum:true,is_admin_hub:false,auto_add_chats:false,show_source_chat:true,show_sender:true}));
beforeEach(()=>{cleanup();mock.mockReset();mock.mockImplementation(async(path)=>{
 if(path==='/api/telegram/groups')return {groups};
 if(path==='/api/routes')return [{id:1,max_chat_id:'A',telegram_chat_id:'-1',telegram_thread_id:'11',label:'Чат A',enabled:true},{id:2,max_chat_id:'B',telegram_chat_id:'-2',telegram_thread_id:'22',label:'Чат B',enabled:true}];
 if(path==='/api/max/chats')return {chats:[]};
 return {ok:true};
});});
it('shows only selected group routes and saves settings for that group',async()=>{
 render(<MemoryRouter><GroupsPage/></MemoryRouter>);
 await screen.findByLabelText('Название A');
 expect(screen.queryByLabelText('Название B')).not.toBeInTheDocument();
 fireEvent.change(screen.getByLabelText('Telegram-группа'),{target:{value:'2'}});
 expect(screen.getByLabelText('Название B')).toBeInTheDocument();
 expect(screen.queryByLabelText('Название A')).not.toBeInTheDocument();
 fireEvent.click(screen.getByLabelText('Имя отправителя, когда оно доступно'));
 await waitFor(()=>expect(mock).toHaveBeenCalledWith('/api/telegram/groups/2',{method:'PATCH',body:JSON.stringify({show_sender:false})}));
});
it('edits the selected destination topic without changing other routes',async()=>{
 render(<MemoryRouter><GroupsPage/></MemoryRouter>);
 const topic=await screen.findByLabelText('Тема A');
 fireEvent.change(topic,{target:{value:'15'}});
 fireEvent.click(screen.getByRole('button',{name:'Сохранить привязку'}));
 await waitFor(()=>expect(mock).toHaveBeenCalledWith('/api/telegram/groups/1/routes/1',{method:'PATCH',body:JSON.stringify({title:'Чат A',thread_id:'15'})}));
});
it('keeps group settings available while MAX chat listing is unavailable',async()=>{
 const implementation=mock.getMockImplementation()!;
 mock.mockImplementation(async(path,options)=>{if(path==='/api/max/chats')throw new Error('offline');return implementation(path,options);});
 render(<MemoryRouter><GroupsPage/></MemoryRouter>);
 expect(await screen.findByLabelText('Название A')).toBeInTheDocument();
});
