"""document text and classification

Revision ID: a39e0c70b580
Revises: a8e002e61a62
Create Date: 2026-09-03 01:24:43.330668

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'a39e0c70b580'
down_revision: Union[str, Sequence[str], None] = 'a8e002e61a62'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Both full-text search and client name matching need pg_trgm.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table('document_text',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('document_id', sa.Integer(), nullable=False),
    sa.Column('page_number', sa.Integer(), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('extraction_method', sa.Text(), nullable=False),
    sa.Column('extractor_version', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('tsv', postgresql.TSVECTOR(), sa.Computed("to_tsvector('english', text)", persisted=True), nullable=False),
    sa.ForeignKeyConstraint(['document_id'], ['document.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('document_id', 'page_number', 'extractor_version', name='uq_document_text_page_version')
    )
    op.create_index('ix_document_text_tsv', 'document_text', ['tsv'], unique=False, postgresql_using='gin')
    op.create_table('document_classification',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('document_id', sa.Integer(), nullable=False),
    sa.Column('doc_class', sa.Text(), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('classifier_version', sa.Text(), nullable=False),
    sa.Column('model_id', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['document_id'], ['document.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_document_classification_document_id', 'document_classification', ['document_id'], unique=False)
    op.add_column('document', sa.Column('source', sa.Text(), server_default='manual_upload', nullable=False))
    op.add_column('document', sa.Column('agency_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_document_agency_id', 'document', 'agency', ['agency_id'], ['id'])

    # The one UPDATE in the codebase. It runs once, in a migration, over rows
    # that predate the column, so the insert-only invariant on application
    # code stands.
    op.execute("UPDATE document SET agency_id = (SELECT id FROM agency "
               "WHERE slug = 'default')")


def downgrade() -> None:
    """Downgrade schema."""
    # The extensions stay: dropping a shared extension would break unrelated
    # indexes elsewhere in the database.
    op.drop_constraint('fk_document_agency_id', 'document', type_='foreignkey')
    op.drop_column('document', 'agency_id')
    op.drop_column('document', 'source')
    op.drop_index('ix_document_classification_document_id', table_name='document_classification')
    op.drop_table('document_classification')
    op.drop_index('ix_document_text_tsv', table_name='document_text')
    op.drop_table('document_text')
