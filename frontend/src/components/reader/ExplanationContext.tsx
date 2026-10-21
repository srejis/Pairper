import { createContext, useContext, type ReactNode } from 'react';
import type { ExplanationTarget } from '../../types';

export const ExplanationContext = createContext<{
  targets: ExplanationTarget[]; selected: string | null;
  onSelect(target: ExplanationTarget, element: HTMLElement): void;
}>({targets: [], selected: null, onSelect: () => {}});
export const ExplanationBlockContext = createContext('');

export function useExplanationTargets(field: string, runIndex: number | null) {
  const context = useContext(ExplanationContext);
  const block = useContext(ExplanationBlockContext);
  return {...context, targets: context.targets.filter(target => target.block_id === block
    && target.field === field && target.run_index === runIndex)};
}
export function ExplanationTrigger({target, children, display = false, compact = false}: {
  target: ExplanationTarget; children: ReactNode; display?: boolean; compact?: boolean;
}) {
  const {selected, onSelect} = useContext(ExplanationContext);
  const active = selected === target.id;
  const native = display || compact;
  const Tag = native ? 'button' : 'span';
  return <Tag type={native ? 'button' : undefined} role={native ? undefined : 'button'} tabIndex={native ? undefined : 0} className={`explanation-trigger ${compact ? 'equation-explanation-shortcut' : display ? 'display-explanation-trigger' : target.kind === 'math' ? 'inline-math-trigger' : 'term-trigger'}${active ? ' selected' : ''}`}
    aria-label={`${target.kind === 'term' ? target.source_text : '수식'} 해설`}
    aria-expanded={active} aria-controls="explanation-panel" data-explanation-id={target.id}
    onClick={event => onSelect(target, event.currentTarget)}
    onKeyDown={event => {
      if (!native && (event.key === 'Enter' || event.key === ' ')) {
        event.preventDefault();
        onSelect(target, event.currentTarget);
      }
    }}>
    {children}
  </Tag>;
}
