from __future__ import annotations

from typing import Any

from sqlalchemy import Table, func, insert, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection

from . import models as m
from .database import CatalogError
from .schemas import Comment, Discussion, MergeRequest, Repository, Task, User, canonical_url

type Record = dict[str, Any]


def rows(conn: Connection, table: Table, **filters: Any) -> list[Record]:
    query = select(table).filter_by(**filters).order_by(*table.primary_key.columns)
    return [dict(row) for row in conn.execute(query).mappings()]


def one(conn: Connection, table: Table, **filters: Any) -> Record:
    result = conn.execute(select(table).filter_by(**filters)).mappings().one_or_none()
    if result is None:
        raise CatalogError(f"No matching record in {table.name}: {filters}")
    return dict(result)


def resolve_repo(conn: Connection, ref: str) -> Record:
    if "://" in ref or ref.startswith("git@"):
        return one(conn, m.repositories, url=canonical_url(ref))
    matches = rows(conn, m.repositories, name=ref)
    if len(matches) != 1:
        raise CatalogError(
            f"Expected one repository named {ref!r}, found {len(matches)}. Use its URL."
        )
    return matches[0]


def resolve_task(conn: Connection, ref: str) -> Record:
    if ref.startswith("#") and ref[1:].isdecimal():
        return one(conn, m.tasks, id=int(ref[1:]))
    if ":" in ref:
        source, external_id = ref.split(":", 1)
        return one(conn, m.tasks, source=source, external_id=external_id)
    matches = rows(conn, m.tasks, external_id=ref)
    if len(matches) > 1:
        raise CatalogError("Ambiguous task ID. Use source:external_id or #local_id.")
    if matches:
        return matches[0]
    if ref.isdecimal():
        return one(conn, m.tasks, id=int(ref))
    raise CatalogError(f"Task {ref!r} not found")


def save(
    conn: Connection,
    table: Table,
    values: Record,
    key: Record | None = None,
    *,
    create_only: bool = False,
) -> int:
    """Resolve a natural key inside the caller's immediate write transaction."""
    existing = (
        conn.execute(select(table).filter_by(**key)).mappings().one_or_none() if key else None
    )
    if existing is None:
        return int(conn.execute(insert(table).values(**values)).inserted_primary_key[0])
    if create_only:
        raise CatalogError(f"A matching record already exists in {table.name}")
    changes = {k: v for k, v in values.items() if existing[k] != v}
    if changes:
        if table in (m.repositories, m.tasks, m.merge_requests) and "updated_at" not in values:
            changes["updated_at"] = func.current_timestamp()
        conn.execute(table.update().where(table.c.id == existing["id"]).values(**changes))
    return int(existing["id"])


def branch(conn: Connection, repository_id: int, name: str, source_url: str) -> int:
    if not name.strip():
        raise CatalogError("Branch name is required")
    key = {
        "repository_id": repository_id,
        "name": name,
        "source_repository_url": canonical_url(source_url),
    }
    return save(conn, m.branches, key, key)


def link_branch(
    conn: Connection, task_ref: str, repo_ref: str, name: str, source_url: str | None
) -> int:
    repo = resolve_repo(conn, repo_ref)
    task = resolve_task(conn, task_ref)
    branch_id = branch(conn, repo["id"], name, source_url or repo["url"])
    conn.execute(
        sqlite_insert(m.task_branches)
        .values(task_id=task["id"], branch_id=branch_id)
        .on_conflict_do_nothing()
    )
    return branch_id


def author_values(conn: Connection, author: User | None) -> Record:
    if author is None:
        return {}
    values = author.model_dump(exclude_unset=True)
    return {
        "author_id": save(
            conn, m.users, values, {"platform": author.platform, "username": author.username}
        )
    }


def save_comments(conn: Connection, discussion_id: int, comments: list[Comment]) -> None:
    ids: dict[str, int] = {}
    for comment in comments:
        if comment.external_id in ids:
            raise CatalogError(f"Duplicate comment ID: {comment.external_id}")
        key = {"discussion_id": discussion_id, "external_id": comment.external_id}
        values = comment.model_dump(
            exclude_unset=True, exclude={"author", "parent_external_id", "position"}
        )
        comment_id = save(conn, m.comments, values | key | author_values(conn, comment.author), key)
        ids[comment.external_id] = comment_id
        if comment.position:
            position = comment.position.model_dump()
            save(
                conn,
                m.code_positions,
                position | {"comment_id": comment_id},
                position | {"comment_id": comment_id},
            )

    for comment in comments:
        if comment.parent_external_id:
            parent = one(
                conn,
                m.comments,
                discussion_id=discussion_id,
                external_id=comment.parent_external_id,
            )
            if parent["id"] == ids[comment.external_id]:
                raise CatalogError("A comment cannot reply to itself")
            conn.execute(
                m.comments.update()
                .where(m.comments.c.id == ids[comment.external_id])
                .values(parent_comment_id=parent["id"])
            )


def save_discussion(conn: Connection, mr_id: int, discussion: Discussion) -> None:
    key = {"merge_request_id": mr_id, "external_id": discussion.external_id}
    values = discussion.model_dump(exclude_unset=True, exclude={"comments"})
    discussion_id = save(conn, m.discussions, values | key, key)
    save_comments(conn, discussion_id, discussion.comments)


def add_dependency(conn: Connection, table: Table, entity_id: int, depends_on: int) -> None:
    conn.execute(
        sqlite_insert(table)
        .values(entity_id=entity_id, depends_on_id=depends_on)
        .on_conflict_do_nothing()
    )


def store(
    conn: Connection, item: Repository | Task | MergeRequest, *, create_only: bool = False
) -> int:
    if isinstance(item, Repository):
        repo_id = save(
            conn,
            m.repositories,
            item.model_dump(exclude_unset=True, exclude={"depends_on"}),
            {"url": item.url},
            create_only=create_only,
        )
        for ref in item.depends_on:
            add_dependency(
                conn,
                m.metadata.tables["repository_dependencies"],
                repo_id,
                resolve_repo(conn, ref)["id"],
            )
        return repo_id
    if isinstance(item, Task):
        key = {"source": item.source, "external_id": item.external_id} if item.external_id else None
        task_id = save(
            conn,
            m.tasks,
            item.model_dump(exclude_unset=True, exclude={"events", "depends_on"}),
            key,
            create_only=create_only,
        )
        for event in item.events:
            data = event.model_dump() | {"task_id": task_id}
            save(conn, m.task_events, data, data)
        for ref in item.depends_on:
            add_dependency(
                conn, m.metadata.tables["task_dependencies"], task_id, resolve_task(conn, ref)["id"]
            )
        return task_id
    repo_id = resolve_repo(conn, item.repo)["id"]
    branch_id = branch(conn, repo_id, item.source_branch, item.source_repository_url)
    key = {"target_repository_id": repo_id, "external_id": item.external_id}
    values = item.model_dump(
        exclude_unset=True,
        exclude={
            "repo",
            "source_repository_url",
            "discussions",
            "author",
            "commits",
            "depends_on",
            "test_runs",
        },
    )
    mr_id = save(
        conn,
        m.merge_requests,
        key | values | {"branch_id": branch_id} | author_values(conn, item.author),
        key,
        create_only=create_only,
    )
    for ref in item.depends_on:
        add_dependency(
            conn,
            m.metadata.tables["mr_dependencies"],
            mr_id,
            mr_record(conn, ref.repo, ref.external_id)["id"],
        )
    for run in item.test_runs:
        data = run.model_dump() | {"branch_id": branch_id}
        save(conn, m.test_runs, data, data)
    for discussion in item.discussions:
        save_discussion(conn, mr_id, discussion)
    for commit in item.commits:
        commit_repo = resolve_repo(conn, commit.repository)["id"]
        commit_key = {"repository_id": commit_repo, "sha": commit.sha}
        values = commit.model_dump(
            exclude_unset=True, exclude={"repository", "author", "changed_files"}
        )
        commit_id = save(
            conn, m.commits, commit_key | values | author_values(conn, commit.author), commit_key
        )
        conn.execute(
            sqlite_insert(m.mr_commits)
            .values(merge_request_id=mr_id, commit_id=commit_id)
            .on_conflict_do_nothing()
        )
        for changed in commit.changed_files:
            file_key = {
                "commit_id": commit_id,
                "old_path": changed.old_path,
                "new_path": changed.new_path,
            }
            save(conn, m.changed_files, file_key | changed.model_dump(exclude_unset=True), file_key)
    return mr_id


def update_fields(conn: Connection, table: Table, current: Record, changes: Record) -> int:
    if not changes:
        raise CatalogError("Specify at least one field to update")
    if table is m.repositories:
        Repository.model_validate(
            {k: v for k, v in (current | changes).items() if k in Repository.model_fields}
        )
    elif table is m.tasks:
        Task.model_validate(
            {k: v for k, v in (current | changes).items() if k in Task.model_fields}
        )
    elif table is m.merge_requests and "title" in changes and not changes["title"].strip():
        raise CatalogError("Title must contain text")
    return save(conn, table, changes, {"id": current["id"]})


def mr_record(conn: Connection, repo: str, number: int) -> Record:
    return one(
        conn,
        m.merge_requests,
        target_repository_id=resolve_repo(conn, repo)["id"],
        external_id=number,
    )


def mr_detail(conn: Connection, repo: str, number: int) -> Record:
    mr = mr_record(conn, repo, number)
    mr["branch"] = one(conn, m.branches, id=mr["branch_id"])
    mr["test_runs"] = rows(conn, m.test_runs, branch_id=mr["branch_id"])
    mr["discussions"] = rows(conn, m.discussions, merge_request_id=mr["id"])
    for discussion in mr["discussions"]:
        discussion["comments"] = rows(conn, m.comments, discussion_id=discussion["id"])
        for comment in discussion["comments"]:
            if comment["author_id"]:
                comment["author"] = one(conn, m.users, id=comment["author_id"])
            comment["positions"] = rows(conn, m.code_positions, comment_id=comment["id"])
    mr["commits"] = [
        one(conn, m.commits, id=link["commit_id"])
        for link in rows(conn, m.mr_commits, merge_request_id=mr["id"])
    ]
    for commit in mr["commits"]:
        commit["changed_files"] = rows(conn, m.changed_files, commit_id=commit["id"])
    return with_documents(conn, m.merge_requests, mr)


def link_column(table: Table) -> str:
    if table is m.repositories:
        return "repository_id"
    if table is m.tasks:
        return "task_id"
    if table is m.merge_requests:
        return "merge_request_id"
    raise CatalogError("Unsupported document link")


def attach(conn: Connection, document_id: int, table: Table, entity_id: int) -> None:
    one(conn, m.raw_documents, id=document_id)
    one(conn, table, id=entity_id)
    conn.execute(
        sqlite_insert(m.document_links)
        .values(document_id=document_id, **{link_column(table): entity_id})
        .on_conflict_do_nothing()
    )


def with_documents(conn: Connection, table: Table, entity: Record) -> Record:
    links = rows(conn, m.document_links, **{link_column(table): entity["id"]})
    return entity | {"document_ids": [link["document_id"] for link in links]}


def delete_record(conn: Connection, table: Table, record: Record) -> None:
    conn.execute(table.delete().where(table.c.id == record["id"]))


def task_detail(conn: Connection, ref: str) -> Record:
    task = resolve_task(conn, ref)
    task["events"] = rows(conn, m.task_events, task_id=task["id"])
    task["branches"] = [
        one(conn, m.branches, id=link["branch_id"])
        for link in rows(conn, m.task_branches, task_id=task["id"])
    ]
    return with_documents(conn, m.tasks, task)
