import { useCallback, useEffect, useRef, useState, type CSSProperties } from 'react';
import type { DisplayDocument, DocumentBlock, PairperApi, Paper } from '../../types';
import { BlockView } from '../blocks';
import { Overlay } from '../common/Overlay';
import { Icon } from '../common/Icon';
import { paperTitle } from '../library/LibraryScreen';
import { SourcePanel } from './SourcePanel';
import { TableOfContents } from './TableOfContents';

function initialFont() {
  try { const value = Number(localStorage.getItem('pairper.ui.v1.fontSize')); return value >= 15 && value <= 21 && Number.isInteger(value) ? value : 17; }
  catch { return 17; }
}

export function ReaderScreen({ api, paper, onBack }: { api: PairperApi; paper: Paper; onBack(): void }) {
  const [document, setDocument] = useState<DisplayDocument | null>(null);
  const [error, setError] = useState('');
  const [panel, setPanel] = useState<'toc' | 'source' | null>(null);
  const [inspected, setInspected] = useState<DocumentBlock | null>(null);
  const [popover, setPopover] = useState<'font' | 'more' | null>(null);
  const [fontSize, setFontSize] = useState(initialFont);
  const [activeHeading, setActiveHeading] = useState('');
  const reader = useRef<HTMLElement>(null);
  const back = useRef<HTMLButtonElement>(null);
  useEffect(() => { back.current?.focus({preventScroll:true}); }, []);
  useEffect(() => {
    let active = true;
    setDocument(null); setError(''); setPanel(null); setInspected(null);
    if (!paper.has_view) { setError('유효한 저장 문서가 없습니다. 라이브러리에서 다시 확인해 주세요.'); return; }
    void api.getDocument(paper.id).then(data => {
      if (data.display_schema_version !== '1.0' || data.source_schema_version !== '1.0') throw new Error('지원하지 않는 문서 버전입니다.');
      if (active) setDocument(data);
    }).catch(() => { if (active) setError('문서를 열지 못했습니다. 원본 PDF와 저장 문서를 확인해 주세요.'); });
    return () => { active = false; };
  }, [api, paper.id, paper.current_view_id, paper.has_view]);
  useEffect(() => {
    try { localStorage.setItem('pairper.ui.v1.fontSize', String(fontSize)); } catch { /* Reading still works without UI persistence. */ }
  }, [fontSize]);
  useEffect(() => {
    if (!document) return;
    window.document.documentElement.dataset.readerReady = 'true';
    return () => { delete window.document.documentElement.dataset.readerReady; };
  }, [document]);
  useEffect(() => {
    if (!document || !reader.current) return;
    const element = reader.current;
    let frame = 0;
    const update = () => {
      const top = element.getBoundingClientRect().top;
      const headings = Array.from(element.querySelectorAll<HTMLElement>('[data-block-type="heading"]'));
      let current = headings[0]?.dataset.blockId ?? '';
      for (const heading of headings) {
        if (heading.getBoundingClientRect().top <= top + 100) current = heading.dataset.blockId ?? current;
      }
      setActiveHeading(current);
    };
    const scroll = () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(update); };
    element.addEventListener('scroll', scroll, {passive:true});
    window.addEventListener('resize', scroll);
    update();
    return () => { element.removeEventListener('scroll', scroll); window.removeEventListener('resize', scroll); cancelAnimationFrame(frame); };
  }, [document, fontSize]);
  const inspect = useCallback((block: DocumentBlock) => { setInspected(block); setPanel('source'); }, []);
  const headings = document?.blocks.filter(block => block.type === 'heading') ?? [];
  return <div className="reader-screen">
    <header className="reader-toolbar"><button ref={back} className="back-button" onClick={onBack}><Icon name="back" /><span>라이브러리</span></button>
      <div className="reader-title" title={paperTitle(paper)}>{paperTitle(paper)}</div>
      <div className="reader-actions"><button className="icon-button" aria-label="목차" aria-expanded={panel === 'toc'} onClick={() => setPanel('toc')}><Icon name="toc" /></button>
        <button className="icon-button font-trigger" aria-label="글자 설정" onClick={() => setPopover('font')}><Icon name="type" /></button>
        <button className="icon-button" aria-label="기타 메뉴" onClick={() => setPopover('more')}><Icon name="more" /></button></div>
    </header>
    <main ref={reader} className="reader" aria-label="논문 읽기" style={{'--reader-font-size': `${fontSize}px`} as CSSProperties}>
      {document ? <article className="paper-document" aria-label="재구성된 논문">
        {document.blocks.map(block => <BlockView key={block.id} block={block} onInspect={inspect} selected={panel === 'source' && inspected?.id === block.id} />)}
      </article> : <div className="empty-state" role={error ? 'alert' : 'status'}><h1>{error ? '문서를 열 수 없습니다' : '문서를 여는 중…'}</h1><p>{error}</p></div>}
    </main>
    {panel ? <Overlay label={panel === 'toc' ? '목차' : '원본 대조'} onClose={() => setPanel(null)}>
      {panel === 'toc' ? <TableOfContents headings={headings} active={activeHeading} onSelect={id => {
        window.document.getElementById(`block-${id}`)?.scrollIntoView({block:'start'}); setActiveHeading(id);
      }} />
        : document && inspected ? <SourcePanel key={inspected.id} api={api} document={document} block={inspected} /> : null}
    </Overlay> : null}
    {popover ? <Overlay label={popover === 'font' ? '글자 설정' : '기타 메뉴'} kind="popover" onClose={() => setPopover(null)}>
      {popover === 'font' ? <div className="font-settings"><p>본문 글자 크기</p><div><button aria-label="글자 크기 감소" disabled={fontSize <= 15} onClick={() => setFontSize(fontSize - 1)}>−</button><output aria-label="본문 글자 크기">{fontSize}px</output><button aria-label="글자 크기 증가" disabled={fontSize >= 21} onClick={() => setFontSize(fontSize + 1)}>+</button></div><button className="text-button" onClick={() => setFontSize(17)}>기본값 복원</button></div>
        : <div className="reader-more"><button className="compact-font" onClick={() => setPopover('font')}>글자 설정</button><details className="normalization-audit"><summary>텍스트 연결 이력</summary><pre>{JSON.stringify(document?.normalization_audit ?? [], null, 2)}</pre></details><p>원본 확인은 문서 블록에 초점을 두거나 마우스를 올리면 표시됩니다.</p></div>}
    </Overlay> : null}
  </div>;
}
