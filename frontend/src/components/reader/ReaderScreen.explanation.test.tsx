import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { BlockContent, DisplayDocument, ExplanationBundle, ExplanationTarget, PairperApi, Paper } from '../../types';
import { DocumentBlocks } from '../blocks';
import { ExplanationContext } from './ExplanationContext';
import { ReaderScreen } from './ReaderScreen';
const content: BlockContent = {runs:[],items:[],cells:[],level:2,ordered:false,source_verified:true,image_url:null,latex:null,number:'',caption_id:null};
const target = (patch: Partial<ExplanationTarget> = {}): ExplanationTarget => ({id:'term',kind:'term',block_id:'p',field:'runs',run_index:0,start:2,end:5,source_text:'MLP',display:false,...patch});
const formula = {kind:'math' as const,prefix:' ',latex:'x+y',source_verified:true,image_url:''};
const identity = {source_pdf_sha256:'pdf',view_id:'view',view_sha256:'viewhash',source_snapshot_sha256:'snapshot'};
const source: DisplayDocument = {source_identity:identity,display_schema_version:'1.0',source_schema_version:'1.1',paper_id:'paper',view_id:'view',normalization_audit:[],blocks:[
  {id:'p',type:'paragraph',order:0,validation_status:'source_verified',content:{...content,runs:[{kind:'text',prefix:'',text:'🧪 MLP processing'},formula]}},
  {id:'e',type:'equation',order:1,validation_status:'source_verified',content:{...content,latex:'x+y'}},
]};
const mathTarget = target({id:'math',kind:'math',run_index:1,start:null,end:null,source_text:'x+y'});
const bundle: ExplanationBundle = {paper_id:'paper',source_identity:identity,fingerprint:'fingerprint',revision:2,status:'published',targets:[target(),mathTarget,target({...mathTarget,id:'display',block_id:'e',field:'latex',run_index:null,display:true})],explanations:[
  {target_id:'term',kind:'term',category:'신경망',description:[{kind:'text',text:'입력 숫자를 계산하는 신경망이다.'}]},
  ...['math','display'].map(target_id => ({target_id,kind:'math' as const,symbols:[{symbol:'x',meaning:'입력값'}],meaning:[{kind:'text' as const,text:'두 입력값 '},{kind:'math' as const,latex:'x+y'},{kind:'text' as const,text:'를 더한다.'}],process:[{kind:'text' as const,text:'두 값을 합한다.'}]})),
]};
const paper: Paper = {id:'paper',title:'Paper',filename:'paper.pdf',page_count:1,status:'complete',has_view:true,current_view_id:'view'};

it('starts a different explanation at the top without closing or moving the paper', async () => {
  const api = {getDocument:vi.fn(async()=>source),getExplanations:vi.fn(async()=>bundle),onLibraryChanged:vi.fn(()=>()=>{})} as unknown as PairperApi;
  render(<ReaderScreen api={api} paper={paper} onBack={()=>{}} />);
  await screen.findByRole('button',{name:'MLP 해설'});
  fireEvent.click(screen.getAllByRole('button',{name:'수식 해설'})[0]);
  const panel = screen.getByRole('complementary',{name:'해설'});
  const reader = screen.getByRole('main');
  panel.scrollTop = 420;
  reader.scrollTop = 95;
  fireEvent.click(screen.getAllByRole('button',{name:'수식 해설'}).at(-1)!);
  expect(panel.scrollTop).toBe(0);
  expect(reader.scrollTop).toBe(95);
  expect(within(panel).getByRole('heading',{name:'해설'})).toHaveFocus();
});

describe('source-exact explanation anchors', () => {
  it('uses Unicode codepoints and preserves all original text and KaTeX', () => {
    const select = vi.fn();
    const {container} = render(<ExplanationContext.Provider value={{targets:bundle.targets,selected:null,onSelect:select}}><DocumentBlocks blocks={source.blocks} /></ExplanationContext.Provider>);
    expect(container.querySelector('p')?.textContent).toContain('🧪 MLP processing');
    fireEvent.click(screen.getByRole('button',{name:'MLP 해설'}));
    expect(select.mock.calls[0][0].id).toBe('term');
    expect(container.querySelectorAll('.katex')).toHaveLength(2);
    expect(container.querySelector('button button')).toBeNull();
    expect(screen.getAllByRole('button',{name:'수식 해설'})).toHaveLength(3);
  });
  it('rejects stale offsets instead of wrapping unrelated text', () => {
    render(<ExplanationContext.Provider value={{targets:[target({start:1,end:4})],selected:null,onSelect:vi.fn()}}><DocumentBlocks blocks={source.blocks} /></ExplanationContext.Provider>);
    expect(screen.queryByRole('button',{name:'MLP 해설'})).toBeNull();
    expect(screen.getByText('🧪 MLP processing')).toBeInTheDocument();
  });
  it('finds inline formulas in lists, captions and original table cell positions', () => {
    const blocks = [
      {id:'list',type:'list' as const,order:0,validation_status:'source_verified',content:{...content,items:[[formula]]}},
      {id:'caption',type:'caption' as const,order:1,validation_status:'source_verified',content:{...content,runs:[formula]}},
      {id:'table',type:'table' as const,order:2,validation_status:'source_verified',content:{...content,cells:[
        {row:1,column:0,rowspan:1,colspan:1,header:false,runs:[formula]},
        {row:0,column:0,rowspan:1,colspan:1,header:true,runs:[{kind:'text' as const,prefix:'',text:'Header'}]},
      ]}},
    ];
    const targets = [['list','items/0'],['caption','runs'],['table','cells/0']].map(([block_id,field],index)=>target({...mathTarget,id:`m${index}`,block_id,field,run_index:0}));
    render(<ExplanationContext.Provider value={{targets,selected:null,onSelect:vi.fn()}}><DocumentBlocks blocks={blocks} /></ExplanationContext.Provider>);
    expect(screen.getAllByRole('button',{name:'수식 해설'})).toHaveLength(3);
    expect(within(screen.getByRole('cell')).getByRole('button')).toBeInTheDocument();
  });
});
it('opens saved explanation, closes with Escape, preserves reading scroll and clears selection across languages', async () => {
  const api = {getDocument:vi.fn(async()=>source),getExplanations:vi.fn(async()=>bundle),onLibraryChanged:vi.fn(()=>()=>{}),listTranslations:vi.fn(async()=>[{session_id:'s',status:'published',revision:1}]),getTranslationDocument:vi.fn(async()=>({...source,translation:{status:'published',complete:true,missing_unit_count:0},blocks:[{...source.blocks[0],content:{...content,runs:[{kind:'text',prefix:'',text:'번역된 문장'}]}}]}))} as unknown as PairperApi;
  render(<ReaderScreen api={api} paper={paper} onBack={()=>{}} />);
  const trigger = await screen.findByRole('button',{name:'MLP 해설'});
  const reader = screen.getByRole('main');reader.scrollTop = 210;
  fireEvent.click(trigger);
  expect(screen.getByText('입력 숫자를 계산하는 신경망이다.')).toBeInTheDocument();
  expect(trigger).toHaveAttribute('aria-expanded','true');
  expect(reader.scrollTop).toBe(210);
  fireEvent.keyDown(window,{key:'Escape'});
  expect(trigger).toHaveFocus();
  fireEvent.click(trigger);
  fireEvent.click(screen.getByRole('button',{name:'한국어'}));
  await screen.findByText('번역된 문장');
  expect(screen.queryByRole('button',{name:'MLP 해설'})).toBeNull();
  expect(screen.queryByText('입력 숫자를 계산하는 신경망이다.')).toBeNull();
});
it('refreshes after published explanations change without reopening or scrolling the paper', async () => {
  let listener = ()=>{};
  let saved: ExplanationBundle = {...bundle,status:'empty',targets:[],explanations:[]};
  const api = {getDocument:vi.fn(async()=>source),getExplanations:vi.fn(async()=>saved),onLibraryChanged:vi.fn((fn:()=>void)=>{listener=fn;return ()=>{};})} as unknown as PairperApi;
  render(<ReaderScreen api={api} paper={paper} onBack={()=>{}} />);
  await screen.findByText('🧪 MLP processing');
  expect(screen.queryByRole('button',{name:'MLP 해설'})).toBeNull();
  const reader = screen.getByRole('main');reader.scrollTop=95;
  saved=bundle;
  await act(async()=>listener());
  expect(await screen.findByRole('button',{name:'MLP 해설'})).toBeInTheDocument();
  expect(reader.scrollTop).toBe(95);
  expect(api.getDocument).toHaveBeenCalledTimes(1);
});


it('hides explanation markers without altering source content and restores them on demand', async () => {
  const api = {getDocument:vi.fn(async()=>source),getExplanations:vi.fn(async()=>bundle),onLibraryChanged:vi.fn(()=>()=>{})} as unknown as PairperApi;
  render(<ReaderScreen api={api} paper={paper} onBack={()=>{}} />);
  const term = await screen.findByRole('button',{name:'MLP 해설'});
  fireEvent.click(term);
  const toggle=screen.getByRole('button',{name:'해설 표시'});
  fireEvent.click(toggle);
  expect(toggle).toHaveAttribute('aria-pressed','false');
  expect(screen.queryByRole('button',{name:'MLP 해설'})).toBeNull();
  expect(screen.getByText('🧪 MLP processing')).toBeInTheDocument();
  expect(screen.queryByText('입력 숫자를 계산하는 신경망이다.')).toBeNull();
  fireEvent.click(toggle);
  expect(screen.getByRole('button',{name:'MLP 해설'})).toBeInTheDocument();
});
it('rejects explanations from a changed source with the same view ID', async () => {
  const api={getDocument:vi.fn(async()=>source),getExplanations:vi.fn(async()=>({...bundle,source_identity:{...identity,view_sha256:'changed'}})),onLibraryChanged:vi.fn(()=>()=>{})} as unknown as PairperApi;
  render(<ReaderScreen api={api} paper={paper} onBack={()=>{}} />);
  await screen.findByText('🧪 MLP processing');
  expect(screen.queryByRole('button',{name:'MLP 해설'})).toBeNull();
});
it('explains a literal quantity without inventing symbols or a calculation process', async () => {
  const literalBundle: ExplanationBundle={...bundle,targets:[mathTarget],explanations:[{target_id:mathTarget.id,kind:'math',math_role:'literal',symbols:[],meaning:[{kind:'text',text:'실험에 사용한 데이터의 개수를 나타내는 값이다.'}],process:[]}]};
  const api={getDocument:vi.fn(async()=>source),getExplanations:vi.fn(async()=>literalBundle),onLibraryChanged:vi.fn(()=>()=>{})} as unknown as PairperApi;
  render(<ReaderScreen api={api} paper={paper} onBack={()=>{}} />);
  fireEvent.click(await screen.findByRole('button',{name:'수식 해설'}));
  expect(screen.getByText('실험에 사용한 데이터의 개수를 나타내는 값이다.')).toBeInTheDocument();
  expect(screen.queryByRole('heading',{name:'기호'})).toBeNull();
  expect(screen.queryByRole('heading',{name:'계산 과정'})).toBeNull();
});
it('keeps formula symbols and calculation process while hiding notation even from a raw saved bundle', async () => {
  const mixed: ExplanationBundle={...bundle,explanations:bundle.explanations.map(entry=>entry.target_id==='math'
    ? {target_id:'math',kind:'math',math_role:'notation',exclusion_reason:'논문 내용이 아닌 목록 표식이다.',symbols:[],meaning:[],process:[]}
    : entry.kind==='math'?{...entry,math_role:'formula'}:entry)};
  const api={getDocument:vi.fn(async()=>source),getExplanations:vi.fn(async()=>mixed),onLibraryChanged:vi.fn(()=>()=>{})} as unknown as PairperApi;
  const {container}=render(<ReaderScreen api={api} paper={paper} onBack={()=>{}} />);
  const buttons=await screen.findAllByRole('button',{name:'수식 해설'});
  expect(buttons).toHaveLength(2);
  expect(container.querySelector('.inline-math-trigger')).toBeNull();
  fireEvent.click(buttons[0]);
  expect(screen.getByRole('heading',{name:'기호'})).toBeInTheDocument();
  expect(screen.getByRole('heading',{name:'계산 과정'})).toBeInTheDocument();
  expect(screen.getByText('두 값을 합한다.')).toBeInTheDocument();
});

it('waits for the document before projecting explanations to avoid concurrent source asset creation', async () => {
  let finish!: (value: DisplayDocument) => void;
  const api={getDocument:vi.fn(()=>new Promise<DisplayDocument>(resolve=>{finish=resolve;})),getExplanations:vi.fn(async()=>bundle),onLibraryChanged:vi.fn(()=>()=>{})} as unknown as PairperApi;
  render(<ReaderScreen api={api} paper={paper} onBack={()=>{}} />);
  expect(api.getExplanations).not.toHaveBeenCalled();
  await act(async()=>finish(source));
  expect(await screen.findByRole('button',{name:'MLP 해설'})).toBeInTheDocument();
  expect(api.getExplanations).toHaveBeenCalledTimes(1);
});
it('renders every reviewed Korean binding for one concept and restores the actual clicked occurrence', async () => {
  const text='다층 퍼셉트론은 다층 퍼셉트론으로 표현한다.';
  const alias='다층 퍼셉트론';
  const first=text.indexOf(alias),second=text.indexOf(alias,first+alias.length);
  const terms: ExplanationBundle={...bundle,targets:[target({concept_key:'mlp',localized:[first,second].map(start=>({block_id:'p',field:'runs',run_index:0,start,end:start+alias.length,text:alias}))})],explanations:[bundle.explanations[0]]};
  const translated={...source,translation:{status:'published' as const,complete:true,missing_unit_count:0},blocks:[{...source.blocks[0],content:{...content,runs:[{kind:'text' as const,prefix:'',text}]}}]};
  const api={getDocument:vi.fn(async()=>source),getExplanations:vi.fn(async()=>terms),onLibraryChanged:vi.fn(()=>()=>{}),listTranslations:vi.fn(async()=>[{session_id:'s',status:'published',revision:1}]),getTranslationDocument:vi.fn(async()=>translated)} as unknown as PairperApi;
  const {container}=render(<ReaderScreen api={api} paper={paper} onBack={()=>{}} />);
  fireEvent.click(await screen.findByRole('button',{name:'한국어'}));
  const bindings=await screen.findAllByRole('button',{name:`${alias} 해설`});
  expect(bindings).toHaveLength(2);
  expect(container.querySelector('.paper-document p')?.textContent).toBe(text);
  fireEvent.click(bindings[1]);
  expect(bindings.every(binding=>binding.getAttribute('aria-expanded')==='true')).toBe(true);
  fireEvent.click(screen.getByRole('button',{name:'해설 닫기'}));
  expect(bindings[1]).toHaveFocus();
  expect(container.querySelector('.paper-document p')?.textContent).toBe(text);
});
it('hides plain table quantity markers even from raw bundles while keeping a body quantity explained', async () => {
  const table={id:'t',type:'table' as const,order:2,validation_status:'source_verified',content:{...content,cells:[{row:0,column:0,rowspan:1,colspan:1,header:false,runs:[formula]}]}};
  const tableTarget=target({...mathTarget,id:'table-literal',block_id:'t',field:'cells/0',run_index:0});
  const literals: ExplanationBundle={...bundle,targets:[mathTarget,tableTarget],explanations:[mathTarget,tableTarget].map(item=>({target_id:item.id,kind:'math',math_role:'literal',symbols:[],meaning:[{kind:'text',text:'측정된 값이다.'}],process:[]}))};
  const api={getDocument:vi.fn(async()=>({...source,blocks:[...source.blocks,table]})),getExplanations:vi.fn(async()=>literals),onLibraryChanged:vi.fn(()=>()=>{})} as unknown as PairperApi;
  render(<ReaderScreen api={api} paper={paper} onBack={()=>{}} />);
  expect(await screen.findAllByRole('button',{name:'수식 해설'})).toHaveLength(1);
  expect(within(screen.getByRole('cell')).queryByRole('button')).toBeNull();
});
it('opens a whole text formula from any styled source fragment with Enter/Space and preserves its canonical subject', async () => {
  const runs=[{kind:'text' as const,prefix:'',text:'조건 SR',bold:true},{kind:'text' as const,prefix:'',text:' > '},{kind:'text' as const,prefix:'',text:'0 이후',italic:true}];
  const relation=target({id:'relation',kind:'math',anchor_type:'text_math',run_index:0,start:null,end:null,source_text:'SR > 0',text_parts:[{run_index:0,start:3,end:5,text:'SR'},{run_index:1,start:0,end:3,text:' > '},{run_index:2,start:0,end:1,text:'0'}]});
  const relationBundle: ExplanationBundle={...bundle,targets:[relation],explanations:[{target_id:'relation',kind:'math',math_role:'formula',symbols:[{symbol:'SR',meaning:'계산한 성공률'}],meaning:[{kind:'text',text:'성공률이 0보다 큰 경우를 고른다.'}],process:[{kind:'text',text:'성공률을 0과 비교한다.'}]}]};
  const api={getDocument:vi.fn(async()=>({...source,blocks:[{...source.blocks[0],content:{...content,runs}}]})),getExplanations:vi.fn(async()=>relationBundle),onLibraryChanged:vi.fn(()=>()=>{})} as unknown as PairperApi;
  const {container}=render(<ReaderScreen api={api} paper={paper} onBack={()=>{}} />);
  const fragments=await screen.findAllByRole('button',{name:'수식 해설'});
  expect(fragments).toHaveLength(3);
  expect(container.querySelector('.paper-document p')?.textContent).toBe('조건 SR > 0 이후');
  expect(container.querySelector('strong .explanation-trigger')).toHaveTextContent('SR');
  expect(container.querySelector('em .explanation-trigger')).toHaveTextContent('0');
  expect(container.querySelector('.paper-document p button')).toBeNull();
  fragments[1].focus();fireEvent.keyDown(fragments[1],{key:'Enter'});
  expect(container.querySelector('.explanation-subject')?.textContent).toBe('SR > 0');
  expect(fragments.every(fragment=>fragment.getAttribute('aria-expanded')==='true')).toBe(true);
  fireEvent.click(screen.getByRole('button',{name:'해설 닫기'}));
  expect(fragments[1]).toHaveFocus();
  const space=new KeyboardEvent('keydown',{key:' ',bubbles:true,cancelable:true});
  fireEvent(fragments[2],space);
  expect(space.defaultPrevented).toBe(true);
  expect(screen.getByText('성공률이 0보다 큰 경우를 고른다.')).toBeInTheDocument();
  expect(container.querySelector('.paper-document p')?.textContent).toBe('조건 SR > 0 이후');
});
it('uses translated text-formula ranges without replacing the complete formula by an individual part', async () => {
  const text='조건을 SR > 0 로 설정한다.';
  const start=text.indexOf('SR > 0');
  const relation=target({id:'relation',kind:'math',anchor_type:'text_math',run_index:0,start:null,end:null,source_text:'SR > 0',text_parts:[{run_index:0,start:2,end:4,text:'SR'}],localized:[{block_id:'p',field:'runs',run_index:0,start,end:start+6,text:'SR > 0'}]});
  const relationBundle: ExplanationBundle={...bundle,targets:[relation],explanations:[{target_id:'relation',kind:'math',math_role:'formula',symbols:[{symbol:'SR',meaning:'성공률'}],meaning:[{kind:'text',text:'성공률이 양수인지 확인한다.'}],process:[{kind:'text',text:'0과 비교한다.'}]}]};
  const translated={...source,translation:{status:'published' as const,complete:true,missing_unit_count:0},blocks:[{...source.blocks[0],content:{...content,runs:[{kind:'text' as const,prefix:'',text}]}}]};
  const api={getDocument:vi.fn(async()=>source),getExplanations:vi.fn(async()=>relationBundle),onLibraryChanged:vi.fn(()=>()=>{}),listTranslations:vi.fn(async()=>[{session_id:'s',status:'published',revision:1}]),getTranslationDocument:vi.fn(async()=>translated)} as unknown as PairperApi;
  const {container}=render(<ReaderScreen api={api} paper={paper} onBack={()=>{}} />);
  fireEvent.click(await screen.findByRole('button',{name:'한국어'}));
  fireEvent.click(await screen.findByRole('button',{name:'수식 해설'}));
  expect(container.querySelector('.explanation-subject')?.textContent).toBe('SR > 0');
  expect(container.querySelector('.paper-document p')?.textContent).toBe(text);
});
it('gives a reviewed whole formula priority over an overlapping glossary occurrence and keeps another occurrence', async () => {
  const runs=[{kind:'text' as const,prefix:'',text:'조건 SR',bold:true},{kind:'text' as const,prefix:'',text:' > '},{kind:'text' as const,prefix:'',text:'0 이후 SR'}];
  const relation=target({id:'relation',kind:'math',anchor_type:'text_math',run_index:0,start:null,end:null,source_text:'SR > 0',text_parts:[{run_index:0,start:3,end:5,text:'SR'},{run_index:1,start:0,end:3,text:' > '},{run_index:2,start:0,end:1,text:'0'}]});
  const first=target({id:'first-sr',source_text:'SR',concept_key:'success-ratio',start:3,end:5});
  const other=target({id:'other-sr',source_text:'SR',concept_key:'success-ratio',run_index:2,start:5,end:7});
  const entries: ExplanationBundle['explanations']=[{target_id:'relation',kind:'math',math_role:'formula',symbols:[{symbol:'SR',meaning:'성공률'}],meaning:[{kind:'text',text:'성공률이 양수인 조건이다.'}],process:[{kind:'text',text:'성공률을 0과 비교한다.'}]},...[first,other].map(item=>({target_id:item.id,kind:'term' as const,category:'평가 지표',description:[{kind:'text' as const,text:'성공률 개념 해설이다.'}]}))];
  const api={getDocument:vi.fn(async()=>({...source,blocks:[{...source.blocks[0],content:{...content,runs}}]})),getExplanations:vi.fn(async()=>({...bundle,targets:[first,relation,other],explanations:entries})),onLibraryChanged:vi.fn(()=>()=>{})} as unknown as PairperApi;
  const {container}=render(<ReaderScreen api={api} paper={paper} onBack={()=>{}} />);
  expect(await screen.findAllByRole('button',{name:'수식 해설'})).toHaveLength(3);
  expect(screen.getAllByRole('button',{name:'SR 해설'})).toHaveLength(1);
  expect(container.querySelector('[data-explanation-id="first-sr"]')).toBeNull();
  const variable=container.querySelector('strong .explanation-trigger')!;
  fireEvent.click(variable);
  expect(container.querySelector('.explanation-subject')?.textContent).toBe('SR > 0');
  expect(container.querySelector('.paper-document p')?.textContent).toBe('조건 SR > 0 이후 SR');
});
it('suppresses only the overlapping Korean glossary binding from a raw multi-occurrence payload', async () => {
  const text='조건을 SR > 0 로 설정하고 SR 을 확인한다.';
  const first=text.indexOf('SR'),other=text.lastIndexOf('SR');
  const relation=target({id:'relation',kind:'math',anchor_type:'text_math',source_text:'SR > 0',text_parts:[],localized:[{block_id:'p',field:'runs',run_index:0,start:first,end:first+6,text:'SR > 0'}]});
  const term=target({id:'sr',source_text:'SR',localized:[first,other].map(start=>({block_id:'p',field:'runs',run_index:0,start,end:start+2,text:'SR'}))});
  const relationBundle: ExplanationBundle={...bundle,targets:[term,relation],explanations:[{target_id:'relation',kind:'math',math_role:'formula',symbols:[{symbol:'SR',meaning:'성공률'}],meaning:[{kind:'text',text:'성공률이 양수인 조건이다.'}],process:[{kind:'text',text:'0과 비교한다.'}]},{target_id:'sr',kind:'term',category:'평가 지표',description:[{kind:'text',text:'성공률 개념 해설이다.'}]}]};
  const translated={...source,translation:{status:'published' as const,complete:true,missing_unit_count:0},blocks:[{...source.blocks[0],content:{...content,runs:[{kind:'text' as const,prefix:'',text}]}}]};
  const api={getDocument:vi.fn(async()=>source),getExplanations:vi.fn(async()=>relationBundle),onLibraryChanged:vi.fn(()=>()=>{}),listTranslations:vi.fn(async()=>[{session_id:'s',status:'published',revision:1}]),getTranslationDocument:vi.fn(async()=>translated)} as unknown as PairperApi;
  const {container}=render(<ReaderScreen api={api} paper={paper} onBack={()=>{}} />);
  fireEvent.click(await screen.findByRole('button',{name:'한국어'}));
  expect(await screen.findAllByRole('button',{name:'SR 해설'})).toHaveLength(1);
  fireEvent.click(screen.getByRole('button',{name:'수식 해설'}));
  expect(container.querySelector('.explanation-subject')?.textContent).toBe('SR > 0');
  expect(container.querySelector('.paper-document p')?.textContent).toBe(text);
});
it('honors an explicitly suppressed source projection while retaining raw-source backward compatibility', async () => {
  const suppressed: ExplanationBundle={...bundle,targets:[target({localized:[]}),mathTarget],explanations:bundle.explanations};
  const api={getDocument:vi.fn(async()=>source),getExplanations:vi.fn(async()=>suppressed),onLibraryChanged:vi.fn(()=>()=>{})} as unknown as PairperApi;
  render(<ReaderScreen api={api} paper={paper} onBack={()=>{}} />);
  expect(await screen.findByRole('button',{name:'수식 해설'})).toBeInTheDocument();
  expect(screen.queryByRole('button',{name:'MLP 해설'})).toBeNull();
  expect(screen.getByText('🧪 MLP processing')).toBeInTheDocument();
});
it('clears an open explanation when a new authoritative projection suppresses its occurrence', async () => {
  let listener=()=>{};
  let saved: ExplanationBundle=bundle;
  const api={getDocument:vi.fn(async()=>source),getExplanations:vi.fn(async()=>saved),onLibraryChanged:vi.fn((fn:()=>void)=>{listener=fn;return()=>{};})} as unknown as PairperApi;
  render(<ReaderScreen api={api} paper={paper} onBack={()=>{}} />);
  fireEvent.click(await screen.findByRole('button',{name:'MLP 해설'}));
  expect(screen.getByText('입력 숫자를 계산하는 신경망이다.')).toBeInTheDocument();
  saved={...bundle,targets:bundle.targets.map(item=>item.id==='term'?{...item,localized:[]}:item)};
  await act(async()=>listener());
  expect(screen.queryByRole('button',{name:'MLP 해설'})).toBeNull();
  expect(screen.queryByText('입력 숫자를 계산하는 신경망이다.')).toBeNull();
  expect(screen.getByText('🧪 MLP processing')).toBeInTheDocument();
});
