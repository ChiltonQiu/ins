"""search indexes

Revision ID: de63e5e41ffe
Revises: 2d63899b782b
Create Date: 2026-09-04 22:09:50.134332

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'de63e5e41ffe'
down_revision: Union[str, Sequence[str], None] = '2d63899b782b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Indexes only. pg_trgm is already installed by the migration that created
    document_text, so nothing here creates an extension."""
    op.create_index(
        "ix_client_display_name_trgm", "client", ["display_name"],
        postgresql_using="gin", postgresql_ops={"display_name": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_carrier_display_name_trgm", "carrier", ["display_name"],
        postgresql_using="gin", postgresql_ops={"display_name": "gin_trgm_ops"},
    )
    op.create_index("ix_policy_policy_number", "policy", ["policy_number"])
    op.create_index(
        "ix_document_uploaded_at_desc", "document",
        [sa.text("uploaded_at DESC")],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_document_uploaded_at_desc", table_name="document")
    op.drop_index("ix_policy_policy_number", table_name="policy")
    op.drop_index("ix_carrier_display_name_trgm", table_name="carrier")
    op.drop_index("ix_client_display_name_trgm", table_name="client")
