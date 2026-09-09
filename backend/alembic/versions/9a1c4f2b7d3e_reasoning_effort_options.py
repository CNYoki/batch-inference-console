"""reasoning effort options

Revision ID: 9a1c4f2b7d3e
Revises: 4ec00b889e61
Create Date: 2026-09-09 10:12:33.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '9a1c4f2b7d3e'
down_revision = '4ec00b889e61'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # MySQL 的 JSON 列不接受 DEFAULT，所以先加可空列、回填 []，再收紧成 NOT NULL
    op.add_column(
        "model_configs", sa.Column("reasoning_effort_options", sa.JSON(), nullable=True)
    )
    op.execute("UPDATE model_configs SET reasoning_effort_options = '[]'")
    op.alter_column("model_configs", "reasoning_effort_options",
                    existing_type=sa.JSON(), nullable=False)
    # 普通字符串列可以直接用 server_default 回填，填完再撤掉
    op.add_column(
        "model_configs",
        sa.Column("reasoning_default_effort", sa.String(length=32),
                  nullable=False, server_default=""),
    )
    op.alter_column("model_configs", "reasoning_default_effort",
                    existing_type=sa.String(length=32),
                    existing_nullable=False, server_default=None)


def downgrade() -> None:
    op.drop_column("model_configs", "reasoning_default_effort")
    op.drop_column("model_configs", "reasoning_effort_options")
