import { useCallback, useEffect, useState } from 'react';
import type { ConnectionStatus, PairperApi, Paper } from './types';
import { PaperLibrary, statusLabels } from './components/PaperLibrary';
import { PaperReader } from './components/PaperReader';

export function App({ api }: { api: PairperApi }) {
  const [papers, setPapers] = useState<Paper[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [connection, setConnection] = useState<ConnectionStatus | null>(null);
  const [importing, setImporting] = useState(false);
  const [notice, setNotice] = useState('');
  const refresh = useCallback(async () => {
    try {
      const list = await api.listPapers();
      setPapers(list);
      setSelected(previous => list.some(p => p.id === previous) ? previous : list[0]?.id ?? null);
    } catch (e) { setNotice((e as Error).message); }
  }, [api]);
  useEffect(() => {
    void refresh();
    return api.onLibraryChanged(() => { void refresh(); });
  }, [api, refresh]);
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    let loading = false;
    async function update() {
      if (loading || !active) return;
      loading = true;
      try { const status = await api.connectionStatus(); if (active) setConnection(status); }
      catch { if (active) setConnection(null); }
      finally { loading = false; }
    }
    function poll() { void update(); timer = setTimeout(poll, 10000); }
    const unsubscribe = api.onConnectionChanged?.(() => { void update(); });
    poll();
    return () => { active = false; clearTimeout(timer); unsubscribe?.(); };
  }, [api]);
  async function importPdfs() {
    setImporting(true);
    setNotice('PDF를 선택해 주세요. 등록 중에는 원본을 복사하고 추출합니다.');
    try {
      const result = await api.importPdfs();
      setNotice(result.imported || result.failed ? `${result.imported}개 등록 확인 · ${result.failed}개 실패` : 'PDF 선택을 취소했습니다.');
      await refresh();
    } catch (e) { setNotice((e as Error).message); }
    finally { setImporting(false); }
  }
  const paper = papers.find(p => p.id === selected);
  return <div className="app-shell">
    <header className="app-header"><div className="brand">Pairper<span>논문을 읽는 새로운 흐름</span></div>
      <div className="header-actions"><div className={`connection ${connection?.ready ? 'ready' : ''}`}>
        <span>{connection?.message ?? 'MCP 상태 확인 중'}</span>
        {connection?.last_healthy_at ? <small>최근 정상 확인 {new Date(connection.last_healthy_at).toLocaleTimeString('ko-KR')}</small> : null}
        {connection?.last_tunnel_tool_at ? <small>터널 도구 호출 {connection.last_tunnel_tool} · ChatGPT 발신은 별도 확인</small> : null}
      </div><button type="button" onClick={() => { void api.openDiagnostics().catch(e => setNotice(e.message)); }}>연결 진단</button>
      <button type="button" onClick={() => {
        void api.reconnectTunnel?.().then(setConnection).catch(e => setNotice(e.message));
      }}>다시 연결</button>
      <button type="button" className="primary" disabled={importing} onClick={() => { void importPdfs(); }}>{importing ? 'PDF 등록 중…' : '+ PDF 추가'}</button></div>
    </header>
    <div className="workspace"><PaperLibrary papers={papers} selected={selected} onSelect={setSelected} />
      <div className="reading-pane"><div className="paper-toolbar"><span>{paper?.title ?? '라이브러리'}</span>
        {paper ? <small>{paper.page_count}페이지 · {statusLabels[paper.status]}</small> : null}</div>
        <PaperReader paper={paper} api={api} />
      </div></div>
    <footer role="status" aria-live="polite">{notice || '원본 PDF와 문서의 연결은 로컬 저장소에 보관됩니다.'}</footer>
  </div>;
}
