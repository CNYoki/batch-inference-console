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

function parseTime(v: string): number {
  const withZone = /[Zz]|[+-]\d{2}:?\d{2}$/.test(v) ? v : `${v}Z`
  return new Date(withZone).getTime()
}

function formatSeconds(sec: number): string {
  if (sec < 60) return `${sec} 秒`
  if (sec < 3600) return `${Math.floor(sec / 60)} 分 ${sec % 60} 秒`
  return `${Math.floor(sec / 3600)} 时 ${Math.floor((sec % 3600) / 60)} 分`
}

/** 任务实际运行的时长：多次暂停/恢复的各段运行叠加，暂停、排队的时间不算 */
export function formatRunDuration(job: {
  started_at?: string | null; run_seconds?: number; run_started_at?: string | null
}): string {
  if (!job.started_at) return '—'
  let sec = job.run_seconds ?? 0
  if (job.run_started_at) {
    const s = parseTime(job.run_started_at)
    if (!Number.isNaN(s)) sec += Math.max(0, Math.floor((Date.now() - s) / 1000))
  }
  return formatSeconds(sec)
}

/** 进度条的状态和颜色：运行中带动效、有失败标红、已取消置灰 */
export function progressProps(job: { status: string; failed_items: number }): {
  status?: 'active' | 'exception' | 'normal'; strokeColor?: string
} {
  if (job.status === 'canceled') return { status: 'normal', strokeColor: '#bfbfbf' }
  if (job.status === 'running') return { status: 'active' }
  return job.failed_items ? { status: 'exception' } : {}
}
