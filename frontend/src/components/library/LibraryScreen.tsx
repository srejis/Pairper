import { useEffect, useRef, useState } from 'react';
import type { Paper } from '../../types';
import { Icon } from '../common/Icon';
import { Overlay } from '../common/Overlay';

type Filter = 'all' | 'ready' | 'pending' | 'review';
export const statusLabel = (paper: Paper) => paper.has_view ? '읽기 가능' : ({
  registered: '처리 필요', processing: '처리 중', complete: '처리 필요',
  review_required: '검토 필요', failed: '처리 실패',
})[paper.status];
export const paperTitle = (paper: Paper) => paper.title.trim() || paper.filename;
const filters: { value: Filter; label: string }[] = [
  {value:'all',label:'전체'}, {value:'ready',label:'읽기 가능'},
  {value:'pending',label:'처리 필요'}, {value:'review',label:'검토 필요'},
];

export function LibraryScreen({ papers, onRead, onNotify, onCopy, loading, returnFocusId }: {
  papers: Paper[]; onRead(paper: Paper): void; onNotify(message: string, error?: boolean): void; onCopy(paperId: string): Promise<{copied: boolean}>; loading: boolean; returnFocusId: string | null;
}) {
  const [query, setQuery] = useState('');
  const [sort, setSort] = useState('recent');
  const [filter, setFilter] = useState<Filter>('all');
  const [filterOpen, setFilterOpen] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  const returnFocus = useRef<HTMLButtonElement>(null);
  useEffect(() => { returnFocus.current?.focus({preventScroll:true}); }, []);
  const result = papers.filter(paper => {
    const matches = `${paperTitle(paper)} ${paper.filename}`.toLocaleLowerCase().includes(query.toLocaleLowerCase().trim());
    return matches && (filter === 'all' || filter === 'ready' && paper.has_view
      || filter === 'pending' && !paper.has_view && paper.status !== 'review_required'
      || filter === 'review' && !paper.has_view && paper.status === 'review_required');
  }).sort((a, b) => sort === 'title' ? paperTitle(a).localeCompare(paperTitle(b), 'ko')
    : (a.created_at ?? '').localeCompare(b.created_at ?? '') * (sort === 'oldest' ? 1 : -1));
  async function copyRequest(paper: Paper) {
    try {
      const result = await onCopy(paper.id);
      if (!result.copied) throw new Error();
      onNotify('요청 문구를 복사했습니다. ChatGPT에 붙여 넣어 주세요.');
    } catch { onNotify('복사하지 못했습니다. 아래 요청 문구를 직접 선택해 복사해 주세요.', true); }
  }
  return <main className="library-screen" aria-labelledby="library-title">
    <div className="library-title"><h1 id="library-title">내 라이브러리</h1><span>{papers.length}편</span></div>
    <div className="library-controls">
      <label className="search"><Icon name="search" size={18} /><input aria-label="논문 검색" placeholder="제목 또는 파일명으로 검색" value={query} onChange={e => setQuery(e.target.value)} />
        {query ? <button className="icon-button" aria-label="검색 지우기" onClick={() => setQuery('')}><Icon name="close" size={16} /></button> : null}</label>
      <select aria-label="논문 정렬" value={sort} onChange={e => setSort(e.target.value)}>
        <option value="recent">최근 추가순</option><option value="title">제목순</option><option value="oldest">오래된 등록순</option>
      </select>
      <button className={`icon-button filter-button ${filter !== 'all' ? 'active' : ''}`} aria-label="상태 필터" aria-expanded={filterOpen} onClick={() => setFilterOpen(true)}><Icon name="filter" /></button>
    </div>
    {filter !== 'all' ? <p className="filter-summary">{filters.find(f => f.value === filter)?.label} · {result.length}편 <button className="text-button" onClick={() => setFilter('all')}>필터 해제</button></p> : null}
    {filterOpen ? <Overlay label="상태 필터" kind="popover" onClose={() => setFilterOpen(false)}>
      <div className="filter-options" role="radiogroup" aria-label="논문 상태">{filters.map(item => <label key={item.value}><input type="radio" name="status-filter" value={item.value} checked={filter === item.value} onChange={() => { setFilter(item.value); setFilterOpen(false); }} />{item.label}</label>)}</div>
    </Overlay> : null}
    <div className="paper-list" aria-label="등록 논문">
      {result.map(paper => <div className="paper-entry" key={paper.id}>
        <button className="paper-row" ref={paper.id === returnFocusId ? returnFocus : undefined} aria-expanded={!paper.has_view ? expanded === paper.id : undefined}
          onClick={() => paper.has_view ? onRead(paper) : setExpanded(expanded === paper.id ? null : paper.id)}>
          <span className="document-icon"><Icon name="file" size={24} /></span>
          <span className="paper-info"><strong>{paperTitle(paper)}</strong><span>{paper.filename} <span className="meta-dot">·</span> {paper.page_count}페이지</span></span>
          <span className={`paper-status ${paper.has_view ? 'ready' : 'pending'}`}><Icon name={paper.has_view ? 'check' : paper.status === 'review_required' || paper.status === 'failed' ? 'warning' : 'clock'} size={15} /><span>{statusLabel(paper)}</span></span>
          <span className={expanded === paper.id ? 'row-arrow expanded' : 'row-arrow'}><Icon name="arrow" size={16} /></span>
        </button>
        {!paper.has_view && expanded === paper.id ? <div className="paper-expansion">
          <p><strong>{statusLabel(paper)}</strong> · {paper.original_available === true ? '원본 PDF 등록됨' : paper.original_available === false ? '원본 PDF를 찾을 수 없음' : '원본 등록 상태 확인 필요'}</p>
          <p>아직 재구성된 읽기용 문서가 없습니다. 연결된 ChatGPT에 아래 문구로 요청해 주세요.</p>
          <div className="request-copy"><code>Pairper 논문 업데이트해 줘</code><button onClick={() => { void copyRequest(paper); }}><Icon name="copy" size={15} />요청 문구 복사</button></div>
          {paper.error ? <p className="inline-error" role="alert">{paper.error}</p> : null}
        </div> : null}
      </div>)}
      {!result.length ? <div className="empty-state"><Icon name={papers.length ? 'search' : 'file'} size={32} /><h2>{loading ? '라이브러리를 불러오는 중…' : papers.length ? '검색 결과가 없습니다' : '첫 논문을 추가해 보세요'}</h2><p>{papers.length ? '검색어 또는 상태 필터를 바꿔 보세요.' : 'PDF를 등록한 뒤 ChatGPT에 논문 업데이트를 요청하세요.'}</p></div> : null}
    </div>
  </main>;
}
