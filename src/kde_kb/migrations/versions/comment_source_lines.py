"""Store source line ranges for comments parsed from copied pages."""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("comments", sa.Column("source_start_line", sa.Integer))
    op.add_column("comments", sa.Column("source_end_line", sa.Integer))


def downgrade() -> None:
    op.drop_column("comments", "source_end_line")
    op.drop_column("comments", "source_start_line")
