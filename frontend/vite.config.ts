import { readFileSync } from 'node:fs';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

let backendPort = 8767;
try {
  const config = JSON.parse(readFileSync(new URL('../var/listen-port.json', import.meta.url), 'utf8')) as { port?: unknown };
  if (typeof config.port === 'number' && Number.isInteger(config.port) && config.port >= 1024 && config.port <= 65535) backendPort = config.port;
} catch {
  // Use the backend's default port when this checkout has no local port file.
}

export default defineConfig({
  plugins: [react()],
  base: '/',
  server: {
    proxy: {
      '/api': {
        target: `http://127.0.0.1:${backendPort}`,
        changeOrigin: true,
        configure: proxy => {
          // The API only accepts same-origin requests; align the dev proxy's
          // Origin with the local backend port.
          proxy.on('proxyReq', proxyRequest => {
            proxyRequest.setHeader('origin', `http://127.0.0.1:${backendPort}`);
          });
        },
      },
    },
  },
  build: { outDir: 'dist', emptyOutDir: true, sourcemap: false },
});
