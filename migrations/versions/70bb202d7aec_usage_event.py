"""usage event

Autogenerate produced this table and, beside it, a proposal to drop both
trigram search indexes, the document status and uploaded_at indexes, the
partial unique constraint on comparison_column, and several others. Those are
hand-written in earlier migrations and invisible to the model metadata, so
--autogenerate reads them as drift. They are not. Everything below is the new
table and nothing else.

Revision ID: 70bb202d7aec
Revises: 61b9dccccb66
Create Date: 2026-09-22 19:28:49.436076

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '70bb202d7aec'
down_revision: Union[str, Sequence[str], None] = '61b9dccccb66'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'usage_event',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('occurred_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('route', sa.Text(), nullable=False),
        sa.Column('method', sa.Text(), nullable=False),
        sa.Column('status', sa.Integer(), nullable=False),
        sa.Column('duration_ms', sa.Integer(), nullable=False),
        sa.Column('from_route', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['app_user.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_usage_event_route'), 'usage_event', ['route'])
    op.create_index(op.f('ix_usage_event_user_id'), 'usage_event', ['user_id'])
    # Every report this feeds is "what happened between these two dates".
    op.create_index('ix_usage_event_occurred_at', 'usage_event', ['occurred_at'])


def downgrade() -> None:
    op.drop_index('ix_usage_event_occurred_at', table_name='usage_event')
    op.drop_index(op.f('ix_usage_event_user_id'), table_name='usage_event')
    op.drop_index(op.f('ix_usage_event_route'), table_name='usage_event')
    op.drop_table('usage_event')
