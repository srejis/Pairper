import { useEffect, useState } from 'react';
import type { DisplayDocument, DocumentBlock, PairperApi, SourceComparison } from '../../types';
import { SourceImage } from '../blocks';
import { Icon } from '../common/Icon';

export function SourcePanel({ api, document, block }: { api: PairperApi; document: DisplayDocument; block: DocumentBlock }) {
  const regions = block.source_regions ?? [];
  const [index, setIndex] = useState(0);
  const [zoom, setZoom] = useState(100);
  const [image, setImage] = useState<SourceComparison | null>(null);
  const [error, setError] = useState('');
  const region = regions[index];
  useEffect(() => {
    let active = true;
    setImage(null); setError('');
    if (region) void api.sourceRegion(document.paper_id, document.view_id, block.id, region.id)
      .then(data => { if (active) setImage(data); })
      .catch(() => { if (active) setError('원본 영역을 불러오지 못했습니다. PDF와 문서 연결을 확인해 주세요.'); });
    return () => { active = false; };
  }, [api, document.paper_id, document.view_id, block.id, region?.id]);
  return <div className="source-panel-content">
    <p className="panel-description">등록된 PDF의 실제 원본 영역입니다.</p>
    <div className="source-navigation"><button className="icon-button" aria-label="이전 원본 영역" disabled={index === 0} onClick={() => { setIndex(index - 1); setZoom(100); }}><Icon name="back" size={16} /></button>
      <span>{index + 1} / {regions.length} 영역 · {region?.page}페이지</span>
      <button className="icon-button" aria-label="다음 원본 영역" disabled={index >= regions.length - 1} onClick={() => { setIndex(index + 1); setZoom(100); }}><Icon name="arrow" size={16} /></button></div>
    <div className="zoom-controls"><button aria-label="원본 축소" disabled={zoom <= 50} onClick={() => setZoom(zoom - 25)}>−</button><output aria-label="원본 배율">{zoom}%</output><button aria-label="원본 확대" disabled={zoom >= 300} onClick={() => setZoom(zoom + 25)}>+</button></div>
    {error ? <p role="alert" className="inline-error">{error}</p> : image ? <div className="comparison-viewport"><div style={{ width: `${zoom}%` }}><SourceImage url={image.image_url} /></div></div> : <p role="status">원본 영역을 불러오는 중…</p>}
    {region ? <p className="region-caption">PDF {region.page}페이지 · 영역 {index + 1}</p> : null}
  </div>;
}
