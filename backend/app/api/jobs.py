"""任务：上传校验 → 创建 → 队列控制 → 结果预览与导出。"""
from __future__ import annotations

import itertools
import logging
import re
import shutil
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from ..config import settings
from ..core.deps import DB, CurrentAdmin, CurrentUser
from ..core.security import decrypt_secret, encrypt_secret
from ..models import (
    DOWNLOADABLE_STATUSES,
    MODEL_EDITABLE_STATUSES,
    PIN_BLOCKING_STATUSES,
    TERMINAL_STATUSES,
    AuditLog,
    BillingRecord,
    BillingStatus,
    Job,
    JobError,
    JobStatus,
    ModelConfig,
    ModelSource,
    Project,
    User,
    UserRole,
)
from ..schemas import (
    JobBatchProject,
    JobBatchProjectOut,
    JobBillingOut,
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
    PauseAllOut,
    QueueOut,
    QueueReorder,
    UploadValidateOut,
)
from ..services import billing
from ..services import jsonl as jsonl_svc
from ..services import results as results_svc
from ..services.gateway import (
    GatewayError,
    billing_applies,
    billing_config_error,
    billing_mode,
    build_personal_config,
    list_user_models,
)
from ..services.inference import InferenceClient, build_request_body, resolve_params
from ..services.pins import as_timestamp, enqueue_job, preempt_for
from ..services.projects import ensure_default_project
from ..services.queue import queue
from ..services.settings_store import read_runtime
from ..services.storage import QUOTA_MESSAGE, check_before_upload
from .admin_settings import WORKER_STALE_SECONDS, get_runtime_settings

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
            select(Job)
            .options(selectinload(Job.model_config), selectinload(Job.user), selectinload(Job.project))
            .where(Job.id == job_id)
        )
    ).scalar_one_or_none()
    if job is None or not _can_access(user, job):
        # 无权访问也返回 404，不泄露任务是否存在
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    return job


async def _check_project(db: DB, owner_id: str, project_id: str | None) -> str:
    """项目只能是任务提交者本人的；管理员替别人归类也只能挪进对方自己的项目。

    任务总要归属某个项目：没指定（命令行等老客户端不传）就放进提交者的默认项目。
    """
    if not project_id:
        return (await ensure_default_project(db, owner_id)).id
    project = await db.get(Project, project_id)
    if project is None or project.user_id != owner_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "项目不存在")
    return project.id


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
        project_id=job.project_id,
        project_name=job.project.name if job.project_id and job.project else None,
        pinned=job.pinned,
        pinned_at=job.pinned_at,
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
        cached_tokens=job.cached_tokens,
        billing_enabled=job.billing_enabled,
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

    project_id = await _check_project(db, user.id, payload.project_id)

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
        project_id=project_id,
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
        # 只是先标上给前端看；worker 每次开跑时会按当时的设置再定一次
        billing_enabled=personal and billing_applies(payload.personal_model or "", await read_runtime(db)),
    )
    db.add(job)
    await db.commit()

    try:
        await enqueue_job(queue, job)
    except Exception as exc:
        log.exception("任务 %s 入队失败", job.id)
        job.status = JobStatus.failed
        job.error = f"入队失败，请检查 Redis 连接: {exc}"
        await db.commit()
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(job.error)) from exc

    await db.refresh(job, ["user", "model_config", "project"])
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

    note, billing_ok = None, None
    if payload.model_source == ModelSource.personal.value:
        # 重定向的模型名不给用户看：回显的请求体和响应里都换回用户选的名字
        _mask_upstream(body, mc)
        _mask_upstream(out.get("response"), mc)
        if out.get("ok"):
            note, billing_ok = await _bill_dry_run(db, user, payload, out.get("usage") or {})

    return JobDryRunOut(
        custom_id=item.custom_id, item_index=payload.item_index, request_body=body,
        billing=note, billing_ok=billing_ok, **out,
    )


def _mask_upstream(data: object, mc: ModelConfig) -> None:
    if isinstance(data, dict) and mc.model_name != mc.display_name and data.get("model") == mc.model_name:
        data["model"] = mc.display_name


async def _bill_dry_run(db: DB, user: User, payload: JobDryRun, usage: dict) -> tuple[str | None, bool | None]:
    """独立计费时，试跑那一条用的是平台的推理 Key，也要向用户扣费。

    返回（说明，是否扣上）。扣费失败或计费接口不通时前端不提交任务 ——
    整批跑起来同样会扣不上，不如现在就让用户处理。
    """
    if not billing_applies(payload.personal_model or "", await read_runtime(db)):
        return None, None
    record = billing.record_for_try(user, payload.personal_model or "", usage)
    if record is None:
        return "网关没有返回用量，本次试跑未计费", None
    if payload.personal_token:
        record.key_encrypted = encrypt_secret(payload.personal_token)
    db.add(record)
    await db.commit()

    sent = await billing.send_record(record.id)
    if sent is None or sent.status == BillingStatus.pending:
        return "试跑计费失败：计费接口暂时不可用，请稍后再试（这一条稍后会自动补扣）", False
    if sent.status == BillingStatus.failed:
        # 说明文案是按暂停中的任务写的（点「继续」）；这里还没有任务，改成重新提交
        hint = billing.describe(sent.error_code, sent.error, sent.model_name)
        hint = re.sub(r"(后)?点「继续」(即可补扣并接着跑)?", r"\1重新提交", hint)
        return "试跑计费失败：" + hint, False
    return f"已按用量扣费（{sent.prompt_tokens} 输入 + {sent.completion_tokens} 输出 tokens）", True


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

    billed = billing_mode(runtime)
    token = payload.personal_token or decrypt_secret(user.llm_token_encrypted)
    if not token:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "请先填写你的 batch 分组 Key" if billed else "请先填写你的网关 token",
        )

    # 提交时再校验一次权限：token 可能已被网关回收，或模型权限被调整。
    # 早失败好过让任务排到队列里再整批报错。
    try:
        allowed, info = await list_user_models(runtime, token)
        # 这次带了新 Key：「限制 API Key 添加」开着时先确认是 batch 分组
        if billed and payload.personal_token and runtime.get("user_gateway_billing_require_batch_key"):
            await billing.verify_batch_key(runtime, payload.personal_token, info)
    except GatewayError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message) from exc

    if payload.personal_model not in allowed:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"你的 Key 不能使用模型 {payload.personal_model}，请重新选择"
            if billed else f"你的 token 没有模型 {payload.personal_model} 的权限，请重新选择",
        )

    # 分模型配置可以单独指定推理接口，选定模型后再确认它的推理接口配齐了
    if billed and (problem := billing_config_error(runtime, payload.personal_model)):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, problem)

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
    project: str | None = Query(None, description="项目 id；none 表示未归类的任务"),
) -> JobListOut:
    stmt = select(Job).options(
        selectinload(Job.model_config), selectinload(Job.user), selectinload(Job.project)
    )
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
    if project:
        cond = Job.project_id.is_(None) if project == "none" else Job.project_id == project
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)

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


# --------------------------------------------------------------------------- #
# 管理员：队列排序与置顶
# --------------------------------------------------------------------------- #
QUEUE_VIEW_LIMIT = 500


def _audit(db: DB, admin: User, action: str, target: str | None, detail: dict) -> None:
    db.add(AuditLog(user_id=admin.id, username=admin.username, action=action, target=target, detail=detail))


@router.get("/queue", response_model=QueueOut)
async def get_queue(_: CurrentAdmin, db: DB, limit: int = Query(200, ge=1, le=QUEUE_VIEW_LIMIT)) -> QueueOut:
    """置顶任务 + 按领取顺序排好的排队任务（前 limit 个）。"""
    return await _queue_view(db, limit)


async def _queue_view(db: DB, limit: int) -> QueueOut:
    opts = (selectinload(Job.model_config), selectinload(Job.user), selectinload(Job.project))
    # 置顶任务连同暂停中的一起列出：暂停期间不占模型，但管理员得知道它还挂着置顶
    pinned = (
        await db.execute(
            select(Job).options(*opts)
            .where(Job.pinned.is_(True), Job.status.in_([*PIN_BLOCKING_STATUSES, JobStatus.paused]))
            .order_by(Job.pinned_at)
        )
    ).scalars().all()

    order = await queue.peek(0, limit)
    rows = {
        j.id: j for j in (
            await db.execute(select(Job).options(*opts).where(Job.id.in_(order)))
        ).scalars().all()
    } if order else {}
    pinned_ids = {j.id for j in pinned}
    positions = {jid: i + 1 for i, jid in enumerate(order)}
    live = await _safe_progress_many([j.id for j in pinned if j.status == JobStatus.running])

    return QueueOut(
        pinned=[_to_out(j, positions.get(j.id), live.get(j.id)) for j in pinned],
        queued=[
            _to_out(rows[jid], positions[jid])
            for jid in order if jid in rows and jid not in pinned_ids
        ],
        total_queued=await queue.queued_count(),
    )


@router.post("/queue/reorder", response_model=QueueOut)
async def reorder_queue(payload: QueueReorder, admin: CurrentAdmin, db: DB) -> QueueOut:
    """按给定先后重排排队中的任务。只在列出的任务之间调换，没列出的位置不变。"""
    jobs = {
        j.id: j for j in (
            await db.execute(select(Job).where(Job.id.in_(payload.job_ids)))
        ).scalars().all()
    }
    for jid in payload.job_ids:
        job = jobs.get(jid)
        if job is None or job.status != JobStatus.queued:
            raise HTTPException(status.HTTP_409_CONFLICT, "队列已经变化（有任务开始运行或被取消），请刷新后重试")
        if job.pinned:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "置顶任务固定在队首，不参与排序")

    try:
        scores = await queue.reorder(payload.job_ids)
    except KeyError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "队列已经变化（有任务开始运行或被取消），请刷新后重试"
        ) from exc

    # 优先级跟着新位置走，之后改优先级、被置顶任务挤回队列时仍大致保持这个顺序
    for jid, score in scores.items():
        jobs[jid].priority = queue.priority_of(score)
    _audit(db, admin, "queue_reorder", None, {"job_ids": payload.job_ids[:50], "count": len(payload.job_ids)})
    await db.commit()
    return await _queue_view(db, max(200, len(payload.job_ids)))


@router.post("/{job_id}/pin", response_model=JobOut)
async def pin_job(job_id: str, admin: CurrentAdmin, db: DB) -> JobOut:
    """置顶：排到队首，同模型正在跑的其他任务让出来回到队列，等它结束后再继续。"""
    job = await _get_job_or_404(db, admin, job_id)
    if job.status not in {JobStatus.queued, JobStatus.running, JobStatus.paused}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "只有排队中、运行中或已暂停的任务可以置顶")
    if not job.pinned:
        job.pinned = True
        job.pinned_at = datetime.now(UTC)
    _audit(db, admin, "job_pin", job.id, {"name": job.name})
    await db.commit()

    if job.status == JobStatus.queued:
        await queue.rescore(job.id, job.priority, pinned=True, ts=as_timestamp(job.pinned_at))
    if job.status in PIN_BLOCKING_STATUSES:
        await preempt_for(db, queue, job.id)

    await db.refresh(job, ["user", "model_config", "project"])
    position = await _safe_position(job.id) if job.status == JobStatus.queued else None
    return _to_out(job, position)


@router.post("/{job_id}/unpin", response_model=JobOut)
async def unpin_job(job_id: str, admin: CurrentAdmin, db: DB) -> JobOut:
    """取消置顶：回到按优先级排队，被它挤回队列的任务随即可以继续被领取。"""
    job = await _get_job_or_404(db, admin, job_id)
    if not job.pinned:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "该任务没有置顶")
    job.pinned = False
    job.pinned_at = None
    _audit(db, admin, "job_unpin", job.id, {"name": job.name})
    await db.commit()

    if job.status == JobStatus.queued:
        await queue.rescore(job.id, job.priority, ts=as_timestamp(job.queued_at))

    await db.refresh(job, ["user", "model_config", "project"])
    position = await _safe_position(job.id) if job.status == JobStatus.queued else None
    return _to_out(job, position)


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
    # project_id 显式传 null 表示挪回默认项目，不能跟着 exclude_none 一起丢掉
    if "project_id" in payload.model_fields_set:
        data["project_id"] = await _check_project(db, job.user_id, payload.project_id)

    if "priority" in data and job.status != JobStatus.queued:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "只有排队中的任务可以调整优先级")

    for key, value in data.items():
        setattr(job, key, value)
    await db.commit()

    if "priority" in data:
        # 重新入队以刷新排序分值（置顶任务不看优先级，位置不变）
        await queue.remove(job.id)
        await enqueue_job(queue, job)

    await db.refresh(job, ["user", "model_config", "project"])
    return _to_out(job, await _safe_position(job.id))


@router.post("/batch-project", response_model=JobBatchProjectOut)
async def move_jobs_to_project(payload: JobBatchProject, user: CurrentUser, db: DB) -> JobBatchProjectOut:
    """把自己的一批任务挪进某个项目；project_id 为空表示挪回默认项目。"""
    project_id = await _check_project(db, user.id, payload.project_id)
    jobs = (
        await db.execute(select(Job).where(Job.id.in_(payload.job_ids), Job.user_id == user.id))
    ).scalars().all()
    for job in jobs:
        job.project_id = project_id
    await db.commit()
    return JobBatchProjectOut(updated=len(jobs))


async def _worker_gone(worker_id: str | None) -> bool:
    """任务名下的 worker 是否已经没了心跳（被强杀、OOM、宕机）。

    这种任务没有谁会响应暂停/取消信号，只能由接口直接落状态，否则会永远卡在「运行中」。
    Redis 读不到时按「还活着」处理 —— 宁可让用户过会儿再点，也不能把正在跑的任务误改掉。
    """
    if not worker_id:
        return True
    try:
        last_seen = await queue.worker_last_seen(worker_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("读取 worker %s 心跳失败: %s", worker_id, exc)
        return False
    return last_seen is None or time.time() - last_seen >= WORKER_STALE_SECONDS


async def _settle_orphan(job: Job, new_status: JobStatus) -> None:
    """替已经没了的 worker 收尾。

    计数取库里与 Redis 实时进度中较大的：worker 每 10s 才写一次库，Redis 里的更接近真实值。
    恢复时还会按结果文件重新统计，这里差一点也不影响续跑。
    """
    live = (await _safe_progress_many([job.id])).get(job.id) or {}
    job.completed_items = max(job.completed_items, int(float(live.get("completed", 0))))
    job.failed_items = max(job.failed_items, int(float(live.get("failed", 0))))
    job.status = new_status
    job.worker_id = None
    if new_status == JobStatus.canceled:
        job.finished_at = datetime.now(UTC)
        job.pinned = False


@router.post("/{job_id}/cancel", response_model=JobOut)
async def cancel_job(job_id: str, user: CurrentUser, db: DB) -> JobOut:
    job = await _get_job_or_404(db, user, job_id)
    if job.status in TERMINAL_STATUSES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "任务已结束，无法取消")
    orphaned = job.status == JobStatus.running and await _worker_gone(job.worker_id)

    await queue.signal(job.id, "cancel")
    await queue.remove(job.id)

    if orphaned:
        await _settle_orphan(job, JobStatus.canceled)
        await db.commit()
        # 信号留着：万一 worker 只是卡顿、其实还活着，看到信号仍会自己停下
    elif job.status in {JobStatus.queued, JobStatus.pending, JobStatus.paused}:
        # 排队中的任务不会有 worker 来响应信号，这里直接落终态
        job.status = JobStatus.canceled
        job.finished_at = datetime.now(UTC)
        job.pinned = False
        await db.commit()
        await queue.clear_signal(job.id)

    await db.refresh(job, ["user", "model_config", "project"])
    return _to_out(job)


async def _pause(job: Job, db: DB) -> bool:
    """暂停一个排队中/运行中的任务。

    返回 True 表示已直接落成暂停；False 表示发了信号、要等 worker 自己收尾。
    """
    orphaned = job.status == JobStatus.running and await _worker_gone(job.worker_id)

    await queue.signal(job.id, "pause")
    await queue.remove(job.id)
    if orphaned:
        await _settle_orphan(job, JobStatus.paused)
        await db.commit()
        return True
    if job.status == JobStatus.queued:
        job.status = JobStatus.paused
        await db.commit()
        await queue.clear_signal(job.id)
        return True
    return False


@router.post("/pause-all", response_model=PauseAllOut)
async def pause_all_jobs(_: CurrentAdmin, db: DB) -> PauseAllOut:
    """管理员一键暂停全站排队中和运行中的任务，例如重建镜像前。

    每个任务的处理与单个暂停完全一样；之后在任务列表里恢复即可断点续跑。
    """
    jobs = (
        await db.execute(
            select(Job).where(Job.status.in_([JobStatus.queued, JobStatus.running]))
        )
    ).scalars().all()

    paused = signaled = failed = 0
    for job in jobs:
        try:
            if await _pause(job, db):
                paused += 1
            else:
                signaled += 1
        except Exception:
            # 一个失败不该挡住其余任务
            log.exception("暂停任务 %s 失败", job.id)
            await db.rollback()
            failed += 1
    return PauseAllOut(paused=paused, signaled=signaled, failed=failed)


@router.post("/{job_id}/pause", response_model=JobOut)
async def pause_job(job_id: str, user: CurrentUser, db: DB) -> JobOut:
    job = await _get_job_or_404(db, user, job_id)
    if job.status not in {JobStatus.running, JobStatus.queued}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "只有排队中或运行中的任务可以暂停")
    await _pause(job, db)

    await db.refresh(job, ["user", "model_config", "project"])
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
    await enqueue_job(queue, job)
    if job.pinned:
        # 置顶任务暂停期间别的任务用上了这个模型，恢复时重新抢占
        await preempt_for(db, queue, job.id)

    await db.refresh(job, ["user", "model_config", "project"])
    return _to_out(job, await _safe_position(job.id))


@router.post("/{job_id}/model", response_model=JobOut)
async def change_model(job_id: str, payload: JobModelChange, user: CurrentUser, db: DB) -> JobOut:
    """给停下来的任务换模型、改推理参数，恢复后剩余条目按新配置跑。

    已完成的条目不会重跑，结果文件里会同时有新旧两份配置的输出。
    params 不传就沿用原任务的那份；无论传没传，都按（新）模型的默认值/强制值/白名单重新合并。
    """
    job = await _get_job_or_404(db, user, job_id)
    if job.status not in MODEL_EDITABLE_STATUSES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "只有已暂停/已取消/失败的任务可以更换模型或修改参数"
        )

    personal = payload.model_source == ModelSource.personal.value
    if personal:
        same_model = (
            job.model_source == ModelSource.personal
            and job.personal_model_name == payload.personal_model
        )
        if job.user_id == user.id:
            mc = await _resolve_personal_model(payload, user, db)
        elif same_model:
            # 管理员只改别人任务的参数：模型没动，用不着拿自己的 token 验权限。
            # 拼一份不带 token 的配置只为合并参数，执行时 worker 用的仍是提交者本人的 token
            mc = build_personal_config(payload.personal_model, "", await read_runtime(db))
        else:
            # worker 执行时用的是任务提交者本人的 token；
            # 管理员替别人换成自己网关里的模型，排上队也跑不起来
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "个人网关模型只能由任务提交者本人设置")
    else:
        mc = await _resolve_shared_model(payload, user, db)

    # 换模型前把旧模型的用量结成一笔，计费起点挪到当前用量
    await billing.cut_before_change(
        db, job, personal and billing_applies(payload.personal_model or "", await read_runtime(db)),
    )
    job.model_source = ModelSource.personal if personal else ModelSource.shared
    job.model_config_id = None if personal else mc.id
    job.personal_model_name = payload.personal_model if personal else None
    if payload.params is not None:
        job.params = payload.params.model_dump(exclude_none=True)
    job.resolved_params = _resolve_job_params(mc, JobParams.model_validate(job.params or {}))
    if job.concurrency:
        # 原来的并发是按旧模型上限截过的，换了模型要按新上限再截一次
        job.concurrency = min(job.concurrency, mc.max_concurrency)
    await db.commit()

    await db.refresh(job, ["user", "model_config", "project"])
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
    await enqueue_job(queue, job)

    await db.refresh(job, ["user", "model_config", "project"])
    return _to_out(job, await _safe_position(job.id))


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_job(job_id: str, user: CurrentUser, db: DB) -> None:
    job = await _get_job_or_404(db, user, job_id)
    if job.status in {JobStatus.running, JobStatus.queued}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请先取消任务再删除")

    input_path = job.input_path
    if job.billing_enabled:
        # 任务删了，没切出来的用量也要先记下来，计费记录不随任务删除
        await billing.cut(db, job.id)
        job = await _get_job_or_404(db, user, job_id)
    await db.delete(job)
    await db.commit()
    await queue.remove(job_id)
    await queue.clear_signal(job_id)
    results_svc.delete_job_files(job_id, input_path)


# --------------------------------------------------------------------------- #
# 个人网关计费
# --------------------------------------------------------------------------- #
async def _billing_out(db: DB, job: Job, message: str | None = None) -> JobBillingOut:
    from .billing import record_out

    summary = await billing.job_summary(db, job)
    rows = (
        await db.execute(
            select(BillingRecord)
            .where(BillingRecord.job_id == job.id)
            .order_by(BillingRecord.id.desc())
            .limit(50)
        )
    ).scalars().all()
    return JobBillingOut(**summary, records=[record_out(r) for r in rows], message=message)


@router.get("/{job_id}/billing", response_model=JobBillingOut)
async def job_billing(job_id: str, user: CurrentUser, db: DB) -> JobBillingOut:
    job = await _get_job_or_404(db, user, job_id)
    return await _billing_out(db, job)


@router.post("/{job_id}/billing/retry", response_model=JobBillingOut)
async def retry_job_billing(job_id: str, user: CurrentUser, db: DB) -> JobBillingOut:
    """立即重发这个任务没扣上的计费记录（比如充值之后）。同一个 request_id 不会重复扣。"""
    job = await _get_job_or_404(db, user, job_id)
    owner = await db.get(User, job.user_id)
    if owner is not None:
        await billing.rekey_failed(db, job.id, owner)
    if job.status != JobStatus.running:
        await billing.cut(db, job.id)
    result = await billing.settle_job(job.id, include_failed=True)
    if result.problem:
        message = result.problem
    elif result.sent:
        message = f"已重发 {result.sent} 笔"
    else:
        message = "没有需要重发的计费记录"
    job = await _get_job_or_404(db, user, job_id)
    await db.refresh(job)
    return await _billing_out(db, job, message)
