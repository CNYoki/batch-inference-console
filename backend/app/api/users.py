"""用户管理（管理员）。OIDC 账号在首次登录时自动创建，此处主要管本地账号与角色。"""
from __future__ import annotations

import contextlib
import logging

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from ..config import settings
from ..core.deps import DB, CurrentAdmin
from ..core.security import hash_password
from ..models import AuthSource, Job, JobStatus, User, UserRole
from ..schemas import UserCreate, UserOut, UserUpdate
from ..services.users import would_orphan_admin

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/users", tags=["admin"])


@router.get("", response_model=dict)
async def list_users(
    _: CurrentAdmin,
    db: DB,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    keyword: str | None = None,
) -> dict:
    stmt = select(User)
    count_stmt = select(func.count(User.id))
    if keyword:
        like = f"%{keyword}%"
        cond = or_(User.username.like(like), User.email.like(like), User.display_name.like(like))
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    total = (await db.execute(count_stmt)).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(User.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars().all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [UserOut.model_validate(u) for u in rows],
    }


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(payload: UserCreate, _: CurrentAdmin, db: DB) -> User:
    user = User(
        username=payload.username,
        email=payload.email,
        display_name=payload.display_name or payload.username,
        role=UserRole(payload.role),
        auth_source=AuthSource.local,
        password_hash=hash_password(payload.password),
        max_concurrent_jobs=payload.max_concurrent_jobs,
        max_storage_mb=payload.max_storage_mb,
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, f"用户名 {payload.username} 已存在") from exc
    await db.refresh(user)
    return user


async def _get_or_404(db: DB, user_id: str) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    return user


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(user_id: str, payload: UserUpdate, admin: CurrentAdmin, db: DB) -> User:
    user = await _get_or_404(db, user_id)
    data = payload.model_dump(exclude_unset=True)

    demoting = (data.get("role") and data["role"] != UserRole.admin.value) or data.get("is_active") is False
    if demoting and await would_orphan_admin(db, user):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "系统必须保留至少一个启用状态的管理员")

    if (password := data.pop("password", None)) is not None:
        if user.auth_source != AuthSource.local:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "统一登录账号不能在此设置密码")
        user.password_hash = hash_password(password)

    if (role := data.pop("role", None)) is not None:
        user.role = UserRole(role)

    for key, value in data.items():
        if value is not None:
            setattr(user, key, value)

    await db.commit()
    await db.refresh(user)
    return user


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(user_id: str, admin: CurrentAdmin, db: DB) -> None:
    user = await _get_or_404(db, user_id)
    if user.id == admin.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能删除自己")
    if await would_orphan_admin(db, user):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "系统必须保留至少一个启用状态的管理员")

    active = (
        await db.execute(
            select(func.count(Job.id)).where(
                Job.user_id == user.id, Job.status.in_([JobStatus.queued, JobStatus.running])
            )
        )
    ).scalar_one()
    if active:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"该用户还有 {active} 个进行中的任务")

    # 顺手清掉他还没提交成任务的暂存上传 —— 否则会变成没人认领的孤儿文件，
    # 要等 stale_upload_hours 那一轮才会被清理
    removed = 0
    for path in settings.upload_dir.glob(f"{user.id}__*.jsonl"):
        with contextlib.suppress(OSError):
            path.unlink()
            removed += 1
    if removed:
        log.info("删除用户 %s 时清理了 %d 个暂存上传", user.username, removed)

    await db.delete(user)
    await db.commit()
