"""Create the local catalog, original documents and full-text index."""

from importlib.resources import files
from sqlite3 import complete_statement

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# Table and column names are constants from this revision.
SEARCH_FIELDS = (
    (
        "repositories",
        "repo",
        (
            "name",
            "namespace",
            "description",
            "architecture_summary",
            "build_instructions",
            "test_instructions",
        ),
    ),
    (
        "tasks",
        "task",
        (
            "title",
            "description",
            "external_id",
            "reproduction_steps",
            "actual_behavior",
            "expected_behavior",
            "solution_summary",
        ),
    ),
    (
        "merge_requests",
        "mr",
        ("title", "description", "source_branch", "target_branch", "resolution_summary"),
    ),
    ("comments", "comment", ("body", "summary", "category")),
    ("raw_documents", "document", ("content",)),
)


def upgrade() -> None:
    connection = op.get_bind()
    statement = ""
    for line in files("kde_kb.migrations").joinpath("0001.sql").read_text().splitlines(True):
        statement += line
        if complete_statement(statement):
            connection.exec_driver_sql(statement)
            statement = ""
    connection.exec_driver_sql(
        "CREATE VIRTUAL TABLE search_index USING fts5(kind UNINDEXED, entity_id UNINDEXED, body)"
    )
    for table, kind, columns in SEARCH_FIELDS:
        body = " || char(10) || ".join(f"coalesce(new.{column}, '')" for column in columns)
        insert = f"INSERT INTO search_index VALUES ('{kind}', new.id, {body});"
        delete = f"DELETE FROM search_index WHERE kind = '{kind}' AND entity_id = old.id;"
        for event, sql in (("insert", insert), ("update", delete + insert), ("delete", delete)):
            connection.exec_driver_sql(
                f"CREATE TRIGGER fts_{table}_{event} AFTER {event} ON {table} BEGIN {sql} END"
            )


def downgrade() -> None:
    connection = op.get_bind()
    for table in (
        "document_links",
        "raw_documents",
        "test_runs",
        "task_events",
        "task_dependencies",
        "repository_dependencies",
        "mr_dependencies",
        "changed_files",
        "mr_commits",
        "commits",
        "code_positions",
        "comments",
        "discussions",
        "merge_requests",
        "users",
        "task_branches",
        "branches",
        "tasks",
        "repositories",
        "search_index",
    ):
        connection.exec_driver_sql(f"DROP TABLE {table}")
