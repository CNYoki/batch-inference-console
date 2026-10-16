"""管理员置顶任务。

置顶任务在排队/运行期间独占它的模型：
- 队列里排在所有普通任务前面；
- 同模型正在跑的其他任务收到 requeue 信号，停下后带着原来的入队时间回到队列；
- 它结束（成功、完成、失败、取消）之前，worker 不会再领同模型的其他任务。

「同模型」与单模型任务数上限的口径一致：按真实模型名算，公用模型和个人网关同名算同一个。
置顶任务被暂停时不再占着模型，恢复后重新抢占。
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import PIN_BLOCKING_STATUSES, Job, JobStatus
from .model_limits import job_model_names
from .queue import JobQueue

log = logging.getLogger(__name__)


async def pinned_models(db: AsyncSession) -> dict[str, set[str]]:
    """真实模型名 → 正占着它的置顶任务 id。"""
    pinned_ids = list(
        (
            await db.execute(
                select(Job.id).where(Job.pinned.is_(True), Job.status.in_(PIN_BLOCKING_STATUSES))
            )
        ).scalars().all()
    )
    out: dict[str, set[str]] = {}
    for job_id, model in (await job_model_names(db, pinned_ids)).items():
        out.setdefault(model, set()).add(job_id)
    return out


async def blocked_by_pin(db: AsyncSession, job_id: str) -> bool:
    """该任务是否被同模型的置顶任务挡住（它自己是置顶任务时不算）。"""
    model = (await job_model_names(db, [job_id])).get(job_id)
    if not model:
        return False
    holders = (await pinned_models(db)).get(model)
    return bool(holders) and job_id not in holders


async def preempt_for(db: AsyncSession, queue: JobQueue, pinned_job_id: str) -> list[str]:
    """让同模型上正在跑的其他非置顶任务让出来，返回发了信号的任务 id。

    除了库里状态为 running 的，还要算上 Redis 里登记在该模型名下、持有租约的 ——
    刚被领走的任务在库里还是 queued，漏掉它就会和置顶任务一起跑。
    万一还是有漏网的，worker 启动任务前也会再查一次置顶（见 JobRunner.run）。
    """
    model = (await job_model_names(db, [pinned_job_id])).get(pinned_job_id)
    if not model:
        return []

    running_ids = list(
        (await db.execute(select(Job.id).where(Job.status == JobStatus.running))).scalars().all()
    )
    candidates = {jid for jid, name in (await job_model_names(db, running_ids)).items() if name == model}
    candidates |= await queue.running_on_model(model)
    candidates.discard(pinned_job_id)
    if not candidates:
        return []

    # 别的置顶任务不让：同一个模型上多个置顶任务可以一起跑
    pinned = set(
        (
            await db.execute(select(Job.id).where(Job.id.in_(candidates), Job.pinned.is_(True)))
        ).scalars().all()
    )
    signaled: list[str] = []
    for job_id in sorted(candidates - pinned):
        if not await queue.has_lease(job_id):
            # 没人在跑（已结束、已被回收），发信号只会在它下次启动时误伤
            continue
        await queue.signal(job_id, "requeue", only_if_absent=True)
        signaled.append(job_id)
    if signaled:
        log.info("置顶任务 %s 抢占模型 %s，让出的任务: %s", pinned_job_id, model, signaled)
    return signaled


def as_timestamp(value: datetime | None) -> float | None:
    if value is None:
        return None
    # SQLite 读回来的是不带时区的 UTC 时间
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).timestamp()


async def enqueue_job(queue: JobQueue, job: Job, *, keep_position: bool = False) -> None:
    """按任务当前的优先级 / 置顶状态入队。

    置顶任务按置顶时间排；keep_position 时普通任务按原入队时间排，
    这样被挤回队列的任务不会排到后来者后面。
    """
    if job.pinned:
        ts = as_timestamp(job.pinned_at)
    elif keep_position:
        ts = as_timestamp(job.queued_at)
    else:
        ts = None
    await queue.enqueue(job.id, job.priority, pinned=job.pinned, ts=ts)


async def requeue_preempted(db: AsyncSession, queue: JobQueue, job_id: str) -> None:
    """worker 让出任务、释放租约之后，把它放回队列。

    让出的过程中用户可能又点了暂停/取消（信号被覆盖成了 cancel/pause），
    这时就别再入队了，直接按用户的意思落状态。
    """
    job = await db.get(Job, job_id)
    if job is None or job.status != JobStatus.queued:
        return
    signal = await queue.get_signal(job_id)
    if signal in {"cancel", "pause"}:
        job.status = JobStatus.canceled if signal == "cancel" else JobStatus.paused
        if job.status == JobStatus.canceled:
            job.finished_at = datetime.now(UTC)
            job.pinned = False
        await db.commit()
        await queue.clear_signal(job_id)
        return
    await queue.clear_signal(job_id)
    await enqueue_job(queue, job, keep_position=True)
