import { useEffect, useRef, useState, type ReactNode } from 'react';
import { Icon } from './Icon';

export function Overlay({ label, onClose, children, kind = 'panel' }: {
  label: string; onClose(): void; children: ReactNode; kind?: 'panel' | 'popover' | 'settings';
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [closing, setClosing] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const element = dialog.current!;
    element.showModal();
    return () => { clearTimeout(timer.current); element.close(); previous?.focus({ preventScroll: true }); };
  }, []);
  function requestClose() {
    if (closing) return;
    setClosing(true);
    // Honor reduced motion while retaining the same focus restoration path.
    const duration = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 0 : 200;
    timer.current = setTimeout(() => close.current(), duration);
  }
  return <dialog ref={dialog} className={`overlay overlay-${kind} ${closing ? 'closing' : ''}`} aria-label={label}
    onCancel={event => { event.preventDefault(); requestClose(); }}
    onClick={event => {
      if (event.target !== event.currentTarget) return;
      const box = event.currentTarget.getBoundingClientRect();
      if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) requestClose();
    }}>
    <div className="overlay-heading"><h2>{label}</h2><button className="icon-button" aria-label={`${label} 닫기`} onClick={requestClose}><Icon name="close" /></button></div>
    {children}
  </dialog>;
}
