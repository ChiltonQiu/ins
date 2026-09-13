"""policy_term_kind

Revision ID: 6dccde627b6b
Revises: 1b3ebaddba05
Create Date: 2026-09-13 17:17:16.989188

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6dccde627b6b'
down_revision: Union[str, Sequence[str], None] = '1b3ebaddba05'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Every term written before this migration was a bound policy term. The
    # server default records that rather than leaving it to be assumed.
    op.add_column("policy_term", sa.Column(
        "kind", sa.Text(), nullable=False, server_default="bound"
    ))
    op.create_check_constraint(
        "ck_policy_term_kind", "policy_term", "kind IN ('bound', 'quoted')"
    )


def downgrade() -> None:
    op.drop_constraint("ck_policy_term_kind", "policy_term", type_="check")
    op.drop_column("policy_term", "kind")
