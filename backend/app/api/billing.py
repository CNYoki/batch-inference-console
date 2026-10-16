"""个人网关计费：管理员查看计费记录、手动重试、测试推理/计费接口。"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from ..core.deps import DB, CurrentAdmin
from ..core.security import decrypt_secret
from ..models import AuditLog, BillingRecord, BillingStatus, Job, User
from ..schemas import BillingRecordListOut, BillingRecordOut, GatewayTestResult
from ..services import billing
from ..services.gateway import GatewayError, billing_auth, billing_endpoint, fetch_models, inference_endpoint
from ..services.settings_store import read_runtime

router = APIRouter(prefix="/admin/billing", tags=["billing"])


def record_out(record: BillingRecord, username: str | None = None, job_name: str | None = None) -> BillingRecordOut:
    out = BillingRecordOut.model_validate(record)
    out.username = username
    out.job_name = job_name
    if record.status != BillingStatus.succeeded and record.error_code:
        out.error_hint = billing.describe(record.error_code, record.error, record.model_name)
    return out


@router.get("/records", response_model=BillingRecordListOut)
async def list_records(
    _: CurrentAdmin,
    db: DB,
    status_: str | None = Query(None, alias="status"),
    username: str | None = None,
    job_id: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
) -> BillingRecordListOut:
    stmt = (
        select(BillingRecord, User.username, Job.name)
        .join(User, User.id == BillingRecord.user_id)
        .outerjoin(Job, Job.id == BillingRecord.job_id)
    )
    if status_:
        try:
            stmt = stmt.where(BillingRecord.status == BillingStatus(status_))
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "未知的状态") from exc
    if username:
        stmt = stmt.where(User.username.contains(username.strip()))
    if job_id:
        stmt = stmt.where(BillingRecord.job_id == job_id.strip())

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(BillingRecord.id.desc()).offset((page - 1) * page_size).limit(page_size)
        )
    ).all()
    counts = dict(
        (await db.execute(select(BillingRecord.status, func.count(BillingRecord.id)).group_by(BillingRecord.status))).all()
    )
    return BillingRecordListOut(
        total=total,
        items=[record_out(r, u, n) for r, u, n in rows],
        counts={(k.value if isinstance(k, BillingStatus) else str(k)): v for k, v in counts.items()},
    )


@router.post("/records/{record_id}/retry", response_model=BillingRecordOut)
async def retry_record(record_id: int, admin: CurrentAdmin, db: DB) -> BillingRecordOut:
    """立即重发一笔（含被拒绝过的）。同一个 request_id，网关不会重复扣。"""
    record = await db.get(BillingRecord, record_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "计费记录不存在")
    if record.status == BillingStatus.succeeded:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "这笔已经扣费成功了")

    db.add(AuditLog(
        user_id=admin.id, username=admin.username, action="billing.retry",
        target=record.request_id, detail={"status": record.status.value},
    ))
    await db.commit()

    sent = await billing.send_record(record_id, include_failed=True)
    if sent is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "这笔正在发送中，请稍后刷新")
    user = await db.get(User, sent.user_id)
    job = await db.get(Job, sent.job_id) if sent.job_id else None
    return record_out(sent, user.username if user else None, job.name if job else None)


@router.post("/test", response_model=GatewayTestResult)
async def test_gateway(admin: CurrentAdmin, db: DB) -> GatewayTestResult:
    """用已保存的配置测试推理接口和计费接口。

    计费接口发一个空请求体：令牌有效时网关回 400 invalid_request（参数缺失），
    不会真的扣费；令牌无效或权限不够时回 401/403。
    """
    runtime = await read_runtime(db)
    # 透传用户 Key 时拿管理员自己保存的 Key 来测
    own_key = decrypt_secret(admin.llm_token_encrypted) or ""

    base, key = inference_endpoint({**runtime, "user_gateway_billing_enabled": True}, own_key)
    if not base:
        inference_ok, inference_detail = False, "未填写推理接口地址"
    elif not key and runtime.get("user_gateway_inference_key_passthrough"):
        inference_ok, inference_detail = False, "推理接口设为透传用户 Key，请先在「新建任务」页保存你自己的 Key 再测试"
    else:
        try:
            models = await fetch_models(base, key, platform=True)
        except GatewayError as exc:
            inference_ok, inference_detail = False, exc.message
        else:
            preview = "、".join(models[:5]) + ("……" if len(models) > 5 else "")
            inference_ok, inference_detail = True, f"连接正常，共 {len(models)} 个模型：{preview}"

    url, _ = billing_endpoint(runtime)
    token = billing_auth(runtime, own_key)
    if not url:
        billing_ok, billing_detail = False, "未填写计费接口地址"
    elif not token:
        billing_ok, billing_detail = False, (
            "计费接口设为透传用户 Key，请先在「新建任务」页保存你自己的 Key 再测试"
            if runtime.get("user_gateway_billing_key_passthrough") else "未填写计费接口访问令牌"
        )
    else:
        async with httpx.AsyncClient(timeout=billing.BILLING_TIMEOUT) as client:
            outcome = await billing.post_billing(client, url, token, {})
        if outcome.code in {"invalid_request", "http_400"}:
            billing_ok, billing_detail = True, "令牌有效，计费接口可用（测试请求不会扣费）"
        elif outcome.ok:
            # 不该发生：空请求体也扣费成功了
            billing_ok, billing_detail = True, "计费接口返回成功"
        else:
            billing_ok = False
            billing_detail = billing.describe(outcome.code, outcome.message)
    return GatewayTestResult(
        inference_ok=inference_ok, inference_detail=inference_detail,
        billing_ok=billing_ok, billing_detail=billing_detail,
    )
