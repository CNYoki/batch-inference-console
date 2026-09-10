"""SQLAlchemy ORM 模型。

设计要点：条目级别的请求/响应体不入库（大任务可达数十万条），
而是以 JSONL 落盘（uploads/ 与 results/），数据库只保存任务元数据、
进度计数与失败样本，保证 MySQL/Postgres 在大任务下依然轻量。
"""
from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

LongText = Text().with_variant(mysql.LONGTEXT, "mysql")
# SQLite 只对 INTEGER PRIMARY KEY 做自增，BIGINT 主键会拿不到 rowid
AutoBigInt = BigInteger().with_variant(Integer, "sqlite")


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, server_default=func.now(), nullable=False
    )


# --------------------------------------------------------------------------- #
# 枚举
# --------------------------------------------------------------------------- #
class UserRole(str, enum.Enum):
    admin = "admin"
    user = "user"


class AuthSource(str, enum.Enum):
    local = "local"
    oidc = "oidc"


class ModelSource(str, enum.Enum):
    shared = "shared"      # 管理员在后台配置的公用模型
    personal = "personal"  # 用户自带 token，从网关拉到的个人模型


class JobStatus(str, enum.Enum):
    pending = "pending"        # 已创建，尚未入队（解析中）
    queued = "queued"          # 已入队，等待 worker 领取
    running = "running"        # 处理中
    paused = "paused"          # 用户暂停
    succeeded = "succeeded"    # 全部完成且无失败
    completed = "completed"    # 全部处理完，但存在失败条目
    failed = "failed"          # 任务级失败（解析错误 / 模型不可用等）
    canceled = "canceled"      # 用户取消


TERMINAL_STATUSES = {JobStatus.succeeded, JobStatus.completed, JobStatus.failed, JobStatus.canceled}

# 只有这些状态允许下载结果。运行中的任务结果文件还在被追加写入，
# 中途下载会拿到不完整甚至截断的内容
DOWNLOADABLE_STATUSES = {JobStatus.succeeded, JobStatus.completed, JobStatus.canceled}

# 只有停下来、且不会再有 worker 在跑的任务才能换模型。
# 运行中的 worker 手里握着旧配置，这时改库也不会生效
MODEL_EDITABLE_STATUSES = {JobStatus.paused, JobStatus.canceled, JobStatus.failed}


# --------------------------------------------------------------------------- #
# 用户
# --------------------------------------------------------------------------- #
class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    role: Mapped[UserRole] = mapped_column(
        SAEnum(UserRole, native_enum=False, length=16), default=UserRole.user, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    auth_source: Mapped[AuthSource] = mapped_column(
        SAEnum(AuthSource, native_enum=False, length=16), default=AuthSource.local, nullable=False
    )
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    oidc_issuer: Mapped[str | None] = mapped_column(String(512), nullable=True)
    oidc_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)

    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 用户自己的大模型网关 token（Fernet 加密，明文不落库也不回传前端）。
    # 用来拉取该用户有权限的模型列表，并在执行任务时以其身份调用网关。
    llm_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_token_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # 配额：0 表示不限制（走全局默认值）
    max_concurrent_jobs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 单位 MB。0 = 不单独设限，使用后台的全局默认值
    max_storage_mb: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    # 任务结束后是否邮件通知本人。默认开启，用户可自行关闭
    notify_email: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    jobs: Mapped[list[Job]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    prompts: Mapped[list[UserPrompt]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    api_tokens: Mapped[list[ApiToken]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        UniqueConstraint("oidc_issuer", "oidc_subject", name="uq_user_oidc"),
        Index("ix_users_role", "role"),
    )

    @property
    def llm_token_masked(self) -> str | None:
        """给前端看的掩码，例 sk-a***f2c9。解密失败时明确告知需要重填。"""
        if not self.llm_token_encrypted:
            return None
        from .core.security import decrypt_secret, mask_secret

        plain = decrypt_secret(self.llm_token_encrypted)
        return mask_secret(plain) if plain else "（无法解密，请重新填写）"


# --------------------------------------------------------------------------- #
# 模型配置（后台维护）
# --------------------------------------------------------------------------- #
class ModelConfig(Base, TimestampMixin):
    __tablename__ = "model_configs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    # 供用户在前端选择的唯一标识
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    # OpenAI 兼容端点，例：https://api.openai.com/v1 或 http://127.0.0.1:8000/v1
    base_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    # 真正下发给端点的 model 字段
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 对称加密后的 API Key（Fernet），明文永不落库、永不回传前端
    api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 端点路径，默认 /chat/completions，也可指向 /completions 或自建路径
    endpoint_path: Mapped[str] = mapped_column(String(255), default="/chat/completions", nullable=False)
    # 额外请求头，例 {"X-Tenant": "abc"}
    extra_headers: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 是否只有管理员可用
    admin_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # ---- 推理开关 / 默认参数 ----
    # 用户在创建任务时可覆盖的默认采样参数
    default_params: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # 强制参数：始终覆盖用户传入值（用于锁定 temperature / max_tokens 等）
    forced_params: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # 允许用户覆盖的参数白名单；为空表示允许全部
    allowed_param_keys: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    supports_temperature: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    supports_system_prompt: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    supports_json_mode: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supports_tools: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 推理（thinking / reasoning）开关：off | optional | forced
    reasoning_mode: Mapped[str] = mapped_column(String(16), default="off", nullable=False)
    # 开启推理时附加的请求体片段，例 {"reasoning_effort": "$effort"}；
    # 片段里任意位置的 "$effort" 会被换成用户选的档位
    reasoning_payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # 允许用户挑选的推理档位，例 ["low", "medium", "high"]。
    # 留空表示不让用户选，payload 原样发出
    reasoning_effort_options: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # 用户没选时用哪一档；留空或不在名单里就取名单第一项。
    # 单独存而不是"取第一项"，是因为档位名单通常按强度排序，
    # 排头的可能是 none/minimal 这种不该当默认值的档
    reasoning_default_effort: Mapped[str] = mapped_column(String(32), default="", nullable=False)

    # ---- 限流 / 重试 ----
    max_concurrency: Mapped[int] = mapped_column(Integer, default=8, nullable=False)
    rpm_limit: Mapped[int] = mapped_column(Integer, default=0, nullable=False)   # 0 = 不限
    tpm_limit: Mapped[int] = mapped_column(Integer, default=0, nullable=False)   # 0 = 不限
    request_timeout: Mapped[int] = mapped_column(Integer, default=300, nullable=False)
    max_retries: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    # 单条请求的最大 tokens 上限（防呆），0 = 不限
    max_tokens_cap: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    jobs: Mapped[list[Job]] = relationship(back_populates="model_config")


# --------------------------------------------------------------------------- #
# 任务
# --------------------------------------------------------------------------- #
class Job(Base, TimestampMixin):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    model_config_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("model_configs.id", ondelete="SET NULL"), nullable=True
    )

    # shared 时用 model_config_id；personal 时用 personal_model_name + 用户自己的 token
    model_source: Mapped[ModelSource] = mapped_column(
        SAEnum(ModelSource, native_enum=False, length=16), default=ModelSource.shared, nullable=False
    )
    personal_model_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    status: Mapped[JobStatus] = mapped_column(
        SAEnum(JobStatus, native_enum=False, length=16), default=JobStatus.pending, nullable=False
    )
    # 数值越小越优先
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)

    # 用户提交的推理参数（已与模型配置合并前的原始值）
    params: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # 合并 default/forced 之后、真正发送的参数
    resolved_params: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    concurrency: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # 0 = 用模型配置值

    # ---- 输入 ----
    input_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    input_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    input_size: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    # 结果文件（output.jsonl + errors.jsonl）的字节数，由 worker 定期回写。
    # 存下来是为了统计配额时不用去遍历磁盘 —— 一条 SQL SUM 就够了
    result_size: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    # ---- 进度 ----
    total_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    prompt_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # 文件被保留期策略清除的时间。非空即表示输入/结果文件已不在磁盘上，
    # 但任务记录与用量统计仍然保留
    files_purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="jobs")
    model_config: Mapped[ModelConfig | None] = relationship(back_populates="jobs")
    errors: Mapped[list[JobError]] = relationship(
        back_populates="job", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        Index("ix_jobs_user_created", "user_id", "created_at"),
        Index("ix_jobs_status", "status"),
    )

    @property
    def progress(self) -> float:
        if not self.total_items:
            return 0.0
        return round((self.completed_items + self.failed_items) / self.total_items * 100, 2)


class JobError(Base):
    """失败条目样本（每个任务上限见 services.results.MAX_STORED_ERRORS）。"""

    __tablename__ = "job_errors"

    id: Mapped[int] = mapped_column(AutoBigInt, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(32), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    item_index: Mapped[int] = mapped_column(Integer, nullable=False)
    custom_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    message: Mapped[str | None] = mapped_column(LongText, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False
    )

    job: Mapped[Job] = relationship(back_populates="errors")

    __table_args__ = (Index("ix_job_errors_job", "job_id", "item_index"),)


# --------------------------------------------------------------------------- #
# 我的 Prompt（数据预处理时复用）
# --------------------------------------------------------------------------- #
class UserPrompt(Base, TimestampMixin):
    """用户保存的 Prompt 模板，连同它引用的数据变量一起存。

    变量只是「变量名 → 源字段名」的映射，和生成脚本时填的是同一份结构，
    存下来就能在不同数据集之间直接复用，不用每次重填。
    """

    __tablename__ = "user_prompts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_template: Mapped[str] = mapped_column(LongText, nullable=False)
    # [{"name": "data", "field": "content"}, ...]
    variables: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    user: Mapped[User] = relationship(back_populates="prompts")

    # 同一个人的 Prompt 不能重名，否则在下拉框里分不清；唯一索引的前缀也覆盖了按用户查询
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_user_prompt_name"),)


# --------------------------------------------------------------------------- #
# 个人 API Token（命令行 / Claude Code Skills 调用）
# --------------------------------------------------------------------------- #
class ApiToken(Base, TimestampMixin):
    """长期有效、可单独吊销的访问凭证。

    OIDC 用户没有密码，会话 JWT 又只在 httpOnly Cookie 里且 12 小时过期，
    脚本拿不到可用的凭证 —— 这张表就是给它们用的。库里只存 SHA-256，
    明文只在创建时返回一次。
    """

    __tablename__ = "api_tokens"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # 明文开头几位，列表里用来辨认是哪一个
    token_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    # 空 = 永不过期
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="api_tokens")

    __table_args__ = (Index("ix_api_tokens_user", "user_id"),)


# --------------------------------------------------------------------------- #
# 系统设置（后台可改的全局开关）
# --------------------------------------------------------------------------- #
class SystemSetting(Base, TimestampMixin):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(AutoBigInt, primary_key=True, autoincrement=True)
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    target: Mapped[str | None] = mapped_column(String(255), nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_audit_created", "created_at"),)
