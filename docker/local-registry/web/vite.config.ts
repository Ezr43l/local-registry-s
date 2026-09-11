import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// base './' — la página la sirve el propio server.py desde /app/web, y así los
// enlaces a los ficheros construidos son relativos y no dependen de la ruta.
//
// El proxy solo actúa en `npm run dev`, para poder trabajar en la interfaz desde
// el PC apuntando a un nodo de verdad:
//   VITE_API=http://127.0.0.1:5001 npm run dev
export default defineConfig({
  base: './',
  plugins: [react()],
  build: { outDir: 'dist', emptyOutDir: true },
  server: {
    proxy: {
      '/api': {
        target: process.env.VITE_API || 'http://127.0.0.1:5001',
        changeOrigin: true,
      },
    },
  },
})
