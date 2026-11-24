"""Relational domain models used by the catalog and export services."""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)

metadata = MetaData()


def pk() -> Column:
    return Column("id", Integer, primary_key=True)


def string(name: str, *, required: bool = False) -> Column:
    return Column(name, Text, nullable=not required)


def fk(
    name: str, target: str, *, optional: bool = False, cascade: bool = False, primary: bool = False
) -> Column:
    return Column(
        name,
        Integer,
        ForeignKey(f"{target}.id", ondelete="CASCADE" if cascade else "RESTRICT"),
        nullable=optional,
        primary_key=primary,
        index=True,
    )


def timestamps() -> tuple[Column, Column]:
    return string("created_at"), string("updated_at")


repositories = Table(
    "repositories",
    metadata,
    pk(),
    Column("name", Text, nullable=False, index=True),
    Column("url", Text, nullable=False, unique=True),
    *(
        string(k)
        for k in (
            "namespace",
            "description",
            "default_branch",
            "local_path",
            "architecture_summary",
            "build_instructions",
            "test_instructions",
        )
    ),
    *timestamps(),
)

tasks = Table(
    "tasks",
    metadata,
    pk(),
    string("title", required=True),
    *(
        string(k)
        for k in (
            "description",
            "external_id",
            "external_url",
            "source",
            "type",
            "status",
            "external_status",
            "priority",
            "reproduction_steps",
            "actual_behavior",
            "expected_behavior",
            "solution_summary",
            "completed_at",
        )
    ),
    *timestamps(),
    UniqueConstraint("source", "external_id"),
    CheckConstraint("external_id IS NULL OR source IS NOT NULL"),
    CheckConstraint("type IN ('bug','feature','refactoring','documentation','research','other')"),
)

branches = Table(
    "branches",
    metadata,
    pk(),
    fk("repository_id", "repositories"),
    string("name", required=True),
    string("source_repository_url", required=True),
    *(
        string(k)
        for k in (
            "base_branch",
            "base_commit",
            "head_commit",
            "status",
            "external_status",
            "solution_summary",
        )
    ),
    *timestamps(),
    UniqueConstraint("repository_id", "source_repository_url", "name"),
)

task_branches = Table(
    "task_branches",
    metadata,
    fk("task_id", "tasks", primary=True, cascade=True),
    fk("branch_id", "branches", primary=True, cascade=True),
    string("role"),
    string("description"),
)

users = Table(
    "users",
    metadata,
    pk(),
    string("platform", required=True),
    string("username", required=True),
    string("display_name"),
    string("profile_url"),
    string("description"),
    UniqueConstraint("platform", "username"),
)

merge_requests = Table(
    "merge_requests",
    metadata,
    pk(),
    fk("branch_id", "branches", optional=True),
    fk("target_repository_id", "repositories"),
    fk("author_id", "users", optional=True),
    Column("external_id", Integer),
    string("title", required=True),
    Column("is_draft", Boolean),
    *(
        string(k)
        for k in (
            "url",
            "description",
            "source_branch",
            "target_branch",
            "status",
            "external_status",
            "pipeline_status",
            "merged_at",
            "closed_at",
            "resolution_summary",
        )
    ),
    *timestamps(),
    CheckConstraint("external_id > 0"),
    UniqueConstraint("target_repository_id", "external_id"),
)

discussions = Table(
    "discussions",
    metadata,
    pk(),
    fk("merge_request_id", "merge_requests", cascade=True),
    string("external_id"),
    Column("local_key", Text, unique=True),
    Column("is_resolved", Boolean),
    string("created_at"),
    string("resolved_at"),
    UniqueConstraint("merge_request_id", "external_id"),
)

comments = Table(
    "comments",
    metadata,
    pk(),
    fk("discussion_id", "discussions", cascade=True),
    fk("author_id", "users", optional=True),
    Column("parent_comment_id", Integer),
    string("external_id"),
    Column("local_key", Text, unique=True),
    string("body", required=True),
    string("summary"),
    string("category"),
    string("url"),
    string("created_at"),
    string("updated_at"),
    UniqueConstraint("discussion_id", "external_id"),
    UniqueConstraint("discussion_id", "id"),
    ForeignKeyConstraint(
        ["discussion_id", "parent_comment_id"], ["comments.discussion_id", "comments.id"]
    ),
)

code_positions = Table(
    "code_positions",
    metadata,
    pk(),
    fk("comment_id", "comments", cascade=True),
    string("old_path"),
    string("new_path"),
    Column("old_line", Integer),
    Column("new_line", Integer),
    string("base_sha"),
    string("start_sha"),
    string("head_sha"),
    string("code_context"),
    CheckConstraint("new_path IS NOT NULL OR old_path IS NOT NULL"),
    CheckConstraint("head_sha IS NOT NULL OR base_sha IS NOT NULL OR start_sha IS NOT NULL"),
)

commits = Table(
    "commits",
    metadata,
    pk(),
    fk("repository_id", "repositories"),
    string("sha", required=True),
    fk("author_id", "users", optional=True),
    string("message"),
    string("authored_at"),
    string("committed_at"),
    string("url"),
    string("summary"),
    UniqueConstraint("repository_id", "sha"),
)

mr_commits = Table(
    "mr_commits",
    metadata,
    fk("merge_request_id", "merge_requests", primary=True, cascade=True),
    fk("commit_id", "commits", primary=True, cascade=True),
)

changed_files = Table(
    "changed_files",
    metadata,
    pk(),
    fk("commit_id", "commits", cascade=True),
    string("old_path"),
    string("new_path"),
    string("change_type"),
    Column("additions", Integer),
    Column("deletions", Integer),
    string("diff"),
)

for table_name, target in (
    ("mr_dependencies", "merge_requests"),
    ("repository_dependencies", "repositories"),
    ("task_dependencies", "tasks"),
):
    Table(
        table_name,
        metadata,
        fk("entity_id", target, primary=True, cascade=True),
        fk("depends_on_id", target, primary=True, cascade=True),
        CheckConstraint("entity_id <> depends_on_id"),
    )

task_events = Table(
    "task_events",
    metadata,
    pk(),
    fk("task_id", "tasks", cascade=True),
    string("event_type", required=True),
    string("description"),
    string("created_at"),
    string("previous_value"),
    string("new_value"),
    string("source"),
)
test_runs = Table(
    "test_runs",
    metadata,
    pk(),
    fk("branch_id", "branches", cascade=True),
    string("command"),
    string("revision"),
    string("result"),
    string("output"),
    string("created_at"),
)

raw_documents = Table(
    "raw_documents",
    metadata,
    pk(),
    string("document_type", required=True),
    string("source_url"),
    string("content", required=True),
    string("content_hash", required=True),
    string("format", required=True),
    Column("created_at", Text, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    Column("metadata", JSON),
)
Index(
    "uq_document_origin",
    raw_documents.c.document_type,
    raw_documents.c.format,
    func.coalesce(raw_documents.c.source_url, ""),
    raw_documents.c.content_hash,
    unique=True,
)

document_links = Table(
    "document_links",
    metadata,
    pk(),
    fk("document_id", "raw_documents", cascade=True),
    fk("repository_id", "repositories", optional=True, cascade=True),
    fk("task_id", "tasks", optional=True, cascade=True),
    fk("merge_request_id", "merge_requests", optional=True, cascade=True),
    CheckConstraint(
        "(repository_id IS NOT NULL) + (task_id IS NOT NULL) + (merge_request_id IS NOT NULL) = 1"
    ),
    *(
        UniqueConstraint("document_id", key)
        for key in ("repository_id", "task_id", "merge_request_id")
    ),
)

task_merge_requests = Table(
    "task_merge_requests",
    metadata,
    fk("task_id", "tasks", primary=True, cascade=True),
    fk("merge_request_id", "merge_requests", primary=True, cascade=True),
)

mr_revisions = Table(
    "mr_revisions",
    metadata,
    pk(),
    fk("merge_request_id", "merge_requests", cascade=True),
    string("base_sha"),
    string("start_sha"),
    string("head_sha"),
    fk("source_document_id", "raw_documents", optional=True),
    string("created_at"),
    string("kind", required=True),
)

revision_commits = Table(
    "revision_commits",
    metadata,
    fk("revision_id", "mr_revisions", primary=True, cascade=True),
    fk("commit_id", "commits", primary=True),
)

legacy_values = Table(
    "legacy_values",
    metadata,
    Column("entity", Text, primary_key=True),
    Column("entity_id", Integer, primary_key=True),
    Column("field", Text, primary_key=True),
    string("original", required=True),
)

document_extractions = Table(
    "document_extractions",
    metadata,
    pk(),
    Column(
        "document_id",
        Integer,
        ForeignKey("raw_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    string("model", required=True),
    string("prompt_version", required=True),
    Column("proposal", JSON, nullable=False),
    string("status", required=True),
    string("created_at", required=True),
)
