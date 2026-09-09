"""存储配额：用量统计、上传拦截、按用户/全站两级限制。"""
from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.db import session_scope
from app.models import Job, JobStatus, User
from app.services.settings_store import write_runtime
from app.services.storage import MB, check_before_upload, format_bytes, user_quota

QUOTA_TEXT = "已达到储存空间上限，请联系管理员"


async def _add_job(user_id: str, input_size: int, result_size: int, purged=False) -> str:
    from datetime import UTC, datetime

    async with session_scope() as db:
        job = Job(
            name="quota-fixture", user_id=user_id, status=JobStatus.succeeded,
            input_filename="a.jsonl", input_path="/tmp/nonexistent.jsonl",
            input_size=input_size, result_size=result_size, total_items=1,
            files_purged_at=datetime.now(UTC) if purged else None,
        )
        db.add(job)
        await db.flush()
        return job.id


async def _get_user(username: str) -> User:
    from sqlalchemy import select

    async with session_scope() as db:
        return (await db.execute(select(User).where(User.username == username))).scalar_one()


@pytest.fixture(autouse=True)
async def _reset_quota():
    yield
    async with session_scope() as db:
        await write_runtime(db, {"max_total_storage_gb": 0, "default_user_storage_gb": 0})


# --------------------------------------------------------------------------- #
def test_format_bytes_is_readable():
    assert format_bytes(0) == "0 B"
    assert format_bytes(512) == "512 B"
    assert format_bytes(1536) == "1.5 KB"
    assert format_bytes(5 * 1024 ** 3) == "5.0 GB"


@pytest.mark.asyncio
async def test_usage_sums_input_and_result_sizes():
    user = await _get_user("admin")
    async with session_scope() as db:
        before = await user_quota(db, user)

    await _add_job(user.id, input_size=1000, result_size=2000)

    async with session_scope() as db:
        after = await user_quota(db, user)
    assert after.used - before.used == 3000


@pytest.mark.asyncio
async def test_purged_jobs_stop_counting_against_quota():
    """保留期清理掉文件之后，那部分空间应当立刻还给用户。"""
    user = await _get_user("admin")
    async with session_scope() as db:
        before = await user_quota(db, user)

    await _add_job(user.id, input_size=5000, result_size=5000, purged=True)

    async with session_scope() as db:
        after = await user_quota(db, user)
    assert after.used == before.used


@pytest.mark.asyncio
async def test_unlimited_by_default():
    user = await _get_user("admin")
    async with session_scope() as db:
        q = await user_quota(db, user)
    assert q.unlimited is True
    assert q.remaining == -1
    assert q.would_exceed(10 ** 12) is False


@pytest.mark.asyncio
async def test_user_limit_blocks_when_exceeded():
    user = await _get_user("admin")
    async with session_scope() as db:
        await write_runtime(db, {"default_user_storage_gb": 1})
    await _add_job(user.id, input_size=2 * 1024 * MB, result_size=0)  # 2 GB > 1 GB

    async with session_scope() as db:
        check = await check_before_upload(db, user)
    assert check.ok is False
    assert check.reason == "user"


@pytest.mark.asyncio
async def test_per_user_override_beats_global_default():
    """用户单独设了上限就用自己的，不受全局默认影响。"""
    async with session_scope() as db:
        await write_runtime(db, {"default_user_storage_gb": 1})

    async with session_scope() as db:
        fresh = await _get_user("admin")
        fresh.max_storage_mb = 100 * 1024   # 单独给 100 GB
        db.add(fresh)
        await db.flush()
        q = await user_quota(db, fresh)
    assert q.limit == 100 * 1024 * MB

    async with session_scope() as db:
        fresh = await _get_user("admin")
        fresh.max_storage_mb = 0
        db.add(fresh)


@pytest.mark.asyncio
async def test_upload_rejected_with_exact_message(admin_client: AsyncClient):
    async with session_scope() as db:
        await write_runtime(db, {"default_user_storage_gb": 1})
    user = await _get_user("admin")
    await _add_job(user.id, input_size=2 * 1024 * MB, result_size=0)

    resp = await admin_client.post(
        "/api/jobs/upload", files={"file": ("a.jsonl", b'{"prompt":"x"}\n', "application/json")}
    )
    assert resp.status_code == 507
    assert resp.json()["detail"] == QUOTA_TEXT


@pytest.mark.asyncio
async def test_global_limit_blocks_even_when_user_has_room(admin_client: AsyncClient):
    user = await _get_user("admin")
    await _add_job(user.id, input_size=3 * 1024 * MB, result_size=0)
    async with session_scope() as db:
        # 用户不限，但全站只给 1 GB
        await write_runtime(db, {"default_user_storage_gb": 0, "max_total_storage_gb": 1})

    resp = await admin_client.post(
        "/api/jobs/upload", files={"file": ("a.jsonl", b'{"prompt":"x"}\n', "application/json")}
    )
    assert resp.status_code == 507
    assert resp.json()["detail"] == QUOTA_TEXT


@pytest.mark.asyncio
async def test_upload_allowed_when_under_limit(admin_client: AsyncClient):
    async with session_scope() as db:
        await write_runtime(db, {"default_user_storage_gb": 1000, "max_total_storage_gb": 1000})
    resp = await admin_client.post(
        "/api/jobs/upload", files={"file": ("a.jsonl", b'{"prompt":"x"}\n', "application/json")}
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_usage_endpoint(admin_client: AsyncClient):
    data = (await admin_client.get("/api/usage")).json()
    assert "used" in data["storage"]
    assert data["storage"]["unlimited"] is True
    # 管理员还能看到全站用量
    assert data["total"] is not None


@pytest.mark.asyncio
async def test_storage_settings_roundtrip(admin_client: AsyncClient):
    out = (await admin_client.patch("/api/admin/settings", json={
        "max_total_storage_gb": 500, "default_user_storage_gb": 20, "stale_upload_hours": 48,
    })).json()
    assert out["max_total_storage_gb"] == 500
    assert out["default_user_storage_gb"] == 20
    assert out["stale_upload_hours"] == 48
    await admin_client.patch("/api/admin/settings", json={
        "max_total_storage_gb": 0, "default_user_storage_gb": 0,
    })


@pytest.mark.asyncio
async def test_create_user_persists_storage_limit(admin_client: AsyncClient):
    """创建用户时 max_storage_mb 必须真的存进去 —— 漏了这个字段配额就形同虚设。"""
    created = (await admin_client.post("/api/admin/users", json={
        "username": "quota-user-1", "password": "QuotaUser123!", "role": "user",
        "max_storage_mb": 512, "max_concurrent_jobs": 3,
    })).json()
    assert created["max_storage_mb"] == 512
    assert created["max_concurrent_jobs"] == 3

    listed = (await admin_client.get("/api/admin/users", params={"keyword": "quota-user-1"})).json()
    assert listed["items"][0]["max_storage_mb"] == 512

    updated = (await admin_client.patch(
        f"/api/admin/users/{created['id']}", json={"max_storage_mb": 2048}
    )).json()
    assert updated["max_storage_mb"] == 2048

    await admin_client.delete(f"/api/admin/users/{created['id']}")


@pytest.mark.asyncio
async def test_deleting_user_cleans_their_pending_uploads(admin_client: AsyncClient):
    """删用户要连他没提交的暂存上传一起清，否则会留下没人认领的孤儿文件。"""
    from app.config import settings as cfg

    created = (await admin_client.post("/api/admin/users", json={
        "username": "orphan-test", "password": "OrphanTest123!", "role": "user",
    })).json()

    stray = cfg.upload_dir / f"{created['id']}__deadbeef.jsonl"
    stray.write_text('{"prompt":"x"}\n', encoding="utf-8")
    other = cfg.upload_dir / "someone-else__keepme.jsonl"
    other.write_text('{"prompt":"y"}\n', encoding="utf-8")

    assert (await admin_client.delete(f"/api/admin/users/{created['id']}")).status_code == 204

    assert not stray.exists(), "被删用户的暂存上传应当一并清掉"
    assert other.exists(), "别人的文件不能误删"
    other.unlink()
