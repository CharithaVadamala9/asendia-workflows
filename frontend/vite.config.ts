import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Proxy API calls to FastAPI so the frontend has no base-URL configuration
    // and no CORS considerations in development.
    proxy: { '/api': { target: 'http://localhost:8000', changeOrigin: true } },
  },
})
