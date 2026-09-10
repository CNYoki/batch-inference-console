"""FastAPI 依赖：当前用户、管理员校验。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..models import ApiToken, User, UserRole
from .security import API_TOKEN_PREFIX, decode_session_token, hash_api_token

# last_used_at 只是给人看的，没必要每个请求都写一次库
_TOUCH_INTERVAL = timedelta(minutes=1)


def _aware(dt: datetime | None) -> datetime | None:
    # SQLite 读回来的是 naive datetime，统一按 UTC 处理再比较
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


async def _user_id_from_api_token(db: AsyncSession, token: str) -> str:
    row = (
        await db.execute(select(ApiToken).where(ApiToken.token_hash == hash_api_token(token)))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "API Token 无效或已被吊销")

    now = datetime.now(UTC)
    expires_at = _aware(row.expires_at)
    if expires_at is not None and expires_at <= now:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "API Token 已过期，请到「个人设置」重新生成")

    last_used = _aware(row.last_used_at)
    if last_used is None or now - last_used > _TOUCH_INTERVAL:
        row.last_used_at = now
        await db.commit()
    return row.user_id


async def get_current_user(
    request: Request, db: Annotated[AsyncSession, Depends(get_db)]
) -> User:
    request.state.via_api_token = False
    user_id: str | None = None

    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        # 同时接受 Authorization: Bearer，便于脚本/CI 调用
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录")

    if token.startswith(API_TOKEN_PREFIX):
        user_id = await _user_id_from_api_token(db, token)
        request.state.via_api_token = True
    else:
        payload = decode_session_token(token)
        if not payload:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "会话已过期，请重新登录")
        user_id = payload.get("sub", "")

    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "账号已被禁用")
    return user


async def get_session_user(
    request: Request, user: Annotated[User, Depends(get_current_user)]
) -> User:
    """只接受浏览器会话。

    用 token 签发新 token，等于一个泄露的 token 可以给自己续命、在被吊销前留后门，
    所以 token 的管理只能在登录后的网页上做。
    """
    if request.state.via_api_token:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "API Token 不能用来管理 API Token，请在网页上操作")
    return user


async def get_current_admin(
    user: Annotated[User, Depends(get_current_user)],
) -> User:
    if user.role != UserRole.admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "需要管理员权限")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
SessionUser = Annotated[User, Depends(get_session_user)]
CurrentAdmin = Annotated[User, Depends(get_current_admin)]
DB = Annotated[AsyncSession, Depends(get_db)]
