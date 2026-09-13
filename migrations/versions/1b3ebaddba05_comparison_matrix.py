"""comparison_matrix

Revision ID: 1b3ebaddba05
Revises: 13322ab22f91
Create Date: 2026-09-13 17:17:16.783967

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1b3ebaddba05'
down_revision: Union[str, Sequence[str], None] = '13322ab22f91'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The diff has always emitted one row per field path. Assert it before
    # relying on it, so a duplicate surfaces here rather than as a constraint
    # violation on some later build.
    duplicates = op.get_bind().execute(sa.text(
        "SELECT count(*) FROM ("
        "  SELECT comparison_id, field_path FROM difference"
        "  GROUP BY comparison_id, field_path HAVING count(*) > 1"
        ") d"
    )).scalar()
    assert not duplicates, f"{duplicates} duplicate (comparison_id, field_path)"

    op.create_table(
        "comparison_column",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("comparison_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("policy_term_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=True),
        sa.CheckConstraint("role IN ('baseline', 'comparand')",
                           name="ck_comparison_column_role"),
        sa.ForeignKeyConstraint(["comparison_id"], ["comparison.id"]),
        sa.ForeignKeyConstraint(["policy_term_id"], ["policy_term.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("comparison_id", "position",
                            name="uq_comparison_column_position"),
    )
    op.create_index("ix_comparison_column_comparison_id", "comparison_column",
                    ["comparison_id"])
    # A CHECK cannot see across rows, so one baseline per comparison is a
    # partial unique index. Same technique as uq_app_user_email_lower.
    op.create_index(
        "uq_comparison_one_baseline", "comparison_column", ["comparison_id"],
        unique=True, postgresql_where=sa.text("role = 'baseline'"),
    )

    op.create_table(
        "difference_cell",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("difference_id", sa.Integer(), nullable=False),
        sa.Column("comparison_column_id", sa.Integer(), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["difference_id"], ["difference.id"]),
        sa.ForeignKeyConstraint(["comparison_column_id"],
                                ["comparison_column.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("difference_id", "comparison_column_id",
                            name="uq_difference_cell"),
    )
    op.create_index("ix_difference_cell_difference_id", "difference_cell",
                    ["difference_id"])

    op.create_unique_constraint("uq_difference_path", "difference",
                                ["comparison_id", "field_path"])

    # Not written again after this: the same term ids live on the columns, and
    # a run only exists for a comparison that came from an upload pair. They
    # are kept because for every comparison built before the matrix they are
    # the only record of what was compared.
    for column in ("renewal_run_id", "prior_term_id", "renewal_term_id"):
        op.alter_column("comparison", column, existing_type=sa.Integer(),
                        nullable=True)


def downgrade() -> None:
    for column in ("renewal_term_id", "prior_term_id", "renewal_run_id"):
        op.alter_column("comparison", column, existing_type=sa.Integer(),
                        nullable=False)
    op.drop_constraint("uq_difference_path", "difference", type_="unique")
    op.drop_index("ix_difference_cell_difference_id", table_name="difference_cell")
    op.drop_table("difference_cell")
    op.drop_index("uq_comparison_one_baseline", table_name="comparison_column")
    op.drop_index("ix_comparison_column_comparison_id",
                  table_name="comparison_column")
    op.drop_table("comparison_column")
