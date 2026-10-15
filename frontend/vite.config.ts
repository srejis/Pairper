import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig(({ command }) => ({
  base: './',
  plugins: [react(), ...(command === 'serve' ? [{
    name: 'local-dev-csp',
    transformIndexHtml(html: string) {
      return html.replace("connect-src 'none'", 'connect-src http://127.0.0.1:5173 ws://127.0.0.1:5173')
        .replace("script-src 'self' file: qrc:", "script-src 'self' file: qrc: 'unsafe-inline'");
    },
  }] : [])],
  server: { host: '127.0.0.1', port: 5173, strictPort: true },
  build: { outDir: '../src/pairper/web/frontend', emptyOutDir: true,
    assetsInlineLimit: 0, sourcemap: false },
  test: { environment: 'jsdom', setupFiles: ['./src/test-setup.ts'] },
}));
