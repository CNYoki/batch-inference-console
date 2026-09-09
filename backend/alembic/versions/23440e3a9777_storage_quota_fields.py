"""storage quota fields

Revision ID: 23440e3a9777
Revises: 883849a8d02d
Create Date: 2026-09-08 16:58:49.363614
"""
from alembic import op
import sqlalchemy as sa


revision = '23440e3a9777'
down_revision = '883849a8d02d'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 往非空表加 NOT NULL 列必须给 server_default，回填完再撤掉，
    # 让默认值回到应用层
    op.add_column(
        "jobs", sa.Column("result_size", sa.BigInteger(), nullable=False, server_default="0")
    )
    op.add_column(
        "users", sa.Column("max_storage_mb", sa.BigInteger(), nullable=False, server_default="0")
    )
    op.alter_column("jobs", "result_size", existing_type=sa.BigInteger(),
                    existing_nullable=False, server_default=None)
    op.alter_column("users", "max_storage_mb", existing_type=sa.BigInteger(),
                    existing_nullable=False, server_default=None)


def downgrade() -> None:
    op.drop_column('users', 'max_storage_mb')
    op.drop_column('jobs', 'result_size')
