/**
 * Vite config for the standalone viewer3d benchmark harness.
 *
 * Kept separate from the application config so the harness never becomes a
 * second entry point of the shipped bundle. Build output goes outside the repo
 * by default (see scripts/run-viewer3d-benchmark.sh).
 */
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('.', import.meta.url));
const outDir = process.env.VIEWER3D_BENCH_OUT ?? fileURLToPath(new URL('./.bench-dist', import.meta.url));

export default defineConfig({
  root,
  base: '/',
  plugins: [react()],
  server: { host: '127.0.0.1', port: 8811, strictPort: true },
  preview: { host: '127.0.0.1', port: 8811, strictPort: true },
  build: {
    outDir,
    emptyOutDir: true,
    sourcemap: false,
    chunkSizeWarningLimit: 4096,
    rollupOptions: {
      input: fileURLToPath(new URL('./viewer3d-harness.html', import.meta.url)),
    },
  },
});
