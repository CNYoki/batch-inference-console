import { http } from './client'
import type {
  AuthInfo, Dashboard, DryRunResult, Job, JobErrorRow, JobList, MailTestResult, ModelConfig,
  ModelOption, ModelOptions, MyUsage, PersonalModels, ResultPage, RetentionPreview,
  ScriptConfig, ScriptPreview, SystemSettings, UploadResult, User,
} from './types'

export * from './types'
export { http, downloadUrl } from './client'

// ---------------- 认证 ----------------
export const api = {
  authInfo: () => http.get<AuthInfo>('/auth/info').then((r) => r.data),
  login: (username: string, password: string) =>
    http.post<User>('/auth/login', { username, password }).then((r) => r.data),
  logout: () => http.post('/auth/logout').then((r) => r.data),
  me: () => http.get<User>('/auth/me').then((r) => r.data),
  updatePreferences: (payload: { notify_email?: boolean; email?: string | null }) =>
    http.patch<User>('/auth/preferences', payload).then((r) => r.data),
  changePassword: (old_password: string, new_password: string) =>
    http.post('/auth/change-password', { old_password, new_password }).then((r) => r.data),
  oidcLogoutUrl: () =>
    http.get<{ end_session_url: string | null }>('/auth/oidc/logout').then((r) => r.data),

  // ---------------- 模型 ----------------
  models: () => http.get<ModelOption[]>('/models').then((r) => r.data),
  // 新建任务页用：公用模型 + （若已存 token）个人模型
  modelOptions: () => http.get<ModelOptions>('/models/options').then((r) => r.data),
  personalModels: (token: string, remember = true) =>
    http.post<PersonalModels>('/models/personal', { token, remember }).then((r) => r.data),
  clearPersonalToken: () => http.delete('/models/personal/token'),
  adminModels: () => http.get<ModelConfig[]>('/admin/models').then((r) => r.data),
  createModel: (payload: Partial<ModelConfig> & { api_key?: string }) =>
    http.post<ModelConfig>('/admin/models', payload).then((r) => r.data),
  updateModel: (id: string, payload: Partial<ModelConfig> & { api_key?: string }) =>
    http.patch<ModelConfig>(`/admin/models/${id}`, payload).then((r) => r.data),
  deleteModel: (id: string) => http.delete(`/admin/models/${id}`),
  probeModel: (id: string) =>
    http.post<{ ok: boolean; status_code?: number; latency_ms?: number; detail?: unknown }>(
      `/admin/models/${id}/probe`,
    ).then((r) => r.data),

  // ---------------- 任务 ----------------
  uploadJsonl: (file: File, onProgress?: (percent: number) => void) => {
    const form = new FormData()
    form.append('file', file)
    return http
      .post<UploadResult>('/jobs/upload', form, {
        timeout: 0, // 大文件上传不设超时
        onUploadProgress: (e) => {
          if (onProgress && e.total) onProgress(Math.round((e.loaded / e.total) * 100))
        },
      })
      .then((r) => r.data)
  },
  createJob: (payload: {
    name: string
    upload_id: string
    model_source: 'shared' | 'personal'
    model_config_id?: string | null
    personal_model?: string | null
    personal_token?: string | null
    remember_token?: boolean
    params: Record<string, unknown>
    concurrency?: number
    priority?: number
  }) => http.post<Job>('/jobs', payload).then((r) => r.data),
  // 试跑：真发一条，超时交给后端的 request_timeout 管，前端不另设
  dryRunJob: (payload: {
    upload_id: string
    model_source: 'shared' | 'personal'
    model_config_id?: string | null
    personal_model?: string | null
    personal_token?: string | null
    remember_token?: boolean
    params: Record<string, unknown>
  }) => http.post<DryRunResult>('/jobs/dry-run', payload, { timeout: 0, skipErrorToast: true })
    .then((r) => r.data),
  jobs: (params: {
    page?: number; page_size?: number; status?: string; keyword?: string; mine?: boolean
  }) => http.get<JobList>('/jobs', { params }).then((r) => r.data),
  job: (id: string) => http.get<Job>(`/jobs/${id}`).then((r) => r.data),
  jobErrors: (id: string, limit = 100, offset = 0) =>
    http.get<JobErrorRow[]>(`/jobs/${id}/errors`, { params: { limit, offset } }).then((r) => r.data),
  jobResults: (id: string, offset = 0, limit = 20, only_errors = false) =>
    http.get<ResultPage>(`/jobs/${id}/results`, { params: { offset, limit, only_errors } })
      .then((r) => r.data),
  patchJob: (id: string, payload: { name?: string; priority?: number; concurrency?: number }) =>
    http.patch<Job>(`/jobs/${id}`, payload).then((r) => r.data),
  cancelJob: (id: string) => http.post<Job>(`/jobs/${id}/cancel`).then((r) => r.data),
  pauseJob: (id: string) => http.post<Job>(`/jobs/${id}/pause`).then((r) => r.data),
  resumeJob: (id: string) => http.post<Job>(`/jobs/${id}/resume`).then((r) => r.data),
  retryFailed: (id: string) => http.post<Job>(`/jobs/${id}/retry-failed`).then((r) => r.data),
  deleteJob: (id: string) => http.delete(`/jobs/${id}`),

  // ---------------- 用户 ----------------
  users: (params: { page?: number; page_size?: number; keyword?: string }) =>
    http.get<{ total: number; page: number; page_size: number; items: User[] }>('/admin/users', { params })
      .then((r) => r.data),
  createUser: (payload: {
    username: string; password: string; email?: string; display_name?: string
    role?: 'admin' | 'user'; max_concurrent_jobs?: number; max_storage_mb?: number
  }) => http.post<User>('/admin/users', payload).then((r) => r.data),
  updateUser: (id: string, payload: Partial<User> & { password?: string }) =>
    http.patch<User>(`/admin/users/${id}`, payload).then((r) => r.data),
  deleteUser: (id: string) => http.delete(`/admin/users/${id}`),

  // ---------------- 系统 ----------------
  settings: () => http.get<SystemSettings>('/settings').then((r) => r.data),
  usage: () => http.get<MyUsage>('/usage').then((r) => r.data),
  updateSettings: (payload: Partial<SystemSettings>) =>
    http.patch<SystemSettings>('/admin/settings', payload).then((r) => r.data),
  dashboard: () => http.get<Dashboard>('/admin/dashboard').then((r) => r.data),
  retention: () => http.get<RetentionPreview>('/admin/retention').then((r) => r.data),
  runRetention: () => http.post<RetentionPreview>('/admin/retention/run').then((r) => r.data),
  testMail: (to: string) =>
    http.post<MailTestResult>('/admin/notifications/test', { to }).then((r) => r.data),

  // ---------------- 代码生成 ----------------
  splitScript: (config: ScriptConfig) =>
    http.post<ScriptPreview>('/tools/split-script', config).then((r) => r.data),
  userUsage: () =>
    http.get<Array<{
      user_id: string; username: string; job_count: number; items: number
      prompt_tokens: number; completion_tokens: number; storage_bytes: number
    }>>('/admin/overview/users').then((r) => r.data),
}
