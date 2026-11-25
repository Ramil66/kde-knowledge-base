"""Include a source filename in raw document identity."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("raw_documents", sa.Column("source_name", sa.Text))
    op.drop_index("uq_document_origin", table_name="raw_documents")
    op.create_index(
        "uq_document_origin",
        "raw_documents",
        [
            "document_type",
            "format",
            sa.text("coalesce(source_url, '')"),
            sa.text("coalesce(source_name, '')"),
            "content_hash",
        ],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_document_origin", table_name="raw_documents")
    op.create_index(
        "uq_document_origin",
        "raw_documents",
        ["document_type", "format", sa.text("coalesce(source_url, '')"), "content_hash"],
        unique=True,
    )
    op.drop_column("raw_documents", "source_name")
