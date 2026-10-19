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
  const [source, setSource] = useState<DisplayDocument | null>(null);
  const [sessions, setSessions] = useState<{session_id: string; status: 'draft' | 'published'}[]>([]);
  const [language, setLanguage] = useState<'source' | 'korean'>('source');
  const [translationLoading, setTranslationLoading] = useState(false);
  const generation = useRef(0);
  const anchor = useRef<{id: string; offset: number} | null>(null);
  const reader = useRef<HTMLElement>(null);
  const back = useRef<HTMLButtonElement>(null);
  useEffect(() => { back.current?.focus({preventScroll:true}); }, []);
  useEffect(() => {
    let active = true;
    generation.current++;
    setSource(null); setSessions([]); setLanguage('source'); setTranslationLoading(false);
    anchor.current = null;
    setDocument(null); setError(''); setTocOpen(false);
    if (!paper.has_view) { setError('유효한 저장 문서가 없습니다. 라이브러리에서 다시 확인해 주세요.'); return; }
    void api.getDocument(paper.id).then(data => {
      if (data.display_schema_version !== '1.0' || !['1.0', '1.1'].includes(data.source_schema_version)) throw new Error('지원하지 않는 문서 버전입니다.');
      if (active) { setSource(data); setDocument(data); }
    }).catch(() => { if (active) setError('문서를 열지 못했습니다. 원본 PDF와 저장 문서를 확인해 주세요.'); });
    if (api.listTranslations && api.getTranslationDocument) {
      void api.listTranslations(paper.id).then(data => { if (active) setSessions(data); }).catch(() => {
        if (active) setError('한국어 번역 목록을 불러오지 못했습니다. 원문은 계속 읽을 수 있습니다.');
      });
    }
    return () => { active = false; generation.current++; };
  }, [api, paper.id, paper.current_view_id, paper.has_view]);
  useEffect(() => {
    if (!document || !reader.current || !anchor.current) return;
    const target = Array.from(reader.current.querySelectorAll<HTMLElement>('[data-block-id]'))
      .find(element => element.dataset.blockId === anchor.current?.id);
    if (target) reader.current.scrollTop += target.getBoundingClientRect().top
      - reader.current.getBoundingClientRect().top - anchor.current.offset;
    anchor.current = null;
  }, [document]);
  function changeLanguage(next: 'source' | 'korean') {
    if (next === language && !error) return;
    const top = reader.current?.getBoundingClientRect().top ?? 0;
    const visible = Array.from(reader.current?.querySelectorAll<HTMLElement>('[data-block-id]') ?? [])
      .find(element => element.getBoundingClientRect().bottom > top);
    if (visible) anchor.current = {id: visible.dataset.blockId ?? '', offset: visible.getBoundingClientRect().top - top};
    const request = ++generation.current;
    setLanguage(next); setError(''); setTranslationLoading(false);
    if (next === 'source') { setDocument(source); return; }
    if (!api.getTranslationDocument || !sessions.length) return;
    setTranslationLoading(true); setDocument(null);
    void api.getTranslationDocument(sessions[0].session_id).then(data => {
      if (data.paper_id !== paper.id || data.view_id !== paper.current_view_id || !data.translation
        || data.display_schema_version !== '1.0' || !['1.0', '1.1'].includes(data.source_schema_version)) {
        throw new Error('현재 원문과 일치하지 않는 번역입니다.');
      }
      if (generation.current === request) { setDocument(data); setTranslationLoading(false); }
    }).catch(() => {
      if (generation.current === request) { setTranslationLoading(false); setError('한국어 번역을 열지 못했습니다. 원문으로 돌아가 계속 읽을 수 있습니다.'); }
    });
  }
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
      <div className="reader-actions">
        {sessions.length > 0 && source ? <div className="reader-language" role="group" aria-label="읽기 언어">
          <button aria-pressed={language === 'source'} onClick={() => changeLanguage('source')}>원문</button>
          <button aria-pressed={language === 'korean'} onClick={() => changeLanguage('korean')}>{sessions[0].status === 'published' ? '한국어' : '한국어 초안'}</button>
        </div> : null}
        <button className="icon-button" aria-label="목차" aria-expanded={tocOpen} onClick={() => setTocOpen(true)}><Icon name="toc" /></button></div>
    </header>
    <main ref={reader} className="reader" aria-label="논문 읽기">
      {document ? <article className="paper-document" aria-label={language === 'korean' ? '한국어 논문' : '재구성된 논문'}>
        {document.translation && !document.translation.complete ? <p className="translation-notice" role="status">한국어 초안 · 원문 대조 미완료{document.translation.missing_unit_count ? ' · 번역이 없는 부분은 원문으로 표시됩니다.' : ''}</p> : null}
        {error ? <p role="alert">{error}</p> : null}
        <DocumentBlocks blocks={document.blocks} />
      </article> : <div className="empty-state" role={error ? 'alert' : 'status'}><h1>{error ? '문서를 열 수 없습니다' : translationLoading ? '한국어 번역을 여는 중…' : '문서를 여는 중…'}</h1><p>{error}</p></div>}
    </main>
    {tocOpen ? <Overlay label="목차" onClose={() => setTocOpen(false)}>
      <TableOfContents headings={headings} active={activeHeading} onSelect={id => {
        window.document.getElementById(`block-${id}`)?.scrollIntoView({block:'start'}); setActiveHeading(id);
      }} />
    </Overlay> : null}
  </div>;
}
