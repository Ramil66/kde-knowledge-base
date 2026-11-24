"""Checkpoint independent extraction stages and retain token measurements."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("document_extractions", sa.Column("metrics", sa.JSON))
    op.create_table(
        "import_checkpoints",
        sa.Column("cache_key", sa.Text, primary_key=True),
        sa.Column(
            "document_id",
            sa.Integer,
            sa.ForeignKey("raw_documents.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("stage", sa.Text, nullable=False),
        sa.Column("result", sa.JSON, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("import_checkpoints")
    op.get_bind().exec_driver_sql("ALTER TABLE document_extractions DROP COLUMN metrics")
