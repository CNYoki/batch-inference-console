"""任务归档（软删除）与批量暂停/恢复/归档。队列用假对象，不需要 Redis。"""
from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select

import app.api.jobs as jobs_mod
from app.db import session_scope
from app.models import Job, JobStatus, User
from app.services import results as results_svc


class FakeQueue:
    """记录调用；worker 永远在线，运行中的任务只收到信号。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def worker_last_seen(self, _worker_id: str) -> float:
        return time.time()

    async def get_progress_many(self, _job_ids: list[str]) -> dict:
        return {}

    def __getattr__(self, name: str):
        async def record(*args, **_kwargs):
            self.calls.append((name, args[0] if args else ""))

        return record


@pytest.fixture
def fake_queue(monkeypatch) -> FakeQueue:
    fake = FakeQueue()
    monkeypatch.setattr(jobs_mod, "queue", fake)
    return fake


async def _job(tmp_root: Path, status: JobStatus, username: str = "admin", **kw) -> str:
    input_file = tmp_root / f"{uuid.uuid4().hex}.jsonl"
    input_file.write_text('{"prompt": "x"}\n', "utf-8")
    async with session_scope() as db:
        user = (await db.execute(select(User).where(User.username == username))).scalar_one()
        job = Job(name="t", user_id=user.id, status=status, input_filename="a.jsonl",
                  input_path=str(input_file), **kw)
        db.add(job)
        await db.flush()
        job_id = job.id
    results_svc.output_path(job_id).write_text("{}\n", "utf-8")
    return job_id


async def _user_client(admin_client: AsyncClient) -> tuple[AsyncClient, str]:
    username = f"u{uuid.uuid4().hex[:8]}"
    await admin_client.post("/api/admin/users", json={
        "username": username, "password": "UserPass123!", "role": "user",
    })
    client = AsyncClient(transport=admin_client._transport, base_url="http://test")
    resp = await client.post("/api/auth/login", json={"username": username, "password": "UserPass123!"})
    assert resp.status_code == 200, resp.text
    return client, username


async def _db_job(job_id: str) -> Job:
    async with session_scope() as db:
        return await db.get(Job, job_id)


@pytest.mark.asyncio
async def test_archive_hides_job_from_owner_and_purges_files(admin_client, fake_queue, tmp_root):
    user, username = await _user_client(admin_client)
    try:
        job_id = await _job(tmp_root, JobStatus.succeeded, username)
        input_path = Path((await _db_job(job_id)).input_path)

        assert (await user.delete(f"/api/jobs/{job_id}")).status_code == 405
        assert input_path.exists()

        assert (await user.post(f"/api/jobs/{job_id}/archive")).status_code == 204

        assert not input_path.exists()
        assert not results_svc.output_path(job_id).exists()
        job = await _db_job(job_id)
        assert job is not None and job.archived_at is not None and job.files_purged_at is not None

        assert (await user.get(f"/api/jobs/{job_id}")).status_code == 404
        assert (await user.post(f"/api/jobs/{job_id}/resume")).status_code == 404
        assert (await user.get("/api/jobs")).json()["total"] == 0
        listed = (await admin_client.get("/api/jobs", params={"mine": False, "page_size": 100})).json()
        assert job_id not in {j["id"] for j in listed["items"]}

        # 管理员还能追溯，但不能重复归档
        detail = (await admin_client.get(f"/api/jobs/{job_id}")).json()
        assert detail["archived_at"] is not None
        assert (await admin_client.post(f"/api/jobs/{job_id}/archive")).status_code == 400
    finally:
        await user.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [JobStatus.pending, JobStatus.queued, JobStatus.running])
async def test_active_jobs_cannot_be_archived(admin_client, fake_queue, tmp_root, status):
    job_id = await _job(tmp_root, status)
    assert (await admin_client.post(f"/api/jobs/{job_id}/archive")).status_code == 400
    job = await _db_job(job_id)
    assert job.archived_at is None and Path(job.input_path).exists()


@pytest.mark.asyncio
async def test_archiving_paused_pinned_job_releases_the_pin(admin_client, fake_queue, tmp_root):
    job_id = await _job(tmp_root, JobStatus.paused, pinned=True)
    assert (await admin_client.post(f"/api/jobs/{job_id}/archive")).status_code == 204
    assert (await _db_job(job_id)).pinned is False


@pytest.mark.asyncio
async def test_archived_jobs_leave_project_counts(admin_client, fake_queue, tmp_root):
    project = (await admin_client.post("/api/projects", json={"name": f"归档-{uuid.uuid4().hex[:6]}"})).json()
    keep = await _job(tmp_root, JobStatus.succeeded, project_id=project["id"])
    gone = await _job(tmp_root, JobStatus.succeeded, project_id=project["id"])
    await admin_client.post(f"/api/jobs/{gone}/archive")

    got = (await admin_client.get(f"/api/projects/{project['id']}")).json()
    assert got["job_count"] == 1
    moved = await admin_client.post("/api/jobs/batch-project", json={"job_ids": [keep, gone], "project_id": None})
    assert moved.json()["updated"] == 1


@pytest.mark.asyncio
async def test_batch_pause_skips_ineligible_and_foreign_jobs(admin_client, fake_queue, tmp_root):
    user, username = await _user_client(admin_client)
    try:
        queued = await _job(tmp_root, JobStatus.queued, username)
        running = await _job(tmp_root, JobStatus.running, username, worker_id="w1")
        done = await _job(tmp_root, JobStatus.succeeded, username)
        foreign = await _job(tmp_root, JobStatus.queued)

        resp = await user.post("/api/jobs/batch-action", json={
            "job_ids": [queued, queued, running, done, foreign, "missing"], "action": "pause",
        })

        assert resp.json() == {"succeeded": 2, "skipped": 3, "failed": 0}
        assert (await _db_job(queued)).status == JobStatus.paused
        assert ("signal", running) in fake_queue.calls
        assert (await _db_job(running)).status == JobStatus.running
        assert (await _db_job(foreign)).status == JobStatus.queued
    finally:
        await user.aclose()


@pytest.mark.asyncio
async def test_batch_resume_requeues_and_skips_purged(admin_client, fake_queue, tmp_root):
    paused = await _job(tmp_root, JobStatus.paused)
    canceled = await _job(tmp_root, JobStatus.canceled)
    done = await _job(tmp_root, JobStatus.succeeded)
    purged = await _job(tmp_root, JobStatus.paused)
    await admin_client.post(f"/api/jobs/{purged}/archive")

    resp = await admin_client.post("/api/jobs/batch-action", json={
        "job_ids": [paused, canceled, done, purged], "action": "resume",
    })

    assert resp.json() == {"succeeded": 2, "skipped": 2, "failed": 0}
    assert (await _db_job(paused)).status == JobStatus.queued
    assert (await _db_job(canceled)).status == JobStatus.queued


@pytest.mark.asyncio
async def test_batch_archive_continues_after_one_failure(admin_client, fake_queue, tmp_root, monkeypatch):
    bad = await _job(tmp_root, JobStatus.succeeded)
    good = await _job(tmp_root, JobStatus.failed)
    running = await _job(tmp_root, JobStatus.running)
    real = jobs_mod._archive

    async def flaky(job, db):
        if job.id == bad:
            raise RuntimeError("boom")
        await real(job, db)

    monkeypatch.setattr(jobs_mod, "_archive", flaky)
    resp = await admin_client.post("/api/jobs/batch-action", json={
        "job_ids": [bad, good, running], "action": "archive",
    })

    assert resp.json() == {"succeeded": 1, "skipped": 1, "failed": 1}
    assert (await _db_job(good)).archived_at is not None
    assert (await _db_job(bad)).archived_at is None
    assert (await _db_job(running)).archived_at is None


@pytest.mark.asyncio
async def test_batch_action_validates_payload(admin_client, fake_queue):
    resp = await admin_client.post("/api/jobs/batch-action", json={"job_ids": ["a"], "action": "delete"})
    assert resp.status_code == 422
    assert (await admin_client.post("/api/jobs/batch-action", json={"job_ids": [], "action": "pause"})).status_code == 422
