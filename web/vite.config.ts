import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'

// The built SPA is copied into the Python package so FastAPI can serve it as static
// files from a single container — no nginx, no second process.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '@': path.resolve(__dirname, './src') },
  },
  build: {
    outDir: path.resolve(__dirname, '../src/ttsgrab/web'),
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    port: 5173,
    proxy: {
      '/v1': { target: 'http://127.0.0.1:8300', changeOrigin: true },
      '/healthz': 'http://127.0.0.1:8300',
      '/readyz': 'http://127.0.0.1:8300',
      '/metrics': 'http://127.0.0.1:8300',
      '/openapi.json': 'http://127.0.0.1:8300',
    },
  },
})
