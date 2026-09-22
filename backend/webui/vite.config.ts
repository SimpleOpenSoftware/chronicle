import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'

export default defineConfig({
  plugins: [react()],
  // Generated contracts live outside the app directory; resolve their runtime
  // through this app's installed dependency in both local and container builds.
  resolve: { dedupe: ['@bufbuild/protobuf'] },
  base: process.env.VITE_BASE_PATH || '/',
  server: {
    fs: {
      allow: [process.cwd(), path.resolve(process.cwd(), '../..', 'contracts'), '/contracts'],
    },
    port: 5173,
    host: '0.0.0.0',
    allowedHosts: process.env.VITE_ALLOWED_HOSTS
      ? process.env.VITE_ALLOWED_HOSTS.split(' ').map(host => host.trim()).filter(host => host.length > 0)
      : [
          'localhost',
          '127.0.0.1',
          '.nip.io'
        ],
    hmr: {
      port: 5173,
      // Allow HMR to work through proxy
      clientPort: process.env.VITE_HMR_PORT ? parseInt(process.env.VITE_HMR_PORT) : undefined,
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})
