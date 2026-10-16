import { useEffect, useState } from 'react';
import type { ConnectionStatus, PairperApi } from '../types';

export function useConnection(api: PairperApi) {
  const [connection, setConnection] = useState<ConnectionStatus | null>(null);
  useEffect(() => {
    let active = true;
    let loading = false;
    async function refresh() {
      if (!active || loading) return;
      loading = true;
      try { const data = await api.connectionStatus(); if (active) setConnection(data); }
      catch { if (active) setConnection(null); }
      finally { loading = false; }
    }
    void refresh();
    const timer = setInterval(() => { void refresh(); }, 10000);
    const unsubscribe = api.onConnectionChanged?.(() => { void refresh(); });
    return () => { active = false; clearInterval(timer); unsubscribe?.(); };
  }, [api]);
  return [connection, setConnection] as const;
}
