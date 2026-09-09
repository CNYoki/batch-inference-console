"""保留期清理：到期才删、只删终态、记录保留、幂等。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.db import session_scope
from app.models import Job, JobStatus, User
from app.services import results as results_svc
from app.services.retention import preview, sweep
from app.services.settings_store import write_runtime


async def _make_job(status: JobStatus, finished_days_ago: float | None, *, name="t") -> str:
    """建一个任务并在磁盘上放出对应的输入与结果文件。"""
    async with session_scope() as db:
        user = (await db.execute(User.__table__.select().limit(1))).first()
        user_id = user.id

        job = Job(
            name=name,
            user_id=user_id,
            status=status,
            input_filename=f"{name}.jsonl",
            input_path="",
            input_size=0,
            total_items=1,
            finished_at=(
                datetime.now(UTC) - timedelta(days=finished_days_ago)
                if finished_days_ago is not None else None
            ),
        )
        db.add(job)
        await db.flush()
        job_id = job.id

        # 输入文件
        from app.config import settings as cfg
        input_path = cfg.upload_dir / f"{job_id}.jsonl"
        input_path.write_text('{"prompt":"x"}\n', encoding="utf-8")
        job.input_path = str(input_path)
        job.input_size = input_path.stat().st_size

        # 结果文件
        results_svc.output_path(job_id).write_text(
            '{"_index":0,"custom_id":"a","response":{"status_code":200,"body":{}}}\n', encoding="utf-8"
        )
    return job_id


def _files_exist(job_id: str) -> tuple[bool, bool]:
    from app.config import settings as cfg
    return (cfg.upload_dir / f"{job_id}.jsonl").exists(), results_svc.output_path(job_id).exists()


@pytest.fixture(autouse=True)
async def _reset_retention():
    yield
    async with session_scope() as db:
        await write_runtime(db, {"file_retention_days": 0, "purge_input_files": True})


@pytest.mark.asyncio
async def test_disabled_by_default_nothing_is_deleted():
    job_id = await _make_job(JobStatus.succeeded, finished_days_ago=999)
    async with session_scope() as db:
        result = await sweep(db)
    assert result["enabled"] is False
    assert _files_exist(job_id) == (True, True)


@pytest.mark.asyncio
async def test_purges_only_jobs_past_retention():
    old = await _make_job(JobStatus.succeeded, finished_days_ago=10, name="old")
    fresh = await _make_job(JobStatus.succeeded, finished_days_ago=1, name="fresh")

    async with session_scope() as db:
        await write_runtime(db, {"file_retention_days": 7})
    async with session_scope() as db:
        result = await sweep(db)

    # 用例之间共用一个库，只断言本用例关心的两个任务，不依赖全局计数
    assert result["purged"] >= 1
    assert result["freed_bytes"] > 0
    assert _files_exist(old) == (False, False), "超过保留期的应被清理"
    assert _files_exist(fresh) == (True, True), "未到期的不能动"


@pytest.mark.asyncio
async def test_running_jobs_are_never_purged():
    """非终态任务哪怕「开始得早」也不能动 —— 文件正在被 worker 读写。"""
    running = await _make_job(JobStatus.running, finished_days_ago=None, name="running")
    async with session_scope() as db:
        # 让它的 created_at 也很久远
        job = await db.get(Job, running)
        job.created_at = datetime.now(UTC) - timedelta(days=100)
    async with session_scope() as db:
        await write_runtime(db, {"file_retention_days": 1})
    async with session_scope() as db:
        await sweep(db)

    assert _files_exist(running) == (True, True)


@pytest.mark.asyncio
async def test_job_record_survives_purge():
    """删的是文件，不是记录 —— 用量统计不能出现断层。"""
    job_id = await _make_job(JobStatus.completed, finished_days_ago=30, name="keep-record")
    async with session_scope() as db:
        await write_runtime(db, {"file_retention_days": 7})
    async with session_scope() as db:
        await sweep(db)

    async with session_scope() as db:
        job = await db.get(Job, job_id)
        assert job is not None
        assert job.name == "keep-record"
        assert job.total_items == 1
        assert job.files_purged_at is not None


@pytest.mark.asyncio
async def test_sweep_is_idempotent():
    """清理过的任务不会被反复扫到（靠 files_purged_at 标记）。"""
    await _make_job(JobStatus.succeeded, finished_days_ago=30, name="once")
    async with session_scope() as db:
        await write_runtime(db, {"file_retention_days": 7})
    async with session_scope() as db:
        first = await sweep(db)
    async with session_scope() as db:
        second = await sweep(db)

    assert first["purged"] >= 1
    assert second["purged"] == 0


@pytest.mark.asyncio
async def test_can_keep_input_files():
    """关掉 purge_input_files 时只删结果，输入留着仍可重跑。"""
    job_id = await _make_job(JobStatus.succeeded, finished_days_ago=30, name="keep-input")
    async with session_scope() as db:
        await write_runtime(db, {"file_retention_days": 7, "purge_input_files": False})
    async with session_scope() as db:
        await sweep(db)

    has_input, has_output = _files_exist(job_id)
    assert has_input is True
    assert has_output is False


@pytest.mark.asyncio
async def test_falls_back_to_created_at_when_finished_at_missing():
    """历史数据可能没有 finished_at，用 created_at 兜底而不是永远不清理。"""
    job_id = await _make_job(JobStatus.canceled, finished_days_ago=None, name="no-finish")
    async with session_scope() as db:
        job = await db.get(Job, job_id)
        job.created_at = datetime.now(UTC) - timedelta(days=50)
    async with session_scope() as db:
        await write_runtime(db, {"file_retention_days": 7})
    async with session_scope() as db:
        await sweep(db)

    assert _files_exist(job_id) == (False, False)


@pytest.mark.asyncio
async def test_preview_reports_what_would_be_purged():
    await _make_job(JobStatus.succeeded, finished_days_ago=30, name="p-old")
    await _make_job(JobStatus.succeeded, finished_days_ago=1, name="p-new")
    async with session_scope() as db:
        await write_runtime(db, {"file_retention_days": 7})
    async with session_scope() as db:
        stats = await preview(db)

    assert stats["retention_days"] == 7
    assert stats["expiring_jobs"] >= 1
    assert stats["expiring_bytes"] > 0
    assert stats["tracked_bytes"] >= stats["expiring_bytes"]


# --------------------------------------------------------------------------- #
# worker 关闭顺序：收尾期间必须继续上报心跳
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_heartbeat_survives_drain(monkeypatch):
    """收到退出信号后 worker 要一边收尾一边继续上报，否则看板会误报心跳超时。"""
    import asyncio
    import json as _json

    from app.worker.main import Worker

    beats: list[dict] = []
    drain_started = asyncio.Event()
    drain_done = asyncio.Event()

    class FakeQueue:
        async def ping(self):
            return True

        async def heartbeat(self, worker_id, payload):
            beats.append(_json.loads(payload))

        async def renew_lease(self, job_id):
            pass

        async def claim(self, worker_id):
            return None

        async def drop_worker(self, worker_id):
            pass

        async def close(self):
            pass

        async def reap_expired(self):
            return []

        async def acquire_lock(self, name, ttl):
            return False

    import app.worker.main as wm

    monkeypatch.setattr(wm, "queue", FakeQueue())
    monkeypatch.setattr(wm, "HEARTBEAT_SECONDS", 0.05)
    monkeypatch.setattr(wm, "IDLE_SLEEP", 0.05)
    monkeypatch.setattr(wm, "backfill_result_sizes", lambda db: asyncio.sleep(0))

    worker = Worker()

    async def fake_drain():
        drain_started.set()
        # 模拟一个要跑一会儿才结束的任务
        await asyncio.sleep(0.4)
        drain_done.set()

    monkeypatch.setattr(worker, "_drain", fake_drain)

    task = asyncio.create_task(worker.start())
    await asyncio.sleep(0.15)
    worker.request_shutdown()

    await asyncio.wait_for(drain_started.wait(), timeout=2)
    before = len(beats)
    await asyncio.wait_for(drain_done.wait(), timeout=2)
    await asyncio.wait_for(task, timeout=2)

    assert len(beats) > before, "收尾期间心跳不能停"
    assert any(b.get("draining") for b in beats), "收尾期间应当上报 draining 标记"
