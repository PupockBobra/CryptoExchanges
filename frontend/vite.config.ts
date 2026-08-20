import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// ⚠️ Do NOT add `build.rollupOptions.output.manualChunks` for plotly.  Rollup
// already splits it into its own chunk shared by the lazy chart pages, and it
// stays purely dynamic — but naming it in manualChunks makes Vite emit a
// `<link rel="modulepreload">` for it in index.html, so all 1.46 MB (gzipped)
// gets fetched on every visit, including pages that show no chart at all.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': { target: 'http://backend:8000', changeOrigin: true },
      '/ws': { target: 'ws://backend:8000', ws: true },
      '/health': { target: 'http://backend:8000', changeOrigin: true },
    },
  },
})
