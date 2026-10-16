"""项目：把同一件事的任务和 Prompt 归到一起。每个用户只看得到自己的项目。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from ..core.deps import DB, CurrentUser
from ..models import Job, JobStatus, Project, User, UserPrompt
from ..schemas import ProjectCreate, ProjectOut, ProjectUpdate

router = APIRouter(prefix="/projects", tags=["projects"])

_ACTIVE = [JobStatus.pending, JobStatus.queued, JobStatus.running]


async def _get_own_or_404(db: DB, user: User, project_id: str) -> Project:
    project = await db.get(Project, project_id)
    # 别人的项目也回 404，不暴露它是否存在
    if project is None or project.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "项目不存在")
    return project


async def _commit_or_409(db: DB, name: str) -> None:
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, f"已经有名为「{name}」的项目了") from exc


async def _with_counts(db: DB, projects: list[Project]) -> list[ProjectOut]:
    ids = [p.id for p in projects]
    jobs: dict[str, int] = {}
    active: dict[str, int] = {}
    prompts: dict[str, int] = {}
    if ids:
        jobs = dict((await db.execute(
            select(Job.project_id, func.count(Job.id)).where(Job.project_id.in_(ids)).group_by(Job.project_id)
        )).all())
        active = dict((await db.execute(
            select(Job.project_id, func.count(Job.id))
            .where(Job.project_id.in_(ids), Job.status.in_(_ACTIVE))
            .group_by(Job.project_id)
        )).all())
        prompts = dict((await db.execute(
            select(UserPrompt.project_id, func.count(UserPrompt.id))
            .where(UserPrompt.project_id.in_(ids))
            .group_by(UserPrompt.project_id)
        )).all())
    return [
        ProjectOut(
            id=p.id, name=p.name, description=p.description,
            job_count=jobs.get(p.id, 0), active_job_count=active.get(p.id, 0),
            prompt_count=prompts.get(p.id, 0),
            created_at=p.created_at, updated_at=p.updated_at,
        )
        for p in projects
    ]


@router.get("", response_model=list[ProjectOut])
async def list_projects(user: CurrentUser, db: DB) -> list[ProjectOut]:
    rows = (
        await db.execute(
            select(Project).where(Project.user_id == user.id).order_by(Project.created_at.desc())
        )
    ).scalars().all()
    return await _with_counts(db, list(rows))


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(payload: ProjectCreate, user: CurrentUser, db: DB) -> ProjectOut:
    project = Project(user_id=user.id, name=payload.name, description=payload.description or None)
    db.add(project)
    await _commit_or_409(db, payload.name)
    await db.refresh(project)
    return (await _with_counts(db, [project]))[0]


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(project_id: str, user: CurrentUser, db: DB) -> ProjectOut:
    return (await _with_counts(db, [await _get_own_or_404(db, user, project_id)]))[0]


@router.patch("/{project_id}", response_model=ProjectOut)
async def update_project(project_id: str, payload: ProjectUpdate, user: CurrentUser, db: DB) -> ProjectOut:
    project = await _get_own_or_404(db, user, project_id)
    data = payload.model_dump(exclude_unset=True)
    if data.get("name"):
        project.name = data["name"]
    if "description" in data:
        project.description = data["description"] or None
    await _commit_or_409(db, project.name)
    await db.refresh(project)
    return (await _with_counts(db, [project]))[0]


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(project_id: str, user: CurrentUser, db: DB) -> None:
    """删项目不删内容：里面的任务和 Prompt 回到「未归类」。"""
    project = await _get_own_or_404(db, user, project_id)
    # 外键虽然是 SET NULL，但不依赖数据库替我们做（SQLite 外键可能没开）
    await db.execute(update(Job).where(Job.project_id == project.id).values(project_id=None))
    await db.execute(
        update(UserPrompt).where(UserPrompt.project_id == project.id).values(project_id=None)
    )
    await db.delete(project)
    await db.commit()
