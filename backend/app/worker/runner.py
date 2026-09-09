"""单个任务的执行器：读输入 → 并发调用模型 → 结果落盘 → 回写进度。"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, update

from ..config import settings
from ..core.security import decrypt_secret
from ..db import session_scope
from ..models import Job, JobError, JobStatus, ModelConfig, ModelSource, User
from ..services import results as results_svc
from ..services.gateway import build_personal_config
from ..services.inference import InferenceClient, InferenceError, build_request_body
from ..services.jsonl import JsonlError, iter_items
from ..services.notifications import notify_job
from ..services.queue import queue
from ..services.settings_store import read_runtime

log = logging.getLogger(__name__)

PROGRESS_FLUSH_SECONDS = 3.0   # Redis 进度刷新间隔
DB_FLUSH_SECONDS = 10.0        # 数据库计数刷新间隔
SIGNAL_POLL_SECONDS = 2.0      # 暂停/取消信号轮询间隔


class _Stop(Exception):
    """任务被取消或暂停时用于中断分发循环。"""

    def __init__(self, status: JobStatus):
        self.status = status


class JobRunner:
    def __init__(self, job_id: str, worker_id: str) -> None:
        self.job_id = job_id
        self.worker_id = worker_id
        self.completed = 0
        self.failed = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.error_samples: list[dict] = []
        self._stored_errors = 0
        self._stop_status: JobStatus | None = None
        self._last_progress_flush = 0.0
        self._last_db_flush = 0.0
        self._counter_lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    async def run(self) -> JobStatus:
        job, mc, load_error = await self._load()
        if job is None:
            log.warning("任务 %s 不存在，跳过", self.job_id)
            return JobStatus.failed

        if job.status in {JobStatus.canceled, JobStatus.succeeded, JobStatus.completed}:
            log.info("任务 %s 状态为 %s，无需执行", self.job_id, job.status.value)
            return job.status

        if mc is None or not mc.enabled:
            await self._fail_job(load_error or "模型配置不存在或已被禁用")
            return JobStatus.failed

        input_file = Path(job.input_path)
        if not input_file.exists():
            await self._fail_job(self._missing_input_message(job))
            return JobStatus.failed

        # 断点续跑：读回已有结果
        state = await asyncio.to_thread(results_svc.scan_resume_state, self.job_id)
        self.completed = state.completed
        self.failed = state.failed
        self.prompt_tokens = state.prompt_tokens
        self.completion_tokens = state.completion_tokens
        if state.done_indices:
            log.info("任务 %s 断点续跑，已完成 %d 条", self.job_id, len(state.done_indices))

        await self._mark_running()

        concurrency = job.concurrency or mc.max_concurrency or settings.default_item_concurrency
        writer = results_svc.ResultWriter(self.job_id)
        stop_event = asyncio.Event()
        watcher = asyncio.create_task(self._watch_signals(stop_event))

        final_status: JobStatus
        try:
            async with InferenceClient(mc) as client:
                await self._dispatch(
                    job=job, mc=mc, client=client, writer=writer,
                    input_file=input_file, done=state.done_indices,
                    concurrency=concurrency, stop_event=stop_event,
                )
            final_status = JobStatus.succeeded if self.failed == 0 else JobStatus.completed
        except _Stop as stop:
            final_status = stop.status
        except InferenceError as exc:
            log.error("任务 %s 模型客户端初始化失败: %s", self.job_id, exc)
            await writer.flush()
            await self._fail_job(str(exc))
            watcher.cancel()
            return JobStatus.failed
        except JsonlError as exc:
            log.error("任务 %s 输入解析失败: %s", self.job_id, exc)
            await writer.flush()
            await self._fail_job(f"输入文件解析失败: {exc}")
            watcher.cancel()
            return JobStatus.failed
        except Exception as exc:
            log.exception("任务 %s 执行异常", self.job_id)
            await writer.flush()
            await self._fail_job(f"执行异常: {exc}")
            watcher.cancel()
            return JobStatus.failed
        finally:
            watcher.cancel()

        await writer.flush()
        await self._finalize(final_status)
        return final_status

    def _missing_input_message(self, job: Job) -> str:
        """输入文件不见了。区分两种原因，否则最难查的那种会看起来像文件被删了。"""
        upload_dir = str(settings.upload_dir)
        if not job.input_path.startswith(upload_dir):
            return (
                f"输入文件不在本 worker 的数据目录下：任务的路径是 {job.input_path}，"
                f"而本 worker 的数据目录是 {upload_dir}。"
                "通常是多个部署（例如宿主机与容器）共用了同一个 Redis 队列和数据库，"
                "但各自的 DATA_DIR 指向不同位置 —— 任务被无法访问该文件的 worker 领走了。"
                "请让它们共享同一份存储，或给不同部署设置不同的 QUEUE_KEY_PREFIX。"
            )
        return f"输入文件已丢失: {job.input_path}"

    # ------------------------------------------------------------------ #
    async def _dispatch(
        self, *, job: Job, mc: ModelConfig, client: InferenceClient,
        writer: results_svc.ResultWriter, input_file: Path, done: set[int],
        concurrency: int, stop_event: asyncio.Event,
    ) -> None:
        sem = asyncio.Semaphore(max(1, concurrency))
        system_prompt = (job.resolved_params or {}).get("_system_prompt")
        resolved = {k: v for k, v in (job.resolved_params or {}).items() if not k.startswith("_")}
        pending: set[asyncio.Task] = set()

        async def process(index: int, custom_id: str, body: dict) -> None:
            async with sem:
                if stop_event.is_set():
                    return
                try:
                    result = await client.complete(body)
                except InferenceError as exc:
                    await writer.write_failure(index, custom_id, exc.message, exc.status_code, mc.max_retries + 1)
                    await self._bump(failed=1, error={
                        "item_index": index, "custom_id": custom_id,
                        "status_code": exc.status_code, "attempts": mc.max_retries + 1,
                        "message": exc.message[:4000],
                    })
                except Exception as exc:  # noqa: BLE001
                    await writer.write_failure(index, custom_id, f"内部错误: {exc}", None, 1)
                    await self._bump(failed=1, error={
                        "item_index": index, "custom_id": custom_id,
                        "status_code": None, "attempts": 1, "message": str(exc)[:4000],
                    })
                else:
                    await writer.write_success(index, custom_id, result)
                    await self._bump(
                        completed=1,
                        prompt_tokens=result.prompt_tokens,
                        completion_tokens=result.completion_tokens,
                    )

        try:
            for item in iter_items(input_file):
                if stop_event.is_set():
                    raise _Stop(self._stop_status or JobStatus.canceled)
                if item.index in done:
                    continue

                body = build_request_body(mc, item.body, resolved, system_prompt)
                task = asyncio.create_task(process(item.index, item.custom_id, body))
                pending.add(task)
                task.add_done_callback(pending.discard)

                # 控制在途任务数量，避免一次性把几十万条全部建成 Task
                if len(pending) >= concurrency * 4:
                    _, pending_set = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                    pending = set(pending_set)

                await self._maybe_flush()

            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        finally:
            for task in pending:
                task.cancel()
            await writer.flush()
            await self._flush_db()

    # ------------------------------------------------------------------ #
    async def _bump(
        self, *, completed: int = 0, failed: int = 0,
        prompt_tokens: int = 0, completion_tokens: int = 0, error: dict | None = None,
    ) -> None:
        async with self._counter_lock:
            self.completed += completed
            self.failed += failed
            self.prompt_tokens += prompt_tokens
            self.completion_tokens += completion_tokens
            if error and self._stored_errors < results_svc.MAX_STORED_ERRORS:
                self.error_samples.append(error)
                self._stored_errors += 1

    async def _maybe_flush(self) -> None:
        now = time.monotonic()
        if now - self._last_progress_flush >= PROGRESS_FLUSH_SECONDS:
            self._last_progress_flush = now
            await queue.set_progress(self.job_id, self.completed, self.failed)
            await queue.renew_lease(self.job_id)
        if now - self._last_db_flush >= DB_FLUSH_SECONDS:
            self._last_db_flush = now
            await self._flush_db()

    async def _flush_db(self) -> None:
        async with self._counter_lock:
            completed, failed = self.completed, self.failed
            ptok, ctok = self.prompt_tokens, self.completion_tokens
            errors, self.error_samples = self.error_samples, []

        # 顺带把结果文件体积写回去，存储配额统计要用（两次 stat，很便宜）
        sizes = results_svc.result_sizes(self.job_id)
        result_size = sizes["output_bytes"] + sizes["errors_bytes"]

        async with session_scope() as db:
            await db.execute(
                update(Job)
                .where(Job.id == self.job_id)
                .values(
                    completed_items=completed, failed_items=failed,
                    prompt_tokens=ptok, completion_tokens=ctok,
                    result_size=result_size,
                )
            )
            if errors:
                db.add_all([JobError(job_id=self.job_id, **e) for e in errors])

    # ------------------------------------------------------------------ #
    async def _watch_signals(self, stop_event: asyncio.Event) -> None:
        """轮询 Redis 控制键，同时兼作租约续期心跳。"""
        try:
            while not stop_event.is_set():
                await asyncio.sleep(SIGNAL_POLL_SECONDS)
                try:
                    signal = await queue.get_signal(self.job_id)
                    await queue.renew_lease(self.job_id)
                except Exception as exc:  # noqa: BLE001 — Redis 抖动不应中断任务
                    log.warning("任务 %s 信号轮询失败: %s", self.job_id, exc)
                    continue
                if signal == "cancel":
                    self._stop_status = JobStatus.canceled
                    stop_event.set()
                elif signal == "pause":
                    self._stop_status = JobStatus.paused
                    stop_event.set()
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------ #
    async def _load(self) -> tuple[Job | None, ModelConfig | None, str | None]:
        """取任务与它要用的模型配置。

        个人模型没有库里的配置记录 —— 现场用该用户当前的网关 token 拼一个。
        用「当前」而不是提交时快照的 token，是为了让用户换 token 后
        排队中的任务能继续跑，同时也让撤销 token 立即生效。
        """
        async with session_scope() as db:
            job = (await db.execute(select(Job).where(Job.id == self.job_id))).scalar_one_or_none()
            if job is None:
                return None, None, None

            if job.model_source == ModelSource.personal:
                if not job.personal_model_name:
                    return job, None, "任务缺少个人模型名，无法执行"
                user = (
                    await db.execute(select(User).where(User.id == job.user_id))
                ).scalar_one_or_none()
                if user is None:
                    return job, None, "提交该任务的用户已被删除"
                token = decrypt_secret(user.llm_token_encrypted)
                if not token:
                    return job, None, (
                        "找不到可用的网关 token —— 你可能清除了它，或平台 SECRET_KEY 已变更。"
                        "请在「新建任务」页重新填写 token 后恢复本任务。"
                    )
                runtime = await read_runtime(db)
                if not runtime["user_gateway_enabled"]:
                    return job, None, "管理员已关闭个人网关"
                return job, build_personal_config(job.personal_model_name, token, runtime), None

            mc = None
            if job.model_config_id:
                mc = (
                    await db.execute(select(ModelConfig).where(ModelConfig.id == job.model_config_id))
                ).scalar_one_or_none()
            return job, mc, None

    async def _mark_running(self) -> None:
        async with session_scope() as db:
            await db.execute(
                update(Job)
                .where(Job.id == self.job_id)
                .values(
                    status=JobStatus.running,
                    worker_id=self.worker_id,
                    started_at=datetime.now(UTC),
                    error=None,
                    completed_items=self.completed,
                    failed_items=self.failed,
                )
            )
        await queue.set_progress(self.job_id, self.completed, self.failed)

    async def _fail_job(self, message: str) -> None:
        async with session_scope() as db:
            await db.execute(
                update(Job)
                .where(Job.id == self.job_id)
                .values(
                    status=JobStatus.failed, error=message[:2000],
                    finished_at=datetime.now(UTC),
                )
            )
        await queue.clear_signal(self.job_id)
        await notify_job(self.job_id)

    async def _finalize(self, status: JobStatus) -> None:
        await self._flush_db()
        sizes = results_svc.result_sizes(self.job_id)
        values: dict = {
            "status": status,
            "completed_items": self.completed,
            "failed_items": self.failed,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "result_size": sizes["output_bytes"] + sizes["errors_bytes"],
        }
        if status in {JobStatus.succeeded, JobStatus.completed, JobStatus.canceled}:
            values["finished_at"] = datetime.now(UTC)
        if status == JobStatus.paused:
            values["worker_id"] = None

        async with session_scope() as db:
            await db.execute(update(Job).where(Job.id == self.job_id).values(**values))

        await queue.set_progress(self.job_id, self.completed, self.failed)
        await queue.clear_signal(self.job_id)
        log.info(
            "任务 %s 结束：status=%s 成功=%d 失败=%d",
            self.job_id, status.value, self.completed, self.failed,
        )
        # 暂停只是中途停下，不发通知
        if status != JobStatus.paused:
            await notify_job(self.job_id)
