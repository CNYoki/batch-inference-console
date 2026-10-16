"""置顶任务与管理员调整队列顺序。

队列的 Lua 脚本只能在真 Redis 上测：默认连 settings.redis_url，可用 TEST_REDIS_URL 指到别处，
连不上就整体跳过。每个用例用独立的 key 前缀，跑完清掉。
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient

from app.config import settings
from app.services.queue import CLAIM_OK, JobQueue


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


@pytest_asyncio.fixture
async def models():
    """两个模型：m1 是置顶要抢占的，m2 是旁观者。返回 (m1_id, m2_id, admin_id)。"""
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import ModelConfig, User

    tag = uuid.uuid4().hex[:6]
    async with session_scope() as db:
        admin = (await db.execute(select(User).where(User.username == "admin"))).scalar_one()
        m1 = ModelConfig(name=f"pin-m1-{tag}", display_name="m1", base_url="http://x/v1",
                         model_name=f"pin-model-1-{tag}")
        m2 = ModelConfig(name=f"pin-m2-{tag}", display_name="m2", base_url="http://x/v1",
                         model_name=f"pin-model-2-{tag}")
        db.add_all([m1, m2])
        await db.flush()
        ids = (m1.id, m2.id, admin.id)
    yield ids
    async with session_scope() as db:
        from app.models import Job

        for job in (await db.execute(select(Job).where(Job.model_config_id.in_(ids[:2])))).scalars():
            await db.delete(job)
        for mid in ids[:2]:
            await db.delete(await db.get(ModelConfig, mid))


async def _job(model_id: str, user_id: str, name: str, **kw) -> str:
    from app.db import session_scope
    from app.models import Job, JobStatus

    async with session_scope() as db:
        job = Job(name=name, user_id=user_id, model_config_id=model_id,
                  status=kw.pop("status", JobStatus.queued),
                  input_filename="a.jsonl", input_path="/tmp/none.jsonl", **kw)
        db.add(job)
        await db.flush()
        return job.id


async def _set(job_id: str, **values) -> None:
    from app.db import session_scope
    from app.models import Job

    async with session_scope() as db:
        job = await db.get(Job, job_id)
        for k, v in values.items():
            setattr(job, k, v)


# --------------------------------------------------------------------------- #
# 队列层
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_pinned_score_beats_every_priority(rq: JobQueue):
    await rq.enqueue("p0", 0)
    await rq.enqueue("p100", 100)
    await rq.enqueue("pinned", 1000, pinned=True)
    assert await rq.peek(0, 10) == ["pinned", "p0", "p100"]

    # 取消置顶后按原优先级和入队时间回到原位
    assert await rq.rescore("pinned", 1000)
    assert await rq.peek(0, 10) == ["p0", "p100", "pinned"]
    # 不在队列里的不会被加进去
    assert not await rq.rescore("ghost", 0, pinned=True)
    assert await rq.redis.zscore(rq.k_queue, "ghost") is None


@pytest.mark.asyncio
async def test_reorder_swaps_only_listed_jobs(rq: JobQueue):
    now = 1_700_000_000.0
    for i, jid in enumerate(("a", "b", "c", "d")):
        await rq.enqueue(jid, 100, ts=now + i)
    await rq.enqueue("x", 500, ts=now)

    # a、b、d 占着第 1、2、4 位，按 d、b、a 的先后重新分配；c 不动
    scores = await rq.reorder(["d", "b", "a"])
    assert await rq.peek(0, 10) == ["d", "b", "c", "a", "x"]
    assert {rq.priority_of(s) for s in scores.values()} == {100}
    # 时间戳的小数位没有被 Lua 截掉，同一档里还能分出先后
    assert scores["d"] == 100 * 1e12 + now and scores["a"] == 100 * 1e12 + now + 3

    # 有一个已不在队列里：整体不动
    with pytest.raises(KeyError):
        await rq.reorder(["a", "gone", "d"])
    assert await rq.peek(0, 10) == ["d", "b", "c", "a", "x"]

    # 跨优先级档调整：被挪到前面的任务拿走前面那档的分值
    await rq.reorder(["x", "d"])
    assert (await rq.peek(0, 10))[0] == "x"


# --------------------------------------------------------------------------- #
# worker 领取
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_worker_only_claims_pinned_job_on_its_model(rq: JobQueue, models, monkeypatch):
    import app.worker.main as wm
    from app.models import JobStatus

    m1, m2, uid = models
    a = await _job(m1, uid, "a")
    b = await _job(m1, uid, "b")
    other = await _job(m2, uid, "other")
    pinned = await _job(m1, uid, "pinned", pinned=True, pinned_at=datetime.now(UTC))
    # 显式给时间戳：同一档优先级下相隔不到几毫秒的分值在浮点上分不出先后
    for i, jid in enumerate((a, b, other)):
        await rq.enqueue(jid, 100, ts=1_700_000_000.0 + i)
    await rq.enqueue(pinned, 100, pinned=True)

    monkeypatch.setattr(wm, "queue", rq)
    worker = wm.Worker()
    assert await worker._claim() == pinned
    # 同模型的普通任务被挡住，别的模型照常
    assert await worker._claim() == other
    assert await worker._claim() is None

    # 置顶任务被暂停时不占模型
    await _set(pinned, status=JobStatus.paused)
    assert await worker._claim() == a
    # 置顶任务结束后完全放开
    await _set(pinned, status=JobStatus.succeeded, pinned=False)
    assert await worker._claim() == b


@pytest.mark.asyncio
async def test_runner_hands_back_job_blocked_by_pin(models, monkeypatch):
    """领取那一刻没看到置顶的，开跑前也会再查一次、原样交回。"""
    from app.db import session_scope
    from app.models import Job, JobStatus
    from app.worker.runner import JobRunner

    m1, _, uid = models
    await _job(m1, uid, "pinned", pinned=True, status=JobStatus.running)
    victim = await _job(m1, uid, "victim", status=JobStatus.running, worker_id="w-old")

    assert await JobRunner(victim, "w1").run() == JobStatus.queued
    async with session_scope() as db:
        job = await db.get(Job, victim)
        assert job.status == JobStatus.queued and job.worker_id is None


@pytest.mark.asyncio
async def test_requeue_keeps_original_position_and_honours_user_signal(rq: JobQueue, models):
    from app.db import session_scope
    from app.models import Job, JobStatus
    from app.services.pins import requeue_preempted

    m1, _, uid = models
    early = datetime.now(UTC) - timedelta(hours=1)
    preempted = await _job(m1, uid, "preempted", queued_at=early)
    await rq.enqueue("later", 100)

    async with session_scope() as db:
        await rq.signal(preempted, "requeue")
        await requeue_preempted(db, rq, preempted)
    # 带着原来的入队时间回去，排在后来者前面
    assert await rq.peek(0, 10) == [preempted, "later"]
    assert await rq.get_signal(preempted) is None

    # 让出途中用户点了暂停：不再入队，直接落成暂停
    paused = await _job(m1, uid, "paused-meanwhile")
    await rq.signal(paused, "pause")
    async with session_scope() as db:
        await requeue_preempted(db, rq, paused)
    async with session_scope() as db:
        assert (await db.get(Job, paused)).status == JobStatus.paused
    assert paused not in await rq.peek(0, 10)


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_pin_preempts_running_jobs_on_same_model(
    admin_client: AsyncClient, rq: JobQueue, models, monkeypatch,
):
    import app.api.jobs as jobs_mod
    from app.models import JobStatus

    monkeypatch.setattr(jobs_mod, "queue", rq)
    m1, m2, uid = models

    running = await _job(m1, uid, "running-m1", status=JobStatus.running)
    canceling = await _job(m1, uid, "canceling-m1", status=JobStatus.running)
    other_model = await _job(m2, uid, "running-m2", status=JobStatus.running)
    # 刚被领走、库里还是 queued 的也要让
    just_claimed = await _job(m1, uid, "just-claimed")
    target = await _job(m1, uid, "target", priority=500)
    await rq.enqueue("ahead", 0)
    for jid in (running, canceling, other_model, just_claimed):
        await rq.enqueue(jid, 100)
    from app.db import session_scope
    from app.services.model_limits import job_model_names

    async with session_scope() as db:
        names = await job_model_names(db, [running, other_model])
    for jid in (running, canceling, just_claimed):
        assert await rq.claim_job(jid, "w1", names[running], 0) == CLAIM_OK
    assert await rq.claim_job(other_model, "w1", names[other_model], 0) == CLAIM_OK
    await rq.enqueue(target, 500)
    await rq.signal(canceling, "cancel")

    out = (await admin_client.post(f"/api/jobs/{target}/pin")).json()
    assert out["pinned"] and out["pinned_at"] and out["queue_position"] == 1

    assert await rq.get_signal(running) == "requeue"
    assert await rq.get_signal(just_claimed) == "requeue"
    # 不盖掉用户的取消，不碰别的模型
    assert await rq.get_signal(canceling) == "cancel"
    assert await rq.get_signal(other_model) is None

    view = (await admin_client.get("/api/jobs/queue")).json()
    assert [j["id"] for j in view["pinned"]] == [target]
    assert all(j["id"] != target for j in view["queued"])

    # 取消置顶：回到原来的优先级档
    out = (await admin_client.post(f"/api/jobs/{target}/unpin")).json()
    assert not out["pinned"]
    assert (await rq.peek(0, 10))[-1] == target
    assert (await admin_client.post(f"/api/jobs/{target}/unpin")).status_code == 400

    # 已结束的不能置顶
    await _set(target, status=JobStatus.succeeded)
    assert (await admin_client.post(f"/api/jobs/{target}/pin")).status_code == 400


@pytest.mark.asyncio
async def test_cancel_clears_pin(admin_client: AsyncClient, rq: JobQueue, models, monkeypatch):
    import app.api.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "queue", rq)
    m1, _, uid = models
    target = await _job(m1, uid, "pin-then-cancel")
    await rq.enqueue(target, 100)
    assert (await admin_client.post(f"/api/jobs/{target}/pin")).json()["pinned"]
    out = (await admin_client.post(f"/api/jobs/{target}/cancel")).json()
    assert out["status"] == "canceled" and not out["pinned"]


@pytest.mark.asyncio
async def test_admin_reorders_queue(admin_client: AsyncClient, rq: JobQueue, models, monkeypatch):
    import app.api.jobs as jobs_mod
    from app.models import JobStatus

    monkeypatch.setattr(jobs_mod, "queue", rq)
    m1, m2, uid = models
    ids = [await _job(m1 if i % 2 else m2, uid, f"q{i}") for i in range(4)]
    for i, jid in enumerate(ids):
        await rq.enqueue(jid, 100, ts=1_700_000_000.0 + i)

    view = (await admin_client.get("/api/jobs/queue")).json()
    assert [j["id"] for j in view["queued"]] == ids and view["total_queued"] == 4
    assert [j["queue_position"] for j in view["queued"]] == [1, 2, 3, 4]

    wanted = [ids[3], ids[0], ids[2], ids[1]]
    view = (await admin_client.post("/api/jobs/queue/reorder", json={"job_ids": wanted})).json()
    assert [j["id"] for j in view["queued"]] == wanted
    assert await rq.peek(0, 10) == wanted

    # 重复、置顶任务、已不在排队的都拒绝
    assert (await admin_client.post("/api/jobs/queue/reorder",
                                    json={"job_ids": [ids[0], ids[0]]})).status_code == 422
    await admin_client.post(f"/api/jobs/{ids[1]}/pin")
    assert (await admin_client.post("/api/jobs/queue/reorder",
                                    json={"job_ids": [ids[1], ids[0]]})).status_code == 400
    await _set(ids[2], status=JobStatus.running)
    assert (await admin_client.post("/api/jobs/queue/reorder",
                                    json={"job_ids": [ids[2], ids[0]]})).status_code == 409

    # 普通用户不能看也不能动
    await admin_client.post("/api/admin/users", json={
        "username": "queueuser", "password": "UserPass123!", "role": "user",
    })
    async with AsyncClient(transport=admin_client._transport, base_url="http://test") as user_client:
        await user_client.post("/api/auth/login", json={"username": "queueuser", "password": "UserPass123!"})
        assert (await user_client.get("/api/jobs/queue")).status_code == 403
        assert (await user_client.post("/api/jobs/queue/reorder",
                                       json={"job_ids": [ids[0]]})).status_code == 403
        assert (await user_client.post(f"/api/jobs/{ids[0]}/pin")).status_code == 403
