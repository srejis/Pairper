import { createRoot } from 'react-dom/client';
import 'katex/dist/katex.min.css';
import './styles.css';
import { App } from './App';
import { connectBridge } from './bridge';

const root = createRoot(document.getElementById('root')!);
connectBridge().then(api => root.render(<App api={api} />)).catch((error: Error) => {
  root.render(<main className="empty-state" role="alert"><h1>Pairper 연결을 확인하세요</h1><p>{error.message}</p></main>);
});
