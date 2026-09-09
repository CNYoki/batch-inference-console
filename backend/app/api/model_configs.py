"""模型配置：管理员维护端点/密钥/开关，普通用户只读精简列表。"""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..core.deps import DB, CurrentAdmin, CurrentUser
from ..core.security import decrypt_secret, encrypt_secret, mask_secret
from ..models import ModelConfig, User, UserRole
from ..schemas import (
    ModelConfigCreate,
    ModelConfigOut,
    ModelConfigUpdate,
    ModelOption,
    ModelOptionsOut,
    PersonalModelsOut,
    PersonalTokenIn,
    ProbeResult,
)
from ..services.gateway import GatewayError, fetch_models
from ..services.inference import InferenceClient, InferenceError
from ..services.settings_store import read_runtime

router = APIRouter(tags=["models"])


def _to_out(mc: ModelConfig) -> ModelConfigOut:
    data = ModelConfigOut.model_validate(mc)
    data.api_key_masked = mask_secret(decrypt_secret(mc.api_key_encrypted))
    return data


# --------------------------------------------------------------------------- #
# 普通用户：可选模型列表
# --------------------------------------------------------------------------- #
@router.get("/models", response_model=list[ModelOption])
async def list_available_models(user: CurrentUser, db: DB) -> list[ModelOption]:
    """只列公用模型。新建任务页请用 /models/options。"""
    return await _shared_models(user, db)


async def _shared_models(user: User, db: DB) -> list[ModelOption]:
    stmt = select(ModelConfig).where(ModelConfig.enabled.is_(True))
    if user.role != UserRole.admin:
        stmt = stmt.where(ModelConfig.admin_only.is_(False))
    stmt = stmt.order_by(ModelConfig.sort_order, ModelConfig.display_name)
    rows = (await db.execute(stmt)).scalars().all()
    return [ModelOption.model_validate(mc, from_attributes=True) for mc in rows]


@router.get("/models/options", response_model=ModelOptionsOut)
async def list_model_options(user: CurrentUser, db: DB) -> ModelOptionsOut:
    """新建任务页要用的全部可选模型。

    公用模型直接读库；个人模型需要用户的网关 token —— 已保存过就顺带拉一次，
    拉取失败不算错误，把原因带回前端让用户重填 token 即可。
    """
    runtime = await read_runtime(db)
    out = ModelOptionsOut(
        shared=await _shared_models(user, db),
        gateway_enabled=bool(runtime["user_gateway_enabled"]),
        gateway_label=runtime["user_gateway_label"],
        gateway_base_url=runtime["user_gateway_base_url"],
        has_saved_token=bool(user.llm_token_encrypted),
    )

    if not out.gateway_enabled or not user.llm_token_encrypted:
        return out

    token = decrypt_secret(user.llm_token_encrypted)
    if token is None:
        out.personal_error = "已保存的 token 无法解密（SECRET_KEY 可能已变更），请重新填写"
        out.has_saved_token = False
        return out

    try:
        out.personal = await fetch_models(runtime["user_gateway_base_url"], token)
    except GatewayError as exc:
        out.personal_error = exc.message
    return out


@router.post("/models/personal", response_model=PersonalModelsOut)
async def list_personal_models(payload: PersonalTokenIn, user: CurrentUser, db: DB) -> PersonalModelsOut:
    """用用户填写的 token 拉取有权限的模型；remember=true 时加密保存下来。"""
    runtime = await read_runtime(db)
    if not runtime["user_gateway_enabled"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "管理员未开启个人网关")

    try:
        models = await fetch_models(runtime["user_gateway_base_url"], payload.token)
    except GatewayError as exc:
        # token 错、网关挂了都属于用户可自行修正的问题，用 400 而不是 502
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message) from exc

    saved = False
    if payload.remember:
        user.llm_token_encrypted = encrypt_secret(payload.token)
        user.llm_token_updated_at = datetime.now(UTC)
        await db.commit()
        saved = True

    return PersonalModelsOut(models=models, saved=saved)


@router.delete("/models/personal/token", status_code=status.HTTP_204_NO_CONTENT)
async def clear_personal_token(user: CurrentUser, db: DB) -> None:
    """清除已保存的 token。注意排队中的个人模型任务会因此失败。"""
    user.llm_token_encrypted = None
    user.llm_token_updated_at = None
    await db.commit()


# --------------------------------------------------------------------------- #
# 管理员：CRUD
# --------------------------------------------------------------------------- #
@router.get("/admin/models", response_model=list[ModelConfigOut])
async def admin_list_models(_: CurrentAdmin, db: DB) -> list[ModelConfigOut]:
    rows = (
        await db.execute(select(ModelConfig).order_by(ModelConfig.sort_order, ModelConfig.name))
    ).scalars().all()
    return [_to_out(mc) for mc in rows]


@router.post("/admin/models", response_model=ModelConfigOut, status_code=status.HTTP_201_CREATED)
async def admin_create_model(payload: ModelConfigCreate, _: CurrentAdmin, db: DB) -> ModelConfigOut:
    data = payload.model_dump(exclude={"api_key"})
    mc = ModelConfig(**data)
    if payload.api_key:
        mc.api_key_encrypted = encrypt_secret(payload.api_key)
    db.add(mc)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, f"模型标识 {payload.name} 已存在") from exc
    await db.refresh(mc)
    return _to_out(mc)


async def _get_or_404(db: DB, model_id: str) -> ModelConfig:
    mc = await db.get(ModelConfig, model_id)
    if mc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "模型配置不存在")
    return mc


@router.get("/admin/models/{model_id}", response_model=ModelConfigOut)
async def admin_get_model(model_id: str, _: CurrentAdmin, db: DB) -> ModelConfigOut:
    return _to_out(await _get_or_404(db, model_id))


@router.patch("/admin/models/{model_id}", response_model=ModelConfigOut)
async def admin_update_model(
    model_id: str, payload: ModelConfigUpdate, _: CurrentAdmin, db: DB
) -> ModelConfigOut:
    mc = await _get_or_404(db, model_id)
    data = payload.model_dump(exclude_unset=True)

    if "api_key" in data:
        api_key = data.pop("api_key")
        # 空字符串 = 清除密钥；None = 不改动
        if api_key == "":
            mc.api_key_encrypted = None
        elif api_key is not None:
            mc.api_key_encrypted = encrypt_secret(api_key)

    for key, value in data.items():
        if value is not None:
            setattr(mc, key, value)

    await db.commit()
    await db.refresh(mc)
    return _to_out(mc)


@router.delete("/admin/models/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_delete_model(model_id: str, _: CurrentAdmin, db: DB) -> None:
    mc = await _get_or_404(db, model_id)
    await db.delete(mc)
    await db.commit()


@router.post("/admin/models/{model_id}/probe", response_model=ProbeResult)
async def admin_probe_model(model_id: str, _: CurrentAdmin, db: DB) -> ProbeResult:
    """向端点发一条最小请求，验证 base_url / 密钥 / model 名是否正确。"""
    mc = await _get_or_404(db, model_id)
    try:
        async with InferenceClient(mc) as client:
            return ProbeResult(**await client.probe())
    except InferenceError as exc:
        return ProbeResult(ok=False, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 — 探测失败要把原因原样回显给管理员
        return ProbeResult(ok=False, detail=f"{type(exc).__name__}: {exc}")
