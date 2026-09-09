"""personal gateway token and model source

Revision ID: 7505fb00847c
Revises: 5ec5b8899f44
Create Date: 2026-09-08 16:07:57.053864
"""
from alembic import op
import sqlalchemy as sa


revision = '7505fb00847c'
down_revision = '5ec5b8899f44'
branch_labels = None
depends_on = None


MODEL_SOURCE = sa.Enum("shared", "personal", name="modelsource", native_enum=False, length=16)


def upgrade() -> None:
    # 已有任务全部是公用模型，用 server_default 回填，
    # 否则往非空表加 NOT NULL 列会失败
    op.add_column(
        "jobs",
        sa.Column("model_source", MODEL_SOURCE, nullable=False, server_default="shared"),
    )
    op.add_column("jobs", sa.Column("personal_model_name", sa.String(length=255), nullable=True))
    op.add_column("users", sa.Column("llm_token_encrypted", sa.Text(), nullable=True))
    op.add_column(
        "users", sa.Column("llm_token_updated_at", sa.DateTime(timezone=True), nullable=True)
    )
    # 回填完成后撤掉 server_default，让默认值回到应用层
    op.alter_column("jobs", "model_source", existing_type=MODEL_SOURCE,
                    existing_nullable=False, server_default=None)


def downgrade() -> None:
    op.drop_column('users', 'llm_token_updated_at')
    op.drop_column('users', 'llm_token_encrypted')
    op.drop_column('jobs', 'personal_model_name')
    op.drop_column('jobs', 'model_source')
