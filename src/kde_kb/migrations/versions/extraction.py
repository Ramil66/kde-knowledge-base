"""Persist reviewed LLM proposals and distinguish local and external identities."""

import sqlalchemy as sa
from alembic import op

from kde_kb.migrations.rebuild import replace_columns

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def rebuild_mrs(nullable: bool) -> None:
    types = ("INTEGER NOT NULL", "INTEGER") if nullable else ("INTEGER", "INTEGER NOT NULL")
    replace_columns("merge_requests", {field: types for field in ("external_id", "branch_id")})


def upgrade() -> None:
    rebuild_mrs(True)
    for table in ("comments", "discussions"):
        op.add_column(table, sa.Column("local_key", sa.Text))
        op.create_index(f"uq_{table}_local_key", table, ["local_key"], unique=True)
    op.create_table(
        "document_extractions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "document_id",
            sa.Integer,
            sa.ForeignKey("raw_documents.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("prompt_version", sa.Text, nullable=False),
        sa.Column("proposal", sa.JSON, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
    )


def downgrade() -> None:
    rebuild_mrs(False)
    op.drop_table("document_extractions")
    for table in ("comments", "discussions"):
        op.drop_index(f"uq_{table}_local_key", table_name=table)
        op.get_bind().exec_driver_sql(f"ALTER TABLE {table} DROP COLUMN local_key")
