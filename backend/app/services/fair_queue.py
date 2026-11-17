"""普惠队列模式：设了单模型任务数上限的模型，名额在有任务的用户之间平均分配。

分配规则（每个模型单独算）：
- 用户排个先后，按这个先后一人一个轮流发名额，直到发完或大家的任务都分到了
  （一个人分到的不超过他在这个模型上的任务数）。所以 4 个名额、A/B/C/D 都有任务时各 1 个；
  A 有 10 个、B 只有 1 个时 A 3 个、B 1 个；除不尽时多出来的给排在前面的人。
- 用户的先后：正在跑的人按他最早那个任务的入队时间；没在跑的人还要和他上一次有任务停下的时间
  取较晚的那个。这样人比名额多时，空出来的名额给等得最久的人，而不是刚跑完一个、还有任务的那个人。
- 超出配额的运行中任务会被要求让出（停下后带着原入队时间回到队列，已完成的条目保留、断点续跑），
  由 worker 定期巡检执行；领任务时也只领还没用满配额的用户的任务。

被置顶任务占着的模型、没设上限的模型不参与，照常按队列先后。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Job, ModelConfig, ModelSource
from .model_limits import job_limit_for
from .pins import as_timestamp
from .queue import JobQueue

log = logging.getLogger(__name__)


class JobOwner(NamedTuple):
    model: str
    user_id: str
    queued_ts: float


async def job_owners(db: AsyncSession, job_ids: list[str]) -> dict[str, JobOwner]:
    """任务 id → (真实模型名, 提交者, 入队时间戳)。模型名查不到的记成空串。"""
    if not job_ids:
        return {}
    rows = (
        await db.execute(
            select(Job.id, Job.user_id, Job.queued_at, Job.created_at, Job.model_source,
                   Job.personal_model_name, ModelConfig.model_name)
            .outerjoin(ModelConfig, ModelConfig.id == Job.model_config_id)
            .where(Job.id.in_(job_ids))
        )
    ).all()
    return {
        job_id: JobOwner(
            (personal if source == ModelSource.personal else shared) or "",
            user_id,
            as_timestamp(queued_at or created_at) or 0.0,
        )
        for job_id, user_id, queued_at, created_at, source, personal, shared in rows
    }


def user_order(
    arrival: dict[str, float], running: dict[str, int], last_stop: dict[str, float],
) -> list[str]:
    """分名额时用户的先后，见模块说明。"""
    def key(user: str) -> tuple[float, str]:
        ts = arrival[user]
        if not running.get(user):
            ts = max(ts, last_stop.get(user, 0.0))
        return (ts, user)

    return sorted(arrival, key=key)


def fair_quotas(limit: int, users: list[str], demand: dict[str, int]) -> dict[str, int]:
    """按给定先后一人一个轮流发名额，每人最多发到他的任务数。"""
    quotas = {u: 0 for u in users}
    remaining = limit
    while remaining > 0:
        progressed = False
        for user in users:
            if remaining == 0:
                break
            if quotas[user] < demand.get(user, 0):
                quotas[user] += 1
                remaining -= 1
                progressed = True
        if not progressed:
            break
    return quotas


@dataclass
class ModelShare:
    limit: int
    quotas: dict[str, int]
    # 用户 → 他在这个模型上正在跑（持有租约）的任务，按入队时间从早到晚
    running: dict[str, list[str]] = field(default_factory=dict)

    def can_claim(self, user: str) -> bool:
        return len(self.running.get(user, [])) < self.quotas.get(user, 0)

    def excess(self) -> list[str]:
        """超出配额、该让出的运行中任务：每人留下最早入队的那几个。"""
        out: list[str] = []
        for user, ids in self.running.items():
            out.extend(ids[self.quotas.get(user, 0):])
        return out


async def compute_shares(
    db: AsyncSession, queue: JobQueue, queued_ids: list[str], rules: list[dict],
    pinned: dict[str, set[str]],
) -> tuple[dict[str, ModelShare], dict[str, JobOwner]]:
    """给有任务排队、设了上限、没被置顶占着的模型算配额。返回 (模型 → 配额, 排队任务的归属)。"""
    queued = await job_owners(db, queued_ids)
    models = sorted({
        o.model for o in queued.values()
        if o.model and o.model not in pinned and job_limit_for(o.model, rules) > 0
    })
    if not models:
        return {}, queued

    live = {m: await queue.live_running_on_model(m) for m in models}
    running_owners = await job_owners(db, [j for ids in live.values() for j in ids])
    last_stops = await queue.fair_last_stops(models)

    shares: dict[str, ModelShare] = {}
    for model in models:
        limit = job_limit_for(model, rules)
        on_model = [(j, running_owners[j]) for j in live[model] if j in running_owners]
        waiting = [(j, o) for j, o in queued.items() if o.model == model]
        running: dict[str, list[tuple[float, str]]] = {}
        for job_id, owner in on_model:
            running.setdefault(owner.user_id, []).append((owner.queued_ts, job_id))
        arrival: dict[str, float] = {}
        demand: dict[str, int] = {}
        for _, owner in on_model + waiting:
            arrival[owner.user_id] = min(arrival.get(owner.user_id, owner.queued_ts), owner.queued_ts)
            demand[owner.user_id] = demand.get(owner.user_id, 0) + 1
        order = user_order(arrival, {u: len(v) for u, v in running.items()}, last_stops.get(model, {}))
        shares[model] = ModelShare(
            limit=limit,
            quotas=fair_quotas(limit, order, demand),
            running={u: [j for _, j in sorted(v)] for u, v in running.items()},
        )
    return shares, queued


async def rebalance(
    db: AsyncSession, queue: JobQueue, queued_ids: list[str], rules: list[dict],
    pinned: dict[str, set[str]],
) -> list[str]:
    """让超出配额的运行中任务让出名额，返回发了信号的任务 id。"""
    shares, _ = await compute_shares(db, queue, queued_ids, rules, pinned)
    signaled: list[str] = []
    for model, share in shares.items():
        excess = share.excess()
        for job_id in excess:
            await queue.signal(job_id, "yield", only_if_absent=True)
        if excess:
            log.info("普惠队列：模型 %s 配额 %s，让出 %s", model, share.quotas, excess)
        signaled.extend(excess)
    return signaled
