import { lazy, Suspense, useCallback, useEffect, useState } from 'react';
import type { PairperApi, Paper } from './types';
import { LibraryScreen } from './components/library/LibraryScreen';
import { SettingsPanel } from './components/settings/SettingsPanel';
import { Icon } from './components/common/Icon';
import { useConnection } from './hooks/useConnection';

const ReaderScreen = lazy(() => import('./components/reader/ReaderScreen').then(module => ({default: module.ReaderScreen})));

export function App({ api }: { api: PairperApi }) {
  const [papers, setPapers] = useState<Paper[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [screen, setScreen] = useState<'library' | 'reader'>('library');
  const [loading, setLoading] = useState(true);
  const [settings, setSettings] = useState(false);
  const [importing, setImporting] = useState(false);
  const [notice, setNotice] = useState<{ message: string; error: boolean } | null>(null);
  const [connection, setConnection] = useConnection(api);
  const notify = useCallback((message: string, error = false) => setNotice({message, error}), []);
  const refresh = useCallback(async () => {
    try { setPapers(await api.listPapers()); }
    catch { notify('라이브러리를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.', true); }
    finally { setLoading(false); }
  }, [api, notify]);
  useEffect(() => { void refresh(); return api.onLibraryChanged(() => { void refresh(); }); }, [api, refresh]);
  useEffect(() => {
    if (!notice || notice.error) return;
    const timer = setTimeout(() => setNotice(null), 5000);
    return () => clearTimeout(timer);
  }, [notice]);
  async function importPdfs() {
    setImporting(true);
    try {
      const result = await api.importPdfs();
      if (result.imported || result.failed) notify(`${result.imported}개 등록 확인 · ${result.failed}개 실패`, !!result.failed);
      await refresh();
    } catch { notify('PDF를 등록하지 못했습니다. 파일과 저장소 상태를 확인해 주세요.', true); }
    finally { setImporting(false); }
  }
  const paper = papers.find(p => p.id === selected);
  return <div className="app-shell">
    {screen === 'reader' && paper ? <Suspense fallback={<main className="empty-state" role="status">리더를 여는 중…</main>}><ReaderScreen key={paper.id} api={api} paper={paper} onBack={() => setScreen('library')} /></Suspense> : <>
      <header className="app-header"><a className="brand" href="#" onClick={e => { e.preventDefault(); setScreen('library'); }}>Pairper<span className="brand-mark">.</span></a>
        <div className="header-actions"><button className="icon-button" aria-label="설정" onClick={() => setSettings(true)}><Icon name="settings" /></button>
          <button className="primary" disabled={importing} onClick={() => { void importPdfs(); }}><Icon name="plus" size={18} />{importing ? '등록 중…' : 'PDF 추가'}</button></div>
      </header>
      {connection && !connection.ready && !['starting','reconnecting'].includes(connection.state) ? <button className="connection-notice" onClick={() => setSettings(true)}>ChatGPT 연결을 확인해 주세요 <span>설정 보기</span></button> : null}
      <LibraryScreen papers={papers} loading={loading} returnFocusId={selected} onRead={p => { setSelected(p.id); setScreen('reader'); }} onCopy={api.copyUpdateRequest} onNotify={notify} />
      {settings ? <SettingsPanel api={api} connection={connection} onConnection={setConnection} onClose={() => setSettings(false)} /> : null}
    </>}
    {notice ? <div className={`toast ${notice.error ? 'toast-error' : ''}`} role={notice.error ? 'alert' : 'status'}><span>{notice.message}</span><button className="icon-button" aria-label="알림 닫기" onClick={() => setNotice(null)}><Icon name="close" size={16} /></button></div> : null}
  </div>;
}
