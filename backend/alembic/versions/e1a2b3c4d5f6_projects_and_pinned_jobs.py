"""projects and pinned jobs

Revision ID: e1a2b3c4d5f6
Revises: d7f3a9c2e1b4
Create Date: 2026-10-16 10:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'e1a2b3c4d5f6'
down_revision = 'd7f3a9c2e1b4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('projects',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('user_id', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('description', sa.String(length=1024), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'name', name='uq_project_name')
    )

    # batch 模式：SQLite 不支持直接 ALTER 加外键，MySQL/PostgreSQL 下等同普通 ALTER
    with op.batch_alter_table('jobs') as batch:
        batch.add_column(sa.Column('project_id', sa.String(length=32), nullable=True))
        batch.add_column(sa.Column('pinned', sa.Boolean(), server_default=sa.false(), nullable=False))
        batch.add_column(sa.Column('pinned_at', sa.DateTime(timezone=True), nullable=True))
        batch.create_foreign_key('fk_jobs_project', 'projects', ['project_id'], ['id'], ondelete='SET NULL')
        batch.create_index('ix_jobs_project', ['project_id'], unique=False)

    with op.batch_alter_table('user_prompts') as batch:
        batch.add_column(sa.Column('project_id', sa.String(length=32), nullable=True))
        batch.create_foreign_key(
            'fk_user_prompts_project', 'projects', ['project_id'], ['id'], ondelete='SET NULL'
        )
        batch.create_index('ix_user_prompts_project', ['project_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('user_prompts') as batch:
        batch.drop_index('ix_user_prompts_project')
        batch.drop_constraint('fk_user_prompts_project', type_='foreignkey')
        batch.drop_column('project_id')

    with op.batch_alter_table('jobs') as batch:
        batch.drop_index('ix_jobs_project')
        batch.drop_constraint('fk_jobs_project', type_='foreignkey')
        batch.drop_column('pinned_at')
        batch.drop_column('pinned')
        batch.drop_column('project_id')

    op.drop_table('projects')
