"""个人 API Token：给命令行和 Claude Code Skills 用的长期凭证。

只能在浏览器会话里签发与吊销（见 deps.get_session_user），明文只在创建时返回一次。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import func, select

from ..core.deps import DB, SessionUser
from ..core.security import generate_api_token, hash_api_token
from ..models import ApiToken, AuditLog, User
from ..schemas import ApiTokenCreate, ApiTokenCreated, ApiTokenOut

router = APIRouter(prefix="/auth/tokens", tags=["auth"])

MAX_TOKENS_PER_USER = 20
# 列表里展示的明文前缀长度：bic_ 加 4 位随机字符，够辨认、不够猜
PREFIX_LEN = 8


def _audit(db: DB, user: User, action: str, request: Request, detail: dict) -> None:
    db.add(
        AuditLog(
            user_id=user.id,
            username=user.username,
            action=action,
            ip=request.client.host if request.client else None,
            detail=detail,
        )
    )


@router.get("", response_model=list[ApiTokenOut])
async def list_tokens(user: SessionUser, db: DB) -> list[ApiToken]:
    stmt = select(ApiToken).where(ApiToken.user_id == user.id).order_by(ApiToken.created_at.desc())
    return list((await db.execute(stmt)).scalars().all())


@router.post("", response_model=ApiTokenCreated, status_code=status.HTTP_201_CREATED)
async def create_token(payload: ApiTokenCreate, request: Request, user: SessionUser, db: DB) -> ApiTokenCreated:
    count = (
        await db.execute(select(func.count(ApiToken.id)).where(ApiToken.user_id == user.id))
    ).scalar_one()
    if count >= MAX_TOKENS_PER_USER:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"最多保留 {MAX_TOKENS_PER_USER} 个 token，请先删除不用的"
        )

    plain = generate_api_token()
    expires_at = (
        datetime.now(UTC) + timedelta(days=payload.expires_in_days) if payload.expires_in_days else None
    )
    token = ApiToken(
        user_id=user.id,
        name=payload.name,
        token_hash=hash_api_token(plain),
        token_prefix=plain[:PREFIX_LEN],
        expires_at=expires_at,
    )
    db.add(token)
    _audit(db, user, "api_token_create", request, {"name": payload.name, "prefix": token.token_prefix})
    await db.commit()
    await db.refresh(token)
    return ApiTokenCreated(**ApiTokenOut.model_validate(token).model_dump(), token=plain)


@router.delete("/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_token(token_id: str, request: Request, user: SessionUser, db: DB) -> None:
    token = await db.get(ApiToken, token_id)
    if token is None or token.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Token 不存在")
    _audit(db, user, "api_token_delete", request, {"name": token.name, "prefix": token.token_prefix})
    await db.delete(token)
    await db.commit()
