"""项目的共享规则：每个用户都有一个默认项目。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Project

DEFAULT_PROJECT_NAME = "默认项目"
DEFAULT_PROJECT_DESCRIPTION = "没有指定项目的任务都放在这里"


async def ensure_default_project(db: AsyncSession, user_id: str) -> Project:
    """取用户的默认项目，没有就建一个（只 flush，由调用方提交）。

    老用户由迁移补齐，新用户在第一次用到时建 —— 用户的创建入口有好几个（本地、OIDC、后台），
    放在这里懒建就不用每处都记着。请在改动其他数据之前调用：撞上并发建的唯一约束时会回滚会话。
    """
    project = (
        await db.execute(select(Project).where(Project.user_id == user_id, Project.is_default.is_(True)))
    ).scalars().first()
    if project is not None:
        return project

    # 用户自己建过同名项目，就直接把它当默认项目
    project = (
        await db.execute(select(Project).where(Project.user_id == user_id, Project.name == DEFAULT_PROJECT_NAME))
    ).scalar_one_or_none()
    if project is not None:
        project.is_default = True
        await db.flush()
        return project

    project = Project(
        user_id=user_id, name=DEFAULT_PROJECT_NAME, description=DEFAULT_PROJECT_DESCRIPTION, is_default=True,
    )
    db.add(project)
    try:
        await db.flush()
    except IntegrityError:
        # 另一个请求刚建好
        await db.rollback()
        return (
            await db.execute(select(Project).where(Project.user_id == user_id, Project.is_default.is_(True)))
        ).scalars().one()
    return project
