"""Store typed relationships between merge requests."""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mr_relations",
        sa.Column(
            "merge_request_id",
            sa.Integer,
            sa.ForeignKey("merge_requests.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "related_merge_request_id",
            sa.Integer,
            sa.ForeignKey("merge_requests.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("relation_type", sa.Text, nullable=False),
        sa.CheckConstraint("merge_request_id <> related_merge_request_id"),
    )


def downgrade() -> None:
    op.drop_table("mr_relations")
