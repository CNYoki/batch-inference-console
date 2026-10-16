"""personal gateway separate billing

Revision ID: f3b6c8d1e2a4
Revises: e1a2b3c4d5f6
Create Date: 2026-10-16 18:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'f3b6c8d1e2a4'
down_revision = 'e1a2b3c4d5f6'
branch_labels = None
depends_on = None

BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
BILLING_STATUS = sa.Enum(
    "pending", "succeeded", "failed", name="billingstatus", native_enum=False, length=16
)


def upgrade() -> None:
    op.create_table('billing_records',
    sa.Column('id', BIGINT, autoincrement=True, nullable=False),
    sa.Column('request_id', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.String(length=32), nullable=False),
    sa.Column('job_id', sa.String(length=32), nullable=True),
    sa.Column('key_encrypted', sa.Text(), nullable=True),
    sa.Column('model_name', sa.String(length=255), nullable=False),
    sa.Column('prompt_tokens', sa.BigInteger(), nullable=False),
    sa.Column('completion_tokens', sa.BigInteger(), nullable=False),
    sa.Column('cached_tokens', sa.BigInteger(), nullable=False),
    sa.Column('note', sa.String(length=255), nullable=True),
    sa.Column('status', BILLING_STATUS, nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('error_code', sa.String(length=64), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('quota', sa.BigInteger(), nullable=True),
    sa.Column('billing_source', sa.String(length=32), nullable=True),
    sa.Column('duplicate', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('billed_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('request_id')
    )
    op.create_index('ix_billing_job', 'billing_records', ['job_id'], unique=False)
    op.create_index('ix_billing_status_next', 'billing_records', ['status', 'next_attempt_at'], unique=False)
    op.create_index('ix_billing_user_created', 'billing_records', ['user_id', 'created_at'], unique=False)

    # 已有任务一律视为未开独立计费、用量从 0 记；server_default 只为回填旧行
    with op.batch_alter_table('jobs') as batch:
        batch.add_column(sa.Column('cached_tokens', sa.BigInteger(), server_default='0', nullable=False))
        batch.add_column(sa.Column('billing_enabled', sa.Boolean(), server_default=sa.false(), nullable=False))
        batch.add_column(sa.Column('billed_prompt_tokens', sa.BigInteger(), server_default='0', nullable=False))
        batch.add_column(sa.Column('billed_completion_tokens', sa.BigInteger(), server_default='0', nullable=False))
        batch.add_column(sa.Column('billed_cached_tokens', sa.BigInteger(), server_default='0', nullable=False))
        batch.add_column(sa.Column('billing_seq', sa.Integer(), server_default='0', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('jobs') as batch:
        batch.drop_column('billing_seq')
        batch.drop_column('billed_cached_tokens')
        batch.drop_column('billed_completion_tokens')
        batch.drop_column('billed_prompt_tokens')
        batch.drop_column('billing_enabled')
        batch.drop_column('cached_tokens')

    op.drop_index('ix_billing_user_created', table_name='billing_records')
    op.drop_index('ix_billing_status_next', table_name='billing_records')
    op.drop_index('ix_billing_job', table_name='billing_records')
    op.drop_table('billing_records')
