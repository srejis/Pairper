const paths = {
  file: 'M6 3h8l4 4v14H6V3Zm8 0v5h5M9 12h6M9 16h6',
  plus: 'M12 5v14M5 12h14', back: 'm14 6-6 6 6 6', arrow: 'm9 6 6 6-6 6',
  search: 'M16 16l5 5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
  filter: 'M4 6h16M7 12h10M10 18h4', close: 'm6 6 12 12M18 6 6 18',
  settings: 'M4 7h16M4 17h16M9 4v6M15 14v6',
  toc: 'M9 6h11M9 12h11M9 18h11M4 6h.1M4 12h.1M4 18h.1',
  type: 'M3 5h12M9 5v15M5 20h8M15 14h6M18 11v9',
  more: 'M5 12h.1M12 12h.1M19 12h.1',
  check: 'm5 12 4 4L19 6', clock: 'M12 7v5l3 2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0',
  warning: 'm12 3 10 18H2L12 3Zm0 6v5m0 3h.1', copy: 'M8 8h12v13H8V8ZM4 16V3h12',
  source: 'M3 4h7v16H3V4Zm11 0h7v16h-7V4Z',
} as const;
export function Icon({ name, size = 20 }: { name: keyof typeof paths; size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name]} /></svg>;
}
