import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
  },
  build: {
    outDir: 'dist',
  },
  preview: {
    port: 14173,
    host: true,
    // Allow nginx to proxy the public hostname to `vite preview`.
    // Must be the boolean `true` (or an array of hostnames) — the string
    // 'all' is not a valid value in Vite 5.4.12+ and leaves the host blocked.
    allowedHosts: true,
  },
})
