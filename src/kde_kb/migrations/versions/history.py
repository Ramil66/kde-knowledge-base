"""Preserve MR snapshots, task changes and original status spellings."""

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from kde_kb.migrations.rebuild import replace_columns

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

DATES = {
    "repositories": ("created_at", "updated_at"),
    "branches": ("created_at", "updated_at"),
    "tasks": ("created_at", "updated_at", "completed_at"),
    "merge_requests": ("created_at", "updated_at", "closed_at", "merged_at"),
    "discussions": ("created_at", "resolved_at"),
    "comments": ("created_at", "updated_at"),
    "commits": ("authored_at", "committed_at"),
    "task_events": ("created_at",),
    "test_runs": ("created_at",),
    "raw_documents": ("created_at",),
}

STATUS = {
    "tasks": {
        "open": ("open", "new", "unconfirmed", "confirmed", "reported"),
        "completed": ("completed", "done", "resolved", "closed", "verified", "fixed"),
        "reopened": ("reopened",),
        "in_progress": ("in_progress", "in progress", "assigned"),
    },
    "branches": {
        "active": ("active", "open"),
        "merged": ("merged",),
        "abandoned": ("abandoned", "deleted", "closed"),
    },
    "merge_requests": {
        "open": ("open", "opened", "reopened"),
        "merged": ("merged", "accepted"),
        "closed": ("closed",),
    },
}


def upgrade() -> None:
    conn = op.get_bind()
    for table in ("repositories", "branches", "tasks", "merge_requests"):
        replace_columns(
            table,
            {
                field: ("TEXT DEFAULT CURRENT_TIMESTAMP NOT NULL", "TEXT")
                for field in ("created_at", "updated_at")
            },
        )
    for table, mapping in STATUS.items():
        op.add_column(table, sa.Column("external_status", sa.Text))
        conn.exec_driver_sql(
            f"UPDATE {table} SET external_status=status, status='unknown' WHERE status IS NOT NULL"
        )
        for canonical, aliases in mapping.items():
            params = ",".join("?" for _ in aliases)
            conn.exec_driver_sql(
                f"UPDATE {table} SET status=? WHERE lower(trim(external_status)) IN ({params})",
                (canonical, *aliases),
            )
    for column in ("source", "previous_value", "new_value"):
        op.add_column("task_events", sa.Column(column, sa.Text))
    conn.exec_driver_sql("""CREATE TABLE legacy_values (entity TEXT NOT NULL, entity_id INTEGER NOT NULL,
        field TEXT NOT NULL, original TEXT NOT NULL, PRIMARY KEY(entity,entity_id,field))""")
    for table, columns in DATES.items():
        for column in columns:
            query = f"SELECT id,{column} FROM {table} WHERE {column} IS NOT NULL"
            for uid, original in conn.exec_driver_sql(query):
                try:
                    date = datetime.fromisoformat(original)
                    if date.tzinfo is None:
                        # SQLite CURRENT_TIMESTAMP in 0001 was written in UTC.
                        if (
                            table
                            not in (
                                "repositories",
                                "branches",
                                "tasks",
                                "merge_requests",
                                "raw_documents",
                            )
                            or column not in ("created_at", "updated_at")
                            or len(original) != 19
                            or original[10] != " "
                        ):
                            raise ValueError("Unknown timezone")
                        date = date.replace(tzinfo=UTC)
                    normalized = date.astimezone(UTC).isoformat().replace("+00:00", "Z")
                except (ValueError, TypeError, OverflowError):
                    conn.exec_driver_sql(
                        "INSERT INTO legacy_values VALUES (?,?,?,?)", (table, uid, column, original)
                    )
                    if table != "raw_documents":
                        conn.exec_driver_sql(f"UPDATE {table} SET {column}=NULL WHERE id=?", (uid,))
                    continue
                conn.exec_driver_sql(f"UPDATE {table} SET {column}=? WHERE id=?", (normalized, uid))
    conn.exec_driver_sql("""CREATE TABLE task_merge_requests (
        task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        merge_request_id INTEGER NOT NULL REFERENCES merge_requests(id) ON DELETE CASCADE,
        PRIMARY KEY(task_id,merge_request_id))""")
    conn.exec_driver_sql("CREATE INDEX ix_task_mrs_mr ON task_merge_requests(merge_request_id)")
    conn.exec_driver_sql("""INSERT INTO task_merge_requests SELECT tb.task_id, mr.id
        FROM task_branches tb JOIN merge_requests mr ON mr.branch_id=tb.branch_id""")
    conn.exec_driver_sql("""CREATE TABLE mr_revisions (
        id INTEGER PRIMARY KEY, merge_request_id INTEGER NOT NULL REFERENCES merge_requests(id) ON DELETE CASCADE,
        base_sha TEXT, start_sha TEXT, head_sha TEXT, source_document_id INTEGER REFERENCES raw_documents(id),
        created_at TEXT, kind TEXT NOT NULL)""")
    conn.exec_driver_sql(
        "CREATE INDEX ix_mr_revisions_merge_request_id ON mr_revisions(merge_request_id)"
    )
    conn.exec_driver_sql("""CREATE TABLE revision_commits (
        revision_id INTEGER NOT NULL REFERENCES mr_revisions(id) ON DELETE CASCADE,
        commit_id INTEGER NOT NULL REFERENCES commits(id), PRIMARY KEY(revision_id,commit_id))""")
    conn.exec_driver_sql(
        "INSERT INTO mr_revisions(merge_request_id,kind) SELECT DISTINCT merge_request_id,'legacy' FROM mr_commits"
    )
    conn.exec_driver_sql("""INSERT INTO revision_commits SELECT r.id,c.commit_id FROM mr_revisions r
        JOIN mr_commits c ON r.merge_request_id=c.merge_request_id""")


def downgrade() -> None:
    conn = op.get_bind()
    for table, uid, field, original in conn.exec_driver_sql(
        "SELECT entity,entity_id,field,original FROM legacy_values"
    ):
        if table in DATES and field in DATES[table]:
            conn.exec_driver_sql(
                f"UPDATE {table} SET {field}=? WHERE id=? AND {field} IS NULL", (original, uid)
            )
    for table in ("repositories", "branches", "tasks", "merge_requests"):
        replace_columns(
            table,
            {
                field: ("TEXT", "TEXT DEFAULT CURRENT_TIMESTAMP NOT NULL")
                for field in ("created_at", "updated_at")
            },
        )
    for table in ("revision_commits", "mr_revisions", "task_merge_requests", "legacy_values"):
        op.drop_table(table)
    for column in ("source", "previous_value", "new_value"):
        conn.exec_driver_sql(f"ALTER TABLE task_events DROP COLUMN {column}")
    for table in STATUS:
        conn.exec_driver_sql(f"UPDATE {table} SET status=coalesce(external_status,status)")
        conn.exec_driver_sql(f"ALTER TABLE {table} DROP COLUMN external_status")
