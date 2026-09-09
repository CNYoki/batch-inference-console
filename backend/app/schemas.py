"""Pydantic 请求/响应模型。

注意：Pydantic v2 默认把 `model_` 前缀视为保留命名空间，本文件中凡是出现
`model_name` / `model_config_id` 的模型都显式关闭了该保护。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_ALLOW_MODEL_PREFIX = ConfigDict(protected_namespaces=())


# --------------------------------------------------------------------------- #
# 认证 / 用户
# --------------------------------------------------------------------------- #
class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str = Field(min_length=8, max_length=256)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    username: str
    email: str | None = None
    display_name: str | None = None
    role: str
    is_active: bool
    auth_source: str
    max_concurrent_jobs: int = 0
    max_storage_mb: int = 0
    notify_email: bool = True
    last_login_at: datetime | None = None
    created_at: datetime
    # 只回掩码，明文永不出后端
    llm_token_masked: str | None = None
    llm_token_updated_at: datetime | None = None


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=8, max_length=256)
    email: str | None = None
    display_name: str | None = None
    role: Literal["admin", "user"] = "user"
    max_concurrent_jobs: int = 0
    max_storage_mb: int = 0


class UserUpdate(BaseModel):
    email: str | None = None
    display_name: str | None = None
    role: Literal["admin", "user"] | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=8, max_length=256)
    max_concurrent_jobs: int | None = Field(default=None, ge=0)
    max_storage_mb: int | None = Field(default=None, ge=0)
    notify_email: bool | None = None


class MyPreferences(BaseModel):
    """用户能自己改的偏好。"""

    notify_email: bool | None = None
    email: str | None = Field(default=None, max_length=255)


class StorageUsage(BaseModel):
    """给用户看自己的用量，给管理员看全站用量。"""

    used: int
    limit: int          # 0 = 不限
    remaining: int      # -1 = 不限
    percent: float
    pending: int        # 其中未提交成任务的暂存上传
    unlimited: bool


class MyUsage(BaseModel):
    storage: StorageUsage
    total: StorageUsage | None = None   # 仅管理员可见


class AuthInfo(BaseModel):
    """未登录时前端用来决定展示哪些登录方式。"""

    local_auth_enabled: bool
    oidc_enabled: bool
    oidc_display_name: str


# --------------------------------------------------------------------------- #
# 模型配置
# --------------------------------------------------------------------------- #
class ModelConfigBase(BaseModel):
    model_config = _ALLOW_MODEL_PREFIX

    name: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    base_url: str = Field(min_length=1, max_length=1024)
    model_name: str = Field(min_length=1, max_length=255)
    endpoint_path: str = "/chat/completions"
    extra_headers: dict[str, str] = Field(default_factory=dict)

    enabled: bool = True
    admin_only: bool = False

    default_params: dict[str, Any] = Field(default_factory=dict)
    forced_params: dict[str, Any] = Field(default_factory=dict)
    allowed_param_keys: list[str] = Field(default_factory=list)

    supports_temperature: bool = True
    supports_system_prompt: bool = True
    supports_json_mode: bool = False
    supports_tools: bool = False
    reasoning_mode: Literal["off", "optional", "forced"] = "off"
    reasoning_payload: dict[str, Any] = Field(default_factory=dict)
    reasoning_effort_options: list[str] = Field(default_factory=list, max_length=16)
    reasoning_default_effort: str = Field(default="", max_length=32)

    max_concurrency: int = Field(default=8, ge=1, le=512)
    rpm_limit: int = Field(default=0, ge=0)
    tpm_limit: int = Field(default=0, ge=0)
    request_timeout: int = Field(default=300, ge=5, le=3600)
    max_retries: int = Field(default=3, ge=0, le=10)
    max_tokens_cap: int = Field(default=0, ge=0)
    sort_order: int = 0

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("base_url 必须以 http:// 或 https:// 开头")
        return v.rstrip("/")


class ModelConfigCreate(ModelConfigBase):
    api_key: str | None = None


class ModelConfigUpdate(BaseModel):
    """全部字段可选；api_key 传空字符串表示清除，传 None 表示不修改。"""

    model_config = _ALLOW_MODEL_PREFIX

    display_name: str | None = None
    description: str | None = None
    base_url: str | None = None
    model_name: str | None = None
    endpoint_path: str | None = None
    extra_headers: dict[str, str] | None = None
    api_key: str | None = None

    enabled: bool | None = None
    admin_only: bool | None = None

    default_params: dict[str, Any] | None = None
    forced_params: dict[str, Any] | None = None
    allowed_param_keys: list[str] | None = None

    supports_temperature: bool | None = None
    supports_system_prompt: bool | None = None
    supports_json_mode: bool | None = None
    supports_tools: bool | None = None
    reasoning_mode: Literal["off", "optional", "forced"] | None = None
    reasoning_payload: dict[str, Any] | None = None
    reasoning_effort_options: list[str] | None = Field(default=None, max_length=16)
    reasoning_default_effort: str | None = Field(default=None, max_length=32)

    max_concurrency: int | None = Field(default=None, ge=1, le=512)
    rpm_limit: int | None = Field(default=None, ge=0)
    tpm_limit: int | None = Field(default=None, ge=0)
    request_timeout: int | None = Field(default=None, ge=5, le=3600)
    max_retries: int | None = Field(default=None, ge=0, le=10)
    max_tokens_cap: int | None = Field(default=None, ge=0)
    sort_order: int | None = None


class ModelConfigOut(ModelConfigBase):
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: str
    api_key_masked: str | None = None
    created_at: datetime
    updated_at: datetime


class ModelOption(BaseModel):
    """给普通用户的精简视图，不含端点与密钥。"""

    model_config = _ALLOW_MODEL_PREFIX

    id: str
    name: str
    display_name: str
    description: str | None = None
    default_params: dict[str, Any]
    allowed_param_keys: list[str]
    supports_temperature: bool
    supports_system_prompt: bool
    supports_json_mode: bool
    reasoning_mode: str
    reasoning_effort_options: list[str] = Field(default_factory=list)
    reasoning_default_effort: str = ""
    max_concurrency: int
    max_tokens_cap: int


class ModelOptionsOut(BaseModel):
    """新建任务页要用的全部可选模型：公用 + 个人。"""

    shared: list[ModelOption] = Field(default_factory=list)
    personal: list[str] = Field(default_factory=list)
    gateway_enabled: bool = True
    gateway_label: str = "个人网关"
    gateway_base_url: str = ""
    has_saved_token: bool = False
    # 拉取个人模型失败时的原因（token 无效、网关不可达等），前端原样展示
    personal_error: str | None = None


class PersonalTokenIn(BaseModel):
    token: str = Field(min_length=1, max_length=512)
    # 是否加密保存，下次不用再填
    remember: bool = True


class PersonalModelsOut(BaseModel):
    models: list[str]
    saved: bool = False


# --------------------------------------------------------------------------- #
# 任务
# --------------------------------------------------------------------------- #
class JobParams(BaseModel):
    """创建任务时前端提交的推理参数。"""

    system_prompt: str | None = Field(default=None, max_length=20000)
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_tokens: int | None = Field(default=None, ge=1, le=200_000)
    frequency_penalty: float | None = Field(default=None, ge=-2, le=2)
    presence_penalty: float | None = Field(default=None, ge=-2, le=2)
    stop: list[str] | None = None
    seed: int | None = None
    json_mode: bool = False
    reasoning: bool = False
    # 推理档位，取值必须落在模型声明的 reasoning_effort_options 里，否则退回第一档
    reasoning_effort: str | None = Field(default=None, max_length=32)
    extra: dict[str, Any] = Field(default_factory=dict)


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: str
    name: str
    status: str
    priority: int
    user_id: str
    username: str | None = None
    model_config_id: str | None = None
    model_display_name: str | None = None
    model_source: str = "shared"

    input_filename: str
    input_size: int
    result_size: int = 0
    total_items: int
    completed_items: int
    failed_items: int
    progress: float = 0.0
    queue_position: int | None = None

    prompt_tokens: int = 0
    completion_tokens: int = 0
    concurrency: int = 0
    params: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    worker_id: str | None = None

    created_at: datetime
    files_purged_at: datetime | None = None
    queued_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class JobListOut(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[JobOut]


class JobErrorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    item_index: int
    custom_id: str | None
    status_code: int | None
    attempts: int
    message: str | None
    created_at: datetime


class UploadValidateOut(BaseModel):
    upload_id: str
    filename: str
    size: int
    total_items: int
    errors: list[str]
    preview: list[dict[str, Any]]
    duplicate_custom_ids: list[str] = Field(default_factory=list)


class JobSubmission(BaseModel):
    """建任务与试跑共用的部分：选哪个模型、用哪些参数、跑哪份上传。"""

    model_config = _ALLOW_MODEL_PREFIX

    upload_id: str
    # shared：用管理员配置的公用模型，需要 model_config_id
    # personal：用自己的网关 token，需要 personal_model
    model_source: Literal["shared", "personal"] = "shared"
    model_config_id: str | None = None
    personal_model: str | None = Field(default=None, max_length=255)
    # 本次提交携带的网关 token；留空则用已保存的那个
    personal_token: str | None = Field(default=None, max_length=512)
    remember_token: bool = True
    params: JobParams = Field(default_factory=JobParams)

    @model_validator(mode="after")
    def _check_model_selection(self) -> JobSubmission:
        if self.model_source == "shared":
            if not self.model_config_id:
                raise ValueError("使用公用模型时必须指定 model_config_id")
        elif not self.personal_model:
            raise ValueError("使用个人模型时必须指定 personal_model")
        return self


class JobCreate(JobSubmission):
    name: str = Field(min_length=1, max_length=255)
    concurrency: int = Field(default=0, ge=0, le=512)
    priority: int = Field(default=100, ge=0, le=1000)


class JobDryRun(JobSubmission):
    """提交前的试跑请求：拿上传文件里的某一条真发一次。"""

    # 默认试第一条；文件前几条可能是特例，允许换一条再试
    item_index: int = Field(default=0, ge=0, le=999)


class JobDryRunOut(BaseModel):
    ok: bool
    # 试跑用的是哪条数据、发出去的完整请求体 —— 出错时用户要靠它对照排查
    custom_id: str | None = None
    item_index: int = 0
    request_body: dict[str, Any] = Field(default_factory=dict)
    status_code: int | None = None
    latency_ms: int = 0
    # 成功时给正文与 token 用量，失败时给一句人话的原因 + 原始响应
    content: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    response: dict[str, Any] | None = None


class JobPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    priority: int | None = Field(default=None, ge=0, le=1000)
    concurrency: int | None = Field(default=None, ge=0, le=512)


# --------------------------------------------------------------------------- #
# 代码生成（本地数据 → 平台输入 JSONL）
# --------------------------------------------------------------------------- #
class ScriptVariable(BaseModel):
    """把源数据的一个字段暴露成 Prompt 里的 {{变量}}。"""

    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    field: str = Field(min_length=1, max_length=255)


class ScriptConfig(BaseModel):
    model_config = _ALLOW_MODEL_PREFIX

    # ---- 输入 ----
    source_path: str = Field(min_length=1, max_length=1024)
    source_format: Literal["csv", "jsonl", "parquet"] = "csv"
    csv_delimiter: str = Field(default=",", min_length=1, max_length=4)
    encoding: str = Field(default="utf-8", max_length=32)
    recursive: bool = False

    # ---- custom_id ----
    custom_id_mode: Literal["field", "uuid", "rownum"] = "rownum"
    custom_id_field: str | None = Field(default=None, max_length=255)
    custom_id_prefix: str = Field(default="", max_length=64)

    # ---- 内容 ----
    variables: list[ScriptVariable] = Field(default_factory=list, max_length=50)
    system_prompt: str | None = Field(default=None, max_length=20000)
    prompt_template: str = Field(min_length=1, max_length=50000)
    model: str = Field(default="", max_length=255)
    endpoint_path: str = Field(default="/v1/chat/completions", max_length=255)
    extra_body: dict[str, Any] = Field(default_factory=dict)

    # ---- 输出与切分 ----
    output_dir: str = Field(default="./batch_input", max_length=1024)
    output_prefix: str = Field(default="part", min_length=1, max_length=64)
    max_rows_per_file: int = Field(default=50000, ge=0, le=10_000_000)
    max_file_size_mb: int = Field(default=100, ge=0, le=10240)
    # 对渲染后的 prompt 生效；0 = 不限
    max_prompt_chars: int = Field(default=0, ge=0, le=10_000_000)
    # 对最终 JSONL 单行（UTF-8 字节）生效；0 = 不限
    max_line_bytes: int = Field(default=0, ge=0, le=104_857_600)
    on_oversize: Literal["skip", "truncate"] = "skip"
    skip_empty: bool = True
    check_duplicate_ids: bool = True


class ScriptPreview(BaseModel):
    """生成前的预览：脚本内容、示例输出、以及配置里的问题。"""

    script: str
    script_name: str
    sample_record: dict[str, Any]
    sample_line: str
    detected_variables: list[str]
    problems: list[str]


# --------------------------------------------------------------------------- #
# 系统 / 统计
# --------------------------------------------------------------------------- #
class QueueStats(BaseModel):
    queued: int
    running: int
    workers: int
    redis_ok: bool


class WorkerInfo(BaseModel):
    id: str
    last_seen: float | None = None
    jobs: list[str] = Field(default_factory=list)
    capacity: int = 0
    alive: bool = True
    # 收到退出信号后仍在把手上任务跑完 —— 是正常状态，不是故障
    draining: bool = False
    # worker 的数据目录。多个部署共用一个队列时，这里会不一致
    data_dir: str = ""


class DashboardStats(BaseModel):
    queue: QueueStats
    workers: list[WorkerInfo]
    # 在线 worker 报告了不止一个数据目录 —— 说明多个部署共用了同一个队列，
    # 任务会被随机分给看不到对方文件的 worker
    data_dir_conflict: list[str] = Field(default_factory=list)
    jobs_by_status: dict[str, int]
    total_jobs: int
    total_items_processed: int
    active_models: int


class SystemSettingsOut(BaseModel):
    allow_new_jobs: bool = True
    max_upload_mb: int
    max_items_per_job: int
    default_priority: int = 100
    announcement: str = ""

    # ---- 用户自带 token 的模型网关 ----
    user_gateway_enabled: bool = True
    user_gateway_base_url: str = ""
    user_gateway_label: str = "个人网关"
    user_gateway_max_concurrency: int = 4
    user_gateway_timeout: int = 300
    user_gateway_max_retries: int = 3
    user_gateway_max_tokens_cap: int = 0

    user_gateway_reasoning_enabled: bool = True
    user_gateway_reasoning_payload: dict[str, Any] = Field(default_factory=dict)
    user_gateway_reasoning_effort_options: list[str] = Field(default_factory=list)
    user_gateway_reasoning_default_effort: str = ""

    # ---- 文件保留期 ----
    file_retention_days: int = 0
    purge_input_files: bool = True
    stale_upload_hours: int = 24

    # ---- 存储配额（GB，0 = 不限）----
    max_total_storage_gb: int = 0
    default_user_storage_gb: int = 0

    # ---- 邮件通知 ----
    smtp_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    # 只回掩码，明文永不出后端
    smtp_password_masked: str | None = None
    smtp_security: Literal["none", "starttls", "ssl"] = "starttls"
    smtp_from: str = ""
    smtp_from_name: str = ""
    smtp_timeout: int = 20
    notify_on_success: bool = True
    notify_on_failure: bool = True
    notify_on_canceled: bool = False
    notify_min_items: int = 0
    mail_html: bool = False
    mail_subject_template: str = ""
    mail_body_template: str = ""


class RetentionPreview(BaseModel):
    """后台展示：当前策略下会清掉多少东西。"""

    retention_days: int
    tracked_jobs: int
    tracked_bytes: int
    expiring_jobs: int
    expiring_bytes: int
    already_purged_jobs: int


class SystemSettingsUpdate(BaseModel):
    allow_new_jobs: bool | None = None
    default_priority: int | None = Field(default=None, ge=0, le=1000)
    announcement: str | None = Field(default=None, max_length=2000)

    user_gateway_enabled: bool | None = None
    user_gateway_base_url: str | None = Field(default=None, max_length=1024)
    user_gateway_label: str | None = Field(default=None, max_length=64)
    user_gateway_max_concurrency: int | None = Field(default=None, ge=1, le=128)
    user_gateway_timeout: int | None = Field(default=None, ge=5, le=3600)
    user_gateway_max_retries: int | None = Field(default=None, ge=0, le=10)
    user_gateway_max_tokens_cap: int | None = Field(default=None, ge=0)

    # 0 = 永久保留；上限 3650 天纯粹是防手滑输错
    user_gateway_reasoning_enabled: bool | None = None
    user_gateway_reasoning_payload: dict[str, Any] | None = None
    user_gateway_reasoning_effort_options: list[str] | None = Field(default=None, max_length=16)
    user_gateway_reasoning_default_effort: str | None = Field(default=None, max_length=32)

    file_retention_days: int | None = Field(default=None, ge=0, le=3650)
    purge_input_files: bool | None = None
    stale_upload_hours: int | None = Field(default=None, ge=0, le=8760)

    max_total_storage_gb: int | None = Field(default=None, ge=0, le=1_000_000)
    default_user_storage_gb: int | None = Field(default=None, ge=0, le=1_000_000)

    smtp_enabled: bool | None = None
    smtp_host: str | None = Field(default=None, max_length=255)
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    smtp_username: str | None = Field(default=None, max_length=255)
    # 传空字符串 = 清除密码；不传 = 保持不变
    smtp_password: str | None = Field(default=None, max_length=512)
    smtp_security: Literal["none", "starttls", "ssl"] | None = None
    smtp_from: str | None = Field(default=None, max_length=255)
    smtp_from_name: str | None = Field(default=None, max_length=128)
    smtp_timeout: int | None = Field(default=None, ge=3, le=300)
    notify_on_success: bool | None = None
    notify_on_failure: bool | None = None
    notify_on_canceled: bool | None = None
    notify_min_items: int | None = Field(default=None, ge=0)
    mail_html: bool | None = None
    mail_subject_template: str | None = Field(default=None, max_length=500)
    mail_body_template: str | None = Field(default=None, max_length=20000)

    @field_validator("user_gateway_base_url")
    @classmethod
    def _check_gateway_url(cls, v: str | None) -> str | None:
        if v and not v.startswith(("http://", "https://")):
            raise ValueError("网关地址必须以 http:// 或 https:// 开头")
        return v.rstrip("/") if v else v


class MailTestRequest(BaseModel):
    to: str = Field(min_length=3, max_length=255)


class MailTestResult(BaseModel):
    ok: bool
    detail: str
    # 用当前模板渲染出的样例，方便管理员确认排版
    subject: str = ""
    body: str = ""


class ProbeResult(BaseModel):
    ok: bool
    status_code: int | None = None
    latency_ms: int | None = None
    url: str | None = None
    detail: Any = None
