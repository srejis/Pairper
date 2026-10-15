import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import type { DisplayDocument, PairperApi, Paper } from './types';
import { App } from './App';

it('updates a pending paper when MCP publishes a view and imports through the desktop bridge', async () => {
  let changed = () => {};
  let papers: Paper[] = [{ id:'paper',title:'Existing paper',filename:'paper.pdf',page_count:13,
    status:'registered',has_view:false,current_view_id:null }];
  const doc: DisplayDocument = { display_schema_version:'1.0',source_schema_version:'1.0',
    paper_id:'paper',view_id:'view',normalization_audit:[],blocks:[{
      id:'title',type:'title',order:0,validation_status:'source_verified',source_trace:[],
      content:{ runs:[{kind:'text',prefix:'',text:'Source-faithful paper'}],items:[],cells:[],
        level:2,ordered:false,source_verified:true,image_url:null,latex:null,number:'',caption_id:null },
    }] };
  const api: PairperApi = {
    listPapers: vi.fn(async () => papers), getDocument: vi.fn(async () => doc),
    connectionStatus: vi.fn(async () => ({ state:'runtime_ready',message:'로컬 터널 런타임 준비 완료',
      checked_at:null,last_healthy_at:null,healthy:true,ready:true,admin_url:null,chatgpt_verified:false })),
    importPdfs: vi.fn(async () => ({imported:1,failed:0})),
    openDiagnostics: vi.fn(),
    onLibraryChanged: vi.fn(listener => { changed = listener; return () => {}; }),
  };
  render(<App api={api} />);
  expect(await screen.findByText('처리 대기')).toBeInTheDocument();
  papers = [{...papers[0],status:'complete',has_view:true,current_view_id:'view'}];
  changed();
  expect(await screen.findByRole('heading',{name:'Source-faithful paper'})).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button',{name:'+ PDF 추가'}));
  await waitFor(() => expect(api.importPdfs).toHaveBeenCalledTimes(1));
  expect(await screen.findByText('1개 등록 확인 · 0개 실패')).toBeInTheDocument();
});
