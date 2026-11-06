import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'
import fs from 'node:fs'

/**
 * The build number of the bundle being produced, read from the same file the server
 * reports in /healthz.
 *
 * Baked in at build time on purpose. If the footer only asked the server, a browser
 * running a stale bundle would happily print the *server's* new build number while
 * executing old code — the user concludes the feature was never shipped. Comparing this
 * constant against the server's answer is what makes a stale tab detectable.
 */
function bundleBuild(): string {
  try {
    const raw = fs.readFileSync(path.resolve(__dirname, '../src/ttsgrab/build_info.json'), 'utf8')
    const info = JSON.parse(raw) as { build?: number; commit?: string }
    return `${info.build ?? 'dev'}+${info.commit ?? 'nogit'}`
  } catch {
    return 'dev'
  }
}

// The built SPA is copied into the Python package so FastAPI can serve it as static
// files from a single container — no nginx, no second process.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '@': path.resolve(__dirname, './src') },
  },
  define: {
    __BUNDLE_BUILD__: JSON.stringify(bundleBuild()),
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
