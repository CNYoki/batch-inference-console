"""job files purged timestamp

Revision ID: 883849a8d02d
Revises: 7505fb00847c
Create Date: 2026-09-08 16:32:06.275394
"""
from alembic import op
import sqlalchemy as sa


revision = '883849a8d02d'
down_revision = '7505fb00847c'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('jobs', sa.Column('files_purged_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('jobs', 'files_purged_at')
