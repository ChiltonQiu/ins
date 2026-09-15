"""agency setting

Revision ID: f3b96d41c8a2
Revises: e70c8b2a5941
Create Date: 2026-09-15 00:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3b96d41c8a2'
down_revision: Union[str, Sequence[str], None] = 'e70c8b2a5941'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agency_setting",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("agency_id", sa.Integer(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=True),
        sa.ForeignKeyConstraint(["agency_id"], ["agency.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    # Latest row per key is read on nearly every request.
    op.create_index("ix_agency_setting_lookup", "agency_setting",
                    ["agency_id", "key", "id"])


def downgrade() -> None:
    op.drop_index("ix_agency_setting_lookup", table_name="agency_setting")
    op.drop_table("agency_setting")
