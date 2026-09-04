"""document link

Revision ID: eab2ee43cff8
Revises: a39e0c70b580
Create Date: 2026-09-03 01:25:35.486873

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'eab2ee43cff8'
down_revision: Union[str, Sequence[str], None] = 'a39e0c70b580'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('document_link',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('document_id', sa.Integer(), nullable=False),
    sa.Column('client_id', sa.Integer(), nullable=False),
    sa.Column('policy_id', sa.Integer(), nullable=True),
    sa.Column('method', sa.Text(), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('candidates', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("method IN ('auto', 'manual')", name='ck_document_link_method'),
    sa.ForeignKeyConstraint(['client_id'], ['client.id'], ),
    sa.ForeignKeyConstraint(['document_id'], ['document.id'], ),
    sa.ForeignKeyConstraint(['policy_id'], ['policy.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_document_link_document_id'), 'document_link', ['document_id'], unique=False)
    # Latest row per document is the hot path for the calendar, the queue, and
    # the client overview.
    op.create_index('ix_document_link_document_id_id_desc', 'document_link',
                    ['document_id', sa.text('id DESC')], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_document_link_document_id_id_desc', table_name='document_link')
    op.drop_index(op.f('ix_document_link_document_id'), table_name='document_link')
    op.drop_table('document_link')
