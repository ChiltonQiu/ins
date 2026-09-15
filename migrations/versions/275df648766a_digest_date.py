"""digest date

Revision ID: 275df648766a
Revises: f3b96d41c8a2
Create Date: 2026-09-15 09:01:01.142392

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '275df648766a'
down_revision: Union[str, Sequence[str], None] = 'f3b96d41c8a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "notification_send", sa.Column("digest_date", sa.Date(), nullable=True)
    )
    # Every row written before this migration was the event-triggered email,
    # which is exactly what a NULL digest_date means. Nothing to backfill.
    op.create_index(
        "uq_notification_send_digest_date", "notification_send", ["digest_date"],
        unique=True, postgresql_where=sa.text("digest_date IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_notification_send_digest_date", table_name="notification_send"
    )
    op.drop_column("notification_send", "digest_date")
