import path from 'path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    proxy: {
      '/api': {
        // 用 127.0.0.1 而非 localhost：Windows 上 localhost 可能先解析为 IPv6 ::1，
        // 而后端 uvicorn 默认只监听 IPv4 127.0.0.1，导致代理转发失败（接口 404/连不上）。
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
