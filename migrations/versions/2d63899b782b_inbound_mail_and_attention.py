"""inbound mail and attention

Revision ID: 2d63899b782b
Revises: 7aa1b05a1fbd
Create Date: 2026-09-03 01:27:51.955538

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2d63899b782b'
down_revision: Union[str, Sequence[str], None] = '7aa1b05a1fbd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('inbound_message',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('agency_id', sa.Integer(), nullable=True),
    sa.Column('message_id', sa.Text(), nullable=False),
    sa.Column('from_address', sa.Text(), nullable=False),
    sa.Column('to_address', sa.Text(), nullable=False),
    sa.Column('subject', sa.Text(), nullable=True),
    sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('raw_mime_blob_sha256', sa.Text(), nullable=False),
    sa.Column('body_text', sa.Text(), nullable=False),
    sa.Column('processing_status', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("processing_status IN ('received', 'processed', 'quarantined', 'duplicate', 'failed')", name='ck_inbound_message_status'),
    sa.ForeignKeyConstraint(['agency_id'], ['agency.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('agency_id', 'message_id', name='uq_inbound_message_id')
    )
    op.create_table('attention_item',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('document_id', sa.Integer(), nullable=False),
    sa.Column('reason_code', sa.Text(), nullable=False),
    sa.Column('reason_text', sa.Text(), nullable=False),
    sa.Column('due_date', sa.Date(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['document_id'], ['document.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_attention_item_document_id'), 'attention_item', ['document_id'], unique=False)
    op.create_index(op.f('ix_attention_item_due_date'), 'attention_item', ['due_date'], unique=False)
    op.create_table('attention_event',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('attention_item_id', sa.Integer(), nullable=False),
    sa.Column('action', sa.Text(), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('actor', sa.Text(), server_default='human', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("action IN ('done', 'dismissed')", name='ck_attention_event_action'),
    sa.ForeignKeyConstraint(['attention_item_id'], ['attention_item.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_attention_event_attention_item_id'), 'attention_event', ['attention_item_id'], unique=False)

    # Added here, not with the other document columns, because its target table
    # does not exist until now.
    op.add_column('document', sa.Column('inbound_message_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_document_inbound_message_id', 'document', 'inbound_message', ['inbound_message_id'], ['id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('fk_document_inbound_message_id', 'document', type_='foreignkey')
    op.drop_column('document', 'inbound_message_id')
    op.drop_index(op.f('ix_attention_event_attention_item_id'), table_name='attention_event')
    op.drop_table('attention_event')
    op.drop_index(op.f('ix_attention_item_due_date'), table_name='attention_item')
    op.drop_index(op.f('ix_attention_item_document_id'), table_name='attention_item')
    op.drop_table('attention_item')
    op.drop_table('inbound_message')
