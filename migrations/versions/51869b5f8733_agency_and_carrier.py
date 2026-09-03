"""agency and carrier

Revision ID: 51869b5f8733
Revises: 66cc1100f4f8
Create Date: 2026-09-03 01:22:40.230147

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '51869b5f8733'
down_revision: Union[str, Sequence[str], None] = '66cc1100f4f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('agency',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('slug', sa.Text(), nullable=False),
    sa.Column('display_name', sa.Text(), nullable=False),
    sa.Column('ics_token', sa.Text(), nullable=False),
    sa.Column('intake_address', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('slug'),
    sa.UniqueConstraint('ics_token')
    )
    op.create_table('carrier',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('display_name', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('display_name')
    )
    op.create_table('carrier_alias',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('carrier_id', sa.Integer(), nullable=False),
    sa.Column('alias', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['carrier_id'], ['carrier.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('alias')
    )
    op.create_table('carrier_admitted_status',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('carrier_id', sa.Integer(), nullable=False),
    sa.Column('state', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('set_by', sa.Text(), server_default='human', nullable=False),
    sa.Column('set_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('admitted', 'non_admitted', 'unknown')", name='ck_carrier_admitted_status'),
    sa.ForeignKeyConstraint(['carrier_id'], ['carrier.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_carrier_admitted_status_carrier_state', 'carrier_admitted_status', ['carrier_id', 'state'], unique=False)
    op.add_column('policy', sa.Column('state', sa.Text(), nullable=True))

    # gen_random_bytes lives in pgcrypto; the token is generated in the
    # database so it never passes through application logs.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute(
        "INSERT INTO agency (slug, display_name, ics_token) VALUES "
        "('default', 'Default Agency', encode(gen_random_bytes(32), 'hex'))"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('policy', 'state')
    op.drop_index('ix_carrier_admitted_status_carrier_state', table_name='carrier_admitted_status')
    op.drop_table('carrier_admitted_status')
    op.drop_table('carrier_alias')
    op.drop_table('carrier')
    op.drop_table('agency')
