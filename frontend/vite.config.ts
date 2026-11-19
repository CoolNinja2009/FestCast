import { defineConfig } from 'vite';

const target = 'http://127.0.0.1:8000';
export default defineConfig({
  server: { port: 5173, proxy: { '/api': { target, ws: true }, '/obs': target, '/join': target, '/tv': target } },
  build: { outDir: 'dist', emptyOutDir: true, chunkSizeWarningLimit: 800 },
});
