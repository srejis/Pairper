import { useEffect, useRef } from 'react';
import type { Explanation, ExplanationSegment, ExplanationTarget } from '../../types';
import { MathContent } from '../blocks';

function Segments({segments}: {segments: ExplanationSegment[]}) {
  return <>{segments.map((segment, index) => segment.kind === 'math'
    ? <MathContent key={index} latex={segment.latex} verified image={null} />
    : <span key={index}>{segment.text}</span>)}</>;
}
export function ExplanationPanel({target, explanation, onClose}: {
  target: ExplanationTarget | null; explanation: Explanation | undefined; onClose(): void;
}) {
  const heading = useRef<HTMLHeadingElement>(null);
  const panel = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!target) return;
    if (panel.current) panel.current.scrollTop = 0;
    heading.current?.focus({preventScroll:true});
  }, [target?.id]);
  useEffect(() => {
    if (!target) return;
    const close = (event: KeyboardEvent) => {if (event.key === 'Escape') {event.preventDefault(); onClose();}};
    window.addEventListener('keydown', close);
    return () => window.removeEventListener('keydown', close);
  }, [target, onClose]);
  const visible = target && explanation && (explanation.kind !== 'math' || explanation.math_role !== 'notation');
  return <aside ref={panel} id="explanation-panel" className={`explanation-panel${visible ? ' is-open' : ''}`} aria-label="해설">
    {visible ? <>
      <header className="explanation-panel-header"><h2 ref={heading} tabIndex={-1}>해설</h2>
        <button type="button" className="explanation-close" aria-label="해설 닫기" onClick={onClose}>×</button></header>
      <div className="explanation-panel-content" key={target.id}>
        <div className="explanation-subject">{target.kind === 'math' && target.anchor_type !== 'text_math'
          ? <MathContent latex={target.source_text} verified image={null} display /> : target.source_text}</div>
        {explanation.kind === 'term' ? <><p className="explanation-category">{explanation.category}</p>
          <p><Segments segments={explanation.description} /></p></> : <>
          {explanation.symbols.length ? <><h3>기호</h3><dl className="explanation-symbols">{explanation.symbols.map((symbol, index) => <div key={index}>
            <dt><MathContent latex={symbol.symbol} verified image={null} /></dt><dd>{symbol.meaning}</dd></div>)}</dl></> : null}
          <h3>수식의 의미</h3><p><Segments segments={explanation.meaning} /></p>
          {explanation.process.length ? <><h3>계산 과정</h3><p><Segments segments={explanation.process} /></p></> : null}
        </>}
      </div>
    </> : <div className="explanation-panel-idle" aria-hidden="true"><span>해설</span></div>}
  </aside>;
}
