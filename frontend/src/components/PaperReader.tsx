import { useEffect, useRef, useState } from 'react';
import type { DisplayDocument, PairperApi, Paper } from '../types';
import { BlockView } from './blocks';

const statuses = { registered: '처리 대기', processing: '처리 중', complete: '완료', review_required: '검토 필요', failed: '실패' };
export function PaperReader({ paper, api }: { paper: Paper | undefined; api: PairperApi }) {
  const [document, setDocument] = useState<DisplayDocument | null>(null);
  const [error, setError] = useState('');
  const reader = useRef<HTMLElement>(null);
  const id = paper?.id;
  const view = paper?.has_view ? paper.current_view_id : null;
  useEffect(() => {
    let active = true;
    setDocument(null);
    setError('');
    reader.current?.scrollTo(0, 0);
    if (id && view) api.getDocument(id).then(data => {
      if (data.display_schema_version !== '1.0' || data.source_schema_version !== '1.0')
        throw new Error('지원하지 않는 문서 버전입니다. 앱을 업데이트해 주세요.');
      if (active) setDocument(data);
    }).catch((e: Error) => { if (active) setError(e.message); });
    return () => { active = false; };
  }, [api, id, view]);
  useEffect(() => {
    if (!document) return;
    window.document.documentElement.dataset.readerReady = 'true';
    return () => { delete window.document.documentElement.dataset.readerReady; };
  }, [document]);
  return <main ref={reader} className="reader" aria-label="논문 읽기">
    {error ? <div className="empty-state" role="alert"><h1>문서를 열 수 없습니다</h1><p>{error}</p></div>
    : document ? <article className="paper-document" aria-label="재구성된 논문">
      {document.blocks.map(block => <BlockView key={block.id} block={block} />)}
      <details className="normalization-audit"><summary>텍스트 연결 이력</summary><pre>{JSON.stringify(document.normalization_audit, null, 2)}</pre></details>
    </article> : <div className="empty-state"><span className="eyebrow">PAIRPER LIBRARY</span>
      <h1>{paper ? paper.has_view ? '문서를 여는 중…' : statuses[paper.status] : '논문 읽기를 시작하세요'}</h1>
      <p>{paper ? '연결된 GPT에 “Pairper 논문 업데이트해 줘”라고 요청하세요.' : 'PDF를 추가하고 라이브러리에서 논문을 선택하세요.'}</p>
      <p className="muted">PDF 등록과 AI의 문서 재구성은 별도의 작업입니다.</p>
    </div>}
  </main>;
}
