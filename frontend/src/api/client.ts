import axios from 'axios'
import { message } from 'antd'

declare module 'axios' {
  export interface AxiosRequestConfig {
    /** 调用方自己在界面上展示错误时置 true，拦截器就不再弹 toast */
    skipErrorToast?: boolean
  }
}

export const http = axios.create({
  baseURL: '/api',
  withCredentials: true,   // 会话是 httpOnly Cookie
  timeout: 60000,
})

// 401 统一跳登录；其他错误弹出后端返回的中文 detail
http.interceptors.response.use(
  (res) => res,
  (error) => {
    const status = error.response?.status
    const detail = error.response?.data?.detail
    if (status === 401 && !location.pathname.startsWith('/login')) {
      location.href = '/login'
      return Promise.reject(error)
    }
    if (error.config?.skipErrorToast) return Promise.reject(error)
    if (detail && typeof detail === 'string') {
      message.error(detail)
    } else if (status) {
      message.error(`请求失败 (HTTP ${status})`)
    } else {
      message.error('网络错误，请检查后端服务是否可用')
    }
    return Promise.reject(error)
  },
)

export function downloadUrl(path: string): string {
  return `/api${path}`
}
