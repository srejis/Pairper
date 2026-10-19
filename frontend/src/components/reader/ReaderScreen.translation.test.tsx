import { act, fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { ReaderScreen } from './ReaderScreen';
import type { DisplayDocument, PairperApi, Paper } from '../../types';

const paper: Paper = {id:'paper', title:'Paper', filename:'paper.pdf', page_count:1, status:'complete', has_view:true, current_view_id:'view'};
const source: DisplayDocument = {display_schema_version:'1.0',source_schema_version:'1.1',paper_id:'paper',view_id:'view',normalization_audit:[],blocks:[
  {id:'p',type:'paragraph',order:0,validation_status:'source_verified',content:{runs:[{kind:'text',prefix:'',text:'Source condition'}],items:[],cells:[],level:2,ordered:false,source_verified:true,image_url:null,latex:null,number:'',caption_id:null}},
]};
const translated: DisplayDocument = {...source,translation:{status:'draft',complete:false,missing_unit_count:1},blocks:[
  {...source.blocks[0],content:{...source.blocks[0].content,runs:[{kind:'text',prefix:'',text:'한국어 조건 <script>literal</script>'}]}},
]};
function setup(load: () => Promise<DisplayDocument> = async () => translated) {
  const api = {getDocument:vi.fn(async () => source),listTranslations:vi.fn(async () => [{session_id:'s',status:'draft',revision:1}]),getTranslationDocument:vi.fn(load)} as unknown as PairperApi;
  render(<ReaderScreen api={api} paper={paper} onBack={() => {}} />);
  return api;
}
it('opens source first, switches draft with warning and returns to the original', async () => {
  const api = setup();
  await screen.findByText('Source condition');
  expect(api.getTranslationDocument).not.toHaveBeenCalled();
  fireEvent.click(await screen.findByRole('button',{name:'한국어 초안'}));
  expect(await screen.findByText('한국어 조건 <script>literal</script>')).toBeInTheDocument();
  expect(screen.getByRole('status')).toHaveTextContent('원문 대조 미완료');
  expect(window.document.querySelector('article script')).toBeNull();
  fireEvent.click(screen.getByRole('button',{name:'원문'}));
  expect(await screen.findByText('Source condition')).toBeInTheDocument();
  expect(screen.queryByText('원문 대조 미완료',{exact:false})).toBeNull();
});
it('ignores a late translation response after source is selected', async () => {
  let finish!: (value: DisplayDocument) => void;
  setup(() => new Promise(resolve => {finish = resolve;}));
  fireEvent.click(await screen.findByRole('button',{name:'한국어 초안'}));
  fireEvent.click(screen.getByRole('button',{name:'원문'}));
  await act(async () => finish(translated));
  expect(screen.getByText('Source condition')).toBeInTheDocument();
  expect(screen.queryByRole('article',{name:'한국어 논문'})).toBeNull();
});
it('rejects a translation from another view and keeps source recovery available', async () => {
  setup(async () => ({...translated,view_id:'old-view'}));
  fireEvent.click(await screen.findByRole('button',{name:'한국어 초안'}));
  expect(await screen.findByRole('alert')).toHaveTextContent('원문으로 돌아가');
  fireEvent.click(screen.getByRole('button',{name:'원문'}));
  expect(await screen.findByText('Source condition')).toBeInTheDocument();
});
