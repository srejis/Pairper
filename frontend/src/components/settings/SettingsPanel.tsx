import { useEffect, useState } from 'react';
import type { ConnectionStatus, PairperApi } from '../../types';
import { Overlay } from '../common/Overlay';

export function SettingsPanel({ api, connection, onConnection, onClose }: {
  api: PairperApi; connection: ConnectionStatus | null;
  onConnection(status: ConnectionStatus): void; onClose(): void;
}) {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [reconnecting, setReconnecting] = useState(false);
  useEffect(() => {
    let active = true;
    void api.autostartStatus().then(s => { if (active) setEnabled(s.enabled); })
      .catch(() => { if (active) setError('자동 실행 상태를 확인하지 못했습니다. 설정을 다시 열어 주세요.'); });
    return () => { active = false; };
  }, [api]);
  async function toggle() {
    if (busy || enabled === null) return;
    setBusy(true); setError('');
    try { setEnabled((await api.setAutostart(!enabled)).enabled); }
    catch { setError('자동 실행 설정을 변경하지 못했습니다. Windows 예약 작업의 권한을 확인해 주세요.'); }
    finally { setBusy(false); }
  }
  async function reconnect() {
    setReconnecting(true); setError('');
    try {
      if (!api.reconnectTunnel) throw new Error();
      onConnection(await api.reconnectTunnel());
    } catch { setError('다시 연결하지 못했습니다. 연결 진단을 확인해 주세요.'); }
    finally { setReconnecting(false); }
  }
  return <Overlay label="설정" kind="settings" onClose={onClose}>
    <section className="settings-section"><h3>앱 실행</h3>
      <label className="setting-row"><span><strong>로그인 시 자동 실행</strong><small>로그인 20초 후 트레이에서 실행합니다.</small></span>
        <input className="switch" type="checkbox" role="switch" aria-label="로그인 시 자동 실행" checked={enabled ?? false} disabled={enabled === null || busy} onChange={() => { void toggle(); }} /></label>
      <p className="setting-help">{busy ? '변경 중…' : enabled === null ? '상태 확인 중' : enabled ? '켜짐' : '꺼짐'} · 꺼도 실행 중인 Pairper는 유지됩니다.</p>
    </section>
    <section className="settings-section"><h3>ChatGPT 연결</h3>
      <p className={connection?.ready ? 'connection-ready' : 'connection-pending'}>{connection?.message ?? '연결 상태를 확인하지 못했습니다.'}</p>
      <div className="setting-actions"><button disabled={reconnecting} onClick={() => { void reconnect(); }}>{reconnecting ? '요청 중…' : '다시 연결'}</button>
        <button onClick={() => { void api.openDiagnostics().catch(() => setError('연결 진단을 열지 못했습니다.')); }}>연결 진단</button></div>
      <details className="diagnostic-details"><summary>연결 상세</summary>
        <p>최근 정상 확인: {connection?.last_healthy_at ? new Date(connection.last_healthy_at).toLocaleString('ko-KR') : '기록 없음'}</p>
        <p>최근 도구 호출: {connection?.last_tunnel_tool ?? '기록 없음'}</p>
        <p>로컬 연결 상태와 ChatGPT의 실제 호출 확인은 별개입니다.</p>
      </details>
    </section>
    {error ? <p className="inline-error" role="alert">{error}</p> : null}
  </Overlay>;
}
