import type { PairperApi } from './types';

interface QtSignal {
  connect(fn: (...args: string[]) => void): void;
  disconnect(fn: (...args: string[]) => void): void;
}
interface QtBridge {
  reply: QtSignal; libraryChanged: QtSignal; connectionChanged: QtSignal;
  listPapers(id: string): void;
  getDocument(id: string, paperId: string): void;
  connectionStatus(id: string): void;
  importPdfs(id: string): void;
  openDiagnostics(id: string): void;
  reconnectTunnel(id: string): void;
  autostartStatus(id: string): void;
  setAutostart(id: string, enabled: boolean): void;
  copyUpdateRequest(id: string, paperId: string): void;
  sourceRegion(id: string, paperId: string, viewId: string, blockId: string, regionId: string): void;
}
declare global {
  interface Window {
    qt?: { webChannelTransport: unknown };
    QWebChannel?: new (transport: unknown, callback: (channel: { objects: { pairper: QtBridge } }) => void) => unknown;
  }
}

let connection: Promise<PairperApi> | undefined;
export function connectBridge(): Promise<PairperApi> {
  return connection ??= new Promise((resolve, reject) => {
    if (!window.qt || !window.QWebChannel) {
      reject(new Error('Pairper 데스크톱 앱에서 화면을 열어 주세요.'));
      return;
    }
    new window.QWebChannel(window.qt.webChannelTransport, channel => {
      const bridge = channel.objects.pairper;
      const pending = new Map<string, { resolve: (data: unknown) => void; reject: (error: Error) => void; timer: ReturnType<typeof setTimeout> }>();
      let sequence = 0;
      bridge.reply.connect((id, value) => {
        const item = pending.get(id);
        if (!item) return;
        pending.delete(id);
        clearTimeout(item.timer);
        try {
          const result = JSON.parse(value);
          if (result.ok) item.resolve(result.data);
          else item.reject(new Error(result.error));
        } catch { item.reject(new Error('데스크톱 응답 형식을 확인할 수 없습니다.')); }
      });
      function call<T>(method: 'listPapers' | 'getDocument' | 'connectionStatus' | 'importPdfs' | 'openDiagnostics' | 'reconnectTunnel' | 'autostartStatus' | 'setAutostart' | 'sourceRegion' | 'copyUpdateRequest', argument?: string | boolean | string[]): Promise<T> {
        const id = `${method === 'importPdfs' ? 'import' : 'request'}_${++sequence}`;
        return new Promise((res, rej) => {
          const timer = setTimeout(() => { pending.delete(id); rej(new Error('데스크톱 응답이 지연되고 있습니다.')); }, method === 'importPdfs' ? 600000 : 60000);
          pending.set(id, { resolve: data => res(data as T), reject: rej, timer });
          if (method === 'getDocument') bridge.getDocument(id, argument as string);
          else if (method === 'copyUpdateRequest') bridge.copyUpdateRequest(id, argument as string);
          else if (method === 'setAutostart') bridge.setAutostart(id, argument as boolean);
          else if (method === 'sourceRegion') {
            const [paperId, viewId, blockId, regionId] = argument as string[];
            bridge.sourceRegion(id, paperId, viewId, blockId, regionId);
          }
          else bridge[method](id);
        });
      }
      resolve({
        copyUpdateRequest: paperId => call('copyUpdateRequest', paperId),
        sourceRegion: (paperId, viewId, blockId, regionId) => call('sourceRegion', [paperId, viewId, blockId, regionId]),
        autostartStatus: () => call('autostartStatus'),
        setAutostart: enabled => call('setAutostart', enabled),
        listPapers: () => call('listPapers'),
        getDocument: id => call('getDocument', id),
        connectionStatus: () => call('connectionStatus'),
        importPdfs: () => call('importPdfs'),
        openDiagnostics: () => call('openDiagnostics'),
        reconnectTunnel: () => call('reconnectTunnel'),
        onConnectionChanged: listener => {
          bridge.connectionChanged.connect(listener);
          return () => bridge.connectionChanged.disconnect(listener);
        },
        onLibraryChanged: listener => {
          bridge.libraryChanged.connect(listener);
          return () => bridge.libraryChanged.disconnect(listener);
        },
      });
    });
  });
}
