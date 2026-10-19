import { Fragment, useMemo, type ComponentType, type ReactNode } from 'react';
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
    {run.prefix}{run.kind === 'text' ? <StyledText run={run} /> : <MathContent latex={run.latex}
      verified={run.source_verified} image={run.image_url} />}
  </Fragment>);
}

function StyledText({ run }: { run: Extract<Run, { kind: 'text' }> }) {
  let body: ReactNode = run.text;
  if (run.bold) body = <strong>{body}</strong>;
  if (run.italic) body = <em>{body}</em>;
  if (run.underline) body = <u>{body}</u>;
  if (run.superscript) body = <sup>{body}</sup>;
  return body;
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
export const Figure = ({ content }: Props) => <figure aria-describedby={content.caption_id ? `block-${content.caption_id}` : undefined}>
  <SourceImage url={content.image_url} />
  {content.translation_labels?.length ? <details className="figure-translations"><summary>그림 안의 한국어 번역</summary>
    <dl>{content.translation_labels.map((label, index) => <div key={index}><dt>{label.source}</dt><dd>{label.target}</dd></div>)}</dl>
  </details> : null}
</figure>;
export function Table({ content }: Props) {
  if (!content.source_verified || !content.cells.length) return <div className="source-scroll" role="region" aria-label="논문 표" tabIndex={0}><SourceImage url={content.image_url} /></div>;
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
export const SourceRegion = ({ content }: Props) => <div className="source-scroll" role="region" aria-label="논문 내용" tabIndex={0}><SourceImage url={content.image_url} /></div>;

export function Code({ content }: Props) {
  const lines = content.code_lines ?? [];
  return <div className="code-scroll"><pre className="code-content"><code data-language={content.language ?? undefined}>
    {lines.map((line, index) => <Fragment key={index}>
      {' '.repeat(line.indent)}<Runs runs={line.runs} />{index < lines.length - 1 ? '\n' : ''}
    </Fragment>)}
  </code></pre></div>;
}

const renderers: Record<BlockType, ComponentType<Props>> = {
  title: Title, authors: Authors, heading: Heading, paragraph: Paragraph, list: List,
  equation: Equation, figure: Figure, table: Table, caption: Caption, footnote: Footnote,
  reference: Reference, source_region: SourceRegion, code: Code,
  group: ({ content }) => content.runs.length ? <div className="group-label"><Runs runs={content.runs} /></div> : null,
};
export function BlockView({ block, children }: { block: DocumentBlock; children?: ReactNode }) {
  const Component = renderers[block.type];
  return <section id={`block-${block.id}`} className={`document-block ${block.type}`}
    data-block-type={block.type} data-block-id={block.id}
    data-group-role={block.content.group_role ?? undefined}>
    <div className="block-body"><Component content={block.content} />{children}</div>
  </section>;
}

type RenderNode = { block: DocumentBlock; children: RenderNode[] };

function buildTree(blocks: DocumentBlock[]): RenderNode[] {
  const ordered = [...blocks].sort((a, b) => a.order - b.order);
  const nodes = new Map<string, RenderNode>();
  const roots: RenderNode[] = [];
  const stack: RenderNode[] = [];
  for (const block of ordered) {
    if (nodes.has(block.id)) throw new Error('문서에 중복된 영역이 있습니다.');
    const node: RenderNode = { block, children: [] };
    const parent = block.parent_id ? nodes.get(block.parent_id) : undefined;
    if (block.parent_id && (!parent || parent.block.type !== 'group')) throw new Error('영역의 연결을 확인할 수 없습니다.');
    while (stack.length && stack[stack.length - 1] !== parent) stack.pop();
    if (parent && stack[stack.length - 1] !== parent) throw new Error('영역 순서가 올바르지 않습니다.');
    (parent ? parent.children : roots).push(node);
    nodes.set(block.id, node);
    if (block.type === 'group') stack.push(node);
    if (stack.length > 32) throw new Error('영역의 중첩이 너무 깊습니다.');
  }
  return roots;
}

function NodeView({ node }: { node: RenderNode }) {
  return <BlockView block={node.block}>{node.children.map(child => <NodeView key={child.block.id} node={child} />)}</BlockView>;
}

export function DocumentBlocks({ blocks }: { blocks: DocumentBlock[] }) {
  const result = useMemo(() => {
    try { return { nodes: buildTree(blocks), error: '' }; }
    catch { return { nodes: [], error: '문서 영역의 연결이 올바르지 않아 표시할 수 없습니다.' }; }
  }, [blocks]);
  return result.error ? <p role="alert">{result.error}</p>
    : result.nodes.map(node => <NodeView key={node.block.id} node={node} />);
}
