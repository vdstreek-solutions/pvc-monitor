import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Host header is passed through unchanged so OIDC callback URLs keep the dev server origin.
const backend = { target: 'http://127.0.0.1:8000' }

export default defineConfig({
  plugins: [react()],
  server: {
    // Listen on loopback only; the dev server is not meant to be reachable from the network.
    host: '127.0.0.1',
    port: 3000,
    strictPort: true,
    proxy: {
      '/api': backend,
      '/auth': backend,
      '/healthz': backend,
    },
  },
})
