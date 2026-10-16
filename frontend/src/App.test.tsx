import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import type { DisplayDocument, PairperApi, Paper } from './types';
import { App } from './App';

const pending: Paper = { id:'paper',title:'Existing paper',filename:'paper.pdf',page_count:13,
  status:'registered',has_view:false,current_view_id:null,created_at:'2026-10-01',original_available:true };
const ready: Paper = {...pending,status:'complete',has_view:true,current_view_id:'view'};
const textContent = { runs:[{kind:'text' as const,prefix:'',text:'Source-faithful paper'}],items:[],cells:[],
  level:2,ordered:false,source_verified:true,image_url:null,latex:null,number:'',caption_id:null };
const doc: DisplayDocument = { display_schema_version:'1.0',source_schema_version:'1.0',
  paper_id:'paper',view_id:'view',normalization_audit:[],blocks:[
    {id:'title',type:'title',order:0,validation_status:'source_verified',content:textContent},
    {id:'heading',type:'heading',order:1,validation_status:'source_verified',content:{...textContent,runs:[{kind:'text',prefix:'',text:'Methods'}]}},
    {id:'paragraph',type:'paragraph',order:2,validation_status:'source_verified',content:textContent},
  ] };
function makeApi(papers: Paper[] = [pending]) {
  return {
    listPapers: vi.fn(async () => papers), getDocument: vi.fn(async () => doc),
    copyUpdateRequest: vi.fn(async (_paperId: string) => ({copied:true})),
    connectionStatus: vi.fn(async () => ({state:'connected',message:'연결됨',checked_at:null,last_healthy_at:null,healthy:true,ready:true,admin_url:null,chatgpt_verified:false})),
    importPdfs: vi.fn(async () => ({imported:1,failed:0})), openDiagnostics: vi.fn(async () => ({state:'connected',message:'연결됨',checked_at:null,last_healthy_at:null,healthy:true,ready:true,admin_url:null,chatgpt_verified:false})),
    autostartStatus: vi.fn(async () => ({enabled:true})), setAutostart: vi.fn(async (enabled: boolean) => ({enabled})),
    reconnectTunnel: vi.fn(async () => ({state:'reconnecting',message:'다시 연결 중',checked_at:null,last_healthy_at:null,healthy:false,ready:false,admin_url:null,chatgpt_verified:false})),
    onLibraryChanged: vi.fn((_listener: () => void) => () => {}),
  } satisfies PairperApi;
}

it('separates library and reader, expands pending papers without starting work, and imports PDFs', async () => {
  const api = makeApi();
  render(<App api={api} />);
  const row = await screen.findByRole('button',{name:/Existing paper/});
  expect(screen.queryByRole('main',{name:'논문 읽기'})).toBeNull();
  fireEvent.click(row);
  expect(screen.getByText('원본 PDF 등록됨',{exact:false})).toBeInTheDocument();
  expect(api.getDocument).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button',{name:'요청 문구 복사'}));
  await waitFor(() => expect(api.copyUpdateRequest).toHaveBeenCalledWith('paper'));
  fireEvent.click(screen.getByRole('button',{name:'PDF 추가'}));
  expect(await screen.findByText('1개 등록 확인 · 0개 실패')).toBeInTheDocument();
  api.listPapers.mockResolvedValue([ready]);
  api.onLibraryChanged.mock.calls[0][0]();
  await waitFor(() => expect(screen.getByText('읽기 가능')).toBeInTheDocument());
  fireEvent.click(screen.getByRole('button',{name:/Existing paper/}));
  expect(await screen.findByRole('article',{name:'재구성된 논문'})).toBeInTheDocument();
  expect(screen.queryByRole('heading',{name:'내 라이브러리'})).toBeNull();
  expect(screen.queryByRole('button',{name:'PDF 추가'})).toBeNull();
  expect(screen.queryByRole('button',{name:'다시 연결'})).toBeNull();
  fireEvent.click(screen.getByRole('button',{name:'라이브러리'}));
  expect(screen.getByRole('heading',{name:'내 라이브러리'})).toBeInTheDocument();
});

it('combines title/filename search, chronological/title ordering and status filters', async () => {
  const api = makeApi([ready,{...pending,id:'old',title:'Alpha',filename:'other.pdf',created_at:'2025'},
    {...pending,id:'review',title:'Review',filename:'paper-review.pdf',status:'review_required',created_at:'2027'}]);
  render(<App api={api} />);
  await screen.findByText('Alpha');
  const names = () => within(screen.getByLabelText('등록 논문')).getAllByRole('button').map(el => el.textContent);
  expect(names()[0]).toContain('Review');
  fireEvent.change(screen.getByLabelText('논문 정렬'),{target:{value:'oldest'}});
  expect(names()[0]).toContain('Alpha');
  fireEvent.change(screen.getByLabelText('논문 정렬'),{target:{value:'title'}});
  expect(names()[0]).toContain('Alpha');
  fireEvent.change(screen.getByLabelText('논문 검색'),{target:{value:'paper'}});
  expect(screen.queryByText('Alpha')).toBeNull();
  fireEvent.click(screen.getByRole('button',{name:'상태 필터'}));
  fireEvent.click(screen.getByRole('radio',{name:'읽기 가능'}));
  expect(names()).toHaveLength(1);
  expect(names()[0]).toContain('Existing paper');
  fireEvent.change(screen.getByLabelText('논문 검색'),{target:{value:'absent'}});
  expect(screen.getByText('검색 결과가 없습니다')).toBeInTheDocument();
});

it('retains autostart, diagnostics and reconnect in settings and shows failures', async () => {
  const api = makeApi();
  render(<App api={api} />);
  fireEvent.click(screen.getByRole('button',{name:'설정'}));
  const toggle = screen.getByRole('switch',{name:'로그인 시 자동 실행'});
  await waitFor(() => expect(toggle).toBeChecked());
  fireEvent.click(toggle);
  await waitFor(() => expect(toggle).not.toBeChecked());
  api.setAutostart.mockRejectedValueOnce(new Error('Denied'));
  fireEvent.click(toggle);
  expect(await screen.findByRole('alert')).toHaveTextContent('자동 실행 설정을 변경하지 못했습니다');
  expect(toggle).not.toBeChecked();
  fireEvent.click(screen.getByRole('button',{name:'다시 연결'}));
  expect(await screen.findByText('다시 연결 중')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button',{name:'연결 진단'}));
  expect(api.openDiagnostics).toHaveBeenCalledOnce();
});

it('keeps heading navigation and focus without source or manual font controls', async () => {
  localStorage.clear();
  const api = makeApi([ready]);
  const {container} = render(<App api={api} />);
  fireEvent.click(await screen.findByRole('button',{name:/Existing paper/}));
  await screen.findByRole('heading',{name:'Methods'});
  const toc = screen.getByRole('button',{name:'목차'});
  toc.focus(); fireEvent.click(toc);
  expect(screen.getByRole('navigation',{name:'논문 목차'})).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button',{name:'Methods'}));
  expect(screen.getByRole('button',{name:'Methods'})).toHaveAttribute('aria-current','location');
  fireEvent.click(screen.getByRole('button',{name:'목차 닫기'}));
  await waitFor(() => expect(toc).toHaveFocus());
  expect(screen.queryByRole('button',{name:'원본 확인'})).toBeNull();
  expect(screen.queryByRole('button',{name:'글자 설정'})).toBeNull();
  expect(screen.queryByText('원본 대조')).toBeNull();
  expect(screen.queryByText('원문 참조')).toBeNull();
  expect(container.querySelector('.source-selected')).toBeNull();
  expect(container.querySelector('.reader')).not.toHaveAttribute('style');
  expect(localStorage.getItem('pairper.ui.v1.fontSize')).toBeNull();
  expect(api.getDocument).toHaveBeenCalledOnce();
});
