import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { BlockContent, BlockType, DocumentBlock } from '../types';
import { BlockView, Code, DocumentBlocks, MathContent, Runs, SourceImage, Table } from './blocks';

const image = `file:///library/assets/${'a'.repeat(32)}.png`;
const text = { kind: 'text' as const, prefix: '', text: 'Original 42 units.' };
const content: BlockContent = {
  runs: [text], items: [[text]], cells: [{ row: 0, column: 0, rowspan: 2, colspan: 3, header: true, runs: [text] }],
  level: 2, ordered: false, source_verified: true, image_url: image, latex: 'x=42', number: '(1)', caption_id: null,
};

describe('source-backed compound regions', () => {
  it('renders nested proof content once in source order', () => {
    const blocks: DocumentBlock[] = [
      { id: 'proof', type: 'group', order: 0, validation_status: 'source_verified',
        content: { ...content, runs: [], group_role: 'proof' } },
      { id: 'p', type: 'paragraph', order: 1, parent_id: 'proof',
        validation_status: 'source_verified', content },
      { id: 'next', type: 'paragraph', order: 2, validation_status: 'source_verified',
        content: { ...content, runs: [{ ...text, text: 'Next paragraph' }] } },
    ];
    const { container } = render(<DocumentBlocks blocks={blocks} />);
    expect(container.querySelector('[data-group-role="proof"] [data-block-id="p"]')).toBeInTheDocument();
    expect(screen.getAllByText('Original 42 units.')).toHaveLength(1);
    expect(container.querySelector('[data-group-role="proof"] [data-block-id="next"]')).toBeNull();
  });
  it('shows an error for broken hierarchy without dropping or duplicating content', () => {
    render(<DocumentBlocks blocks={[{ id: 'cycle', type: 'group', parent_id: 'cycle', order: 0,
      validation_status: 'source_verified', content: { ...content, group_role: 'example' } }]} />);
    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(screen.queryByText('Original 42 units.')).toBeNull();
  });
  it('preserves printed prompt symbols and inline emphasis as escaped text', () => {
    const { container } = render(<Runs runs={[{ kind: 'text', prefix: '',
      text: '<script>{user_task}</script> **printed**', bold: true, italic: true }]} />);
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('strong em, em strong')).toHaveTextContent('**printed**');
    expect(container).toHaveTextContent('{user_task}');
  });
});
describe('document rendering contract', () => {
  const types: BlockType[] = ['title','authors','heading','paragraph','list','equation','figure','table','caption','footnote','reference','source_region'];
  it.each(types)('renders %s without exposing internal source controls', type => {
    const block: DocumentBlock = { id: 'b', order: 0, type, validation_status: 'source_verified', content };
    const view = render(<BlockView block={block} />);
    expect(view.container.querySelector(`[data-block-type="${type}"]`)).toBeInTheDocument();
    expect(screen.queryByText('원본 대조')).toBeNull();
    expect(screen.queryByText('원문 참조')).toBeNull();
    expect(view.container.querySelector('details')).toBeNull();
  });
  it('renders merged cells using exact source text', () => {
    render(<Table content={content} />);
    const cell = screen.getByRole('columnheader');
    expect(cell).toHaveAttribute('rowspan','2');
    expect(cell).toHaveAttribute('colspan','3');
    expect(cell).toHaveTextContent('Original 42 units.');
  });
  it('uses original images for invalid or unverified math', () => {
    const { container } = render(<><MathContent latex={'\\invalidCommand{42}'} verified image={image} />
      <MathContent latex="x=42" verified={false} image={image} /></>);
    expect(container.querySelectorAll('.math-fallback')).toHaveLength(2);
    expect(container.querySelectorAll('.katex')).toHaveLength(0);
  });
  it('preserves backend prefixes and escapes source HTML', () => {
    const { container } = render(<p><Runs runs={[text, { kind:'text',prefix:' ',text:'<script>danger()</script>' }]} /></p>);
    expect(container.textContent).toBe('Original 42 units. <script>danger()</script>');
    expect(container.querySelector('script')).toBeNull();
  });
  it('preserves source code indentation and escapes prompt tokens', () => {
    const { container } = render(<Code content={{ ...content, code_lines: [
      { indent: 0, runs: [text] },
      { indent: 4, runs: [{ kind: 'text', prefix: '', text: '<s>[INST] True [/INST]' }] },
    ] }} />);
    expect(container.querySelector('code')?.textContent).toBe('Original 42 units.\n    <s>[INST] True [/INST]');
    expect(container.querySelector('s')).toBeNull();
    expect(container.querySelector('img')).toBeNull();
  });
  it('renders a source footnote marker separately from adjacent math', () => {
    const { container } = render(<Runs runs={[{ kind: 'text', prefix: '', text: '3', superscript: true }]} />);
    expect(container.querySelector('sup')).toHaveTextContent('3');
    expect(container.querySelector('.katex')).toBeNull();
  });
  it('refuses external and arbitrary file image URLs', () => {
    const { container } = render(<><SourceImage url="https://example.com/image.png" />
      <SourceImage url="file:///C:/Users/secret.png" /></>);
    expect(container.querySelectorAll('img')).toHaveLength(0);
    expect(screen.getAllByRole('alert')).toHaveLength(2);
  });
});
