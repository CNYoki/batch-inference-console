"""队列领取与单模型任务数上限。

Lua 脚本只能在真 Redis 上测：默认连 settings.redis_url，可用 TEST_REDIS_URL 指到别处，
连不上就整体跳过。每个用例用独立的 key 前缀，跑完清掉。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from app.config import settings
from app.services.queue import CLAIM_BLOCKED, CLAIM_GONE, CLAIM_OK, JobQueue


@pytest_asyncio.fixture
async def rq():
    q = JobQueue(
        redis_url=os.environ.get("TEST_REDIS_URL", settings.redis_url),
        prefix=f"test-{uuid.uuid4().hex[:8]}",
    )
    if not await q.ping():
        await q.close()
        pytest.skip("没有可用的 Redis，设置 TEST_REDIS_URL 后再跑")
    yield q
    keys = [k async for k in q.redis.scan_iter(match=f"{q.prefix}:*")]
    if keys:
        await q.redis.delete(*keys)
    await q.close()


@pytest.mark.asyncio
async def test_model_limit_blocks_until_a_slot_frees(rq: JobQueue):
    for jid in ("a1", "a2", "a3", "b1"):
        await rq.enqueue(jid)

    assert await rq.claim_job("a1", "w1", "m-a", 2) == CLAIM_OK
    assert await rq.claim_job("a2", "w2", "m-a", 2) == CLAIM_OK
    assert await rq.claim_job("a3", "w1", "m-a", 2) == CLAIM_BLOCKED
    # 别的模型不受影响
    assert await rq.claim_job("b1", "w1", "m-b", 2) == CLAIM_OK
    # 被挡住的任务还留在队列里等名额
    assert await rq.peek(0, 10) == ["a3"]

    await rq.release("a1")      # 正常跑完
    assert await rq.claim_job("a3", "w1", "m-a", 2) == CLAIM_OK
    await rq.remove("a2")       # 被取消
    assert await rq.redis.smembers(rq.k_model_running("m-a")) == {"a3"}
    assert await rq.redis.hget(rq.k_job_model, "a2") is None


@pytest.mark.asyncio
async def test_claim_gone_and_unlimited_still_counted(rq: JobQueue):
    assert await rq.claim_job("nope", "w", "m", 1) == CLAIM_GONE

    for jid in ("x1", "x2", "x3"):
        await rq.enqueue(jid)
        assert await rq.claim_job(jid, "w", "m-x", 0) == CLAIM_OK   # 0 = 不限
    # 不限的也记账：之后管理员加上规则，已经在跑的立刻算数
    await rq.enqueue("x4")
    assert await rq.claim_job("x4", "w", "m-x", 3) == CLAIM_BLOCKED


@pytest.mark.asyncio
async def test_slot_of_killed_worker_frees_after_reap(rq: JobQueue):
    """被强杀的 worker 不会 release：租约过期被回收后，名额要能空出来。"""
    await rq.enqueue("d1")
    await rq.enqueue("d2")
    assert await rq.claim_job("d1", "dead", "m", 1) == CLAIM_OK
    # 租约还没过期：那段时间它的请求可能还在模型上跑，照样占名额
    assert await rq.claim_job("d2", "w", "m", 1) == CLAIM_BLOCKED

    await rq.redis.zadd(rq.k_lease, {"d1": time.time() - 1})
    assert await rq.reap_expired() == ["d1"]
    assert await rq.claim_job("d2", "w", "m", 1) == CLAIM_OK
    # 被回收回队列的 d1 这时轮到它等
    assert await rq.claim_job("d1", "w", "m", 1) == CLAIM_BLOCKED


@pytest.mark.asyncio
async def test_worker_last_seen(rq: JobQueue):
    await rq.heartbeat("w1", json.dumps({"ts": 123.5}))
    assert await rq.worker_last_seen("w1") == 123.5
    assert await rq.worker_last_seen("nobody") is None


@pytest.mark.asyncio
async def test_worker_skips_jobs_over_model_limit(rq: JobQueue, monkeypatch):
    """队首的模型满了就往后找，别让一个被限流的模型堵住整条队列。"""
    from sqlalchemy import select

    import app.worker.main as wm
    from app.db import session_scope
    from app.models import Job, JobStatus, ModelConfig, ModelSource, User
    from app.services.settings_store import write_runtime

    async with session_scope() as db:
        user = (await db.execute(select(User).where(User.username == "admin"))).scalar_one()
        limited = ModelConfig(name=f"lim-{rq.prefix}", display_name="限流模型",
                              base_url="http://x/v1", model_name="qwen3.8-27b")
        free = ModelConfig(name=f"free-{rq.prefix}", display_name="不限模型",
                           base_url="http://x/v1", model_name="other-model")
        db.add_all([limited, free])
        await db.flush()

        def make(name: str, **kw) -> Job:
            job = Job(name=name, user_id=user.id, status=JobStatus.queued,
                      input_filename="a.jsonl", input_path="/tmp/none.jsonl", **kw)
            db.add(job)
            return job

        jobs = [
            make("q1", model_config_id=limited.id),
            make("q2", model_config_id=limited.id),
            # 个人网关里同名的模型算同一个
            make("q3", model_source=ModelSource.personal, personal_model_name="qwen3.8-27b"),
            make("o1", model_config_id=free.id),
        ]
        await db.flush()
        ids = [j.id for j in jobs]
        model_ids = [limited.id, free.id]
        await write_runtime(db, {"model_job_limits": [{"pattern": "qwen3.8-*", "max_running_jobs": 2}]})

    try:
        for i, (jid, priority) in enumerate(zip(ids, (10, 10, 10, 20), strict=True)):
            await rq.enqueue(jid, priority, ts=1_000_000.0 + i)
        monkeypatch.setattr(wm, "queue", rq)
        worker = wm.Worker()

        assert await worker._claim() == ids[0]
        assert await worker._claim() == ids[1]
        # q3 被上限挡住，排在后面的其他模型任务先跑
        assert await worker._claim() == ids[3]
        assert await worker._claim() is None
        await rq.release(ids[0])
        assert await worker._claim() == ids[2]
    finally:
        async with session_scope() as db:
            await write_runtime(db, {"model_job_limits": []})
        async with session_scope() as db:
            for jid in ids:
                await db.delete(await db.get(Job, jid))
            for mid in model_ids:
                await db.delete(await db.get(ModelConfig, mid))


@pytest.mark.asyncio
async def test_worker_fair_mode_takes_back_slots_for_new_users(rq: JobQueue, monkeypatch):
    """普惠队列模式：上限 4，A 先占满 4 个；B、C、D 进来后 A 让出 3 个，变成各跑 1 个。"""
    from sqlalchemy import select

    import app.worker.main as wm
    from app.db import session_scope
    from app.models import Job, JobStatus, ModelConfig, User
    from app.services.fair_queue import rebalance
    from app.services.settings_store import write_runtime

    rules = [{"pattern": "qwen3.8-27b", "max_running_jobs": 4}]
    async with session_scope() as db:
        admin = (await db.execute(select(User).where(User.username == "admin"))).scalar_one()
        users = [User(username=f"fair{c}-{rq.prefix}", password_hash="x") for c in "BCD"]
        mc = ModelConfig(name=f"fair-{rq.prefix}", display_name="普惠模型",
                         base_url="http://x/v1", model_name="qwen3.8-27b")
        db.add_all([*users, mc])
        await db.flush()
        base = datetime(2026, 1, 1, tzinfo=UTC)
        jobs = [
            Job(name=f"{tag}{i}", user_id=uid, status=JobStatus.queued, model_config_id=mc.id,
                input_filename="a.jsonl", input_path="/tmp/none.jsonl",
                queued_at=base + timedelta(minutes=minute + i))
            for tag, uid, n, minute in (
                ("a", admin.id, 6, 0), ("b", users[0].id, 2, 10),
                ("c", users[1].id, 2, 20), ("d", users[2].id, 20, 30),
            )
            for i in range(n)
        ]
        db.add_all(jobs)
        await db.flush()
        by_tag = {t: [j.id for j in jobs if j.name.startswith(t)] for t in "abcd"}
        all_ids = [j.id for j in jobs]
        user_ids, mc_id = [u.id for u in users], mc.id
        await write_runtime(db, {"model_job_limits": rules, "fair_queue_enabled": True})

    def ts(job_id: str) -> float:
        return all_ids.index(job_id) + 1_000_000.0

    try:
        monkeypatch.setattr(wm, "queue", rq)
        worker = wm.Worker()
        for jid in by_tag["a"]:
            await rq.enqueue(jid, ts=ts(jid))
        # 只有 A 在排队：4 个名额都给 A
        assert [await worker._claim() for _ in range(4)] == by_tag["a"][:4]

        for jid in by_tag["b"] + by_tag["c"] + by_tag["d"]:
            await rq.enqueue(jid, ts=ts(jid))
        assert await worker._claim() is None   # 名额还被 A 占着

        async with session_scope() as db:
            signaled = await rebalance(db, rq, await rq.peek(0, 100), rules, {})
        # A 留下最早的那个，后面 3 个让出
        assert sorted(signaled) == sorted(by_tag["a"][1:4])
        for jid in signaled:
            assert await rq.get_signal(jid) == "yield"
            await rq.release(jid)             # worker 停下、释放租约后带着原入队时间回到队列
            await rq.clear_signal(jid)
            await rq.enqueue(jid, ts=ts(jid))

        claimed = [await worker._claim() for _ in range(3)]
        assert claimed == [by_tag["b"][0], by_tag["c"][0], by_tag["d"][0]]
        assert await worker._claim() is None
        async with session_scope() as db:
            assert await rebalance(db, rq, await rq.peek(0, 100), rules, {}) == []
    finally:
        async with session_scope() as db:
            await write_runtime(db, {"model_job_limits": [], "fair_queue_enabled": False})
        async with session_scope() as db:
            for jid in all_ids:
                await db.delete(await db.get(Job, jid))
            await db.delete(await db.get(ModelConfig, mc_id))
            for uid in user_ids:
                await db.delete(await db.get(User, uid))
