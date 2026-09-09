"""启动时的初始化：建表、创建初始管理员、示例模型配置。"""
from __future__ import annotations

import logging

from sqlalchemy import func, select

from .config import settings
from .core.security import hash_password
from .db import create_all, session_scope
from .models import AuthSource, User, UserRole

log = logging.getLogger(__name__)


async def init_database(auto_create_tables: bool = True) -> None:
    if auto_create_tables:
        await create_all()
    await _ensure_admin()


async def _ensure_admin() -> None:
    if not settings.bootstrap_admin_password:
        async with session_scope() as db:
            count = (await db.execute(select(func.count(User.id)))).scalar_one()
        if count == 0:
            log.warning(
                "系统中还没有任何用户。请设置 BOOTSTRAP_ADMIN_PASSWORD 后重启，"
                "或启用 OIDC —— 首个通过 OIDC 登录的用户会自动成为管理员。"
            )
        return

    async with session_scope() as db:
        existing = (
            await db.execute(select(User).where(User.username == settings.bootstrap_admin_username))
        ).scalar_one_or_none()
        if existing is not None:
            # 已存在则不覆盖密码，避免每次重启把管理员密码重置回环境变量的值
            if existing.role != UserRole.admin:
                existing.role = UserRole.admin
                log.info("已将 %s 提升为管理员", existing.username)
            return

        db.add(
            User(
                username=settings.bootstrap_admin_username,
                email=settings.bootstrap_admin_email,
                display_name="管理员",
                role=UserRole.admin,
                auth_source=AuthSource.local,
                password_hash=hash_password(settings.bootstrap_admin_password),
            )
        )
        log.info("已创建初始管理员账号: %s", settings.bootstrap_admin_username)
