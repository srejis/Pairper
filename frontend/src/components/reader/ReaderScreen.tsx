import { useEffect, useRef, useState } from 'react';
import type { DisplayDocument, PairperApi, Paper } from '../../types';
import { DocumentBlocks } from '../blocks';
import { Overlay } from '../common/Overlay';
import { Icon } from '../common/Icon';
import { paperTitle } from '../library/LibraryScreen';
import { TableOfContents } from './TableOfContents';

export function ReaderScreen({ api, paper, onBack }: { api: PairperApi; paper: Paper; onBack(): void }) {
  const [document, setDocument] = useState<DisplayDocument | null>(null);
  const [error, setError] = useState('');
  const [tocOpen, setTocOpen] = useState(false);
  const [activeHeading, setActiveHeading] = useState('');
  const reader = useRef<HTMLElement>(null);
  const back = useRef<HTMLButtonElement>(null);
  useEffect(() => { back.current?.focus({preventScroll:true}); }, []);
  useEffect(() => {
    let active = true;
    setDocument(null); setError(''); setTocOpen(false);
    if (!paper.has_view) { setError('유효한 저장 문서가 없습니다. 라이브러리에서 다시 확인해 주세요.'); return; }
    void api.getDocument(paper.id).then(data => {
      if (data.display_schema_version !== '1.0' || !['1.0', '1.1'].includes(data.source_schema_version)) throw new Error('지원하지 않는 문서 버전입니다.');
      if (active) setDocument(data);
    }).catch(() => { if (active) setError('문서를 열지 못했습니다. 원본 PDF와 저장 문서를 확인해 주세요.'); });
    return () => { active = false; };
  }, [api, paper.id, paper.current_view_id, paper.has_view]);
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
  }, [document]);
  const headings = document?.blocks.filter(block => block.type === 'heading') ?? [];
  return <div className="reader-screen">
    <header className="reader-toolbar"><button ref={back} className="back-button" onClick={onBack}><Icon name="back" /><span>라이브러리</span></button>
      <div className="reader-title" title={paperTitle(paper)}>{paperTitle(paper)}</div>
      <div className="reader-actions"><button className="icon-button" aria-label="목차" aria-expanded={tocOpen} onClick={() => setTocOpen(true)}><Icon name="toc" /></button></div>
    </header>
    <main ref={reader} className="reader" aria-label="논문 읽기">
      {document ? <article className="paper-document" aria-label="재구성된 논문">
        <DocumentBlocks blocks={document.blocks} />
      </article> : <div className="empty-state" role={error ? 'alert' : 'status'}><h1>{error ? '문서를 열 수 없습니다' : '문서를 여는 중…'}</h1><p>{error}</p></div>}
    </main>
    {tocOpen ? <Overlay label="목차" onClose={() => setTocOpen(false)}>
      <TableOfContents headings={headings} active={activeHeading} onSelect={id => {
        window.document.getElementById(`block-${id}`)?.scrollIntoView({block:'start'}); setActiveHeading(id);
      }} />
    </Overlay> : null}
  </div>;
}
