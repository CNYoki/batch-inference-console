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
        self.billing_passthrough = False
        # (哪个推理接口, Authorization, 请求里的 model)
        self.inferred: list[tuple[str, str, str]] = []
        self.usage = {"prompt_tokens": 10, "completion_tokens": 5,
                      "prompt_tokens_details": {"cached_tokens": 2}}

    async def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("http://infer2.test/v1/chat"):
            # 分模型配置里单独指定的推理接口
            body = json.loads(request.content)
            self.inferred.append(("infer2", request.headers["authorization"], body["model"]))
            return httpx.Response(200, json={
                "model": body["model"], "choices": [{"message": {"content": "ok"}}], "usage": self.usage,
            })
        if url.startswith(INFER):
            assert request.headers["authorization"] == "Bearer platform-key"
            if url.endswith("/chat/completions"):
                body = json.loads(request.content)
                self.inferred.append(("infer", request.headers["authorization"], body["model"]))
            if url.endswith("/models"):
                return httpx.Response(200, json={"data": [{"id": "gw-m"}, {"id": "gw-other"}]})
            if self.infer_delay:
                await asyncio.sleep(self.infer_delay)
            return httpx.Response(200, json={
                "model": json.loads(request.content)["model"],
                "choices": [{"message": {"content": "ok"}}], "usage": self.usage,
            })
        if url == "http://gw.test/v1/models":
            # 拉模型仍是用户 Key 走个人网关
            assert request.headers["authorization"].startswith("Bearer sk-")
            return httpx.Response(200, json={"data": [{"id": "gw-m"}, {"id": "gw-other"}]})
        if url.startswith("http://gw.test/api/usage/token"):
            # sk-nogroup* 模拟老网关：用量查询不返回 group，只能靠 0 元计费试探
            group = {"Bearer sk-batch": "batch", "Bearer sk-normal": "default",
                     "Bearer sk-nogroup": None, "Bearer sk-nogroup-plain": None}
            auth = request.headers["authorization"]
            if auth not in group:
                return httpx.Response(401, json={"success": False, "message": "无效的令牌"})
            data = {
                "name": "batch", "total_available": 123456, "unlimited_quota": False,
                "model_limits_enabled": True, "model_limits": {"gw-m": True},
            }
            if group[auth]:
                data["group"] = group[auth]
            return httpx.Response(200, json={"code": True, "data": data})
        if url == BILL:
            body = json.loads(request.content or b"{}")
            # 透传模式下请求头里是用户自己的 Key
            expected = f"Bearer {body.get('key')}" if self.billing_passthrough else "Bearer nap-token"
            assert request.headers["authorization"] == expected
            self.calls.append(body)
            if not body:
                return httpx.Response(400, json={"success": False, "code": "invalid_request", "message": "缺参数"})
            if "amount" in body:
                # 0 元计费：非 batch 的 Key 被拒，batch Key 扣 0 元
                if body["key"] in ("sk-normal", "sk-nogroup-plain"):
                    return httpx.Response(403, json={"success": False, "code": "key_not_batch", "message": "非 batch"})
                return httpx.Response(200, json={"success": True, "data": {
                    "request_id": body["request_id"], "quota": 0, "billing_source": "wallet", "duplicate": False}})
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


# --------------------------------------------------------------------------- #
# 分模型配置：模型独立计费（单独的推理接口、重定向模型名）
# --------------------------------------------------------------------------- #
def _route_runtime(**rule) -> dict:
    return {
        **DEFAULTS, **BILLING_RUNTIME,
        "user_gateway_inference_api_key_encrypted": encrypt_secret("platform-key"),
        "user_gateway_billing_token_encrypted": encrypt_secret("nap-token"),
        "user_gateway_reasoning_rules": [{"pattern": "gw-*", "enabled": True, **rule}],
    }


def test_model_route_overrides_and_falls_back():
    from app.services.gateway import resolve_route

    full = _route_runtime(
        billing_override=True, inference_base_url="http://infer2.test/v1/",
        inference_api_key_encrypted=encrypt_secret("model-key"), upstream_model="real-model",
    )
    assert resolve_route("gw-m", full, "sk-batch") == ("http://infer2.test/v1", "model-key", "real-model")
    # 没命中规则的模型走全局推理接口
    assert resolve_route("other", full, "sk-batch") == (INFER, "platform-key", "other")
    # 留空的项各自退回全局
    partial = _route_runtime(billing_override=True, upstream_model="real-model")
    assert resolve_route("gw-m", partial, "sk-batch") == (INFER, "platform-key", "real-model")
    # 规则没打开「模型独立计费」，或全局独立计费没开，都不看分模型的这几项
    assert resolve_route("gw-m", {**full, "user_gateway_reasoning_rules": [
        {**full["user_gateway_reasoning_rules"][0], "billing_override": False}]}, "sk") == (INFER, "platform-key", "gw-m")
    off = {**full, "user_gateway_billing_enabled": False, "user_gateway_base_url": "http://gw.test/v1"}
    assert resolve_route("gw-m", off, "sk-user") == ("http://gw.test/v1", "sk-user", "gw-m")

    mc = build_personal_config("gw-m", "sk-batch", full)
    assert (mc.display_name, mc.model_name, mc.base_url) == ("gw-m", "real-model", "http://infer2.test/v1")


@pytest.mark.asyncio
async def test_rule_keys_are_write_only_and_hidden_from_users(admin_client: AsyncClient):
    rule = {"pattern": "gw-*", "enabled": True, "billing_override": True,
            "inference_base_url": "http://infer2.test/v1", "inference_api_key": "model-secret-key",
            "upstream_model": "real-model"}
    data = (await admin_client.patch("/api/admin/settings", json={"user_gateway_reasoning_rules": [rule]})).json()
    [saved] = data["user_gateway_reasoning_rules"]
    assert saved["id"] and saved["inference_api_key_masked"] == "mode******-key"
    assert saved["inference_api_key"] is None and saved["upstream_model"] == "real-model"

    # 不传 Key = 保持；按 id 认，调换顺序也不串
    other = {"pattern": "x-*", "enabled": True}
    data = (await admin_client.patch("/api/admin/settings", json={"user_gateway_reasoning_rules": [
        other, {**saved, "inference_api_key": None, "inference_api_key_masked": None},
    ]})).json()
    assert [r["inference_api_key_masked"] for r in data["user_gateway_reasoning_rules"]] == [None, "mode******-key"]
    # 空串 = 清除
    kept = data["user_gateway_reasoning_rules"][1]
    data = (await admin_client.patch("/api/admin/settings", json={"user_gateway_reasoning_rules": [
        {**kept, "inference_api_key": ""}]})).json()
    assert data["user_gateway_reasoning_rules"][0]["inference_api_key_masked"] is None

    # 普通用户读设置看不到推理接口和重定向模型名
    await admin_client.patch("/api/admin/settings", json={
        "user_gateway_reasoning_rules": [rule], "user_gateway_billing_url": BILL,
        "user_gateway_inference_base_url": INFER,
    })
    name = f"ruleuser-{uuid.uuid4().hex[:6]}"
    await admin_client.post("/api/admin/users", json={"username": name, "password": "UserPass123!", "role": "user"})
    async with AsyncClient(transport=admin_client._transport, base_url="http://test") as c:
        login = await c.post("/api/auth/login", json={"username": name, "password": "UserPass123!"})
        assert login.status_code == 200, login.text
        public = (await c.get("/api/settings")).json()
    # 只看值，字段名里本来就有 inference
    def values(obj):
        if isinstance(obj, dict):
            for v in obj.values():
                yield from values(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from values(v)
        elif isinstance(obj, str):
            yield obj
    leaked = [v for v in values(public) if any(x in v for x in ("real-model", "infer", "gw.test"))]
    assert leaked == []
    assert public["user_gateway_reasoning_rules"][0]["pattern"] == "gw-*"

    async with session_scope() as db:
        await write_runtime(db, {k: DEFAULTS[k] for k in DEFAULTS if k.startswith("user_gateway_")})


async def _upload(client: AsyncClient) -> str:
    content = json.dumps({"custom_id": "a", "body": {"messages": [{"role": "user", "content": "hi"}]}})
    resp = await client.post("/api/jobs/upload", files={"file": ("in.jsonl", content.encode(), "application/json")})
    return resp.json()["upload_id"]


@pytest.mark.asyncio
async def test_dry_run_reports_billing_failure_and_hides_upstream_model(
    admin_client: AsyncClient, gw, billing_runtime,
):
    async with session_scope() as db:
        await write_runtime(db, {"user_gateway_reasoning_rules": [{
            "id": "r1", "pattern": "gw-*", "enabled": True, "billing_override": True,
            "inference_base_url": "http://infer2.test/v1",
            "inference_api_key_encrypted": encrypt_secret("model-key"), "upstream_model": "real-model",
        }]})
    body = {"model_source": "personal", "personal_model": "gw-m", "personal_token": "sk-batch",
            "remember_token": False, "params": {}}

    out = (await admin_client.post("/api/jobs/dry-run", json={**body, "upload_id": await _upload(admin_client)})).json()
    assert out["ok"] and out["billing_ok"] is True
    # 推理发到了分模型接口、用的是重定向名；回显给用户的仍是原名
    assert gw.inferred[-1] == ("infer2", "Bearer model-key", "real-model")
    assert out["request_body"]["model"] == "gw-m" and out["response"]["model"] == "gw-m"
    # 计费用的是用户看到的模型名（网关按它定价）
    assert gw.calls[-1]["model_name"] == "gw-m"

    gw.billing_error = (402, "insufficient_quota")
    out = (await admin_client.post("/api/jobs/dry-run", json={**body, "upload_id": await _upload(admin_client)})).json()
    assert out["ok"] and out["billing_ok"] is False and "余额" in out["billing"]

    gw.billing_error = (503, "internal_error")
    out = (await admin_client.post("/api/jobs/dry-run", json={**body, "upload_id": await _upload(admin_client)})).json()
    assert out["billing_ok"] is False and "暂时不可用" in out["billing"]


# --------------------------------------------------------------------------- #
# 限制 API Key 添加
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_require_batch_key_rejects_other_groups(admin_client: AsyncClient, gw, billing_runtime):
    async with session_scope() as db:
        await write_runtime(db, {"user_gateway_billing_require_batch_key": True})

    ok = await admin_client.post("/api/models/personal", json={"token": "sk-batch", "remember": False})
    assert ok.status_code == 200 and ok.json()["key"]["group"] == "batch"

    resp = await admin_client.post("/api/models/personal", json={"token": "sk-normal", "remember": True})
    assert resp.status_code == 400 and "batch 分组" in resp.json()["detail"]
    # 网关没返回分组时发 0 元计费试探：batch Key 放行，其余拒绝
    calls_before = len(gw.calls)
    resp = await admin_client.post("/api/models/personal", json={"token": "sk-nogroup", "remember": False})
    assert resp.status_code == 200, resp.text
    probe = gw.calls[calls_before]
    assert probe["amount"] == "0" and probe["request_id"].startswith("bic-keycheck-")
    resp = await admin_client.post("/api/models/personal", json={"token": "sk-nogroup-plain", "remember": True})
    assert resp.status_code == 400 and "batch 分组" in resp.json()["detail"]
    # 被拒的 Key 不会保存下来
    me = (await admin_client.get("/api/auth/me")).json()
    async with session_scope() as db:
        assert (await db.get(User, me["id"])).llm_token_encrypted is None

    # 开关关着时不检查分组
    async with session_scope() as db:
        await write_runtime(db, {"user_gateway_billing_require_batch_key": False})
    resp = await admin_client.post("/api/models/personal", json={"token": "sk-normal", "remember": False})
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# 透传用户 Key、仅单独配置的模型计费
# --------------------------------------------------------------------------- #
def test_passthrough_uses_user_key():
    from app.services.gateway import billing_auth, billing_endpoint_error, resolve_route

    runtime = {**DEFAULTS, **BILLING_RUNTIME,
               "user_gateway_inference_key_passthrough": True, "user_gateway_billing_key_passthrough": True}
    assert resolve_route("gw-m", runtime, "sk-user")[1] == "sk-user"
    assert billing_auth(runtime, "sk-user") == "sk-user"
    # 透传时不要求平台的访问令牌
    assert billing_endpoint_error(runtime) is None
    assert billing_config_error_for(runtime, "gw-m") is None

    # 分模型配置也能单独透传
    rule_runtime = _route_runtime(billing_override=True, inference_base_url="http://infer2.test/v1",
                                  inference_key_passthrough=True)
    assert resolve_route("gw-m", rule_runtime, "sk-user") == ("http://infer2.test/v1", "sk-user", "gw-m")


def billing_config_error_for(runtime: dict, model: str):
    from app.services.gateway import billing_config_error

    return billing_config_error(runtime, model)


def test_only_matched_models_are_billed():
    from app.services.gateway import billing_applies

    runtime = _route_runtime(billing_override=True)
    assert billing_applies("gw-m", runtime) and billing_applies("other", runtime)
    only = {**runtime, "user_gateway_billing_only_matched": True}
    assert billing_applies("gw-m", only)
    # 没命中、或命中了但没打开「模型独立计费」的都不计费
    assert not billing_applies("other", only)
    assert not billing_applies("gw-m", {**only, "user_gateway_reasoning_rules": [
        {"pattern": "gw-*", "billing_override": False}]})
    # 不计费的模型不要求计费接口配齐
    assert billing_config_error_for({**only, "user_gateway_billing_url": ""}, "other") is None
    assert billing_config_error_for({**only, "user_gateway_billing_url": ""}, "gw-m")


@pytest.mark.asyncio
async def test_runner_skips_billing_for_unmatched_model(gw, billing_runtime, bill_user, redis_ok):
    from app.worker.runner import JobRunner

    async with session_scope() as db:
        await write_runtime(db, {
            "user_gateway_billing_only_matched": True,
            "user_gateway_reasoning_rules": [{"pattern": "gw-other", "billing_override": True}],
        })
    job_id = await _job(bill_user, input_path=_input(3), total_items=3)
    assert await JobRunner(job_id, "w-test").run() == JobStatus.succeeded
    job = await _get(job_id)
    assert not job.billing_enabled and job.prompt_tokens == 30
    assert await _records(job_id) == [] and gw.calls == []


@pytest.mark.asyncio
async def test_runner_bills_with_passthrough_key(gw, billing_runtime, bill_user, redis_ok):
    from app.worker.runner import JobRunner

    gw.billing_passthrough = True
    async with session_scope() as db:
        await write_runtime(db, {"user_gateway_billing_key_passthrough": True,
                                 "user_gateway_billing_token_encrypted": ""})
    job_id = await _job(bill_user, input_path=_input(2), total_items=2, billing_enabled=False)
    assert await JobRunner(job_id, "w-test").run() == JobStatus.succeeded
    [record] = await _records(job_id)
    assert record.status == BillingStatus.succeeded and gw.calls[-1]["key"] == "sk-batch"



def test_passthrough_requests_carry_batch_marker():
    from app.services.inference import build_request_body

    runtime = {**DEFAULTS, **BILLING_RUNTIME, "user_gateway_inference_key_passthrough": True}
    mc = build_personal_config("gw-m", "sk-user", runtime)
    body = build_request_body(mc, {"messages": [], "batch": False}, {}, None)
    # 默认标识 {"batch": true}，用户数据里的同名字段盖不掉
    assert body["batch"] is True

    custom = {**runtime, "user_gateway_passthrough_marker": {"x_source": "batch", "batch": True}}
    body = build_request_body(build_personal_config("gw-m", "sk-user", custom), {"messages": []}, {}, None)
    assert body["x_source"] == "batch" and body["batch"] is True

    # 用平台 Key 时不带
    platform = {**runtime, "user_gateway_inference_key_passthrough": False,
                "user_gateway_inference_api_key_encrypted": encrypt_secret("platform-key")}
    assert "batch" not in build_request_body(build_personal_config("gw-m", "sk-user", platform), {"messages": []}, {}, None)

    # 分模型配置：单独设了 Key 的不带，单独透传的带
    own_key = _route_runtime(billing_override=True, inference_api_key_encrypted=encrypt_secret("model-key"))
    own_key["user_gateway_inference_key_passthrough"] = True
    assert "batch" not in build_request_body(build_personal_config("gw-m", "sk", own_key), {"messages": []}, {}, None)
    rule_pt = _route_runtime(billing_override=True, inference_key_passthrough=True)
    assert build_request_body(build_personal_config("gw-m", "sk", rule_pt), {"messages": []}, {}, None)["batch"] is True
    # 直连模式（没开独立计费）不带
    direct = {**DEFAULTS, "user_gateway_base_url": "http://gw.test/v1"}
    assert "batch" not in build_request_body(build_personal_config("gw-m", "sk", direct), {"messages": []}, {}, None)
