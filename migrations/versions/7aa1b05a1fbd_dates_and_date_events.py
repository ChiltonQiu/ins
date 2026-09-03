"""dates and date events

Revision ID: 7aa1b05a1fbd
Revises: eab2ee43cff8
Create Date: 2026-09-03 01:26:40.401527

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7aa1b05a1fbd'
down_revision: Union[str, Sequence[str], None] = 'eab2ee43cff8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


DATE_TYPE_SQL = (
    "'policy_effective', 'policy_expiration', 'renewal_due', "
    "'cancellation_effective', 'non_renewal_effective', 'payment_due', "
    "'inspection_deadline', 'remediation_deadline', 'audit_date', 'other'"
)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('document_date',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('document_id', sa.Integer(), nullable=False),
    sa.Column('date_value', sa.Date(), nullable=False),
    sa.Column('date_type', sa.Text(), nullable=False),
    sa.Column('source_page', sa.Integer(), nullable=False),
    sa.Column('source_text', sa.Text(), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('extractor_version', sa.Text(), nullable=False),
    sa.Column('pass', sa.Text(), nullable=False),
    sa.Column('is_derived', sa.Boolean(), nullable=False),
    sa.Column('anchor_date', sa.Date(), nullable=True),
    sa.Column('anchor_source_text', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint(f"date_type IN ({DATE_TYPE_SQL})", name='ck_document_date_type'),
    sa.CheckConstraint("\"pass\" IN ('regex', 'llm')", name='ck_document_date_pass'),
    sa.ForeignKeyConstraint(['document_id'], ['document.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_document_date_document_id'), 'document_date', ['document_id'], unique=False)
    op.create_index(op.f('ix_document_date_date_value'), 'document_date', ['date_value'], unique=False)
    op.create_table('date_event',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('document_date_id', sa.Integer(), nullable=False),
    sa.Column('action', sa.Text(), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('actor', sa.Text(), server_default='human', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("action IN ('confirmed', 'dismissed', 'superseded')", name='ck_date_event_action'),
    sa.ForeignKeyConstraint(['document_date_id'], ['document_date.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_date_event_document_date_id'), 'date_event', ['document_date_id'], unique=False)
    op.create_table('manual_date',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('agency_id', sa.Integer(), nullable=False),
    sa.Column('client_id', sa.Integer(), nullable=True),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('date_value', sa.Date(), nullable=False),
    sa.Column('date_type', sa.Text(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_by', sa.Text(), server_default='human', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint(f"date_type IN ({DATE_TYPE_SQL})", name='ck_manual_date_type'),
    sa.ForeignKeyConstraint(['agency_id'], ['agency.id'], ),
    sa.ForeignKeyConstraint(['client_id'], ['client.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_manual_date_date_value'), 'manual_date', ['date_value'], unique=False)
    op.create_table('manual_date_event',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('manual_date_id', sa.Integer(), nullable=False),
    sa.Column('action', sa.Text(), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('actor', sa.Text(), server_default='human', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("action IN ('confirmed', 'dismissed', 'superseded')", name='ck_manual_date_event_action'),
    sa.ForeignKeyConstraint(['manual_date_id'], ['manual_date.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_manual_date_event_manual_date_id'), 'manual_date_event', ['manual_date_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_manual_date_event_manual_date_id'), table_name='manual_date_event')
    op.drop_table('manual_date_event')
    op.drop_index(op.f('ix_manual_date_date_value'), table_name='manual_date')
    op.drop_table('manual_date')
    op.drop_index(op.f('ix_date_event_document_date_id'), table_name='date_event')
    op.drop_table('date_event')
    op.drop_index(op.f('ix_document_date_date_value'), table_name='document_date')
    op.drop_index(op.f('ix_document_date_document_id'), table_name='document_date')
    op.drop_table('document_date')
