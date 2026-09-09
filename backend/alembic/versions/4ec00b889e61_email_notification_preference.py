"""email notification preference

Revision ID: 4ec00b889e61
Revises: 23440e3a9777
Create Date: 2026-09-09 11:23:37.217967
"""
from alembic import op
import sqlalchemy as sa


revision = '4ec00b889e61'
down_revision = '23440e3a9777'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 存量用户默认开启通知；回填完撤掉 server_default，让默认值回到应用层
    op.add_column(
        "users",
        sa.Column("notify_email", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.alter_column("users", "notify_email", existing_type=sa.Boolean(),
                    existing_nullable=False, server_default=None)


def downgrade() -> None:
    op.drop_column('users', 'notify_email')
