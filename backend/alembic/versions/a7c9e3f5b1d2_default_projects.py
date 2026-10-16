"""default project per user

Revision ID: a7c9e3f5b1d2
Revises: f3b6c8d1e2a4
Create Date: 2026-10-16 20:00:00.000000
"""
import uuid

from alembic import op
import sqlalchemy as sa


revision = 'a7c9e3f5b1d2'
down_revision = 'f3b6c8d1e2a4'
branch_labels = None
depends_on = None

DEFAULT_NAME = "默认项目"
DEFAULT_DESCRIPTION = "没有指定项目的任务都放在这里"


def upgrade() -> None:
    with op.batch_alter_table('projects') as batch:
        batch.add_column(sa.Column('is_default', sa.Boolean(), server_default=sa.false(), nullable=False))

    # 给每个用户补一个默认项目（已有同名项目的直接把它当默认），再把未归类的任务放进去
    conn = op.get_bind()
    users = sa.table('users', sa.column('id', sa.String))
    projects = sa.table(
        'projects',
        sa.column('id', sa.String), sa.column('user_id', sa.String), sa.column('name', sa.String),
        sa.column('description', sa.String), sa.column('is_default', sa.Boolean),
    )
    jobs = sa.table('jobs', sa.column('user_id', sa.String), sa.column('project_id', sa.String))

    named = dict(conn.execute(
        sa.select(projects.c.user_id, projects.c.id).where(projects.c.name == DEFAULT_NAME)
    ).all())
    for (user_id,) in conn.execute(sa.select(users.c.id)).all():
        project_id = named.get(user_id)
        if project_id:
            conn.execute(projects.update().where(projects.c.id == project_id).values(is_default=True))
        else:
            project_id = uuid.uuid4().hex
            conn.execute(projects.insert().values(
                id=project_id, user_id=user_id, name=DEFAULT_NAME,
                description=DEFAULT_DESCRIPTION, is_default=True,
            ))
        conn.execute(
            jobs.update()
            .where(jobs.c.user_id == user_id, jobs.c.project_id.is_(None))
            .values(project_id=project_id)
        )


def downgrade() -> None:
    # 只撤掉标记；补建的默认项目和归进去的任务保持原样，当普通项目用
    with op.batch_alter_table('projects') as batch:
        batch.drop_column('is_default')
