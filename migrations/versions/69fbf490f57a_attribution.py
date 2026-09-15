"""attribution

Revision ID: 69fbf490f57a
Revises: 275df648766a
Create Date: 2026-09-15 19:10:27.260821

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '69fbf490f57a'
down_revision: Union[str, Sequence[str], None] = '275df648766a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLES = (
    "correction", "date_event", "manual_date", "manual_date_event",
    "attention_event", "document_link", "comparison", "reclassification",
)


def upgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column("user_id", sa.Integer(), nullable=True))
        # RESTRICT: deleting a user would take the record of what they decided
        # with them. is_active is how an account is turned off.
        op.create_foreign_key(
            f"fk_{table}_user_id", table, "app_user", ["user_id"], ["id"],
            ondelete="RESTRICT",
        )
    # Deliberately no backfill. Every existing row was written before anyone
    # could be named, and attributing the history to the only account that
    # happens to exist would manufacture evidence.


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_constraint(f"fk_{table}_user_id", table, type_="foreignkey")
        op.drop_column(table, "user_id")
