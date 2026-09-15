"""document status

Revision ID: c4a1f0b83d17
Revises: 0bf79764eb52
Create Date: 2026-09-14 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4a1f0b83d17'
down_revision: Union[str, Sequence[str], None] = '0bf79764eb52'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 'processed' for every existing row: they were all ingested synchronously
    # and are finished. No backfill query is needed.
    op.add_column(
        "document",
        sa.Column("status", sa.Text(), nullable=False,
                  server_default="processed"),
    )
    op.add_column(
        "document",
        sa.Column("status_changed_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
    )
    op.create_check_constraint(
        "ck_document_status", "document",
        "status IN ('processing', 'processed', 'failed')",
    )
    # The inbox filters on it on every load.
    op.create_index("ix_document_status", "document", ["status"])


def downgrade() -> None:
    op.drop_index("ix_document_status", table_name="document")
    op.drop_constraint("ck_document_status", "document", type_="check")
    op.drop_column("document", "status_changed_at")
    op.drop_column("document", "status")
