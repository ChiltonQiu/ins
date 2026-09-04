"""billing type

Revision ID: a8e002e61a62
Revises: 51869b5f8733
Create Date: 2026-09-03 01:23:45.822150

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a8e002e61a62'
down_revision: Union[str, Sequence[str], None] = '51869b5f8733'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('policy_billing_type',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('policy_id', sa.Integer(), nullable=False),
    sa.Column('billing_type', sa.Text(), nullable=False),
    sa.Column('set_by', sa.Text(), server_default='human', nullable=False),
    sa.Column('set_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("billing_type IN ('direct_bill', 'agency_bill', 'unknown')", name='ck_policy_billing_type'),
    sa.ForeignKeyConstraint(['policy_id'], ['policy.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_policy_billing_type_policy_id', 'policy_billing_type', ['policy_id'], unique=False)
    op.add_column('policy_term', sa.Column('billing_type', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('policy_term', 'billing_type')
    op.drop_index('ix_policy_billing_type_policy_id', table_name='policy_billing_type')
    op.drop_table('policy_billing_type')
