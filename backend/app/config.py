"""应用配置：全部通过环境变量 / .env 注入。"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 仓库根目录（app/config.py -> app -> backend -> 根）。
# 相对路径的 DATA_DIR 锚定到这里而不是 CWD —— API 与 worker 常常从不同目录启动，
# 若按 CWD 解析，两者会指向不同的数据目录，结果文件就互相看不见了。
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"), env_file_encoding="utf-8", extra="ignore"
    )

    # ---- 基础 ----
    app_name: str = "Batch Inference Platform"
    debug: bool = False
    api_prefix: str = "/api"
    # 逗号分隔的前端来源；开发模式下 Vite 默认 5173
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # ---- 安全 ----
    # 用于签发会话 JWT + 派生 API Key 加密密钥。生产必须覆盖。
    secret_key: str = "change-me-in-production-please-use-a-long-random-string"
    session_ttl_hours: int = 12
    session_cookie_name: str = "biu_session"
    session_cookie_secure: bool = False
    session_cookie_samesite: str = "lax"

    # ---- 数据库 ----
    # MySQL:      mysql+asyncmy://user:pass@127.0.0.1:3306/batch_inference
    # Postgres:   postgresql+asyncpg://user:pass@127.0.0.1:5432/batch_inference
    # SQLite:     sqlite+aiosqlite:///./data/app.db（相对路径按 CWD 解析，多进程部署请用绝对路径）
    database_url: str = "sqlite+aiosqlite:///./data/app.db"
    db_echo: bool = False
    # 启动时自动建表。本地开发图方便；生产/容器部署应设为 false，
    # 改由 `alembic upgrade head` 统一管理 —— create_all 只建表不改表，
    # 靠它升级会导致新增字段永远不生效
    auto_create_tables: bool = True
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # ---- Redis / 队列 ----
    redis_url: str = "redis://127.0.0.1:6379/0"
    queue_key_prefix: str = "biu"
    # worker 领取任务后的租约秒数，超时未续租则任务被回收重排
    job_lease_seconds: int = 60

    # ---- Worker ----
    # 单个 worker 进程同时处理的任务数
    worker_max_concurrent_jobs: int = 4
    # 单个任务默认的条目并发（可被任务/模型配置覆盖）
    default_item_concurrency: int = 8
    # 结果落盘的批量大小
    result_flush_size: int = 200

    # ---- 存储 ----
    data_dir: Path = Path("./data")
    max_upload_mb: int = 512
    max_items_per_job: int = 500_000

    # ---- 本地账号 ----
    local_auth_enabled: bool = True
    # 启动时若不存在则创建的初始管理员
    bootstrap_admin_username: str = "admin"
    bootstrap_admin_password: str = ""  # 为空则不创建
    bootstrap_admin_email: str = "admin@example.com"

    # ---- OIDC ----
    oidc_enabled: bool = False
    oidc_display_name: str = "统一登录"
    oidc_issuer: str = ""  # 例：https://pocket-id.example.com
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_scopes: str = "openid profile email"
    # 后端回调地址，需在 IdP 注册。例：http://localhost:8000/api/auth/oidc/callback
    oidc_redirect_uri: str = "http://localhost:8000/api/auth/oidc/callback"
    # 登录完成后跳转的前端地址
    frontend_base_url: str = "http://localhost:5173"
    # 从 ID Token 的哪个 claim 读取用户名 / 邮箱 / 展示名
    oidc_username_claim: str = "preferred_username"
    oidc_email_claim: str = "email"
    oidc_name_claim: str = "name"
    # 拥有该 group/role 的用户自动成为管理员；claim 名可配置
    oidc_groups_claim: str = "groups"
    oidc_admin_group: str = ""
    # 首次通过 OIDC 登录是否自动建号
    oidc_auto_create_user: bool = True

    @field_validator("data_dir", mode="after")
    @classmethod
    def _abs_data_dir(cls, v: Path) -> Path:
        v = v.expanduser()
        return v.resolve() if v.is_absolute() else (PROJECT_ROOT / v).resolve()

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def result_dir(self) -> Path:
        return self.data_dir / "results"

    def ensure_dirs(self) -> None:
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.result_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s


settings = get_settings()
