import { Fragment, type ComponentType } from 'react';
import katex from 'katex';
import type { BlockContent, BlockType, DocumentBlock, Run } from '../types';

export function SourceImage({ url, inline = false }: { url: string | null; inline?: boolean }) {
  // URLs are produced by Python. Defence in depth against injected remote/data/JS schemes.
  return url?.startsWith('file:///') && /\/[a-f0-9]{32}\.png$/.test(url)
    ? <img className={inline ? 'inline-source' : 'source-image'} src={url} alt="원본 영역" />
    : <span role="alert">원본 이미지를 확인할 수 없습니다.</span>;
}

export function MathContent({ latex, verified, image, display = false }: {
  latex: string | null; verified: boolean; image: string | null; display?: boolean;
}) {
  if (verified && latex) {
    try {
      const markup = katex.renderToString(latex, {
        displayMode: display, throwOnError: true, trust: false, strict: 'error', maxExpand: 1000,
      });
      // Only KaTeX output reaches this sink, with trust disabled. Original prose is React text.
      return <span className="rendered-math" dangerouslySetInnerHTML={{ __html: markup }} />;
    } catch { /* Preserve the original region when KaTeX rejects the reconstruction. */ }
  }
  return <span className="math-fallback"><SourceImage url={image} inline={!display} /></span>;
}

export function Runs({ runs }: { runs: Run[] }) {
  return runs.map((run, index) => <Fragment key={index}>
    {run.prefix}{run.kind === 'text' ? run.text : <MathContent latex={run.latex}
      verified={run.source_verified} image={run.image_url} />}
  </Fragment>);
}

type Props = { content: BlockContent };
export const Title = ({ content }: Props) => <h1><Runs runs={content.runs} /></h1>;
export const Authors = ({ content }: Props) => <p className="authors-text"><Runs runs={content.runs} /></p>;
export function Heading({ content }: Props) {
  const Tag = `h${content.level}` as 'h2' | 'h3' | 'h4' | 'h5' | 'h6';
  return <Tag><Runs runs={content.runs} /></Tag>;
}
export const Paragraph = ({ content }: Props) => <p><Runs runs={content.runs} /></p>;
export function List({ content }: Props) {
  const Tag = content.ordered ? 'ol' : 'ul';
  return <Tag>{content.items.map((runs, i) => <li key={i}><Runs runs={runs} /></li>)}</Tag>;
}
export const Equation = ({ content }: Props) => <div className="equation-scroll">
  <MathContent latex={content.latex} verified={content.source_verified} image={content.image_url} display />
  {content.number ? <span className="equation-number">{content.number}</span> : null}
</div>;
export const Figure = ({ content }: Props) => <figure aria-describedby={content.caption_id ? `block-${content.caption_id}` : undefined}><SourceImage url={content.image_url} /></figure>;
export function Table({ content }: Props) {
  if (!content.source_verified || !content.cells.length) return <SourceImage url={content.image_url} />;
  const rows = new Map<number, typeof content.cells>();
  for (const cell of [...content.cells].sort((a, b) => a.row - b.row || a.column - b.column)) {
    const row = rows.get(cell.row) ?? [];
    row.push(cell);
    rows.set(cell.row, row);
  }
  return <div className="table-scroll" role="region" aria-label="논문 표" tabIndex={0}>
    <table><tbody>{[...rows].map(([index, cells]) => <tr key={index}>
      {cells.map(cell => { const Tag = cell.header ? 'th' : 'td';
        return <Tag key={cell.column} rowSpan={cell.rowspan} colSpan={cell.colspan}>
          <Runs runs={cell.runs} /></Tag>;
      })}</tr>)}</tbody></table></div>;
}
export const Caption = ({ content }: Props) => <p className="caption-text"><Runs runs={content.runs} /></p>;
export const Footnote = ({ content }: Props) => <p className="footnote-text"><Runs runs={content.runs} /></p>;
export const Reference = ({ content }: Props) => <p className="reference-text"><Runs runs={content.runs} /></p>;
export const SourceRegion = ({ content }: Props) => <SourceImage url={content.image_url} />;

const renderers: Record<BlockType, ComponentType<Props>> = {
  title: Title, authors: Authors, heading: Heading, paragraph: Paragraph, list: List,
  equation: Equation, figure: Figure, table: Table, caption: Caption, footnote: Footnote,
  reference: Reference, source_region: SourceRegion,
};
export function BlockView({ block, onInspect, selected = false }: {
  block: DocumentBlock; onInspect?(block: DocumentBlock): void; selected?: boolean;
}) {
  const Component = renderers[block.type];
  return <section id={`block-${block.id}`} tabIndex={onInspect ? 0 : undefined} aria-label={onInspect ? `${block.type} 블록` : undefined}
    className={`document-block ${block.type} ${selected ? 'source-selected' : ''}`} data-block-type={block.type} data-block-id={block.id}>
    <div className="block-body"><Component content={block.content} /></div>
    {onInspect && block.source_regions?.length ? <button className="inspect-button" onClick={() => onInspect(block)}>원본 확인</button> : null}
    {onInspect && block.source_error ? <small className="source-error">{block.source_error}</small> : null}
    {!onInspect && block.content.image_url ? <details className="source-comparison">
      <summary>원본 대조</summary><SourceImage url={block.content.image_url} />
    </details> : null}
    {!onInspect && block.source_trace.length ? <details className="source-trace"><summary>원문 참조</summary>
      {block.source_trace.map(ref => <p key={ref.id}><small>원본 {ref.page}쪽 · {ref.id}</small><br />{ref.text}</p>)}
    </details> : null}
  </section>;
}
