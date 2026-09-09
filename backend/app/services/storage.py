"""存储配额：统计用量、判断能否继续上传。

用量 = 数据库里未被清理任务的 input_size + result_size，再加上该用户尚未提交成
任务的暂存上传文件。前者一条 SQL SUM 就能拿到，后者只需扫 uploads 目录里属于
该用户的文件 —— 都不用遍历结果目录，上传接口里调用足够快。
"""
from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import Job, User
from .settings_store import read_runtime

log = logging.getLogger(__name__)

GB = 1024 ** 3
MB = 1024 ** 2

QUOTA_MESSAGE = "已达到储存空间上限，请联系管理员"


@dataclass
class Quota:
    used: int          # 已用字节
    limit: int         # 上限字节，0 = 不限
    pending: int       # 其中属于暂存上传（未提交任务）的部分

    @property
    def unlimited(self) -> bool:
        return self.limit <= 0

    @property
    def remaining(self) -> int:
        return -1 if self.unlimited else max(0, self.limit - self.used)

    @property
    def percent(self) -> float:
        if self.unlimited:
            return 0.0
        return round(min(100.0, self.used / self.limit * 100), 2)

    def would_exceed(self, incoming: int) -> bool:
        return not self.unlimited and self.used + incoming > self.limit


def _pending_upload_bytes(user_id: str) -> int:
    """该用户已上传但还没提交成任务的暂存文件。"""
    total = 0
    try:
        for path in settings.upload_dir.glob(f"{user_id}__*.jsonl"):
            try:
                total += path.stat().st_size
            except OSError:
                continue
    except OSError as exc:  # 目录不存在等
        log.warning("统计暂存上传失败: %s", exc)
    return total


async def _job_bytes(db: AsyncSession, user_id: str | None = None) -> int:
    """已落库任务占用的字节。已被保留期清理的任务不再计入。"""
    stmt = select(
        func.coalesce(func.sum(Job.input_size), 0) + func.coalesce(func.sum(Job.result_size), 0)
    ).where(Job.files_purged_at.is_(None))
    if user_id is not None:
        stmt = stmt.where(Job.user_id == user_id)
    return int((await db.execute(stmt)).scalar_one() or 0)


async def user_quota(db: AsyncSession, user: User, runtime: dict | None = None) -> Quota:
    runtime = runtime if runtime is not None else await read_runtime(db)
    # 用户单独设了就用自己的，否则用全局默认
    limit_mb = user.max_storage_mb or int(runtime.get("default_user_storage_gb") or 0) * 1024
    pending = _pending_upload_bytes(user.id)
    used = await _job_bytes(db, user.id) + pending
    return Quota(used=used, limit=limit_mb * MB, pending=pending)


async def total_quota(db: AsyncSession, runtime: dict | None = None) -> Quota:
    runtime = runtime if runtime is not None else await read_runtime(db)
    limit_gb = int(runtime.get("max_total_storage_gb") or 0)
    pending = 0
    # 目录不存在或权限问题时按 0 计，不能因为统计不了就把上传全拦下来
    with contextlib.suppress(OSError):
        pending = sum(p.stat().st_size for p in settings.upload_dir.glob("*__*.jsonl"))
    used = await _job_bytes(db) + pending
    return Quota(used=used, limit=limit_gb * GB, pending=pending)


@dataclass
class QuotaCheck:
    ok: bool
    reason: str = ""
    user: Quota | None = None
    total: Quota | None = None


async def check_before_upload(db: AsyncSession, user: User, incoming: int = 0) -> QuotaCheck:
    """上传前的配额检查。incoming 为本次将要写入的字节数（未知时传 0）。

    个人配额和全站配额任一超出都拒绝 —— 对用户的提示文案保持一致，
    具体是哪一条超了由管理员在后台看。
    """
    runtime = await read_runtime(db)
    user_q = await user_quota(db, user, runtime)
    total_q = await total_quota(db, runtime)

    if user_q.would_exceed(incoming):
        return QuotaCheck(False, "user", user_q, total_q)
    if total_q.would_exceed(incoming):
        return QuotaCheck(False, "total", user_q, total_q)
    return QuotaCheck(True, "", user_q, total_q)


async def backfill_result_sizes(db: AsyncSession, limit: int = 5000) -> int:
    """给 result_size 还是 0 的历史任务补上真实体积。

    这个字段是随存储配额一起加的，之前跑完的任务都是 0 —— 不补的话
    用量统计会明显低估。worker 启动时调一次，之后由 runner 自己维护。
    """
    from . import results as results_svc

    rows = (
        await db.execute(
            select(Job).where(
                Job.result_size == 0,
                Job.files_purged_at.is_(None),
                Job.completed_items + Job.failed_items > 0,
            ).limit(limit)
        )
    ).scalars().all()

    fixed = 0
    for job in rows:
        sizes = results_svc.result_sizes(job.id)
        total = sizes["output_bytes"] + sizes["errors_bytes"]
        if total:
            job.result_size = total
            fixed += 1
    if fixed:
        await db.commit()
        log.info("补齐了 %d 个历史任务的结果体积", fixed)
    return fixed


def format_bytes(n: int) -> str:
    if n <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    v = float(n)
    while v >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    return f"{v:.1f} {units[i]}" if i else f"{int(v)} B"
