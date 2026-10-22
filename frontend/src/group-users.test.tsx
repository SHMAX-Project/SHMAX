import React from 'react';
import {it,expect,vi,beforeEach} from 'vitest';
import {render,screen,fireEvent,waitFor,cleanup,act} from '@testing-library/react';
import {MemoryRouter} from 'react-router-dom';
import '@testing-library/jest-dom/vitest';
import {GroupUsersPage} from './group-users';
import {api} from './api';
vi.mock('./api',()=>({api:vi.fn()}));
const mock=vi.mocked(api);
beforeEach(()=>{cleanup();mock.mockReset();});
it('ignores a late user list from the previous group',async()=>{
 let finishA:(v:any)=>void=()=>{};
 mock.mockImplementation(async(url)=>{
  if(url==='/api/telegram/groups')return {groups:[{id:1,chat_id:'-1',title:'A',is_active:true},{id:2,chat_id:'-2',title:'B',is_active:true}]};
  if(url==='/api/telegram/groups/1/users')return await new Promise(resolve=>{finishA=resolve;});
  if(url==='/api/telegram/groups/2/users')return {users:[{user_id:'bob',display_name:'Bob in B',role:'read_only',approved:false,blocked:false,globally_blocked:false,membership:'member',is_admin:false}]};
  return {configured:false};
 });
 render(<MemoryRouter><GroupUsersPage/></MemoryRouter>);
 await waitFor(()=>expect(mock).toHaveBeenCalledWith('/api/telegram/groups/1/users'));
 fireEvent.change(screen.getByLabelText('Группа пользователя'),{target:{value:'2'}});
 await screen.findByText('Bob in B');
 await act(async()=>finishA({users:[{user_id:'alice',display_name:'Alice in A',role:'operator',approved:true}]}));
 expect(screen.queryByText('Alice in A')).not.toBeInTheDocument();
 expect(screen.getByText('Bob in B')).toBeInTheDocument();
 expect(screen.getByLabelText('Разрешён в этой группе')).not.toBeChecked();
});
