"""api tokens

Revision ID: c4d8e2f1a6b9
Revises: b3e7d1a0c5f2
Create Date: 2026-09-10 14:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'c4d8e2f1a6b9'
down_revision = 'b3e7d1a0c5f2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('api_tokens',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('user_id', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('token_prefix', sa.String(length=16), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash')
    )
    op.create_index('ix_api_tokens_user', 'api_tokens', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_api_tokens_user', table_name='api_tokens')
    op.drop_table('api_tokens')
