"""邮件通知：模板渲染 + SMTP 发送。

用标准库的 smtplib，放到线程里执行 —— 发信是阻塞 IO，不能卡住 worker 的事件循环。
发信失败一律吞掉并记日志：通知不到是小事，把任务搞挂是大事。
"""
from __future__ import annotations

import html
import logging
import re
import smtplib
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any

from ..config import settings as env_settings
from ..core.security import decrypt_secret
from ..models import Job, JobStatus, User

log = logging.getLogger(__name__)

VAR_PATTERN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

STATUS_LABEL = {
    JobStatus.succeeded: "已完成",
    JobStatus.completed: "已完成（含失败条目）",
    JobStatus.failed: "异常终止",
    JobStatus.canceled: "已取消",
    JobStatus.paused: "已暂停",
    JobStatus.running: "运行中",
    JobStatus.queued: "排队中",
    JobStatus.pending: "准备中",
}


class MailError(Exception):
    """发信失败。message 直接展示给管理员。"""


@dataclass
class Mail:
    to: str
    subject: str
    body: str
    html: bool = False


# --------------------------------------------------------------------------- #
# 模板
# --------------------------------------------------------------------------- #
def render(template: str, values: dict[str, Any], as_html: bool = False) -> str:
    """把 {{变量}} 替换成值。HTML 模式下对值转义，防止内容里的尖括号破坏排版。"""
    def replace(match: re.Match[str]) -> str:
        raw = values.get(match.group(1))
        if raw is None:
            return match.group(0)  # 未知变量原样保留，方便管理员发现拼错
        text = str(raw)
        return html.escape(text) if as_html else text

    return VAR_PATTERN.sub(replace, template or "")


def _format_duration(job: Job) -> str:
    if not job.started_at:
        return "—"
    end = job.finished_at or datetime.now(UTC)
    start = job.started_at
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    seconds = max(0, int((end - start).total_seconds()))
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分 {seconds % 60} 秒"
    return f"{seconds // 3600} 时 {(seconds % 3600) // 60} 分"


def build_values(job: Job, user: User, model_name: str | None) -> dict[str, Any]:
    base = env_settings.frontend_base_url.rstrip("/")
    error_block = ""
    if job.error:
        error_block = f"\n  错误：{job.error}\n"
    return {
        "job_name": job.name,
        "job_id": job.id,
        "status": job.status.value,
        "status_label": STATUS_LABEL.get(job.status, job.status.value),
        "total": job.total_items,
        "completed": job.completed_items,
        "failed": job.failed_items,
        "duration": _format_duration(job),
        "tokens": job.prompt_tokens + job.completion_tokens,
        "model": model_name or "—",
        "username": user.display_name or user.username,
        "email": user.email or "",
        "job_url": f"{base}/jobs/{job.id}",
        "error": job.error or "",
        "error_block": error_block,
    }


# --------------------------------------------------------------------------- #
# 发送
# --------------------------------------------------------------------------- #
def send_sync(mail: Mail, runtime: dict) -> None:
    """同步发送。调用方负责放到线程里。失败抛 MailError。"""
    host = (runtime.get("smtp_host") or "").strip()
    if not host:
        raise MailError("未配置 SMTP 服务器地址")

    sender = (runtime.get("smtp_from") or runtime.get("smtp_username") or "").strip()
    if not sender:
        raise MailError("未配置发件人地址")

    message = EmailMessage()
    message["Subject"] = mail.subject
    message["From"] = formataddr((runtime.get("smtp_from_name") or "", sender))
    message["To"] = mail.to
    if mail.html:
        message.set_content(re.sub(r"<[^>]+>", "", mail.body))  # 纯文本兜底
        message.add_alternative(mail.body, subtype="html")
    else:
        message.set_content(mail.body)

    port = int(runtime.get("smtp_port") or 0) or 25
    security = (runtime.get("smtp_security") or "starttls").lower()
    timeout = int(runtime.get("smtp_timeout") or 20)
    username = (runtime.get("smtp_username") or "").strip()
    password = decrypt_secret(runtime.get("smtp_password_encrypted") or "") or ""
    if runtime.get("smtp_password_encrypted") and not password:
        raise MailError("SMTP 密码解密失败（SECRET_KEY 可能已变更），请在后台重新填写")

    try:
        if security == "ssl":
            server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=timeout)
        else:
            server = smtplib.SMTP(host, port, timeout=timeout)
        with server:
            server.ehlo()
            if security == "starttls":
                server.starttls()
                server.ehlo()
            if username:
                server.login(username, password)
            server.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        raise MailError(f"SMTP 认证失败：{exc.smtp_error.decode(errors='replace') if exc.smtp_error else exc}") from exc
    except smtplib.SMTPException as exc:
        raise MailError(f"SMTP 错误：{exc}") from exc
    except OSError as exc:
        raise MailError(f"无法连接 {host}:{port} —— {exc}") from exc


def should_notify(job: Job, user: User, runtime: dict) -> tuple[bool, str]:
    """判断这次要不要发。返回 (要发吗, 不发的原因)。"""
    if not runtime.get("smtp_enabled"):
        return False, "邮件通知未启用"
    if not user.notify_email:
        return False, "用户已关闭邮件通知"
    if not user.email:
        return False, "用户没有邮箱地址"

    min_items = int(runtime.get("notify_min_items") or 0)
    if min_items and job.total_items < min_items:
        return False, f"任务条目数少于 {min_items}，按配置不打扰"

    if job.status in (JobStatus.succeeded, JobStatus.completed):
        return (True, "") if runtime.get("notify_on_success") else (False, "未开启成功通知")
    if job.status == JobStatus.failed:
        return (True, "") if runtime.get("notify_on_failure") else (False, "未开启失败通知")
    if job.status == JobStatus.canceled:
        return (True, "") if runtime.get("notify_on_canceled") else (False, "未开启取消通知")
    return False, f"状态 {job.status.value} 不触发通知"


def build_job_mail(job: Job, user: User, model_name: str | None, runtime: dict) -> Mail:
    values = build_values(job, user, model_name)
    as_html = bool(runtime.get("mail_html"))
    return Mail(
        to=user.email or "",
        subject=render(runtime.get("mail_subject_template") or "", values).strip() or "批量推理任务通知",
        body=render(runtime.get("mail_body_template") or "", values, as_html=as_html),
        html=as_html,
    )
