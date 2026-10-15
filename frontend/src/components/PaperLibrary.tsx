import type { Paper } from '../types';

export const statusLabels = { registered: '등록됨', processing: '처리 중', complete: '완료', review_required: '검토 필요', failed: '실패' };
export function PaperLibrary({ papers, selected, onSelect }: {
  papers: Paper[]; selected: string | null; onSelect(id: string): void;
}) {
  return <aside className="library" aria-label="논문 라이브러리">
    <div className="library-heading"><h2>라이브러리</h2><span>{papers.length}편</span></div>
    <nav aria-label="논문 목록">{papers.map(paper => <button type="button" key={paper.id}
      className={`paper-item ${paper.id === selected ? 'selected' : ''}`}
      aria-current={paper.id === selected ? 'true' : undefined} onClick={() => onSelect(paper.id)}>
      <strong>{paper.title || paper.filename}</strong>
      <span><i className={`status-dot ${paper.status}`} />{statusLabels[paper.status]} · {paper.has_view ? '뷰 있음' : '뷰 대기'}</span>
    </button>)}</nav>
    <div className="library-note">원본을 보존하는 읽기 공간<br />AI가 구성하고, 코드가 검증합니다.</div>
  </aside>;
}
