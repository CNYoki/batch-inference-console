"""运行时设置的读写。

这些值存在 system_settings 表里、管理员可在后台随时修改，无需重启进程。
API 层和 worker 都要读它，所以放在 services 层而不是 api 层。
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings as env_settings
from ..models import SystemSetting

SETTINGS_KEY = "runtime"

# 缺省值。管理员没改过的项就用这里的值。
DEFAULTS: dict = {
    "allow_new_jobs": True,
    "default_priority": 100,
    "announcement": "",
    # ---- 用户自带 token 的模型网关 ----
    # 需要管理员在后台填好网关地址后才启用
    "user_gateway_enabled": False,
    # OpenAI 兼容网关，填到 /v1 为止，例 https://gateway.example.com/v1
    "user_gateway_base_url": "",
    "user_gateway_label": "个人网关",
    # 用户自带 token 的任务默认限制（个人 token 通常配额有限，默认给得保守些）
    "user_gateway_max_concurrency": 4,
    "user_gateway_timeout": 300,
    "user_gateway_max_retries": 3,
    "user_gateway_max_tokens_cap": 0,
    # 个人模型的推理开关：默认允许用户自行开关，开关默认关闭。
    # 开启时附加到请求体的字段因网关而异（Qwen 系用 enable_thinking，
    # OpenAI 系用 reasoning_effort），所以做成可配置的
    "user_gateway_reasoning_enabled": True,
    "user_gateway_reasoning_payload": {"enable_thinking": True},
    # 让用户在建任务时挑档位，例 ["low", "medium", "high"]。payload 里写 "$effort"
    # 的地方会被换成所选档位；留空则不给选，payload 原样发出
    "user_gateway_reasoning_effort_options": [],
    # 用户没选时用哪一档；留空则取名单第一项
    "user_gateway_reasoning_default_effort": "",
    # 按模型名覆盖上面那份默认配置。个人模型不入库，只能靠模型名匹配：
    # [{"pattern": "qwen3-*", "enabled": true, "payload": {...},
    #   "effort_options": [...], "default_effort": "medium"}]
    # pattern 支持 * 通配，按顺序取第一条命中的
    "user_gateway_reasoning_rules": [],
    # ---- 文件保留期 ----
    # 终态任务（成功/完成/失败/取消）的输入与结果文件保留多少天，0 = 永久保留。
    # 到期后由 worker 自动清除文件，任务记录与统计保留。
    "file_retention_days": 0,
    # 清理时是否连输入文件一起删。关掉的话只删结果，输入留着仍可重跑
    "purge_input_files": True,
    # 未提交成任务的暂存上传，超过多少小时后清掉。0 = 不清理
    "stale_upload_hours": 24,
    # ---- 存储配额（单位 GB，0 = 不限）----
    "max_total_storage_gb": 0,
    # 每个用户的默认上限；用户可在用户管理里单独覆盖
    "default_user_storage_gb": 0,
    # ---- 邮件通知 ----
    "smtp_enabled": False,
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_username": "",
    # Fernet 密文，明文不落库也不回传前端
    "smtp_password_encrypted": "",
    # none（明文，仅内网自建）/ starttls（587 常用）/ ssl（465）
    "smtp_security": "starttls",
    "smtp_from": "",
    "smtp_from_name": "批量推理平台",
    "smtp_timeout": 20,
    # 哪些场景发信
    "notify_on_success": True,
    "notify_on_failure": True,
    "notify_on_canceled": False,
    # 条目数少于这个值的任务不打扰；0 = 全部通知
    "notify_min_items": 0,
    "mail_html": False,
    "mail_subject_template": "[批量推理] {{job_name}} {{status_label}}",
    "mail_body_template": (
        "你好 {{username}}：\n\n"
        "任务「{{job_name}}」{{status_label}}。\n\n"
        "  模型：{{model}}\n"
        "  进度：成功 {{completed}} / 失败 {{failed}} / 共 {{total}}\n"
        "  耗时：{{duration}}\n"
        "  消耗 tokens：{{tokens}}\n"
        "{{error_block}}"
        "\n查看详情：{{job_url}}\n\n"
        "—— 本邮件由批量推理平台自动发送，如需关闭请在「个人设置」中取消勾选。\n"
    ),
}


async def read_runtime(db: AsyncSession) -> dict:
    """返回合并了缺省值的运行时设置。"""
    row = await db.get(SystemSetting, SETTINGS_KEY)
    stored = dict(row.value or {}) if row else {}
    merged = {**DEFAULTS, **stored}
    # 这两项由环境变量决定，后台只读不改
    merged["max_upload_mb"] = env_settings.max_upload_mb
    merged["max_items_per_job"] = env_settings.max_items_per_job
    return merged


async def write_runtime(db: AsyncSession, changes: dict) -> dict:
    """合并写入；只接受 DEFAULTS 里声明过的键。"""
    row = await db.get(SystemSetting, SETTINGS_KEY)
    if row is None:
        row = SystemSetting(key=SETTINGS_KEY, value={})
        db.add(row)

    value = dict(row.value or {})
    value.update({k: v for k, v in changes.items() if k in DEFAULTS})
    row.value = value  # 重新赋值以触发 JSON 字段的变更检测
    await db.commit()
    return await read_runtime(db)
