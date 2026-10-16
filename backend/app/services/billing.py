"""个人网关独立计费：把任务用量切成计费记录，再逐条调网关的计费接口扣费。

不重复计费靠两层：

1. 平台侧：任务上记着「已切进计费记录的用量」（billed_*）。切一条记录 = 取当前用量与
   billed_* 的差额、写一条记录、把 billed_* 推到当前值，三件事在同一个事务里，
   并以 billing_seq 做乐观锁 —— 多个 worker、API 同时切也只会有一个成功。
2. 网关侧：每条记录的 request_id 固定为 `bic-<任务ID>-<序号>`，发多少次都不变，
   网关按 request_id 去重。所以网络超时、进程崩溃后补发都是安全的。

记录先落库再发请求；发送失败的留在库里由 worker 定期重试。
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.security import decrypt_secret
from ..db import session_scope
from ..models import BillingRecord, BillingStatus, Job, JobStatus, ModelSource, User
from .gateway import (
    BATCH_GROUP,
    NOT_BATCH_MESSAGE,
    GatewayError,
    KeyInfo,
    billing_auth,
    billing_endpoint,
)
from .settings_store import read_runtime

log = logging.getLogger(__name__)

REQUEST_PREFIX = "bic"
BILLING_TIMEOUT = 20.0
# 认领一条记录去发送时先占住这么久，防止别的 worker 同时发同一笔（同一 request_id 并发会得到 409）
IN_FLIGHT_SECONDS = 120
# 临时错误的重试退避上限
MAX_BACKOFF_SECONDS = 600
# 同一笔遇到临时错误这么多次还没扣上，就把任务停下来 —— 不能让没扣费的用量无限累积
SOFT_FAIL_LIMIT = 5
MIN_INTERVAL = 10

# 网关说「稍后再试」的错误码；其余失败都要人处理
RETRIABLE_CODES = {"request_in_progress", "internal_error"}
# 跟用户的 Key 有关的错误：用户换了 Key 之后，这些失败记录改用新 Key 重发
KEY_CODES = {
    "invalid_key", "key_unavailable", "key_not_batch", "model_not_allowed", "insufficient_key_quota",
}

_MESSAGES = {
    "insufficient_quota": "个人网关余额或订阅额度不足，充值后点「继续」即可补扣并接着跑",
    "insufficient_key_quota": "Key 本身的额度不足，请在个人网关调高该 Key 的额度后点「继续」",
    "key_unavailable": "Key 已禁用、已过期或额度已用尽，请在个人网关处理后点「继续」",
    "invalid_key": "Key 不存在，请在「新建任务」页重新填写 batch 分组的 Key 后点「继续」",
    "key_not_batch": "Key 不属于 batch 分组，请换一个 batch 分组的 Key 后点「继续」",
    "model_not_allowed": "Key 限制了可用模型、不包含模型 {model}，请在个人网关放开限制后点「继续」",
    "user_disabled": "你的个人网关账号已被禁用",
    "batch_group_unavailable": "你的个人网关账号不能使用 batch 分组，请联系网关管理员",
    "AUTH_UNAUTHORIZED": "平台的计费令牌无效，请联系管理员",
    "ACCESS_TOKEN_SCOPE_DENIED": "平台的计费令牌没有 batch_billing:write 权限，请联系管理员",
    "AUTH_INSUFFICIENT_PRIVILEGE": "平台的计费令牌不是管理员令牌，请联系管理员",
    "model_price_error": "个人网关没有给模型 {model} 配置价格，请联系管理员",
}


def describe(code: str | None, message: str | None, model: str = "") -> str:
    """把网关错误码翻成给用户看的话。"""
    text = _MESSAGES.get(code or "")
    if text:
        return text.format(model=model)
    detail = message or "未知错误"
    return f"计费接口拒绝（{code}）：{detail}" if code else detail


def request_id(job_id: str, seq: int) -> str:
    return f"{REQUEST_PREFIX}-{job_id}-{seq}"


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# 切记录
# --------------------------------------------------------------------------- #
def rebase(job: Job, enabled: bool) -> None:
    """切换任务的计费开关，并把计费起点挪到当前用量。

    从不计费切到计费时，之前的用量（公用模型、直连网关时已由网关自己计过）都不该再扣；
    反过来切走时调用方应先 cut() 把差额结掉。
    """
    job.billing_enabled = enabled
    job.billed_prompt_tokens = job.prompt_tokens
    job.billed_completion_tokens = job.completion_tokens
    job.billed_cached_tokens = job.cached_tokens


async def cut(db: AsyncSession, job_id: str, *, note: str | None = None) -> BillingRecord | None:
    """把任务尚未计费的用量切成一条计费记录并提交。没有新用量时返回 None。

    只落库不发请求，所以 API 里也能放心调用。
    """
    job = (
        await db.execute(select(Job).where(Job.id == job_id).execution_options(populate_existing=True))
    ).scalar_one_or_none()
    if job is None or not job.billing_enabled:
        return None

    # 进程崩溃后断点续跑，库里的用量可能比已计费的还少（没落盘的结果会重跑）。
    # 这时不倒扣，等用量重新超过已计费部分后只计超出的那截
    dp = max(0, job.prompt_tokens - job.billed_prompt_tokens)
    dc = max(0, job.completion_tokens - job.billed_completion_tokens)
    if dp == 0 and dc == 0:
        return None
    dcached = min(max(0, job.cached_tokens - job.billed_cached_tokens), dp)

    seq = job.billing_seq + 1
    result = await db.execute(
        update(Job)
        .where(Job.id == job_id, Job.billing_seq == job.billing_seq)
        .values(
            billing_seq=seq,
            billed_prompt_tokens=job.billed_prompt_tokens + dp,
            billed_completion_tokens=job.billed_completion_tokens + dc,
            billed_cached_tokens=job.billed_cached_tokens + dcached,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        # 别人刚切过一条，这次的差额已经算在那条里了
        await db.rollback()
        return None

    user = await db.get(User, job.user_id)
    record = BillingRecord(
        request_id=request_id(job.id, seq),
        user_id=job.user_id,
        job_id=job.id,
        key_encrypted=user.llm_token_encrypted if user else None,
        model_name=job.personal_model_name or "",
        prompt_tokens=dp,
        completion_tokens=dc,
        cached_tokens=dcached,
        note=(note or f"批量推理「{job.name}」第 {seq} 笔")[:255],
        status=BillingStatus.pending,
    )
    db.add(record)
    await db.commit()
    return record


async def cut_job(job_id: str) -> BillingRecord | None:
    async with session_scope() as db:
        return await cut(db, job_id)


async def cut_before_change(db: AsyncSession, job: Job, enabled_after: bool) -> None:
    """任务换模型 / 切换计费方式之前调用：先把旧模型的差额结掉，再以当前用量为新起点。

    调用后 job 上的计费字段已更新但未提交，由调用方随其他改动一起提交。
    """
    if job.billing_enabled:
        await cut(db, job.id)
        await db.refresh(job)
    rebase(job, enabled_after)


def record_for_try(user: User, model_name: str, usage: dict[str, Any]) -> BillingRecord | None:
    """试跑那一条也是真请求，用量照样计费。没有用量时不记。"""
    from .inference import cached_tokens_of

    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    if prompt <= 0 and completion <= 0:
        return None
    return BillingRecord(
        request_id=f"{REQUEST_PREFIX}-try-{uuid.uuid4().hex}",
        user_id=user.id,
        job_id=None,
        key_encrypted=user.llm_token_encrypted,
        model_name=model_name,
        prompt_tokens=prompt,
        completion_tokens=completion,
        cached_tokens=min(cached_tokens_of(usage), prompt),
        note="批量推理试跑",
        status=BillingStatus.pending,
    )


# --------------------------------------------------------------------------- #
# 发送
# --------------------------------------------------------------------------- #
@dataclass
class Outcome:
    ok: bool
    retriable: bool = False
    code: str | None = None
    message: str | None = None
    data: dict | None = None


async def post_billing(client: httpx.AsyncClient, url: str, token: str, payload: dict) -> Outcome:
    """调一次计费接口。按 code 判断结果，不看 message 的文字。"""
    try:
        resp = await client.post(url, json=payload, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        return Outcome(False, retriable=True, code="network", message=f"无法连接计费接口：{exc}")

    try:
        body = resp.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return Outcome(
            False, retriable=resp.status_code >= 500 or resp.status_code == 429,
            code=f"http_{resp.status_code}",
            message=f"计费接口返回 HTTP {resp.status_code}：{resp.text[:200]}",
        )

    if resp.status_code < 300 and body.get("success") is True:
        data = body.get("data")
        return Outcome(True, data=data if isinstance(data, dict) else {})

    code = body.get("code")
    code = str(code) if code not in (None, "", False, True) else f"http_{resp.status_code}"
    message = str(body.get("message") or "")[:500] or None
    retriable = code in RETRIABLE_CODES or resp.status_code >= 500 or resp.status_code == 429
    return Outcome(False, retriable=retriable, code=code, message=message)


def build_payload(record: BillingRecord, key: str) -> dict:
    payload: dict[str, Any] = {
        "key": key,
        "request_id": record.request_id,
        "model_name": record.model_name,
        "prompt_tokens": record.prompt_tokens,
        "completion_tokens": record.completion_tokens,
    }
    if record.cached_tokens:
        payload["cached_tokens"] = record.cached_tokens
    if record.note:
        payload["note"] = record.note
    return payload


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(10 * 2 ** max(0, attempts - 1), MAX_BACKOFF_SECONDS))


async def _claim(record_id: int, statuses: set[BillingStatus]) -> bool:
    """占住一条记录去发送。别的 worker 正在发、或还没到重试时间的，返回 False。"""
    now = _now()
    async with session_scope() as db:
        result = await db.execute(
            update(BillingRecord)
            .where(
                BillingRecord.id == record_id,
                BillingRecord.status.in_(list(statuses)),
                or_(BillingRecord.next_attempt_at.is_(None), BillingRecord.next_attempt_at <= now),
            )
            .values(status=BillingStatus.pending, next_attempt_at=now + timedelta(seconds=IN_FLIGHT_SECONDS))
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1


async def send(
    client: httpx.AsyncClient, record_id: int, runtime: dict, *, include_failed: bool = False,
) -> BillingRecord | None:
    """发送一条计费记录并保存结果。没认领到（别人在发 / 未到时间）时返回 None。"""
    statuses = {BillingStatus.pending}
    if include_failed:
        statuses.add(BillingStatus.failed)
    if not await _claim(record_id, statuses):
        return None

    url, _ = billing_endpoint(runtime)
    async with session_scope() as db:
        record = await db.get(BillingRecord, record_id)
        if record is None:
            return None
        key = decrypt_secret(record.key_encrypted)
        token = billing_auth(runtime, key or "")
        if not url or (not token and key):
            outcome = Outcome(False, retriable=True, code="not_configured", message="计费接口未配置")
        elif not key:
            outcome = Outcome(
                False, code="missing_key",
                message="找不到可用的 batch Key —— 可能已被清除，或平台 SECRET_KEY 已变更",
            )
        else:
            outcome = await post_billing(client, url, token, build_payload(record, key))

        record.attempts += 1
        if outcome.ok:
            data = outcome.data or {}
            record.status = BillingStatus.succeeded
            record.quota = _int_or_none(data.get("quota"))
            record.billing_source = str(data.get("billing_source") or "")[:32] or None
            record.duplicate = bool(data.get("duplicate"))
            record.billed_at = _now()
            record.next_attempt_at = None
            record.error_code = None
            record.error = None
        elif outcome.retriable:
            record.status = BillingStatus.pending
            record.next_attempt_at = _now() + _backoff(record.attempts)
            record.error_code = outcome.code
            record.error = outcome.message
        else:
            record.status = BillingStatus.failed
            record.next_attempt_at = None
            record.error_code = outcome.code
            record.error = outcome.message
            log.warning(
                "计费记录 %s 被拒绝：%s %s", record.request_id, outcome.code, outcome.message,
            )
        await db.commit()
        await db.refresh(record)
        db.expunge(record)
        return record


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=BILLING_TIMEOUT)


async def send_record(record_id: int, *, include_failed: bool = False) -> BillingRecord | None:
    async with session_scope() as db:
        runtime = await read_runtime(db)
    async with _client() as client:
        return await send(client, record_id, runtime, include_failed=include_failed)


# --------------------------------------------------------------------------- #
# 限制 API Key 添加
# --------------------------------------------------------------------------- #
# 这些错误说明 Key 本身不能用于批量计费
_KEY_REJECT_CODES = {"invalid_key", "key_unavailable", "key_not_batch", "user_disabled", "batch_group_unavailable"}
KEY_CHECK_MODEL = "batch-key-check"


async def verify_batch_key(runtime: dict, key: str, info: KeyInfo | None) -> None:
    """确认用户新填的 Key 属于 batch 分组，不是就抛 GatewayError。

    Key 用量查询返回了 group 就直接看 group；没返回就向计费接口发一笔 0 元计费试探：
    非 batch 的 Key 会被拒（key_not_batch 等），batch Key 扣 0 元、不影响余额。
    结果说不清的一律不放行 —— 宁可让用户找管理员，也不能把非 batch 的 Key 放进来。
    """
    if info is not None and info.group:
        if info.group.strip().lower() != BATCH_GROUP:
            raise GatewayError(f"{NOT_BATCH_MESSAGE}（当前 Key 属于「{info.group}」分组）")
        return

    url, _ = billing_endpoint(runtime)
    token = billing_auth(runtime, key)
    if not url or not token:
        raise GatewayError("计费接口未配置，无法校验 Key 分组，请联系管理员")
    payload = {
        "key": key,
        "request_id": f"{REQUEST_PREFIX}-keycheck-{uuid.uuid4().hex}",
        "model_name": KEY_CHECK_MODEL,
        "amount": "0",
        "note": "批量推理平台校验 Key 分组（0 元，不扣费）",
    }
    async with _client() as client:
        outcome = await post_billing(client, url, token, payload)
    if outcome.ok:
        return
    if outcome.code == "key_not_batch":
        raise GatewayError(NOT_BATCH_MESSAGE)
    if outcome.code in _KEY_REJECT_CODES:
        raise GatewayError(describe(outcome.code, outcome.message))
    if outcome.code == "model_not_allowed":
        # 过了分组检查，只是这个 Key 限定了模型、不含校验用的模型名 —— Key 本身没问题
        return
    if outcome.retriable:
        raise GatewayError(f"暂时无法校验 Key 分组（{outcome.message or outcome.code}），请稍后重试")
    raise GatewayError(
        f"无法确认 Key 是否属于 batch 分组：计费接口返回 {outcome.code}"
        f"{'：' + outcome.message if outcome.message else ''}，请联系管理员"
    )


# --------------------------------------------------------------------------- #
# 按任务结算
# --------------------------------------------------------------------------- #
@dataclass
class SettleResult:
    sent: int = 0
    # 需要停下任务的原因（已转成给用户看的话）；None 表示可以继续跑
    problem: str | None = None


async def rekey_failed(db: AsyncSession, job_id: str, user: User) -> int:
    """用户换了 Key 后，把因 Key 问题失败的记录改挂到新 Key 上。返回改了几条。"""
    if not user.llm_token_encrypted:
        return 0
    current = decrypt_secret(user.llm_token_encrypted)
    rows = (
        await db.execute(
            select(BillingRecord).where(
                BillingRecord.job_id == job_id,
                BillingRecord.status == BillingStatus.failed,
                BillingRecord.error_code.in_(KEY_CODES | {"missing_key"}),
            )
        )
    ).scalars().all()
    changed = 0
    for record in rows:
        if decrypt_secret(record.key_encrypted) != current:
            record.key_encrypted = user.llm_token_encrypted
            changed += 1
    if changed:
        await db.commit()
    return changed


async def settle_job(job_id: str, *, include_failed: bool = False) -> SettleResult:
    """把任务所有没扣上的记录发一遍。

    include_failed=True 时连被拒绝过的也重发（任务恢复、用户点重试时用）。
    遇到被拒绝的就停：后面的多半是同样的原因，没必要接着撞。
    """
    out = SettleResult()
    statuses = [BillingStatus.pending]
    if include_failed:
        statuses.append(BillingStatus.failed)
    async with session_scope() as db:
        runtime = await read_runtime(db)
        ids = (
            await db.execute(
                select(BillingRecord.id)
                .where(BillingRecord.job_id == job_id, BillingRecord.status.in_(statuses))
                .order_by(BillingRecord.id)
            )
        ).scalars().all()
    if not ids:
        return out

    async with _client() as client:
        for record_id in ids:
            record = await send(client, record_id, runtime, include_failed=include_failed)
            if record is None:
                continue
            out.sent += 1
            if record.status == BillingStatus.failed:
                out.problem = "计费失败：" + describe(record.error_code, record.error, record.model_name)
                return out

    # 临时错误一直不好，也得停下来，免得没扣费的用量越堆越多
    async with session_scope() as db:
        stuck = (
            await db.execute(
                select(BillingRecord)
                .where(
                    BillingRecord.job_id == job_id,
                    BillingRecord.status == BillingStatus.pending,
                    BillingRecord.attempts >= SOFT_FAIL_LIMIT,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
    if stuck is not None:
        out.problem = (
            f"计费接口连续 {stuck.attempts} 次没有扣费成功（{stuck.error or stuck.error_code}），"
            "已暂停任务，请稍后点「继续」重试"
        )
    return out


async def bill_job(job_id: str) -> SettleResult:
    """运行中的任务定期调用：切出新增用量，再把待扣的都发出去。"""
    await cut_job(job_id)
    return await settle_job(job_id)


# --------------------------------------------------------------------------- #
# worker 后台巡检
# --------------------------------------------------------------------------- #
async def sweep(limit: int = 200) -> dict:
    """补切没在运行的任务的差额（worker 崩溃、API 直接取消等），并重发到期的待扣记录。"""
    cut_count = sent = 0
    async with session_scope() as db:
        runtime = await read_runtime(db)
        orphan_ids = (
            await db.execute(
                select(Job.id)
                .where(
                    Job.billing_enabled.is_(True),
                    Job.status != JobStatus.running,
                    or_(
                        Job.prompt_tokens > Job.billed_prompt_tokens,
                        Job.completion_tokens > Job.billed_completion_tokens,
                    ),
                )
                .limit(limit)
            )
        ).scalars().all()
    for job_id in orphan_ids:
        if await cut_job(job_id):
            cut_count += 1

    now = _now()
    async with session_scope() as db:
        due = (
            await db.execute(
                select(BillingRecord.id)
                .where(
                    BillingRecord.status == BillingStatus.pending,
                    or_(BillingRecord.next_attempt_at.is_(None), BillingRecord.next_attempt_at <= now),
                )
                .order_by(BillingRecord.id)
                .limit(limit)
            )
        ).scalars().all()
    if due:
        async with _client() as client:
            for record_id in due:
                if await send(client, record_id, runtime):
                    sent += 1
    return {"cut": cut_count, "sent": sent}


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #
async def job_summary(db: AsyncSession, job: Job) -> dict:
    """任务详情页的计费小结。"""
    rows = (
        await db.execute(
            select(
                BillingRecord.status,
                func.count(BillingRecord.id),
                func.coalesce(func.sum(BillingRecord.prompt_tokens), 0),
                func.coalesce(func.sum(BillingRecord.completion_tokens), 0),
                func.coalesce(func.sum(BillingRecord.quota), 0),
            )
            .where(BillingRecord.job_id == job.id)
            .group_by(BillingRecord.status)
        )
    ).all()
    by_status = {
        (s.value if isinstance(s, BillingStatus) else str(s)): {
            "count": int(c), "prompt_tokens": int(p), "completion_tokens": int(ct), "quota": int(q),
        }
        for s, c, p, ct, q in rows
    }
    empty = {"count": 0, "prompt_tokens": 0, "completion_tokens": 0, "quota": 0}
    return {
        "enabled": job.billing_enabled,
        "personal": job.model_source == ModelSource.personal,
        "succeeded": by_status.get("succeeded", empty),
        "pending": by_status.get("pending", empty),
        "failed": by_status.get("failed", empty),
        # 还没切进记录的用量（运行中的任务下一轮会切）
        "unbilled_prompt_tokens": max(0, job.prompt_tokens - job.billed_prompt_tokens) if job.billing_enabled else 0,
        "unbilled_completion_tokens": (
            max(0, job.completion_tokens - job.billed_completion_tokens) if job.billing_enabled else 0
        ),
    }


def interval_of(runtime: dict) -> float:
    return float(max(MIN_INTERVAL, int(runtime.get("user_gateway_billing_interval") or 60)))
