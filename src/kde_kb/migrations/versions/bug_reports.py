"""Store Bugzilla records and unresolved cross-document links."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bug_reports",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("external_id", sa.Integer, nullable=False, unique=True),
        sa.Column("title", sa.Text),
        sa.Column("status", sa.Text),
        sa.Column("resolution", sa.Text),
        sa.Column("url", sa.Text),
        sa.Column("product", sa.Text),
        sa.Column("component", sa.Text),
        sa.Column("description", sa.Text),
        sa.Column("created_at", sa.Text),
        sa.Column("updated_at", sa.Text),
    )
    for name, source, target in (
        ("task_bug_reports", "tasks", "task_id"),
        ("mr_bug_reports", "merge_requests", "merge_request_id"),
    ):
        op.create_table(
            name,
            sa.Column(
                target,
                sa.Integer,
                sa.ForeignKey(f"{source}.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column(
                "bug_report_id",
                sa.Integer,
                sa.ForeignKey("bug_reports.id", ondelete="CASCADE"),
                primary_key=True,
            ),
        )
    op.create_table(
        "unresolved_links",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("source_type", sa.Text, nullable=False),
        sa.Column("source_id", sa.Integer, nullable=False),
        sa.Column("link_type", sa.Text, nullable=False),
        sa.Column("repository_url", sa.Text),
        sa.Column("external_id", sa.Integer, nullable=False),
        sa.UniqueConstraint(
            "source_type", "source_id", "link_type", "repository_url", "external_id"
        ),
    )


def downgrade() -> None:
    op.drop_table("unresolved_links")
    op.drop_table("mr_bug_reports")
    op.drop_table("task_bug_reports")
    op.drop_table("bug_reports")
