import type { JobStatus } from './api'

export const STATUS_LABEL: Record<JobStatus, string> = {
  pending: '准备中',
  queued: '排队中',
  running: '运行中',
  paused: '已暂停',
  succeeded: '已完成',
  completed: '完成(有失败)',
  failed: '失败',
  canceled: '已取消',
}

export const STATUS_COLOR: Record<JobStatus, string> = {
  pending: 'default',
  queued: 'blue',
  running: 'processing',
  paused: 'orange',
  succeeded: 'success',
  completed: 'warning',
  failed: 'error',
  canceled: 'default',
}

export const ACTIVE_STATUSES: JobStatus[] = ['pending', 'queued', 'running']

export function formatBytes(bytes: number): string {
  if (!bytes) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1)
  return `${(bytes / 1024 ** i).toFixed(i === 0 ? 0 : 1)} ${units[i]}`
}

export function formatNumber(n: number): string {
  return n.toLocaleString('zh-CN')
}

export function formatDateTime(value?: string | null): string {
  if (!value) return '—'
  // 后端返回 UTC，这里交给浏览器按本地时区展示
  const iso = /[Zz]|[+-]\d{2}:?\d{2}$/.test(value) ? value : `${value}Z`
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString('zh-CN', { hour12: false })
}

export function formatDuration(start?: string | null, end?: string | null): string {
  if (!start) return '—'
  const withZone = (v: string) => (/[Zz]|[+-]\d{2}:?\d{2}$/.test(v) ? v : `${v}Z`)
  const s = new Date(withZone(start)).getTime()
  const e = end ? new Date(withZone(end)).getTime() : Date.now()
  if (Number.isNaN(s) || Number.isNaN(e)) return '—'
  const sec = Math.max(0, Math.floor((e - s) / 1000))
  if (sec < 60) return `${sec} 秒`
  if (sec < 3600) return `${Math.floor(sec / 60)} 分 ${sec % 60} 秒`
  return `${Math.floor(sec / 3600)} 时 ${Math.floor((sec % 3600) / 60)} 分`
}
