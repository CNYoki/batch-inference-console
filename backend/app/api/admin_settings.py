"""系统设置与运行态看板（仅管理员可写，设置读取供任务接口复用）。"""
from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter
from sqlalchemy import func, select

from ..core.deps import DB, CurrentAdmin, CurrentUser
from ..core.security import decrypt_secret, encrypt_secret, mask_secret
from ..models import Job, JobStatus, ModelConfig, User, UserRole
from ..schemas import (
    DashboardStats,
    MailTestRequest,
    MailTestResult,
    MyUsage,
    QueueStats,
    RetentionPreview,
    StorageUsage,
    SystemSettingsOut,
    SystemSettingsUpdate,
    WorkerInfo,
)
from ..services import mailer
from ..services.queue import queue
from ..services.retention import preview as retention_preview
from ..services.retention import sweep as retention_sweep
from ..services.settings_store import read_runtime, write_runtime
from ..services.storage import Quota, total_quota, user_quota

router = APIRouter(tags=["admin"])

WORKER_STALE_SECONDS = 45


async def get_runtime_settings(db: DB) -> SystemSettingsOut:
    """读取可在后台修改的运行时设置。SMTP 密码只回掩码。"""
    runtime = await read_runtime(db)
    out = SystemSettingsOut(**runtime)
    out.smtp_password_masked = mask_secret(decrypt_secret(runtime.get("smtp_password_encrypted") or ""))
    return out


@router.get("/settings", response_model=SystemSettingsOut)
async def read_settings(_: CurrentUser, db: DB) -> SystemSettingsOut:
    """所有登录用户都能读（前端需要公告与上传限制）。"""
    return await get_runtime_settings(db)


def _usage(q: Quota) -> StorageUsage:
    return StorageUsage(
        used=q.used, limit=q.limit, remaining=q.remaining,
        percent=q.percent, pending=q.pending, unlimited=q.unlimited,
    )


@router.get("/usage", response_model=MyUsage)
async def my_usage(user: CurrentUser, db: DB) -> MyUsage:
    """当前用户的存储用量；管理员还能看到全站用量。"""
    return MyUsage(
        storage=_usage(await user_quota(db, user)),
        total=_usage(await total_quota(db)) if user.role == UserRole.admin else None,
    )


@router.patch("/admin/settings", response_model=SystemSettingsOut)
async def update_settings(payload: SystemSettingsUpdate, _: CurrentAdmin, db: DB) -> SystemSettingsOut:
    changes = payload.model_dump(exclude_unset=True, exclude_none=True)

    # SMTP 密码单独处理：空串 = 清除，有值 = 加密后存，不传 = 保持不变
    if "smtp_password" in payload.model_fields_set:
        raw = payload.smtp_password
        changes.pop("smtp_password", None)
        changes["smtp_password_encrypted"] = encrypt_secret(raw) if raw else ""

    await write_runtime(db, changes)
    return await get_runtime_settings(db)


@router.post("/admin/notifications/test", response_model=MailTestResult)
async def send_test_mail(payload: MailTestRequest, admin: CurrentAdmin, db: DB) -> MailTestResult:
    """用当前配置和模板发一封测试邮件，把渲染结果一并回传便于核对排版。"""
    import asyncio

    from ..models import Job, JobStatus

    runtime = await read_runtime(db)
    if not runtime.get("smtp_host"):
        return MailTestResult(ok=False, detail="请先填写并保存 SMTP 服务器地址")

    # 造一个假任务来渲染模板，不落库
    sample = Job(
        id="0" * 32, name="示例任务（测试邮件）", user_id=admin.id, status=JobStatus.completed,
        input_filename="sample.jsonl", input_path="", input_size=0,
        total_items=1000, completed_items=985, failed_items=15,
        prompt_tokens=120000, completion_tokens=45000,
        started_at=datetime.now(UTC) - timedelta(minutes=42), finished_at=datetime.now(UTC),
    )
    mail = mailer.build_job_mail(sample, admin, "示例模型", runtime)
    mail.to = payload.to

    try:
        await asyncio.to_thread(mailer.send_sync, mail, runtime)
    except mailer.MailError as exc:
        return MailTestResult(ok=False, detail=str(exc), subject=mail.subject, body=mail.body)
    except Exception as exc:  # noqa: BLE001 — 把原始原因回显给管理员
        return MailTestResult(
            ok=False, detail=f"{type(exc).__name__}: {exc}", subject=mail.subject, body=mail.body
        )
    return MailTestResult(
        ok=True, detail=f"已发送到 {payload.to}", subject=mail.subject, body=mail.body
    )


@router.get("/admin/retention", response_model=RetentionPreview)
async def retention_status(_: CurrentAdmin, db: DB) -> RetentionPreview:
    """当前保留期策略下，占了多少空间、有多少会被清掉。"""
    return RetentionPreview(**await retention_preview(db))


@router.post("/admin/retention/run", response_model=RetentionPreview)
async def retention_run_now(_: CurrentAdmin, db: DB) -> RetentionPreview:
    """立即执行一次清理，不等 worker 的下一轮。"""
    await retention_sweep(db)
    return RetentionPreview(**await retention_preview(db))


@router.get("/admin/dashboard", response_model=DashboardStats)
async def dashboard(_: CurrentAdmin, db: DB) -> DashboardStats:
    redis_ok = await queue.ping()
    stats = await queue.stats() if redis_ok else {"queued": 0, "running": 0, "workers": 0}

    workers: list[WorkerInfo] = []
    if redis_ok:
        now = time.time()
        for wid, raw in (await queue.list_workers()).items():
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                payload = {}
            last_seen = payload.get("ts")
            workers.append(
                WorkerInfo(
                    id=wid,
                    last_seen=last_seen,
                    jobs=payload.get("jobs", []),
                    capacity=payload.get("capacity", 0),
                    alive=bool(last_seen) and (now - last_seen) < WORKER_STALE_SECONDS,
                    draining=bool(payload.get("draining")),
                    data_dir=str(payload.get("data_dir") or ""),
                )
            )

    status_rows = (await db.execute(select(Job.status, func.count(Job.id)).group_by(Job.status))).all()
    by_status = {s.value if isinstance(s, JobStatus) else str(s): c for s, c in status_rows}

    total_jobs = sum(by_status.values())
    processed = (await db.execute(select(func.coalesce(func.sum(Job.completed_items), 0)))).scalar_one()
    active_models = (
        await db.execute(select(func.count(ModelConfig.id)).where(ModelConfig.enabled.is_(True)))
    ).scalar_one()

    live_dirs = sorted({w.data_dir for w in workers if w.alive and w.data_dir})

    return DashboardStats(
        queue=QueueStats(**stats, redis_ok=redis_ok),
        workers=sorted(workers, key=lambda w: w.id),
        data_dir_conflict=live_dirs if len(live_dirs) > 1 else [],
        jobs_by_status=by_status,
        total_jobs=total_jobs,
        total_items_processed=int(processed or 0),
        active_models=active_models,
    )


@router.get("/admin/audit")
async def audit_logs(_: CurrentAdmin, db: DB, limit: int = 100, offset: int = 0) -> dict:
    from ..models import AuditLog

    total = (await db.execute(select(func.count(AuditLog.id)))).scalar_one()
    rows = (
        await db.execute(
            select(AuditLog).order_by(AuditLog.created_at.desc()).offset(offset).limit(min(limit, 500))
        )
    ).scalars().all()
    return {
        "total": total,
        "items": [
            {
                "id": r.id,
                "username": r.username,
                "action": r.action,
                "target": r.target,
                "detail": r.detail,
                "ip": r.ip,
                "created_at": r.created_at,
            }
            for r in rows
        ],
    }


@router.get("/admin/overview/users")
async def user_usage(_: CurrentAdmin, db: DB, limit: int = 20) -> list[dict]:
    """按用户统计任务数与 token 消耗，便于配额与成本核算。"""
    rows = (
        await db.execute(
            select(
                User.id,
                User.username,
                func.count(Job.id).label("job_count"),
                func.coalesce(func.sum(Job.completed_items), 0).label("items"),
                func.coalesce(func.sum(Job.prompt_tokens), 0).label("prompt_tokens"),
                func.coalesce(func.sum(Job.completion_tokens), 0).label("completion_tokens"),
                (
                    func.coalesce(func.sum(Job.input_size), 0)
                    + func.coalesce(func.sum(Job.result_size), 0)
                ).label("storage_bytes"),
            )
            .join(Job, Job.user_id == User.id, isouter=True)
            .group_by(User.id, User.username)
            .order_by(func.count(Job.id).desc())
            .limit(limit)
        )
    ).all()
    return [
        {
            "user_id": r.id,
            "username": r.username,
            "job_count": r.job_count,
            "items": int(r.items or 0),
            "prompt_tokens": int(r.prompt_tokens or 0),
            "completion_tokens": int(r.completion_tokens or 0),
            "storage_bytes": int(r.storage_bytes or 0),
        }
        for r in rows
    ]
