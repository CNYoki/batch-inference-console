"""用户相关的共享规则。"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import User, UserRole


async def would_orphan_admin(db: AsyncSession, target: User) -> bool:
    """把 target 降权或停用后，系统是否会一个启用的管理员都不剩。

    本地用户管理和 OIDC 角色同步都要用它兜底 —— 没有管理员就没人能进后台，
    只能改数据库才能救回来。
    """
    if target.role != UserRole.admin:
        return False
    remaining = (
        await db.execute(
            select(func.count(User.id)).where(
                User.role == UserRole.admin, User.is_active.is_(True), User.id != target.id
            )
        )
    ).scalar_one()
    return remaining == 0
