"""job archived_at

Revision ID: c5e8a2d4f7b1
Revises: a7c9e3f5b1d2
Create Date: 2026-11-10 21:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'c5e8a2d4f7b1'
down_revision = 'a7c9e3f5b1d2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('jobs') as batch:
        batch.add_column(sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('jobs') as batch:
        batch.drop_column('archived_at')
