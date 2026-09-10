"""我的 Prompt：每个用户自己维护的 Prompt 模板与数据变量，只对本人可见。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..core.deps import DB, CurrentUser
from ..models import User, UserPrompt
from ..schemas import PromptCreate, PromptOut, PromptUpdate

router = APIRouter(prefix="/prompts", tags=["prompts"])

# 这几个字段在库里不可空，PATCH 传 null 当作「不修改」
_NOT_NULL = {"name", "prompt_template", "variables"}


async def _get_own_or_404(db: DB, user: User, prompt_id: str) -> UserPrompt:
    prompt = await db.get(UserPrompt, prompt_id)
    # 别人的 Prompt 也回 404，不暴露它是否存在
    if prompt is None or prompt.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Prompt 不存在")
    return prompt


async def _commit_or_409(db: DB, name: str) -> None:
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, f"已经有名为「{name}」的 Prompt 了") from exc


@router.get("", response_model=list[PromptOut])
async def list_prompts(user: CurrentUser, db: DB) -> list[UserPrompt]:
    stmt = (
        select(UserPrompt)
        .where(UserPrompt.user_id == user.id)
        .order_by(UserPrompt.updated_at.desc(), UserPrompt.name)
    )
    return list((await db.execute(stmt)).scalars().all())


@router.post("", response_model=PromptOut, status_code=status.HTTP_201_CREATED)
async def create_prompt(payload: PromptCreate, user: CurrentUser, db: DB) -> UserPrompt:
    prompt = UserPrompt(user_id=user.id, **payload.model_dump())
    db.add(prompt)
    await _commit_or_409(db, payload.name)
    await db.refresh(prompt)
    return prompt


@router.get("/{prompt_id}", response_model=PromptOut)
async def get_prompt(prompt_id: str, user: CurrentUser, db: DB) -> UserPrompt:
    return await _get_own_or_404(db, user, prompt_id)


@router.patch("/{prompt_id}", response_model=PromptOut)
async def update_prompt(
    prompt_id: str, payload: PromptUpdate, user: CurrentUser, db: DB
) -> UserPrompt:
    prompt = await _get_own_or_404(db, user, prompt_id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        if value is None and key in _NOT_NULL:
            continue
        setattr(prompt, key, value)
    await _commit_or_409(db, prompt.name)
    await db.refresh(prompt)
    return prompt


@router.delete("/{prompt_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_prompt(prompt_id: str, user: CurrentUser, db: DB) -> None:
    prompt = await _get_own_or_404(db, user, prompt_id)
    await db.delete(prompt)
    await db.commit()
