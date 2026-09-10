"""任务：上传校验 → 创建 → 队列控制 → 结果预览与导出。"""
from __future__ import annotations

import itertools
import logging
import re
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from ..config import settings
from ..core.deps import DB, CurrentUser
from ..core.security import decrypt_secret, encrypt_secret
from ..models import (
    DOWNLOADABLE_STATUSES,
    MODEL_EDITABLE_STATUSES,
    TERMINAL_STATUSES,
    Job,
    JobError,
    JobStatus,
    ModelConfig,
    ModelSource,
    User,
    UserRole,
)
from ..schemas import (
    JobCreate,
    JobDryRun,
    JobDryRunOut,
    JobErrorOut,
    JobListOut,
    JobModelChange,
    JobOut,
    JobParams,
    JobPatch,
    ModelSelection,
    UploadValidateOut,
)
from ..services import jsonl as jsonl_svc
from ..services import results as results_svc
from ..services.gateway import GatewayError, build_personal_config, fetch_models
from ..services.inference import InferenceClient, build_request_body, resolve_params
from ..services.queue import queue
from ..services.settings_store import read_runtime
from ..services.storage import QUOTA_MESSAGE, check_before_upload
from .admin_settings import get_runtime_settings

log = logging.getLogger(__name__)
router = APIRouter(prefix="/jobs", tags=["jobs"])

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.\-一-鿿]+")
CHUNK = 1024 * 1024


def _upload_path(user_id: str, upload_id: str) -> Path:
    """上传文件名内嵌 user_id，天然隔离不同用户的暂存文件。"""
    return settings.upload_dir / f"{user_id}__{upload_id}.jsonl"


def _can_access(user: User, job: Job) -> bool:
    return user.role == UserRole.admin or job.user_id == user.id


async def _get_job_or_404(db: DB, user: User, job_id: str) -> Job:
    job = (
        await db.execute(
            select(Job).options(selectinload(Job.model_config), selectinload(Job.user)).where(Job.id == job_id)
        )
    ).scalar_one_or_none()
    if job is None or not _can_access(user, job):
        # 无权访问也返回 404，不泄露任务是否存在
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    return job


def _to_out(job: Job, queue_position: int | None = None, live: dict[str, str] | None = None) -> JobOut:
    completed = job.completed_items
    failed = job.failed_items
    # 运行中的任务以 Redis 里的实时计数为准（DB 每 10s 才落一次）
    if live:
        completed = max(completed, int(float(live.get("completed", completed))))
        failed = max(failed, int(float(live.get("failed", failed))))

    total = job.total_items or 0
    progress = round((completed + failed) / total * 100, 2) if total else 0.0

    return JobOut(
        id=job.id,
        name=job.name,
        status=job.status.value,
        priority=job.priority,
        user_id=job.user_id,
        username=job.user.username if job.user else None,
        model_config_id=job.model_config_id,
        model_source=job.model_source.value,
        model_display_name=(
            job.personal_model_name
            if job.model_source == ModelSource.personal
            else (job.model_config.display_name if job.model_config else None)
        ),
        input_filename=job.input_filename,
        input_size=job.input_size,
        result_size=job.result_size,
        total_items=total,
        completed_items=completed,
        failed_items=failed,
        progress=progress,
        queue_position=queue_position,
        prompt_tokens=job.prompt_tokens,
        completion_tokens=job.completion_tokens,
        concurrency=job.concurrency,
        params=job.params or {},
        error=job.error,
        worker_id=job.worker_id,
        created_at=job.created_at,
        files_purged_at=job.files_purged_at,
        queued_at=job.queued_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


# --------------------------------------------------------------------------- #
# 上传与校验
# --------------------------------------------------------------------------- #
@router.post("/upload", response_model=UploadValidateOut)
async def upload_jsonl(user: CurrentUser, db: DB, file: UploadFile = File(...)) -> UploadValidateOut:
    runtime = await get_runtime_settings(db)
    if not runtime.allow_new_jobs and user.role != UserRole.admin:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "管理员已暂时关闭新任务提交")

    filename = _SAFE_NAME.sub("_", (file.filename or "input.jsonl"))[:200]
    if not filename.lower().endswith((".jsonl", ".json", ".ndjson", ".txt")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "只接受 .jsonl / .ndjson 文件")

    # 先按已用量拦一道：已经超了就不用白传一遍再拒绝
    quota = await check_before_upload(db, user)
    if not quota.ok:
        raise HTTPException(status.HTTP_507_INSUFFICIENT_STORAGE, QUOTA_MESSAGE)
    remaining = quota.user.remaining if quota.user else -1
    total_remaining = quota.total.remaining if quota.total else -1

    upload_id = uuid.uuid4().hex
    dest = _upload_path(user.id, upload_id)
    max_bytes = settings.max_upload_mb * 1024 * 1024
    size = 0

    try:
        with dest.open("wb") as out:
            while chunk := await file.read(CHUNK):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        f"文件超过 {settings.max_upload_mb} MB 上限",
                    )
                # 边收边判配额：大文件不必等传完才发现放不下
                if (remaining >= 0 and size > remaining) or (
                    total_remaining >= 0 and size > total_remaining
                ):
                    raise HTTPException(status.HTTP_507_INSUFFICIENT_STORAGE, QUOTA_MESSAGE)
                out.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    except OSError as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"写入文件失败: {exc}") from exc
    finally:
        await file.close()

    result = jsonl_svc.validate_file(dest, settings.max_items_per_job)
    dupes: list[str] = []
    if not result.errors:
        try:
            dupes = jsonl_svc.count_duplicate_custom_ids(dest)
        except jsonl_svc.JsonlError:
            dupes = []

    return UploadValidateOut(
        upload_id=upload_id,
        filename=filename,
        size=size,
        total_items=result.total,
        errors=result.errors,
        preview=result.preview,
        duplicate_custom_ids=dupes,
    )


# --------------------------------------------------------------------------- #
# 创建任务
# --------------------------------------------------------------------------- #
@router.post("", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def create_job(payload: JobCreate, user: CurrentUser, db: DB) -> JobOut:
    runtime = await get_runtime_settings(db)
    if not runtime.allow_new_jobs and user.role != UserRole.admin:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "管理员已暂时关闭新任务提交")

    src = _upload_path(user.id, payload.upload_id)
    if not src.exists():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "上传已失效，请重新上传文件")

    if payload.model_source == ModelSource.personal.value:
        mc = await _resolve_personal_model(payload, user, db)
    else:
        mc = await _resolve_shared_model(payload, user, db)

    if user.max_concurrent_jobs:
        active = (
            await db.execute(
                select(func.count(Job.id)).where(
                    Job.user_id == user.id,
                    Job.status.in_([JobStatus.queued, JobStatus.running, JobStatus.pending]),
                )
            )
        ).scalar_one()
        if active >= user.max_concurrent_jobs:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                f"你同时进行中的任务已达上限 {user.max_concurrent_jobs}",
            )

    validation = jsonl_svc.validate_file(src, settings.max_items_per_job)
    if validation.errors:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "输入文件不合法：" + "；".join(validation.errors[:5])
        )

    job_id = uuid.uuid4().hex
    final_path = settings.upload_dir / f"{job_id}.jsonl"
    shutil.move(str(src), final_path)

    resolved = _resolve_job_params(mc, payload.params)
    priority = payload.priority if payload.priority != 100 else runtime.default_priority
    personal = payload.model_source == ModelSource.personal.value

    job = Job(
        id=job_id,
        name=payload.name,
        user_id=user.id,
        # 个人模型没有库里的配置记录，只存模型名，执行时用用户 token 现场拼配置
        model_config_id=None if personal else mc.id,
        model_source=ModelSource.personal if personal else ModelSource.shared,
        personal_model_name=payload.personal_model if personal else None,
        status=JobStatus.queued,
        priority=priority,
        params=payload.params.model_dump(exclude_none=True),
        resolved_params=resolved,
        concurrency=min(payload.concurrency, mc.max_concurrency) if payload.concurrency else 0,
        input_filename=payload.name if payload.name.endswith(".jsonl") else f"{payload.name}.jsonl",
        input_path=str(final_path),
        input_size=final_path.stat().st_size,
        total_items=validation.total,
        queued_at=datetime.now(UTC),
    )
    db.add(job)
    await db.commit()

    try:
        await queue.enqueue(job.id, job.priority)
    except Exception as exc:
        log.exception("任务 %s 入队失败", job.id)
        job.status = JobStatus.failed
        job.error = f"入队失败，请检查 Redis 连接: {exc}"
        await db.commit()
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(job.error)) from exc

    await db.refresh(job, ["user", "model_config"])
    return _to_out(job, await queue.queued_position(job.id))


@router.post("/dry-run", response_model=JobDryRunOut)
async def dry_run(payload: JobDryRun, user: CurrentUser, db: DB) -> JobDryRunOut:
    """拿上传文件里的一条真发一次请求，不建任务、不落盘、不入队。

    参数与提交任务完全一致，所以模型名写错、密钥失效、推理字段网关不认、
    max_tokens 超限这些坑能当场暴露，省得整批排完队再一起失败。
    """
    src = _upload_path(user.id, payload.upload_id)
    if not src.exists():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "上传已失效，请重新上传文件")

    if payload.model_source == ModelSource.personal.value:
        mc = await _resolve_personal_model(payload, user, db)
    else:
        mc = await _resolve_shared_model(payload, user, db)
    # 个人 token 可能刚在 _resolve_personal_model 里存下来
    await db.commit()

    try:
        item = next(itertools.islice(jsonl_svc.iter_items(src), payload.item_index, None), None)
    except jsonl_svc.JsonlError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if item is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"文件里没有第 {payload.item_index + 1} 条"
        )

    # 和 worker 走同一套拼装逻辑，试跑才有意义
    resolved = _resolve_job_params(mc, payload.params)
    system_prompt = resolved.get("_system_prompt")
    body = build_request_body(
        mc, item.body, {k: v for k, v in resolved.items() if not k.startswith("_")}, system_prompt,
    )

    try:
        async with InferenceClient(mc) as client:
            out = await client.try_once(body)
    except Exception as exc:  # noqa: BLE001 — 建客户端就失败（密钥解密等），原因照样回显
        out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    return JobDryRunOut(
        custom_id=item.custom_id, item_index=payload.item_index, request_body=body, **out,
    )


async def _resolve_shared_model(payload: ModelSelection, user: User, db: DB) -> ModelConfig:
    mc = await db.get(ModelConfig, payload.model_config_id)
    if mc is None or not mc.enabled:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "模型不可用")
    if mc.admin_only and user.role != UserRole.admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "该模型仅管理员可用")
    return mc


async def _resolve_personal_model(payload: ModelSelection, user: User, db: DB) -> ModelConfig:
    """校验用户 token 对该模型确实有权限，并返回一个内存里的虚拟模型配置。"""
    runtime = await read_runtime(db)
    if not runtime["user_gateway_enabled"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "管理员未开启个人网关")

    token = payload.personal_token or decrypt_secret(user.llm_token_encrypted)
    if not token:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请先填写你的网关 token")

    # 提交时再校验一次权限：token 可能已被网关回收，或模型权限被调整。
    # 早失败好过让任务排到队列里再整批报错。
    try:
        allowed = await fetch_models(runtime["user_gateway_base_url"], token)
    except GatewayError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message) from exc

    if payload.personal_model not in allowed:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"你的 token 没有模型 {payload.personal_model} 的权限，请重新选择",
        )

    if payload.personal_token and payload.remember_token:
        user.llm_token_encrypted = encrypt_secret(payload.personal_token)
        user.llm_token_updated_at = datetime.now(UTC)
        await db.flush()

    return build_personal_config(payload.personal_model, token, runtime)


def _resolve_job_params(mc: ModelConfig, p: JobParams) -> dict:
    """把前端参数与模型配置合并成 worker 直接可用的请求参数。"""
    user_params: dict = {
        "temperature": p.temperature,
        "top_p": p.top_p,
        "max_tokens": p.max_tokens,
        "frequency_penalty": p.frequency_penalty,
        "presence_penalty": p.presence_penalty,
        "stop": p.stop,
        "seed": p.seed,
        **(p.extra or {}),
    }
    if p.json_mode and mc.supports_json_mode:
        user_params["response_format"] = {"type": "json_object"}
    if p.reasoning:
        user_params["reasoning"] = True
    if p.reasoning_effort:
        user_params["reasoning_effort"] = p.reasoning_effort

    resolved = resolve_params(mc, user_params)
    if p.system_prompt and mc.supports_system_prompt:
        # 下划线前缀的键由 runner 单独消费，不会进入请求体
        resolved["_system_prompt"] = p.system_prompt
    return resolved


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #
@router.get("", response_model=JobListOut)
async def list_jobs(
    user: CurrentUser,
    db: DB,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status_filter: str | None = Query(None, alias="status"),
    keyword: str | None = None,
    mine: bool = True,
) -> JobListOut:
    stmt = select(Job).options(selectinload(Job.model_config), selectinload(Job.user))
    count_stmt = select(func.count(Job.id))

    if user.role != UserRole.admin or mine:
        stmt = stmt.where(Job.user_id == user.id)
        count_stmt = count_stmt.where(Job.user_id == user.id)
    if status_filter:
        wanted = [s.strip() for s in status_filter.split(",") if s.strip()]
        try:
            statuses = [JobStatus(s) for s in wanted]
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"未知状态: {wanted}") from exc
        stmt = stmt.where(Job.status.in_(statuses))
        count_stmt = count_stmt.where(Job.status.in_(statuses))
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(Job.name.like(like))
        count_stmt = count_stmt.where(Job.name.like(like))

    total = (await db.execute(count_stmt)).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(Job.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars().all()

    active = [j.id for j in rows if j.status in {JobStatus.running, JobStatus.queued}]
    live = await _safe_progress_many(active)
    positions = {}
    for j in rows:
        if j.status == JobStatus.queued:
            positions[j.id] = await _safe_position(j.id)

    return JobListOut(
        total=total,
        page=page,
        page_size=page_size,
        items=[_to_out(j, positions.get(j.id), live.get(j.id)) for j in rows],
    )


async def _safe_progress_many(job_ids: list[str]) -> dict[str, dict[str, str]]:
    try:
        return await queue.get_progress_many(job_ids)
    except Exception as exc:  # noqa: BLE001 — Redis 不可用时退回数据库计数
        log.warning("读取实时进度失败: %s", exc)
        return {}


async def _safe_position(job_id: str) -> int | None:
    try:
        return await queue.queued_position(job_id)
    except Exception:  # noqa: BLE001
        return None


@router.get("/{job_id}", response_model=JobOut)
async def get_job(job_id: str, user: CurrentUser, db: DB) -> JobOut:
    job = await _get_job_or_404(db, user, job_id)
    live = (await _safe_progress_many([job.id])).get(job.id)
    position = await _safe_position(job.id) if job.status == JobStatus.queued else None
    return _to_out(job, position, live)


@router.get("/{job_id}/errors", response_model=list[JobErrorOut])
async def get_job_errors(
    job_id: str, user: CurrentUser, db: DB, limit: int = Query(100, ge=1, le=1000), offset: int = 0
) -> list[JobError]:
    await _get_job_or_404(db, user, job_id)
    rows = (
        await db.execute(
            select(JobError)
            .where(JobError.job_id == job_id)
            .order_by(JobError.item_index)
            .offset(offset)
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


@router.get("/{job_id}/results")
async def preview_results(
    job_id: str,
    user: CurrentUser,
    db: DB,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=200),
    only_errors: bool = False,
) -> dict:
    await _get_job_or_404(db, user, job_id)
    return results_svc.read_page(job_id, offset, limit, only_errors)


@router.get("/{job_id}/download")
async def download_results(
    job_id: str,
    user: CurrentUser,
    db: DB,
    fmt: str = Query("raw", pattern="^(raw|simple|csv)$"),
    include_errors: bool = False,
) -> StreamingResponse:
    job = await _get_job_or_404(db, user, job_id)
    if job.files_purged_at:
        raise HTTPException(
            status.HTTP_410_GONE,
            "结果文件已超过保留期被自动清理，任务记录与统计仍然保留",
        )
    if job.status not in DOWNLOADABLE_STATUSES:
        # 结果文件正在被 worker 追加写入，此刻下载拿到的可能是半行或缺一大截
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "任务尚未结束，暂不能下载结果。可以先在「结果预览」里查看已完成的部分",
        )
    if not results_svc.output_path(job_id).exists() and not results_svc.errors_path(job_id).exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "该任务还没有任何结果")

    ext = "csv" if fmt == "csv" else "jsonl"
    media = "text/csv; charset=utf-8" if fmt == "csv" else "application/x-ndjson"
    safe = _SAFE_NAME.sub("_", job.name)[:80] or "results"
    filename = f"{safe}_{job_id[:8]}.{ext}"

    return StreamingResponse(
        results_svc.iter_export(job_id, fmt, include_errors),
        media_type=media,
        headers={
            # 中文文件名走 RFC 5987，避免部分浏览器乱码
            "Content-Disposition": f"attachment; filename=\"{job_id[:8]}.{ext}\"; "
            f"filename*=UTF-8''{quote(filename)}"
        },
    )



@router.get("/{job_id}/input")
async def download_input(job_id: str, user: CurrentUser, db: DB) -> StreamingResponse:
    job = await _get_job_or_404(db, user, job_id)
    if job.files_purged_at:
        raise HTTPException(status.HTTP_410_GONE, "输入文件已超过保留期被自动清理")
    path = Path(job.input_path)
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "输入文件已被清理")

    def _iter():
        with path.open("rb") as fh:
            while chunk := fh.read(CHUNK):
                yield chunk

    return StreamingResponse(
        _iter(),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{job_id[:8]}_input.jsonl"'},
    )


# --------------------------------------------------------------------------- #
# 控制
# --------------------------------------------------------------------------- #
@router.patch("/{job_id}", response_model=JobOut)
async def patch_job(job_id: str, payload: JobPatch, user: CurrentUser, db: DB) -> JobOut:
    job = await _get_job_or_404(db, user, job_id)
    data = payload.model_dump(exclude_unset=True, exclude_none=True)

    if "priority" in data and job.status != JobStatus.queued:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "只有排队中的任务可以调整优先级")

    for key, value in data.items():
        setattr(job, key, value)
    await db.commit()

    if "priority" in data:
        # 重新入队以刷新排序分值
        await queue.remove(job.id)
        await queue.enqueue(job.id, job.priority)

    await db.refresh(job, ["user", "model_config"])
    return _to_out(job, await _safe_position(job.id))


@router.post("/{job_id}/cancel", response_model=JobOut)
async def cancel_job(job_id: str, user: CurrentUser, db: DB) -> JobOut:
    job = await _get_job_or_404(db, user, job_id)
    if job.status in TERMINAL_STATUSES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "任务已结束，无法取消")

    await queue.signal(job.id, "cancel")
    await queue.remove(job.id)

    # 排队中的任务不会有 worker 来响应信号，这里直接落终态
    if job.status in {JobStatus.queued, JobStatus.pending, JobStatus.paused}:
        job.status = JobStatus.canceled
        job.finished_at = datetime.now(UTC)
        await db.commit()
        await queue.clear_signal(job.id)

    await db.refresh(job, ["user", "model_config"])
    return _to_out(job)


@router.post("/{job_id}/pause", response_model=JobOut)
async def pause_job(job_id: str, user: CurrentUser, db: DB) -> JobOut:
    job = await _get_job_or_404(db, user, job_id)
    if job.status not in {JobStatus.running, JobStatus.queued}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "只有排队中或运行中的任务可以暂停")

    await queue.signal(job.id, "pause")
    await queue.remove(job.id)
    if job.status == JobStatus.queued:
        job.status = JobStatus.paused
        await db.commit()
        await queue.clear_signal(job.id)

    await db.refresh(job, ["user", "model_config"])
    return _to_out(job)


@router.post("/{job_id}/resume", response_model=JobOut)
async def resume_job(job_id: str, user: CurrentUser, db: DB) -> JobOut:
    """恢复暂停/失败的任务；已完成的条目不会重复计费。"""
    job = await _get_job_or_404(db, user, job_id)
    if job.status not in {JobStatus.paused, JobStatus.failed, JobStatus.canceled}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "只有已暂停/失败/已取消的任务可以恢复")
    if job.files_purged_at or not Path(job.input_path).exists():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "输入文件已超过保留期被清理，无法恢复；请重新上传数据新建任务"
        )

    await queue.clear_signal(job.id)
    job.status = JobStatus.queued
    job.error = None
    job.finished_at = None
    job.queued_at = datetime.now(UTC)
    await db.commit()
    await queue.enqueue(job.id, job.priority)

    await db.refresh(job, ["user", "model_config"])
    return _to_out(job, await _safe_position(job.id))


@router.post("/{job_id}/model", response_model=JobOut)
async def change_model(job_id: str, payload: JobModelChange, user: CurrentUser, db: DB) -> JobOut:
    """给停下来的任务换模型，恢复后剩余条目改用新模型。

    已完成的条目不会重跑，结果文件里会同时有新旧两个模型的输出。
    推理参数沿用原任务提交时的那份，按新模型的默认值/强制值/白名单重新合并。
    """
    job = await _get_job_or_404(db, user, job_id)
    if job.status not in MODEL_EDITABLE_STATUSES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "只有已暂停/已取消/失败的任务可以更换模型")

    personal = payload.model_source == ModelSource.personal.value
    if personal:
        # worker 执行时用的是任务提交者本人的 token，校验也必须拿这个人的 token 做；
        # 管理员替别人换成自己网关里的模型，排上队也跑不起来
        if job.user_id != user.id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "个人网关模型只能由任务提交者本人设置")
        mc = await _resolve_personal_model(payload, user, db)
    else:
        mc = await _resolve_shared_model(payload, user, db)

    job.model_source = ModelSource.personal if personal else ModelSource.shared
    job.model_config_id = None if personal else mc.id
    job.personal_model_name = payload.personal_model if personal else None
    job.resolved_params = _resolve_job_params(mc, JobParams.model_validate(job.params or {}))
    if job.concurrency:
        # 原来的并发是按旧模型上限截过的，换了模型要按新上限再截一次
        job.concurrency = min(job.concurrency, mc.max_concurrency)
    await db.commit()

    await db.refresh(job, ["user", "model_config"])
    return _to_out(job)


@router.post("/{job_id}/retry-failed", response_model=JobOut)
async def retry_failed(job_id: str, user: CurrentUser, db: DB) -> JobOut:
    """只重跑失败条目：清空 errors.jsonl 后重新入队，成功条目会被断点续跑跳过。"""
    job = await _get_job_or_404(db, user, job_id)
    if job.status not in TERMINAL_STATUSES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "任务尚未结束")
    if not job.failed_items:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "该任务没有失败条目")
    if job.files_purged_at or not Path(job.input_path).exists():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "输入文件已超过保留期被清理，无法重试；请重新上传数据新建任务"
        )

    results_svc.errors_path(job_id).unlink(missing_ok=True)
    await db.execute(JobError.__table__.delete().where(JobError.job_id == job_id))

    job.failed_items = 0
    job.status = JobStatus.queued
    job.error = None
    job.finished_at = None
    job.queued_at = datetime.now(UTC)
    await db.commit()

    await queue.clear_signal(job.id)
    await queue.enqueue(job.id, job.priority)

    await db.refresh(job, ["user", "model_config"])
    return _to_out(job, await _safe_position(job.id))


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_job(job_id: str, user: CurrentUser, db: DB) -> None:
    job = await _get_job_or_404(db, user, job_id)
    if job.status in {JobStatus.running, JobStatus.queued}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请先取消任务再删除")

    input_path = job.input_path
    await db.delete(job)
    await db.commit()
    await queue.remove(job_id)
    await queue.clear_signal(job_id)
    results_svc.delete_job_files(job_id, input_path)
