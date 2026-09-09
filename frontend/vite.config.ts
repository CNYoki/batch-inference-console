import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// 端口可通过环境变量覆盖，避免和机器上其他 Vite 项目撞车：
//   VITE_DEV_PORT=5190 npm run dev
// 改了端口记得同步后端 .env 的 CORS_ORIGINS 与 FRONTEND_BASE_URL。
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const port = Number(env.VITE_DEV_PORT || 5180)
  const apiTarget = env.VITE_API_TARGET || 'http://127.0.0.1:8000'

  return {
    plugins: [react()],
    server: {
      port,
      // 端口被占用时直接报错退出，而不是悄悄换一个 ——
      // 静默换端口会让后端的 CORS 白名单和 OIDC 回调地址对不上，排查起来很费时间
      strictPort: true,
      proxy: {
        // 开发模式下把 /api 代理到后端，浏览器同源，Cookie 会话直接可用
        '/api': { target: apiTarget, changeOrigin: true },
      },
    },
    preview: { port, strictPort: true },
    build: {
      outDir: 'dist',
      sourcemap: false,
      rollupOptions: {
        output: {
          // 把体积大且很少变动的依赖拆出来，利用浏览器缓存
          manualChunks: {
            react: ['react', 'react-dom', 'react-router-dom'],
            antd: ['antd', '@ant-design/icons'],
          },
        },
      },
    },
  }
})
