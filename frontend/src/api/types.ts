export type JobStatus =
  | 'pending' | 'queued' | 'running' | 'paused'
  | 'succeeded' | 'completed' | 'failed' | 'canceled'

export interface User {
  id: string
  username: string
  email?: string | null
  display_name?: string | null
  role: 'admin' | 'user'
  is_active: boolean
  auth_source: 'local' | 'oidc'
  max_concurrent_jobs: number
  max_storage_mb: number
  notify_email: boolean
  last_login_at?: string | null
  created_at: string
  // 个人网关 token 只回掩码
  llm_token_masked?: string | null
  llm_token_updated_at?: string | null
}

export interface AuthInfo {
  local_auth_enabled: boolean
  oidc_enabled: boolean
  oidc_display_name: string
}

export interface ModelOption {
  id: string
  name: string
  display_name: string
  description?: string | null
  default_params: Record<string, unknown>
  allowed_param_keys: string[]
  supports_temperature: boolean
  supports_system_prompt: boolean
  supports_json_mode: boolean
  reasoning_mode: 'off' | 'optional' | 'forced'
  max_concurrency: number
  max_tokens_cap: number
}

export interface ModelConfig extends ModelOption {
  base_url: string
  model_name: string
  endpoint_path: string
  extra_headers: Record<string, string>
  api_key_masked?: string | null
  enabled: boolean
  admin_only: boolean
  forced_params: Record<string, unknown>
  supports_tools: boolean
  reasoning_payload: Record<string, unknown>
  rpm_limit: number
  tpm_limit: number
  request_timeout: number
  max_retries: number
  sort_order: number
  created_at: string
  updated_at: string
}

export type ModelSource = 'shared' | 'personal'

export interface ModelOptions {
  shared: ModelOption[]
  personal: string[]
  gateway_enabled: boolean
  gateway_label: string
  gateway_base_url: string
  has_saved_token: boolean
  personal_error?: string | null
}

export interface PersonalModels {
  models: string[]
  saved: boolean
}

export interface Job {
  id: string
  name: string
  status: JobStatus
  priority: number
  user_id: string
  username?: string | null
  model_config_id?: string | null
  model_display_name?: string | null
  model_source: ModelSource
  input_filename: string
  input_size: number
  result_size: number
  total_items: number
  completed_items: number
  failed_items: number
  progress: number
  queue_position?: number | null
  prompt_tokens: number
  completion_tokens: number
  concurrency: number
  params: Record<string, unknown>
  error?: string | null
  worker_id?: string | null
  created_at: string
  // 非空表示输入/结果文件已因超过保留期被清理，任务记录仍在
  files_purged_at?: string | null
  queued_at?: string | null
  started_at?: string | null
  finished_at?: string | null
}

export interface JobList {
  total: number
  page: number
  page_size: number
  items: Job[]
}

export interface UploadResult {
  upload_id: string
  filename: string
  size: number
  total_items: number
  errors: string[]
  preview: Array<Record<string, unknown>>
  duplicate_custom_ids: string[]
}

export interface JobErrorRow {
  item_index: number
  custom_id?: string | null
  status_code?: number | null
  attempts: number
  message?: string | null
  created_at: string
}

export interface ResultRow {
  index: number
  custom_id: string
  output?: string | null
  error?: string | null
  status_code?: number | null
  attempts?: number
  usage?: { prompt_tokens?: number; completion_tokens?: number } | null
}

export interface ResultPage {
  total: number
  offset: number
  limit: number
  rows: ResultRow[]
}

export interface WorkerInfo {
  id: string
  last_seen?: number | null
  jobs: string[]
  capacity: number
  alive: boolean
  // 收到退出信号后仍在把手上任务跑完 —— 正常状态，不是故障
  draining: boolean
  data_dir: string
}

export interface Dashboard {
  queue: { queued: number; running: number; workers: number; redis_ok: boolean }
  workers: WorkerInfo[]
  // 非空表示在线 worker 报告了多个数据目录 —— 多个部署共用了同一个队列
  data_dir_conflict: string[]
  jobs_by_status: Record<string, number>
  total_jobs: number
  total_items_processed: number
  active_models: number
}

export interface SystemSettings {
  allow_new_jobs: boolean
  max_upload_mb: number
  max_items_per_job: number
  default_priority: number
  announcement: string

  user_gateway_enabled: boolean
  user_gateway_base_url: string
  user_gateway_label: string
  user_gateway_max_concurrency: number
  user_gateway_timeout: number
  user_gateway_max_retries: number
  user_gateway_max_tokens_cap: number
  user_gateway_reasoning_enabled: boolean
  user_gateway_reasoning_payload: Record<string, unknown>

  file_retention_days: number
  purge_input_files: boolean
  stale_upload_hours: number

  max_total_storage_gb: number
  default_user_storage_gb: number

  smtp_enabled: boolean
  smtp_host: string
  smtp_port: number
  smtp_username: string
  smtp_password_masked?: string | null
  smtp_security: 'none' | 'starttls' | 'ssl'
  smtp_from: string
  smtp_from_name: string
  smtp_timeout: number
  notify_on_success: boolean
  notify_on_failure: boolean
  notify_on_canceled: boolean
  notify_min_items: number
  mail_html: boolean
  mail_subject_template: string
  mail_body_template: string
}

export interface MailTestResult {
  ok: boolean
  detail: string
  subject: string
  body: string
}

// ---------------- 代码生成 ----------------
export interface ScriptVariable {
  name: string
  field: string
}

export interface ScriptConfig {
  source_path: string
  source_format: 'csv' | 'jsonl' | 'parquet'
  csv_delimiter: string
  encoding: string
  recursive: boolean

  custom_id_mode: 'field' | 'uuid' | 'rownum'
  custom_id_field?: string | null
  custom_id_prefix: string

  variables: ScriptVariable[]
  system_prompt?: string | null
  prompt_template: string
  model: string
  endpoint_path: string
  extra_body: Record<string, unknown>

  output_dir: string
  output_prefix: string
  max_rows_per_file: number
  max_file_size_mb: number
  max_prompt_chars: number
  max_line_bytes: number
  on_oversize: 'skip' | 'truncate'
  skip_empty: boolean
  check_duplicate_ids: boolean
}

export interface ScriptPreview {
  script: string
  script_name: string
  sample_record: Record<string, unknown>
  sample_line: string
  detected_variables: string[]
  problems: string[]
}

export interface StorageUsage {
  used: number
  limit: number       // 0 = 不限
  remaining: number   // -1 = 不限
  percent: number
  pending: number
  unlimited: boolean
}

export interface MyUsage {
  storage: StorageUsage
  total?: StorageUsage | null   // 仅管理员可见
}

export interface RetentionPreview {
  retention_days: number
  tracked_jobs: number
  tracked_bytes: number
  expiring_jobs: number
  expiring_bytes: number
  already_purged_jobs: number
}
