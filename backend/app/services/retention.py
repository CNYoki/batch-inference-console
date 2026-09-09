"""按保留期清理终态任务的文件。

只删磁盘上的输入与结果文件，**任务记录、进度计数、用量统计一律保留** ——
否则历史统计会出现断层。清理过的任务在前端标记为「文件已清理」，
下载按钮隐藏，恢复/重试会给出明确提示。

由 worker 进程定时调用；多 worker 场景下用 Redis 锁保证同一时刻只有一个在扫。
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import TERMINAL_STATUSES, AuditLog, Job
from . import results as results_svc
from .settings_store import read_runtime

log = logging.getLogger(__name__)

# 一次最多清理多少个任务，避免单轮扫描占用过久
BATCH_LIMIT = 500


async def sweep(db: AsyncSession, now: datetime | None = None) -> dict:
    """扫描并清理过期文件，返回本轮统计。"""
    runtime = await read_runtime(db)
    days = int(runtime.get("file_retention_days") or 0)
    if days <= 0:
        # 保留期没开也要清暂存上传 —— 这类文件永远不会有人来认领
        return {
            "enabled": False, "purged": 0, "freed_bytes": 0,
            "stale_uploads_removed": _purge_stale_uploads(runtime, now or datetime.now(UTC)),
        }

    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=days)

    # 用 finished_at 而不是 created_at 计时：长时间运行的任务不该一结束就被清掉。
    # finished_at 为空的终态任务（历史数据）退回用 created_at。
    rows = (
        await db.execute(
            select(Job)
            .where(
                Job.status.in_(list(TERMINAL_STATUSES)),
                Job.files_purged_at.is_(None),
            )
            .order_by(Job.created_at)
            .limit(BATCH_LIMIT)
        )
    ).scalars().all()

    purged = 0
    freed = 0
    for job in rows:
        stamp = job.finished_at or job.created_at
        if stamp is None:
            continue
        # 数据库可能返回不带时区的 datetime，统一按 UTC 处理
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        if stamp > cutoff:
            continue

        sizes = results_svc.result_sizes(job.id)
        freed += sizes["output_bytes"] + sizes["errors_bytes"]
        input_path = job.input_path if runtime.get("purge_input_files", True) else None
        if input_path:
            freed += job.input_size or 0

        results_svc.delete_job_files(job.id, input_path)
        job.files_purged_at = now
        purged += 1

    if purged:
        db.add(
            AuditLog(
                action="retention_purge",
                username="system",
                detail={
                    "retention_days": days,
                    "purged_jobs": purged,
                    "freed_bytes": freed,
                    "purge_input": bool(runtime.get("purge_input_files", True)),
                },
            )
        )
        await db.commit()
        log.info("保留期清理：%d 个任务，释放约 %.1f MB", purged, freed / 1024 / 1024)

    stale = _purge_stale_uploads(runtime, now)
    return {
        "enabled": True, "purged": purged, "freed_bytes": freed,
        "retention_days": days, "stale_uploads_removed": stale,
    }


def _purge_stale_uploads(runtime: dict, now: datetime) -> int:
    """清掉「传了但一直没提交成任务」的暂存文件 —— 否则它们会永远占着配额。

    这类文件名形如 <user_id>__<upload_id>.jsonl；正式任务的输入文件名是
    <job_id>.jsonl，不含双下划线，所以不会误删。
    """
    from ..config import settings as env

    hours = int(runtime.get("stale_upload_hours") or 0)
    if hours <= 0:
        return 0

    cutoff = (now - timedelta(hours=hours)).timestamp()
    removed = 0
    try:
        for path in env.upload_dir.glob("*__*.jsonl"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
    except OSError as exc:
        log.warning("清理暂存上传失败: %s", exc)
    if removed:
        log.info("清理了 %d 个超过 %dh 未提交的暂存上传", removed, hours)
    return removed


async def preview(db: AsyncSession, now: datetime | None = None) -> dict:
    """给后台看的预估：当前策略下有多少任务会被清理、能释放多少空间。"""
    runtime = await read_runtime(db)
    days = int(runtime.get("file_retention_days") or 0)

    rows = (
        await db.execute(
            select(Job).where(Job.status.in_(list(TERMINAL_STATUSES)), Job.files_purged_at.is_(None))
        )
    ).scalars().all()

    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=days) if days > 0 else None

    total_bytes = 0
    expiring = 0
    expiring_bytes = 0
    for job in rows:
        sizes = results_svc.result_sizes(job.id)
        size = sizes["output_bytes"] + sizes["errors_bytes"] + (job.input_size or 0)
        total_bytes += size
        if cutoff is None:
            continue
        stamp = job.finished_at or job.created_at
        if stamp is None:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        if stamp <= cutoff:
            expiring += 1
            expiring_bytes += size

    purged_count = (
        await db.execute(select(Job).where(Job.files_purged_at.is_not(None)))
    ).scalars().all()

    return {
        "retention_days": days,
        "tracked_jobs": len(rows),
        "tracked_bytes": total_bytes,
        "expiring_jobs": expiring,
        "expiring_bytes": expiring_bytes,
        "already_purged_jobs": len(purged_count),
    }
