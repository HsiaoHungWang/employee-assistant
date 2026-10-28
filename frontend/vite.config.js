import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 開發時把 /api 轉給 FastAPI：前端和後端同源，不必處理 CORS
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: { '/api': 'http://127.0.0.1:8000' },
  },
})
