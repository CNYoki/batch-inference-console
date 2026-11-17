"""job run_seconds / run_started_at

Revision ID: a9d4e6b2c8f1
Revises: c5e8a2d4f7b1
Create Date: 2026-11-17 10:00:00.000000
"""
from datetime import UTC, datetime

from alembic import op
import sqlalchemy as sa


revision = 'a9d4e6b2c8f1'
down_revision = 'c5e8a2d4f7b1'
branch_labels = None
depends_on = None


def _aware(v: datetime | None) -> datetime | None:
    if v is None:
        return None
    return v if v.tzinfo else v.replace(tzinfo=UTC)


def upgrade() -> None:
    with op.batch_alter_table('jobs') as batch:
        batch.add_column(sa.Column('run_seconds', sa.Integer(), nullable=False, server_default='0'))
        batch.add_column(sa.Column('run_started_at', sa.DateTime(timezone=True), nullable=True))

    # 历史任务没有分段记录：已结束的按「最后一次开始 → 结束」估算，运行中的从最后一次开始接着计
    jobs = sa.table(
        'jobs',
        sa.column('id', sa.String), sa.column('status', sa.String),
        sa.column('started_at', sa.DateTime(timezone=True)),
        sa.column('finished_at', sa.DateTime(timezone=True)),
        sa.column('run_seconds', sa.Integer),
        sa.column('run_started_at', sa.DateTime(timezone=True)),
    )
    conn = op.get_bind()
    rows = conn.execute(
        sa.select(jobs.c.id, jobs.c.status, jobs.c.started_at, jobs.c.finished_at)
        .where(jobs.c.started_at.is_not(None))
    ).all()
    for job_id, status, started, finished in rows:
        started, finished = _aware(started), _aware(finished)
        if status == 'running':
            conn.execute(jobs.update().where(jobs.c.id == job_id).values(run_started_at=started))
        elif finished and finished > started:
            conn.execute(
                jobs.update().where(jobs.c.id == job_id)
                .values(run_seconds=int((finished - started).total_seconds()))
            )


def downgrade() -> None:
    with op.batch_alter_table('jobs') as batch:
        batch.drop_column('run_started_at')
        batch.drop_column('run_seconds')
