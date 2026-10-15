import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { BlockContent, BlockType, DocumentBlock } from '../types';
import { BlockView, MathContent, Runs, SourceImage, Table } from './blocks';

const image = `file:///library/assets/${'a'.repeat(32)}.png`;
const text = { kind: 'text' as const, prefix: '', text: 'Original 42 units.' };
const content: BlockContent = {
  runs: [text], items: [[text]], cells: [{ row: 0, column: 0, rowspan: 2, colspan: 3, header: true, runs: [text] }],
  level: 2, ordered: false, source_verified: true, image_url: image, latex: 'x=42', number: '(1)', caption_id: null,
};
describe('document rendering contract', () => {
  const types: BlockType[] = ['title','authors','heading','paragraph','list','equation','figure','table','caption','footnote','reference','source_region'];
  it.each(types)('renders %s with source comparison', type => {
    const block: DocumentBlock = { id: 'b', order: 0, type, validation_status: 'source_verified', content, source_trace: [] };
    const view = render(<BlockView block={block} />);
    expect(view.container.querySelector(`[data-block-type="${type}"]`)).toBeInTheDocument();
    expect(screen.getByText('원본 대조')).toBeInTheDocument();
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
  it('refuses external and arbitrary file image URLs', () => {
    const { container } = render(<><SourceImage url="https://example.com/image.png" />
      <SourceImage url="file:///C:/Users/secret.png" /></>);
    expect(container.querySelectorAll('img')).toHaveLength(0);
    expect(screen.getAllByRole('alert')).toHaveLength(2);
  });
});
