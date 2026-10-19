import React from 'react';
import {it,expect,vi,afterEach} from 'vitest';
import {render,screen,fireEvent,waitFor,cleanup} from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import {MemoryRouter} from 'react-router-dom';
import './i18n';
vi.mock('./api',async()=>({...await vi.importActual<any>('./api'),
 telegramTransportGet:async()=>({mode:'bot_api_socks5'}),
 xrayStatus:async()=>({state:'running',selected_node_id:'old',nodes:[{id:'old',name:'Previous',supported:true,protocol:'trojan',security:'tls'},{id:'new',name:'Candidate',supported:true,protocol:'trojan',security:'tls'}]}),
 xraySelect:async()=>({ok:false,reachable:true,rolled_back:true,detail:'Новый узел не принят'})
}));
import {StepTelegramNetwork} from './wizard';
afterEach(cleanup);
it('does not show adapter failure as successful switch and restores saved selection',async()=>{
 render(<MemoryRouter><StepTelegramNetwork onNext={()=>{}} onPrev={()=>{}}/></MemoryRouter>);
 const select=await screen.findByTestId('xray-node-select');fireEvent.change(select,{target:{value:'new'}});fireEvent.click(screen.getByTestId('xray-select'));
 await screen.findByText(/Предыдущий узел восстановлен/);await waitFor(()=>expect(select).toHaveValue('old'));
 expect(screen.getByText('Новый узел не принят')).toBeVisible();expect(screen.queryByText(/Cached node active/)).not.toBeInTheDocument();
});
