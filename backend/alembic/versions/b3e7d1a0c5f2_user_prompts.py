"""user prompts

Revision ID: b3e7d1a0c5f2
Revises: 9a1c4f2b7d3e
Create Date: 2026-09-10 10:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = 'b3e7d1a0c5f2'
down_revision = '9a1c4f2b7d3e'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('user_prompts',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('user_id', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('description', sa.String(length=1024), nullable=True),
    sa.Column('system_prompt', sa.Text(), nullable=True),
    sa.Column('prompt_template', sa.Text().with_variant(mysql.LONGTEXT(), 'mysql'), nullable=False),
    sa.Column('variables', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'name', name='uq_user_prompt_name')
    )


def downgrade() -> None:
    op.drop_table('user_prompts')
