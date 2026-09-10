"""单模型同时运行的任务数上限。

限制的对象是真实模型名：公用模型取配置里的 model_name，个人网关模型取任务上记的模型名。
两边同名算同一个模型 —— 限流保护的是后端那个模型本身，不管用户从哪条路进来。
"""
from __future__ import annotations

from fnmatch import fnmatch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Job, ModelConfig, ModelSource


def job_limit_for(model_name: str, rules: list[dict]) -> int:
    """按顺序取第一条命中的规则，返回上限；0 = 不限。"""
    if not model_name:
        return 0
    for rule in rules or []:
        pattern = str(rule.get("pattern") or "").strip()
        if pattern and fnmatch(model_name, pattern):
            return max(0, int(rule.get("max_running_jobs") or 0))
    return 0


async def job_model_names(db: AsyncSession, job_ids: list[str]) -> dict[str, str]:
    """任务 id → 真实模型名。查不到的（任务已删、模型配置已删）不在结果里。"""
    if not job_ids:
        return {}
    rows = (
        await db.execute(
            select(Job.id, Job.model_source, Job.personal_model_name, ModelConfig.model_name)
            .outerjoin(ModelConfig, ModelConfig.id == Job.model_config_id)
            .where(Job.id.in_(job_ids))
        )
    ).all()
    out: dict[str, str] = {}
    for job_id, source, personal, shared in rows:
        name = personal if source == ModelSource.personal else shared
        if name:
            out[job_id] = name
    return out
