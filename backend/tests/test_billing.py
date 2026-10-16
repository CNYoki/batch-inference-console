"""个人网关独立计费：切记录不重复、按错误码处理、runner 定期扣费与扣不上时暂停。

网关（推理接口 + 计费接口）用 httpx.MockTransport 模拟；runner 的信号/进度要用 Redis，
连不上时相关用例跳过。
"""
from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from app.config import settings
from app.core.security import encrypt_secret
from app.db import session_scope
from app.models import BillingRecord, BillingStatus, Job, JobStatus, ModelSource, User
from app.services import billing
from app.services.gateway import (
    GatewayError,
    build_personal_config,
    check_billing_key,
    list_user_models,
    usage_url,
)
from app.services.settings_store import DEFAULTS, write_runtime

INFER = "http://infer.test/v1"
BILL = "http://gw.test/api/batch/billing"

BILLING_RUNTIME = {
    "user_gateway_enabled": True,
    "user_gateway_base_url": "http://gw.test/v1",
    "user_gateway_billing_enabled": True,
    "user_gateway_inference_base_url": INFER,
    "user_gateway_billing_url": BILL,
}


class FakeGateway:
    """推理接口 + 计费接口。计费按 request_id 去重，和真网关一致。"""

    def __init__(self) -> None:
        self.charged: dict[str, dict] = {}
        self.calls: list[dict] = []
        self.billing_error: tuple[int, str] | None = None
        self.infer_delay = 0.0
        self.usage = {"prompt_tokens": 10, "completion_tokens": 5,
                      "prompt_tokens_details": {"cached_tokens": 2}}

    async def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(INFER):
            assert request.headers["authorization"] == "Bearer platform-key"
            if url.endswith("/models"):
                return httpx.Response(200, json={"data": [{"id": "gw-m"}, {"id": "gw-other"}]})
            if self.infer_delay:
                await asyncio.sleep(self.infer_delay)
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "ok"}}], "usage": self.usage,
            })
        if url == "http://gw.test/v1/models":
            # 拉模型仍是用户 Key 走个人网关
            assert request.headers["authorization"] == "Bearer sk-batch"
            return httpx.Response(200, json={"data": [{"id": "gw-m"}, {"id": "gw-other"}]})
        if url.startswith("http://gw.test/api/usage/token"):
            if request.headers["authorization"] != "Bearer sk-batch":
                return httpx.Response(401, json={"success": False, "message": "无效的令牌"})
            return httpx.Response(200, json={"code": True, "data": {
                "name": "batch", "total_available": 123456, "unlimited_quota": False,
                "model_limits_enabled": True, "model_limits": {"gw-m": True},
            }})
        if url == BILL:
            assert request.headers["authorization"] == "Bearer nap-token"
            body = json.loads(request.content or b"{}")
            self.calls.append(body)
            if not body:
                return httpx.Response(400, json={"success": False, "code": "invalid_request", "message": "缺参数"})
            if self.billing_error:
                status_code, code = self.billing_error
                return httpx.Response(status_code, json={"success": False, "code": code, "message": code})
            rid = body["request_id"]
            if rid in self.charged:
                return httpx.Response(200, json={"success": True, "data": {**self.charged[rid], "duplicate": True}})
            data = {"request_id": rid, "quota": body["prompt_tokens"] + body["completion_tokens"],
                    "billing_source": "wallet", "duplicate": False}
            self.charged[rid] = {**data, "body": body}
            return httpx.Response(200, json={"success": True, "data": data})
        return httpx.Response(404)

    def total_charged(self, key: str) -> int:
        return sum(c["body"][key] for c in self.charged.values())


@pytest.fixture
def gw(monkeypatch) -> FakeGateway:
    fake = FakeGateway()
    real_init = httpx.AsyncClient.__init__

    def fake_init(self, *args, **kwargs):
        # ASGI 测试客户端自带 transport，别动它
        kwargs.setdefault("transport", httpx.MockTransport(fake.handler))
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", fake_init)
    return fake


@pytest_asyncio.fixture
async def billing_runtime():
    async with session_scope() as db:
        await write_runtime(db, {
            **BILLING_RUNTIME,
            "user_gateway_inference_api_key_encrypted": encrypt_secret("platform-key"),
            "user_gateway_billing_token_encrypted": encrypt_secret("nap-token"),
        })
    yield
    async with session_scope() as db:
        await write_runtime(db, {
            k: DEFAULTS[k] for k in DEFAULTS if k.startswith("user_gateway_")
        })


@pytest_asyncio.fixture
async def bill_user():
    async with session_scope() as db:
        user = User(username=f"bill-{uuid.uuid4().hex[:6]}", llm_token_encrypted=encrypt_secret("sk-batch"))
        db.add(user)
        await db.flush()
        uid = user.id
    yield uid
    async with session_scope() as db:
        await db.delete(await db.get(User, uid))


async def _job(user_id: str, **kw) -> str:
    async with session_scope() as db:
        job = Job(
            name=kw.pop("name", "计费任务"), user_id=user_id, model_source=ModelSource.personal,
            personal_model_name="gw-m", status=kw.pop("status", JobStatus.queued),
            input_filename="a.jsonl", input_path=kw.pop("input_path", "/tmp/none.jsonl"),
            billing_enabled=kw.pop("billing_enabled", True), **kw,
        )
        db.add(job)
        await db.flush()
        return job.id


async def _set(job_id: str, **values) -> None:
    async with session_scope() as db:
        job = await db.get(Job, job_id)
        for k, v in values.items():
            setattr(job, k, v)


async def _records(job_id: str) -> list[BillingRecord]:
    async with session_scope() as db:
        return list((await db.execute(
            select(BillingRecord).where(BillingRecord.job_id == job_id).order_by(BillingRecord.id)
        )).scalars())


async def _get(job_id: str) -> Job:
    async with session_scope() as db:
        return await db.get(Job, job_id)


# --------------------------------------------------------------------------- #
# 切记录
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_cut_bills_only_the_delta(bill_user):
    job_id = await _job(bill_user, prompt_tokens=100, completion_tokens=40, cached_tokens=30)

    first = await billing.cut_job(job_id)
    assert first.request_id == f"bic-{job_id}-1"
    assert (first.prompt_tokens, first.completion_tokens, first.cached_tokens) == (100, 40, 30)
    # 没有新用量就不切
    assert await billing.cut_job(job_id) is None

    await _set(job_id, prompt_tokens=150, completion_tokens=40, cached_tokens=200)
    second = await billing.cut_job(job_id)
    # 缓存 token 不会超过本笔的输入 token
    assert (second.request_id, second.prompt_tokens, second.completion_tokens, second.cached_tokens) == (
        f"bic-{job_id}-2", 50, 0, 50,
    )
    job = await _get(job_id)
    assert (job.billed_prompt_tokens, job.billed_completion_tokens, job.billing_seq) == (150, 40, 2)


@pytest.mark.asyncio
async def test_concurrent_cuts_never_double_bill(bill_user):
    job_id = await _job(bill_user, prompt_tokens=500, completion_tokens=500)
    results = await asyncio.gather(*(billing.cut_job(job_id) for _ in range(8)))
    made = [r for r in results if r is not None]
    assert sum(r.prompt_tokens for r in made) == 500
    assert sum(r.completion_tokens for r in made) == 500


@pytest.mark.asyncio
async def test_cut_ignores_counters_that_went_backwards(bill_user):
    """崩溃后断点续跑，库里的用量可能暂时比已计费的少：不倒扣，只计超出部分。"""
    job_id = await _job(bill_user, prompt_tokens=80, completion_tokens=10,
                        billed_prompt_tokens=100, billed_completion_tokens=10)
    assert await billing.cut_job(job_id) is None
    await _set(job_id, prompt_tokens=120, completion_tokens=12)
    record = await billing.cut_job(job_id)
    assert (record.prompt_tokens, record.completion_tokens) == (20, 2)


@pytest.mark.asyncio
async def test_cut_skips_jobs_without_billing(bill_user):
    job_id = await _job(bill_user, prompt_tokens=100, billing_enabled=False)
    assert await billing.cut_job(job_id) is None


# --------------------------------------------------------------------------- #
# 发送
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_settle_success_and_resend_is_idempotent(gw, billing_runtime, bill_user):
    job_id = await _job(bill_user, prompt_tokens=100, completion_tokens=40, cached_tokens=30)
    await billing.cut_job(job_id)
    result = await billing.settle_job(job_id)
    assert result.problem is None and result.sent == 1

    [record] = await _records(job_id)
    assert record.status == BillingStatus.succeeded and record.quota == 140
    assert gw.calls[-1] == {
        "key": "sk-batch", "request_id": f"bic-{job_id}-1", "model_name": "gw-m",
        "prompt_tokens": 100, "completion_tokens": 40, "cached_tokens": 30,
        "note": "批量推理「计费任务」第 1 笔",
    }
    # 已成功的不会再发
    assert (await billing.settle_job(job_id, include_failed=True)).sent == 0
    assert len(gw.calls) == 1


@pytest.mark.asyncio
async def test_transient_error_stays_pending_with_backoff(gw, billing_runtime, bill_user):
    job_id = await _job(bill_user, prompt_tokens=10, completion_tokens=1)
    await billing.cut_job(job_id)
    gw.billing_error = (500, "internal_error")
    result = await billing.settle_job(job_id)
    assert result.problem is None

    [record] = await _records(job_id)
    assert record.status == BillingStatus.pending and record.attempts == 1
    assert record.error_code == "internal_error" and record.next_attempt_at is not None
    # 退避期内巡检不会重发
    assert (await billing.sweep())["sent"] == 0

    # 到期后用同一个 request_id 重发成功
    await _set_record(record.id, next_attempt_at=None)
    gw.billing_error = None
    assert (await billing.sweep())["sent"] >= 1
    [record] = await _records(job_id)
    assert record.status == BillingStatus.succeeded
    assert [c["request_id"] for c in gw.calls] == [f"bic-{job_id}-1"] * 2


async def _set_record(record_id: int, **values) -> None:
    async with session_scope() as db:
        record = await db.get(BillingRecord, record_id)
        for k, v in values.items():
            setattr(record, k, v)


@pytest.mark.asyncio
async def test_rejection_reports_problem_and_needs_explicit_retry(gw, billing_runtime, bill_user):
    job_id = await _job(bill_user, prompt_tokens=10, completion_tokens=1)
    await billing.cut_job(job_id)
    gw.billing_error = (402, "insufficient_quota")
    result = await billing.settle_job(job_id)
    assert "余额" in result.problem

    [record] = await _records(job_id)
    assert record.status == BillingStatus.failed and record.error_code == "insufficient_quota"
    # 被拒绝的不自动重发
    assert (await billing.sweep())["sent"] == 0

    gw.billing_error = None
    assert (await billing.settle_job(job_id, include_failed=True)).problem is None
    [record] = await _records(job_id)
    assert record.status == BillingStatus.succeeded


@pytest.mark.asyncio
async def test_repeated_transient_failures_eventually_stop_the_job(gw, billing_runtime, bill_user):
    job_id = await _job(bill_user, prompt_tokens=10, completion_tokens=1)
    await billing.cut_job(job_id)
    [record] = await _records(job_id)
    await _set_record(record.id, attempts=billing.SOFT_FAIL_LIMIT)
    gw.billing_error = (503, "internal_error")
    assert "连续" in (await billing.settle_job(job_id)).problem


@pytest.mark.asyncio
async def test_sweep_cuts_orphaned_usage(gw, billing_runtime, bill_user):
    """没在跑的任务还有没切的用量（worker 崩溃等），巡检补切并扣掉。"""
    job_id = await _job(bill_user, status=JobStatus.paused, prompt_tokens=7, completion_tokens=3)
    running = await _job(bill_user, status=JobStatus.running, prompt_tokens=7, completion_tokens=3)
    await billing.sweep()
    [record] = await _records(job_id)
    assert record.status == BillingStatus.succeeded
    # 运行中的由 runner 自己扣，巡检不碰
    assert await _records(running) == []


@pytest.mark.asyncio
async def test_rekey_moves_key_failures_to_new_key(gw, billing_runtime, bill_user):
    job_id = await _job(bill_user, prompt_tokens=10, completion_tokens=1)
    await billing.cut_job(job_id)
    gw.billing_error = (404, "invalid_key")
    await billing.settle_job(job_id)

    async with session_scope() as db:
        user = await db.get(User, bill_user)
        user.llm_token_encrypted = encrypt_secret("sk-new")
        await db.flush()
        assert await billing.rekey_failed(db, job_id, user) == 1
    gw.billing_error = None
    await billing.settle_job(job_id, include_failed=True)
    assert gw.calls[-1]["key"] == "sk-new"


# --------------------------------------------------------------------------- #
# 网关：推理接口、Key 校验
# --------------------------------------------------------------------------- #
def test_usage_url_keeps_sub_path():
    assert usage_url("https://gw.example.com/api/batch/billing") == "https://gw.example.com/api/usage/token/"
    assert usage_url("https://x.com/gw/api/batch/billing/") == "https://x.com/gw/api/usage/token/"
    assert usage_url("https://x.com/custom") == "https://x.com/api/usage/token/"


def test_personal_config_uses_platform_inference_key():
    runtime = {**DEFAULTS, **BILLING_RUNTIME,
               "user_gateway_inference_api_key_encrypted": encrypt_secret("platform-key")}
    mc = build_personal_config("gw-m", "sk-batch", runtime)
    from app.core.security import decrypt_secret

    assert mc.base_url == INFER
    assert decrypt_secret(mc.api_key_encrypted) == "platform-key"


@pytest.mark.asyncio
async def test_models_come_from_gateway_with_user_key(gw):
    runtime = {**DEFAULTS, **BILLING_RUNTIME,
               "user_gateway_inference_api_key_encrypted": encrypt_secret("platform-key"),
               "user_gateway_billing_token_encrypted": encrypt_secret("nap-token")}
    models, info = await list_user_models(runtime, "sk-batch")
    assert models == ["gw-m", "gw-other"]
    assert info.total_available == 123456 and info.model_limits == ["gw-m"]

    with pytest.raises(GatewayError, match="Key 不可用"):
        await check_billing_key(runtime, "sk-wrong")

    with pytest.raises(GatewayError, match="计费接口"):
        await list_user_models({**runtime, "user_gateway_billing_url": ""}, "sk-batch")


# --------------------------------------------------------------------------- #
# runner：定期扣费、扣不上暂停、恢复时补扣
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def redis_ok(monkeypatch):
    """runner 要用 Redis 收信号、报进度。每个用例一个新连接 —— 全局 queue 的连接池绑在别的事件循环上。"""
    import os

    import app.worker.runner as runner_mod
    from app.services.queue import JobQueue

    rq = JobQueue(redis_url=os.environ.get("TEST_REDIS_URL", settings.redis_url),
                  prefix=f"test-{uuid.uuid4().hex[:8]}")
    if not await rq.ping():
        await rq.close()
        pytest.skip("没有可用的 Redis")
    monkeypatch.setattr(runner_mod, "queue", rq)
    yield rq
    keys = [k async for k in rq.redis.scan_iter(match=f"{rq.prefix}:*")]
    if keys:
        await rq.redis.delete(*keys)
    await rq.close()


def _input(n: int) -> str:
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    path = settings.upload_dir / f"bill-{uuid.uuid4().hex}.jsonl"
    path.write_text("\n".join(
        json.dumps({"custom_id": f"r{i}", "body": {"messages": [{"role": "user", "content": str(i)}]}})
        for i in range(n)
    ))
    return str(path)


@pytest.mark.asyncio
async def test_runner_bills_once_at_finish(gw, billing_runtime, bill_user, redis_ok):
    from app.worker.runner import JobRunner

    job_id = await _job(bill_user, input_path=_input(6), total_items=6, billing_enabled=False)
    assert await JobRunner(job_id, "w-test").run() == JobStatus.succeeded

    job = await _get(job_id)
    assert job.billing_enabled
    assert (job.prompt_tokens, job.completion_tokens, job.cached_tokens) == (60, 30, 12)
    assert (job.billed_prompt_tokens, job.billed_completion_tokens) == (60, 30)
    [record] = await _records(job_id)
    assert record.status == BillingStatus.succeeded
    assert (gw.total_charged("prompt_tokens"), gw.total_charged("cached_tokens")) == (60, 12)


@pytest.mark.asyncio
async def test_runner_pauses_on_rejection_and_resumes_without_double_billing(
    gw, billing_runtime, bill_user, redis_ok, monkeypatch,
):
    from app.worker.runner import JobRunner

    monkeypatch.setattr(billing, "interval_of", lambda runtime: 0.2)
    gw.infer_delay = 0.05
    gw.billing_error = (402, "insufficient_quota")
    job_id = await _job(bill_user, input_path=_input(30), total_items=30, concurrency=1)

    assert await JobRunner(job_id, "w-test").run() == JobStatus.paused
    job = await _get(job_id)
    assert job.status == JobStatus.paused and "余额" in job.error
    assert job.completed_items < 30
    assert all(r.status == BillingStatus.failed for r in await _records(job_id))

    # 还没充值就恢复：开跑前补扣失败，原地暂停，不多跑一条
    done_before = job.completed_items
    assert await JobRunner(job_id, "w-test").run() == JobStatus.paused
    assert (await _get(job_id)).completed_items == done_before

    # 充值后恢复：先补扣、再跑完；每笔 request_id 只成功扣一次，总数等于实际用量
    gw.billing_error = None
    assert await JobRunner(job_id, "w-test").run() == JobStatus.succeeded
    job = await _get(job_id)
    assert job.completed_items == 30
    records = await _records(job_id)
    assert all(r.status == BillingStatus.succeeded for r in records)
    assert len({r.request_id for r in records}) == len(records)
    assert gw.total_charged("prompt_tokens") == job.prompt_tokens == 300
    assert gw.total_charged("completion_tokens") == 150


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_admin_settings_keep_secrets_write_only(admin_client: AsyncClient):
    resp = await admin_client.patch("/api/admin/settings", json={
        "user_gateway_billing_enabled": True,
        "user_gateway_inference_base_url": INFER + "/",
        "user_gateway_inference_api_key": "sk-platform-secret",
        "user_gateway_billing_url": BILL,
        "user_gateway_billing_token": "nap_secret_token",
        "user_gateway_billing_interval": 30,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["user_gateway_inference_base_url"] == INFER
    assert data["user_gateway_inference_api_key_masked"] == "sk-p******cret"
    assert data["user_gateway_billing_token_masked"].startswith("nap_")
    assert "secret" not in json.dumps((await admin_client.get("/api/settings")).json()).replace(
        "masked", "",
    ).replace("cret", "")

    # 不传 = 保持；空串 = 清除
    data = (await admin_client.patch("/api/admin/settings", json={"user_gateway_billing_interval": 60})).json()
    assert data["user_gateway_billing_token_masked"]
    data = (await admin_client.patch("/api/admin/settings", json={"user_gateway_billing_token": ""})).json()
    assert data["user_gateway_billing_token_masked"] is None

    assert (await admin_client.patch("/api/admin/settings", json={
        "user_gateway_billing_url": "gw.test/api",
    })).status_code == 422
    async with session_scope() as db:
        await write_runtime(db, {k: DEFAULTS[k] for k in DEFAULTS if k.startswith("user_gateway_")})


@pytest.mark.asyncio
async def test_gateway_test_endpoint(admin_client: AsyncClient, gw, billing_runtime):
    data = (await admin_client.post("/api/admin/billing/test")).json()
    assert data["inference_ok"] and "2 个模型" in data["inference_detail"]
    assert data["billing_ok"]
    # 测试请求是空请求体，不会扣费
    assert gw.charged == {}


@pytest.mark.asyncio
async def test_job_billing_endpoints(admin_client: AsyncClient, gw, billing_runtime):
    admin_id = (await admin_client.get("/api/auth/me")).json()["id"]
    async with session_scope() as db:
        admin = await db.get(User, admin_id)
        admin.llm_token_encrypted = encrypt_secret("sk-batch")
    job_id = await _job(admin_id, status=JobStatus.completed, prompt_tokens=10, completion_tokens=2)
    await billing.cut_job(job_id)
    gw.billing_error = (402, "insufficient_key_quota")
    await billing.settle_job(job_id)

    data = (await admin_client.get(f"/api/jobs/{job_id}/billing")).json()
    assert data["enabled"] and data["failed"]["count"] == 1
    assert "Key 本身的额度不足" in data["records"][0]["error_hint"]

    gw.billing_error = None
    data = (await admin_client.post(f"/api/jobs/{job_id}/billing/retry")).json()
    assert data["message"] == "已重发 1 笔"
    assert data["succeeded"]["count"] == 1 and data["failed"]["count"] == 0

    listing = (await admin_client.get("/api/admin/billing/records", params={"job_id": job_id})).json()
    assert listing["total"] == 1 and listing["items"][0]["status"] == "succeeded"

    async with session_scope() as db:
        admin = await db.get(User, admin_id)
        admin.llm_token_encrypted = None
        await db.delete(await db.get(Job, job_id))


@pytest.mark.asyncio
async def test_change_model_settles_old_model_first(admin_client: AsyncClient, billing_runtime):
    admin_id = (await admin_client.get("/api/auth/me")).json()["id"]
    job_id = await _job(admin_id, status=JobStatus.paused, prompt_tokens=40, completion_tokens=4)
    async with session_scope() as db:
        from app.models import ModelConfig

        mc = ModelConfig(name=f"bill-shared-{uuid.uuid4().hex[:6]}", display_name="s",
                         base_url="http://x/v1", model_name="shared-m")
        db.add(mc)
        await db.flush()
        mc_id = mc.id

    resp = await admin_client.post(f"/api/jobs/{job_id}/model", json={
        "model_source": "shared", "model_config_id": mc_id,
    })
    assert resp.status_code == 200, resp.text
    [record] = await _records(job_id)
    assert (record.model_name, record.prompt_tokens) == ("gw-m", 40)
    job = await _get(job_id)
    assert not job.billing_enabled and job.billed_prompt_tokens == 40

    async with session_scope() as db:
        await db.delete(await db.get(Job, job_id))
        await db.delete(await db.get(ModelConfig, mc_id))


@pytest.mark.asyncio
async def test_billing_admin_endpoints_reject_normal_users(admin_client: AsyncClient):
    name = f"billuser-{uuid.uuid4().hex[:6]}"
    await admin_client.post("/api/admin/users", json={"username": name, "password": "UserPass123!", "role": "user"})
    async with AsyncClient(transport=admin_client._transport, base_url="http://test") as c:
        assert (await c.post("/api/auth/login", json={"username": name, "password": "UserPass123!"})).status_code == 200
        assert (await c.get("/api/admin/billing/records")).status_code == 403
        assert (await c.post("/api/admin/billing/records/1/retry")).status_code == 403
        assert (await c.post("/api/admin/billing/test")).status_code == 403


@pytest.mark.asyncio
async def test_switching_between_gateway_models_bills_each_model_separately(
    admin_client: AsyncClient, gw, billing_runtime,
):
    """中途换模型：换之前的用量记在旧模型名下，之后的用量记在新模型名下，互不混算。"""
    admin_id = (await admin_client.get("/api/auth/me")).json()["id"]
    async with session_scope() as db:
        (await db.get(User, admin_id)).llm_token_encrypted = encrypt_secret("sk-batch")
    job_id = await _job(admin_id, status=JobStatus.paused, prompt_tokens=40, completion_tokens=4)
    # 换模型前已经切出并扣过一笔
    await _set(job_id, prompt_tokens=30, completion_tokens=3)
    await billing.cut_job(job_id)
    await _set(job_id, prompt_tokens=40, completion_tokens=4)

    resp = await admin_client.post(f"/api/jobs/{job_id}/model", json={
        "model_source": "personal", "personal_model": "gw-other",
    })
    assert resp.status_code == 200, resp.text
    job = await _get(job_id)
    assert job.billing_enabled and job.personal_model_name == "gw-other"
    assert (job.billed_prompt_tokens, job.billed_completion_tokens) == (40, 4)

    # 换模型之后的用量
    await _set(job_id, prompt_tokens=100, completion_tokens=10)
    await billing.cut_job(job_id)
    await billing.settle_job(job_id)

    records = await _records(job_id)
    assert [(r.model_name, r.prompt_tokens, r.completion_tokens) for r in records] == [
        ("gw-m", 30, 3), ("gw-m", 10, 1), ("gw-other", 60, 6),
    ]
    assert all(r.status == BillingStatus.succeeded for r in records)
    assert {c["model_name"] for c in gw.calls} == {"gw-m", "gw-other"}

    async with session_scope() as db:
        (await db.get(User, admin_id)).llm_token_encrypted = None
        await db.delete(await db.get(Job, job_id))
