"""基于 Redis 的任务队列。

- `{p}:queue`   ZSET  member=job_id, score=priority*1e12+入队时间戳 → 优先级 + FIFO
- `{p}:leases`  ZSET  member=job_id, score=租约到期时间 → worker 崩溃后自动回收
- `{p}:owner`   HASH  job_id → worker_id
- `{p}:ctrl:<job_id>` STRING  cancel / pause 控制信号
- `{p}:workers` HASH  worker_id → 最近心跳时间

领取任务是 ZPOPMIN + 写租约的原子 Lua 脚本，保证同一任务不会被两个 worker 同时拿到。
"""
from __future__ import annotations

import json
import time

import redis.asyncio as aioredis

from ..config import settings

_POP_LUA = """
local queue_key = KEYS[1]
local lease_key = KEYS[2]
local owner_key = KEYS[3]
local worker_id = ARGV[1]
local lease_until = tonumber(ARGV[2])

local popped = redis.call('ZPOPMIN', queue_key, 1)
if #popped == 0 then
  return nil
end
local job_id = popped[1]
redis.call('ZADD', lease_key, lease_until, job_id)
redis.call('HSET', owner_key, job_id, worker_id)
return job_id
"""

# 把过期租约重新放回队列（保留原优先级需重新计算，这里用高优先级让它尽快被重试）
_REAP_LUA = """
local queue_key = KEYS[1]
local lease_key = KEYS[2]
local owner_key = KEYS[3]
local now = tonumber(ARGV[1])
local score = tonumber(ARGV[2])

local expired = redis.call('ZRANGEBYSCORE', lease_key, '-inf', now)
for i, job_id in ipairs(expired) do
  redis.call('ZREM', lease_key, job_id)
  redis.call('HDEL', owner_key, job_id)
  redis.call('ZADD', queue_key, score + i, job_id)
end
return expired
"""


class JobQueue:
    def __init__(self, redis_url: str | None = None, prefix: str | None = None) -> None:
        self._url = redis_url or settings.redis_url
        self.prefix = prefix or settings.queue_key_prefix
        self._redis: aioredis.Redis | None = None
        self._pop_sha: str | None = None
        self._reap_sha: str | None = None

    # ---------------- 连接 ----------------
    @property
    def redis(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = aioredis.from_url(
                self._url, encoding="utf-8", decode_responses=True, health_check_interval=30
            )
        return self._redis

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    async def ping(self) -> bool:
        try:
            return bool(await self.redis.ping())
        except Exception:  # noqa: BLE001 — 健康检查要吞掉一切异常，只回答「通不通」
            return False

    # ---------------- key ----------------
    @property
    def k_queue(self) -> str:
        return f"{self.prefix}:queue"

    @property
    def k_lease(self) -> str:
        return f"{self.prefix}:leases"

    @property
    def k_owner(self) -> str:
        return f"{self.prefix}:owner"

    @property
    def k_workers(self) -> str:
        return f"{self.prefix}:workers"

    def k_ctrl(self, job_id: str) -> str:
        return f"{self.prefix}:ctrl:{job_id}"

    def k_progress(self, job_id: str) -> str:
        return f"{self.prefix}:progress:{job_id}"

    @staticmethod
    def _score(priority: int) -> float:
        return priority * 1e12 + time.time()

    # ---------------- 入队 / 出队 ----------------
    async def enqueue(self, job_id: str, priority: int = 100) -> None:
        await self.redis.zadd(self.k_queue, {job_id: self._score(priority)})

    async def remove(self, job_id: str) -> None:
        """从队列与租约中彻底摘除（取消任务时用）。"""
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.zrem(self.k_queue, job_id)
            pipe.zrem(self.k_lease, job_id)
            pipe.hdel(self.k_owner, job_id)
            await pipe.execute()

    async def claim(self, worker_id: str, lease_seconds: int | None = None) -> str | None:
        lease = lease_seconds or settings.job_lease_seconds
        if self._pop_sha is None:
            self._pop_sha = await self.redis.script_load(_POP_LUA)
        try:
            return await self.redis.evalsha(
                self._pop_sha, 3, self.k_queue, self.k_lease, self.k_owner,
                worker_id, str(time.time() + lease),
            )
        except aioredis.ResponseError as exc:
            if "NOSCRIPT" not in str(exc):
                raise
            self._pop_sha = await self.redis.script_load(_POP_LUA)
            return await self.redis.evalsha(
                self._pop_sha, 3, self.k_queue, self.k_lease, self.k_owner,
                worker_id, str(time.time() + lease),
            )

    async def renew_lease(self, job_id: str, lease_seconds: int | None = None) -> None:
        lease = lease_seconds or settings.job_lease_seconds
        await self.redis.zadd(self.k_lease, {job_id: time.time() + lease})

    async def release(self, job_id: str) -> None:
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.zrem(self.k_lease, job_id)
            pipe.hdel(self.k_owner, job_id)
            await pipe.execute()

    async def reap_expired(self) -> list[str]:
        """回收租约过期的任务，返回被重新入队的 job_id。"""
        if self._reap_sha is None:
            self._reap_sha = await self.redis.script_load(_REAP_LUA)
        try:
            result = await self.redis.evalsha(
                self._reap_sha, 3, self.k_queue, self.k_lease, self.k_owner,
                str(time.time()), str(self._score(0)),
            )
        except aioredis.ResponseError as exc:
            if "NOSCRIPT" not in str(exc):
                raise
            self._reap_sha = await self.redis.script_load(_REAP_LUA)
            result = await self.redis.evalsha(
                self._reap_sha, 3, self.k_queue, self.k_lease, self.k_owner,
                str(time.time()), str(self._score(0)),
            )
        return list(result or [])

    # ---------------- 控制信号 ----------------
    async def signal(self, job_id: str, action: str, ttl: int = 86400) -> None:
        """action: cancel | pause。worker 在处理循环中轮询该键。"""
        await self.redis.set(self.k_ctrl(job_id), action, ex=ttl)

    async def get_signal(self, job_id: str) -> str | None:
        return await self.redis.get(self.k_ctrl(job_id))

    async def clear_signal(self, job_id: str) -> None:
        await self.redis.delete(self.k_ctrl(job_id))

    # ---------------- 实时进度（避免高频写库） ----------------
    async def set_progress(self, job_id: str, completed: int, failed: int, ttl: int = 3600) -> None:
        await self.redis.hset(
            self.k_progress(job_id), mapping={"completed": completed, "failed": failed, "ts": time.time()}
        )
        await self.redis.expire(self.k_progress(job_id), ttl)

    async def get_progress(self, job_id: str) -> dict[str, str] | None:
        data = await self.redis.hgetall(self.k_progress(job_id))
        return data or None

    async def get_progress_many(self, job_ids: list[str]) -> dict[str, dict[str, str]]:
        if not job_ids:
            return {}
        async with self.redis.pipeline(transaction=False) as pipe:
            for jid in job_ids:
                pipe.hgetall(self.k_progress(jid))
            rows = await pipe.execute()
        return {jid: row for jid, row in zip(job_ids, rows, strict=True) if row}

    # ---------------- worker 心跳 ----------------
    async def heartbeat(self, worker_id: str, payload: str) -> None:
        await self.redis.hset(self.k_workers, worker_id, payload)

    async def drop_worker(self, worker_id: str) -> None:
        await self.redis.hdel(self.k_workers, worker_id)

    async def list_workers(self) -> dict[str, str]:
        return await self.redis.hgetall(self.k_workers)

    async def purge_dead_workers(self, max_age: float) -> list[str]:
        """删掉心跳停了超过 max_age 秒的 worker，返回被删的 id。

        worker 只有正常退出才会注销自己；被 SIGKILL / OOM / 宕机的进程会在
        HASH 里留一条永远不更新的记录，而且重启后 id 不同，不会被覆盖。
        """
        now = time.time()
        dead: list[str] = []
        for wid, raw in (await self.list_workers()).items():
            try:
                ts = float(json.loads(raw).get("ts") or 0)
            except (ValueError, TypeError, AttributeError):
                ts = 0.0  # 解析不了的记录不可能是活着的 worker 写的
            if now - ts > max_age:
                dead.append(wid)
        if dead:
            await self.redis.hdel(self.k_workers, *dead)
        return dead

    async def acquire_lock(self, name: str, ttl: int) -> bool:
        """尝试拿一把带超时的互斥锁。用于「同一时刻只允许一个 worker 做」的周期任务。

        锁到期自动释放，所以持有者崩溃也不会把任务永久卡住。
        """
        return bool(await self.redis.set(f"{self.prefix}:lock:{name}", "1", nx=True, ex=ttl))

    async def stats(self) -> dict[str, int]:
        async with self.redis.pipeline(transaction=False) as pipe:
            pipe.zcard(self.k_queue)
            pipe.zcard(self.k_lease)
            pipe.hlen(self.k_workers)
            queued, running, workers = await pipe.execute()
        return {"queued": queued, "running": running, "workers": workers}

    async def queued_position(self, job_id: str) -> int | None:
        rank = await self.redis.zrank(self.k_queue, job_id)
        return None if rank is None else rank + 1


queue = JobQueue()
