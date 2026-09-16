"""mail poll state

Revision ID: 61b9dccccb66
Revises: 69fbf490f57a
Create Date: 2026-09-16 14:46:12.941127

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '61b9dccccb66'
down_revision: Union[str, Sequence[str], None] = '69fbf490f57a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "mail_poll_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("folder", sa.Text(), nullable=False),
        sa.Column("uid_validity", sa.Integer(), nullable=True),
        sa.Column("last_uid", sa.Integer(), nullable=True),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_ingested", sa.Integer(), server_default="0",
                  nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("host", "folder", name="uq_mail_poll_state_folder"),
    )


def downgrade() -> None:
    op.drop_table("mail_poll_state")
