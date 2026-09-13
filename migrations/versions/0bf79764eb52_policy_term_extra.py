"""policy_term_extra

Revision ID: 0bf79764eb52
Revises: 6dccde627b6b
Create Date: 2026-09-13 17:17:17.187433

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0bf79764eb52'
down_revision: Union[str, Sequence[str], None] = '6dccde627b6b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "policy_term_extra",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("policy_term_id", sa.Integer(), nullable=False),
        sa.Column("field_path", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=True),
        sa.ForeignKeyConstraint(["policy_term_id"], ["policy_term.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("policy_term_id", "field_path",
                            name="uq_policy_term_extra"),
    )
    op.create_index("ix_policy_term_extra_policy_term_id", "policy_term_extra",
                    ["policy_term_id"])


def downgrade() -> None:
    op.drop_index("ix_policy_term_extra_policy_term_id",
                  table_name="policy_term_extra")
    op.drop_table("policy_term_extra")
