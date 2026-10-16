import { useEffect, useRef } from 'react';
import type { DocumentBlock } from '../../types';

const headingText = (block: DocumentBlock) => block.content.runs.map(run => run.prefix + (run.kind === 'text' ? run.text : run.latex)).join('');

export function TableOfContents({ headings, active, onSelect }: {
  headings: DocumentBlock[]; active: string; onSelect(id: string): void;
}) {
  const nav = useRef<HTMLElement>(null);
  useEffect(() => { nav.current?.querySelector('[aria-current="location"]')?.scrollIntoView({block:'nearest'}); }, [active]);
  return <nav ref={nav} className="toc" aria-label="논문 목차">{headings.length ? headings.map(block => <button key={block.id} className={active === block.id ? 'active' : ''} aria-current={active === block.id ? 'location' : undefined}
    style={{paddingLeft: `${18 + (block.content.level - 2) * 14}px`}} onClick={() => onSelect(block.id)}>
    {headingText(block)}</button>) : <p className="panel-description">저장된 문서에 목차 항목이 없습니다.</p>}</nav>;
}
