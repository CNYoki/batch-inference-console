"""任务结束后的邮件通知编排。

worker 在任务落终态后调用 notify_job()。这里做完整的判断与发送，
任何异常都在内部消化 —— 通知失败绝不能影响任务本身。
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..models import Job, ModelSource
from . import mailer
from .settings_store import read_runtime

log = logging.getLogger(__name__)


async def notify_job(job_id: str) -> str:
    """给任务提交者发结束通知。返回一句话说明结果，方便日志与测试断言。"""
    from ..db import session_scope

    try:
        async with session_scope() as db:
            job = (
                await db.execute(
                    select(Job)
                    .options(selectinload(Job.user), selectinload(Job.model_config))
                    .where(Job.id == job_id)
                )
            ).scalar_one_or_none()
            if job is None:
                return "任务不存在"

            user = job.user
            if user is None:
                return "任务没有关联用户"

            runtime = await read_runtime(db)
            ok, reason = mailer.should_notify(job, user, runtime)
            if not ok:
                return f"跳过：{reason}"

            model_name = (
                job.personal_model_name
                if job.model_source == ModelSource.personal
                else (job.model_config.display_name if job.model_config else None)
            )
            mail = mailer.build_job_mail(job, user, model_name, runtime)

        # 发信放到线程里，别卡住事件循环
        await asyncio.to_thread(mailer.send_sync, mail, runtime)
    except mailer.MailError as exc:
        log.warning("任务 %s 的通知邮件发送失败：%s", job_id, exc)
        return f"发送失败：{exc}"
    except Exception as exc:  # noqa: BLE001 — 通知失败绝不能影响任务
        log.warning("任务 %s 的通知流程异常：%s", job_id, exc)
        return f"异常：{exc}"

    log.info("已向 %s 发送任务 %s 的结束通知", mail.to, job_id)
    return "已发送"
