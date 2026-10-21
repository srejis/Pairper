import { Fragment, useMemo, type ComponentType, type ReactNode } from 'react';
import katex from 'katex';
import { ExplanationBlockContext, ExplanationTrigger, useExplanationTargets } from './reader/ExplanationContext';
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
  const markup = useMemo(() => {
    if (!verified || !latex) return null;
    try { return katex.renderToString(latex, {
      displayMode: display, throwOnError: true, trust: false, strict: 'error', maxExpand: 1000,
    }); } catch { return null; }
  }, [verified, latex, display]);
  // Only KaTeX output reaches this sink, with trust disabled. Original prose is React text.
  return markup ? <span className="rendered-math" dangerouslySetInnerHTML={{ __html: markup }} />
    : <span className="math-fallback"><SourceImage url={image} inline={!display} /></span>;
}

export function Runs({ runs, field = 'runs' }: { runs: Run[]; field?: string }) {
  return runs.map((run, index) => <Fragment key={index}>
    {run.prefix}<RunContent run={run} field={field} index={index} />
  </Fragment>);
}
function RunContent({run, field, index}: {run: Run; field: string; index: number}) {
  const {targets} = useExplanationTargets(field, index);
  if (run.kind === 'math') {
    const math = <MathContent latex={run.latex} verified={run.source_verified} image={run.image_url} />;
    const target = targets.find(item => item.kind === 'math' && (item.anchor_text ?? item.source_text) === run.latex);
    return target ? <ExplanationTrigger target={target}>{math}</ExplanationTrigger> : math;
  }
  // Python anchor offsets count Unicode code points, not UTF-16 code units.
  const characters = Array.from(run.text);
  const valid = targets.filter(item => (item.kind === 'term' || item.anchor_type === 'text_math') && item.start !== null && item.end !== null
    && item.start >= 0 && item.end > item.start && item.end <= characters.length
    && characters.slice(item.start, item.end).join('') === (item.anchor_text ?? item.source_text))
    .sort((a, b) => a.start! - b.start!);
  // A reviewed whole relation owns its text fragments. Never let a glossary
  // occurrence consume the variable while the remaining relation opens math.
  const formulas = valid.filter(item => item.anchor_type === 'text_math');
  const matches = valid.filter(item => item.kind !== 'term' || !formulas.some(formula =>
    formula.start! < item.end! && item.start! < formula.end!));
  const pieces: ReactNode[] = [];
  let cursor = 0;
  for (const target of matches) {
    if (target.start! < cursor) continue;
    if (target.start! > cursor) pieces.push(characters.slice(cursor, target.start!).join(''));
    pieces.push(<ExplanationTrigger key={`${target.id}:${target.block_id}:${target.field}:${target.run_index}:${target.start}:${target.end}`} target={target}>{target.anchor_text ?? target.source_text}</ExplanationTrigger>);
    cursor = target.end!;
  }
  if (cursor < characters.length) pieces.push(characters.slice(cursor).join(''));
  return <StyledText run={run}>{pieces.length ? pieces : run.text}</StyledText>;
}

function StyledText({ run, children }: { run: Extract<Run, { kind: 'text' }>; children?: ReactNode }) {
  let body: ReactNode = children ?? run.text;
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
  return <Tag>{content.items.map((runs, i) => <li key={i}><Runs runs={runs} field={`items/${i}`} /></li>)}</Tag>;
}
export function Equation({ content }: Props) {
  const {targets} = useExplanationTargets('latex', null);
  const target = targets.find(item => item.kind === 'math' && (item.anchor_text ?? item.source_text) === content.latex);
  const math = <MathContent latex={content.latex} verified={content.source_verified} image={content.image_url} display />;
  return <div className="equation-with-explanation"><div className="equation-scroll">{target
    ? <ExplanationTrigger target={target} display>{math}</ExplanationTrigger> : math}
    {content.number ? <span className="equation-number">{content.number}</span> : null}</div>
    {target ? <ExplanationTrigger target={target} compact>해설</ExplanationTrigger> : null}</div>;
}
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
          <Runs runs={cell.runs} field={`cells/${content.cells.indexOf(cell)}`} /></Tag>;
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
      {' '.repeat(line.indent)}<Runs runs={line.runs} field={`code_lines/${index}`} />{index < lines.length - 1 ? '\n' : ''}
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
    <ExplanationBlockContext.Provider value={block.id}><div className="block-body"><Component content={block.content} />{children}</div></ExplanationBlockContext.Provider>
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
