"""reasoning off payload

Revision ID: d7f3a9c2e1b4
Revises: c4d8e2f1a6b9
Create Date: 2026-09-11 10:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'd7f3a9c2e1b4'
down_revision = 'c4d8e2f1a6b9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # MySQL 的 JSON 列不接受 DEFAULT，所以先加可空列、回填 {}，再收紧成 NOT NULL
    op.add_column(
        "model_configs", sa.Column("reasoning_off_payload", sa.JSON(), nullable=True)
    )
    op.execute("UPDATE model_configs SET reasoning_off_payload = '{}'")
    op.alter_column("model_configs", "reasoning_off_payload",
                    existing_type=sa.JSON(), nullable=False)


def downgrade() -> None:
    op.drop_column("model_configs", "reasoning_off_payload")
