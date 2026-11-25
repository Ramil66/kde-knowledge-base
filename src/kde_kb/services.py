from __future__ import annotations

from typing import Any

from sqlalchemy import Table, insert, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection

from . import models as m
from .database import CatalogError
from .lifecycle import STATES, now, state, transition
from .schemas import Discussion, MergeRequest, Repository, Task, User, canonical_url

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


def task_event(
    conn: Connection,
    task_id: int,
    event: str,
    source: str,
    previous: object = None,
    new: object = None,
) -> None:
    conn.execute(
        m.task_events.insert().values(
            task_id=task_id,
            event_type=event,
            source=source,
            previous_value=previous,
            new_value=new,
            created_at=now(),
        )
    )


def save(
    conn: Connection,
    table: Table,
    values: Record,
    key: Record | None = None,
    *,
    create_only: bool = False,
    source: str = "manual",
) -> int:
    """Resolve a natural key inside the caller's immediate write transaction."""
    existing = (
        conn.execute(select(table).filter_by(**key)).mappings().one_or_none() if key else None
    )
    values = dict(values)
    if table.name in STATES and "status" in values:
        raw = values["status"]
        values["external_status"] = raw if source != "manual" else None
        values["status"] = state(table.name, raw, external=source != "manual") if raw else None
        transition(table.name, existing["status"] if existing else None, values["status"])
    if existing is None:
        if table is m.raw_documents:
            values["created_at"] = values.get("created_at") or now()
        uid = int(conn.execute(insert(table).values(**values)).inserted_primary_key[0])
        if table is m.tasks:
            task_event(conn, uid, "created", source, new=values.get("status"))
        return uid
    if create_only:
        raise CatalogError(f"A matching record already exists in {table.name}")
    changes = {k: v for k, v in values.items() if existing[k] != v}
    if source.startswith("extraction:"):
        conflicting = [k for k in changes if existing[k] is not None and k not in ("updated_at",)]
        if conflicting:
            raise CatalogError(
                f"Reviewed data in {table.name} conflicts on {', '.join(conflicting)}. Original retained."
            )
        changes.pop("updated_at", None)
    if changes:
        conn.execute(table.update().where(table.c.id == existing["id"]).values(**changes))
        if table is m.tasks:
            for field, label in (
                ("status", "status_changed"),
                ("priority", "priority_changed"),
                ("solution_summary", "solution_changed"),
                ("completed_at", "completion_date_changed"),
            ):
                if field in changes:
                    if field == "status":
                        if changes[field] == "completed":
                            label = "completed"
                        elif existing[field] == "completed":
                            label = "reopened"
                    if field == "completed_at" and "status" not in changes:
                        if existing[field] is None:
                            label = "completed"
                        elif changes[field] is None:
                            label = "reopened"
                    task_event(conn, existing["id"], label, source, existing[field], changes[field])
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
    result = conn.execute(
        sqlite_insert(m.task_branches)
        .values(task_id=task["id"], branch_id=branch_id)
        .on_conflict_do_nothing()
    )
    if result.rowcount:
        task_event(conn, task["id"], "branch_linked", "manual", new=branch_id)
        for mr_id in conn.execute(
            select(m.merge_requests.c.id).filter_by(branch_id=branch_id)
        ).scalars():
            link_mr(conn, task["id"], mr_id, "manual")
    return branch_id


def link_mr(conn: Connection, task_id: int, mr_id: int, source: str) -> None:
    result = conn.execute(
        sqlite_insert(m.task_merge_requests)
        .values(task_id=task_id, merge_request_id=mr_id)
        .on_conflict_do_nothing()
    )
    if result.rowcount:
        task_event(conn, task_id, "mr_linked", source, new=mr_id)


def author_values(conn: Connection, author: User | None, source: str) -> Record:
    if author is None:
        return {}
    values = author.model_dump(mode="json", exclude_unset=True)
    return {
        "author_id": save(
            conn,
            m.users,
            values,
            {"platform": author.platform, "username": author.username},
            source=source,
        )
    }


def save_discussion(
    conn: Connection, mr_id: int, discussion: Discussion, scope: str, source: str
) -> None:
    key = {"merge_request_id": mr_id}
    if discussion.external_id is None:
        key["local_key"] = scope
    else:
        key["external_id"] = discussion.external_id
    values = discussion.model_dump(mode="json", exclude_unset=True, exclude={"comments"})
    discussion_id = save(conn, m.discussions, values | key, key, source=source)
    ids: list[int] = []
    for index, comment in enumerate(discussion.comments):
        key = {"discussion_id": discussion_id}
        if comment.external_id is None:
            key["local_key"] = f"{scope}:comment:{index}"
        else:
            key["external_id"] = comment.external_id
        values = comment.model_dump(
            mode="json",
            exclude_unset=True,
            exclude={"author", "parent_external_id", "parent_index", "position"},
        )
        uid = save(
            conn,
            m.comments,
            values | key | author_values(conn, comment.author, source),
            key,
            source=source,
        )
        if uid in ids:
            raise CatalogError("Duplicate comment ID")
        ids.append(uid)
        if comment.position:
            data = comment.position.model_dump(mode="json") | {"comment_id": uid}
            save(conn, m.code_positions, data, data)

    for uid, comment in zip(ids, discussion.comments, strict=True):
        if comment.parent_external_id:
            parent = one(
                conn,
                m.comments,
                discussion_id=discussion_id,
                external_id=comment.parent_external_id,
            )["id"]
        elif comment.parent_index is not None and comment.parent_index < len(ids):
            parent = ids[comment.parent_index]
        elif comment.parent_index is None:
            continue
        else:
            raise CatalogError("Parent index is outside this discussion")
        if parent == uid:
            raise CatalogError("A comment cannot reply to itself")
        save(conn, m.comments, {"parent_comment_id": parent}, {"id": uid}, source=source)


def add_dependency(conn: Connection, table: Table, entity_id: int, depends_on: int) -> None:
    conn.execute(
        sqlite_insert(table)
        .values(entity_id=entity_id, depends_on_id=depends_on)
        .on_conflict_do_nothing()
    )


def store(
    conn: Connection,
    item: Repository | Task | MergeRequest,
    *,
    create_only: bool = False,
    document_id: int | None = None,
    source: str = "manual",
    local_id: int | None = None,
) -> int:
    if isinstance(item, Repository):
        repo_id = save(
            conn,
            m.repositories,
            item.model_dump(mode="json", exclude_unset=True, exclude={"depends_on"}),
            {"url": item.url},
            create_only=create_only,
            source=source,
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
        if local_id is not None and not item.external_id:
            key = {"id": local_id}
        task_id = save(
            conn,
            m.tasks,
            item.model_dump(mode="json", exclude_unset=True, exclude={"events", "depends_on"}),
            key,
            create_only=create_only,
            source=source,
        )
        for event in item.events:
            data = event.model_dump(mode="json") | {"task_id": task_id}
            save(conn, m.task_events, data, data)
        for ref in item.depends_on:
            add_dependency(
                conn, m.metadata.tables["task_dependencies"], task_id, resolve_task(conn, ref)["id"]
            )
        return task_id
    repo_id = resolve_repo(conn, item.repo)["id"]
    branch_id = (
        branch(conn, repo_id, item.source_branch, item.source_repository_url)
        if item.source_branch and item.source_repository_url
        else None
    )
    key = {"target_repository_id": repo_id, "external_id": item.external_id}
    values = item.model_dump(
        mode="json",
        exclude_unset=True,
        exclude={
            "repo",
            "source_repository_url",
            "discussions",
            "author",
            "commits",
            "depends_on",
            "test_runs",
            "branch_status",
            "revision",
            "tasks",
        },
    )
    mr_id = save(
        conn,
        m.merge_requests,
        {"target_repository_id": repo_id}
        | values
        | ({"branch_id": branch_id} if branch_id else {})
        | author_values(conn, item.author, source),
        key
        if item.external_id is not None
        else ({"id": local_id} if local_id is not None else None),
        create_only=create_only,
        source=source,
    )
    if (item.branch_status or item.test_runs) and not branch_id:
        raise CatalogError("Branch status and test runs require an identified branch")
    if item.branch_status:
        save(conn, m.branches, {"status": item.branch_status}, {"id": branch_id}, source=source)
    task_ids = set(
        conn.execute(select(m.task_branches.c.task_id).filter_by(branch_id=branch_id)).scalars()
    )
    task_ids.update(resolve_task(conn, ref)["id"] for ref in item.tasks)
    for task_id in task_ids:
        link_mr(conn, task_id, mr_id, source)
    for ref in item.depends_on:
        add_dependency(
            conn,
            m.metadata.tables["mr_dependencies"],
            mr_id,
            mr_record(conn, ref.repo, ref.external_id)["id"],
        )
    for run in item.test_runs:
        data = run.model_dump(mode="json") | {"branch_id": branch_id}
        save(conn, m.test_runs, data, data)
    for index, discussion in enumerate(item.discussions):
        stable_thread = discussion.external_id or str(index)
        scope = f"mr:{mr_id}:discussion:{stable_thread}"
        save_discussion(conn, mr_id, discussion, scope, source)
    revision_id = None
    if item.revision or item.commits or "commits" in item.model_fields_set:
        complete = bool(item.revision and item.revision.complete)
        if complete:
            conn.execute(m.mr_commits.delete().where(m.mr_commits.c.merge_request_id == mr_id))
        attributes = (
            item.revision.model_dump(mode="json", exclude={"complete"}) if item.revision else {}
        )
        revision_id = save(
            conn,
            m.mr_revisions,
            attributes
            | {
                "merge_request_id": mr_id,
                "source_document_id": document_id,
                "kind": "complete" if complete else "partial",
                "created_at": now(),
            },
        )
    for commit in item.commits:
        commit_repo = resolve_repo(conn, commit.repository)["id"]
        commit_key = {"repository_id": commit_repo, "sha": commit.sha}
        values = commit.model_dump(
            mode="json", exclude_unset=True, exclude={"repository", "author", "changed_files"}
        )
        commit_id = save(
            conn,
            m.commits,
            commit_key | values | author_values(conn, commit.author, source),
            commit_key,
            source=source,
        )
        conn.execute(
            sqlite_insert(m.mr_commits)
            .values(merge_request_id=mr_id, commit_id=commit_id)
            .on_conflict_do_nothing()
        )
        conn.execute(
            sqlite_insert(m.revision_commits)
            .values(revision_id=revision_id, commit_id=commit_id)
            .on_conflict_do_nothing()
        )
        for changed in commit.changed_files:
            file_key = {
                "commit_id": commit_id,
                "old_path": changed.old_path,
                "new_path": changed.new_path,
            }
            save(
                conn,
                m.changed_files,
                file_key | changed.model_dump(mode="json", exclude_unset=True),
                file_key,
                source=source,
            )
    return mr_id


def update_fields(conn: Connection, table: Table, current: Record, changes: Record) -> int:
    if not changes:
        raise CatalogError("Specify at least one field to update")
    if table is m.repositories:
        Repository.model_validate(
            {
                k: v
                for k, v in (current | changes).items()
                if k in Repository.model_fields and (not k.endswith("_at") or k in changes)
            }
        )
    elif table is m.tasks:
        Task.model_validate(
            {
                k: v
                for k, v in (current | changes).items()
                if k in Task.model_fields and (not k.endswith("_at") or k in changes)
            }
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


def mr_detail(conn: Connection, repo: str, number: int, *, local: bool = False) -> Record:
    mr = (
        one(conn, m.merge_requests, id=number, target_repository_id=resolve_repo(conn, repo)["id"])
        if local
        else mr_record(conn, repo, number)
    )
    mr["identity_origin"] = "local" if mr["external_id"] is None else "external"
    mr["branch"] = one(conn, m.branches, id=mr["branch_id"]) if mr["branch_id"] else None
    mr["test_runs"] = rows(conn, m.test_runs, branch_id=mr["branch_id"])
    mr["discussions"] = rows(conn, m.discussions, merge_request_id=mr["id"])
    for discussion in mr["discussions"]:
        discussion["comments"] = rows(conn, m.comments, discussion_id=discussion["id"])
        for comment in discussion["comments"]:
            if comment["author_id"]:
                comment["author"] = one(conn, m.users, id=comment["author_id"])
            comment["positions"] = rows(conn, m.code_positions, comment_id=comment["id"])
    mr["revisions"] = rows(conn, m.mr_revisions, merge_request_id=mr["id"])
    for snapshot in mr["revisions"]:
        snapshot["commit_ids"] = [
            r["commit_id"] for r in rows(conn, m.revision_commits, revision_id=snapshot["id"])
        ]
    mr["commits"] = [
        one(conn, m.commits, id=link["commit_id"])
        for link in rows(conn, m.mr_commits, merge_request_id=mr["id"])
    ]
    for commit in mr["commits"]:
        commit["changed_files"] = rows(conn, m.changed_files, commit_id=commit["id"])
    mr = with_documents(conn, m.merge_requests, mr)
    proposals = conn.execute(
        select(m.document_extractions.c.document_id, m.document_extractions.c.proposal).where(
            m.document_extractions.c.document_id.in_(mr["document_ids"]),
            m.document_extractions.c.status == "confirmed",
        )
    )
    mr["extracted_evidence"] = [
        {
            "document_id": uid,
            **{k: v for k, v in proposal.items() if k not in ("record", "repository")},
        }
        for uid, proposal in proposals
    ]
    return mr


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


def context_rows(conn: Connection, query, limit: int) -> list[Record]:
    result = conn.execute(query.limit(limit)).mappings()
    return [
        {k: (v[:1023] + "…" if isinstance(v, str) and len(v) > 1024 else v) for k, v in row.items()}
        for row in result
    ]


def context_limits(depth: int, limit: int) -> None:
    if not 0 <= depth <= 2 or not 1 <= limit <= 100:
        raise CatalogError("Use depth 0–2 and limit 1–100")


def dependencies(conn: Connection, entity: Table, links: Table, uid: int, limit: int) -> Record:
    return {
        name: context_rows(
            conn,
            select(entity)
            .where(entity.c.id.in_(select(links.c[target]).where(links.c[origin] == uid)))
            .order_by(entity.c.id),
            limit,
        )
        for name, origin, target in (
            ("dependencies", "entity_id", "depends_on_id"),
            ("dependents", "depends_on_id", "entity_id"),
        )
    }


def task_detail(conn: Connection, ref: str, depth: int = 2, limit: int = 20) -> Record:
    context_limits(depth, limit)
    task = resolve_task(conn, ref)
    task["identity_origin"] = "local" if task["external_id"] is None else "external"
    uid = task["id"]
    if not depth:
        return task
    mr_ids = select(m.task_merge_requests.c.merge_request_id).filter_by(task_id=uid)
    branch_ids = (
        select(m.task_branches.c.branch_id)
        .filter_by(task_id=uid)
        .union(select(m.merge_requests.c.branch_id).where(m.merge_requests.c.id.in_(mr_ids)))
    )
    repo_ids = (
        select(m.branches.c.repository_id)
        .where(m.branches.c.id.in_(branch_ids))
        .union(
            select(m.merge_requests.c.target_repository_id).where(m.merge_requests.c.id.in_(mr_ids))
        )
    )
    for key, table, condition in (
        ("branches", m.branches, m.branches.c.id.in_(branch_ids)),
        ("events", m.task_events, m.task_events.c.task_id == uid),
        ("repositories", m.repositories, m.repositories.c.id.in_(repo_ids)),
        ("merge_requests", m.merge_requests, m.merge_requests.c.id.in_(mr_ids)),
    ):
        task[key] = context_rows(conn, select(table).where(condition).order_by(table.c.id), limit)
    task.update(dependencies(conn, m.tasks, m.metadata.tables["task_dependencies"], uid, limit))
    sources = (
        select(m.raw_documents.c.id, m.raw_documents.c.source_url)
        .join(m.document_links)
        .where(
            (m.document_links.c.task_id == uid) | m.document_links.c.merge_request_id.in_(mr_ids)
        )
        .distinct()
        .order_by(m.raw_documents.c.id)
    )
    task["sources"] = context_rows(conn, sources, limit)
    task["document_ids"] = list(
        conn.execute(
            select(m.document_links.c.document_id)
            .filter_by(task_id=uid)
            .order_by(m.document_links.c.document_id)
            .limit(limit)
        ).scalars()
    )
    if depth == 2:
        for key, table, condition in (
            ("discussions", m.discussions, m.discussions.c.merge_request_id.in_(mr_ids)),
            (
                "comments",
                m.comments,
                m.comments.c.discussion_id.in_(
                    select(m.discussions.c.id).where(m.discussions.c.merge_request_id.in_(mr_ids))
                ),
            ),
            (
                "commits",
                m.commits,
                m.commits.c.id.in_(
                    select(m.mr_commits.c.commit_id).where(
                        m.mr_commits.c.merge_request_id.in_(mr_ids)
                    )
                ),
            ),
            ("test_runs", m.test_runs, m.test_runs.c.branch_id.in_(branch_ids)),
        ):
            task[key] = context_rows(
                conn, select(table).where(condition).order_by(table.c.id), limit
            )
    return task


def repo_detail(conn: Connection, ref: str, depth: int = 2, limit: int = 20) -> Record:
    context_limits(depth, limit)
    repo = resolve_repo(conn, ref)
    uid = repo["id"]
    if not depth:
        return repo
    branch_ids = select(m.branches.c.id).filter_by(repository_id=uid)
    mr_ids = select(m.merge_requests.c.id).filter_by(target_repository_id=uid)
    repo["branches"] = context_rows(
        conn,
        select(m.branches)
        .where(
            m.branches.c.repository_id == uid,
            m.branches.c.status.is_(None) | m.branches.c.status.in_(["active", "unknown"]),
        )
        .order_by(m.branches.c.id),
        limit,
    )
    repo["merge_requests"] = {
        status: context_rows(
            conn,
            select(m.merge_requests)
            .where(
                m.merge_requests.c.target_repository_id == uid,
                (m.merge_requests.c.status.is_(None) | (m.merge_requests.c.status == "unknown"))
                if status == "unknown"
                else m.merge_requests.c.status == status,
            )
            .order_by(m.merge_requests.c.id),
            limit,
        )
        for status in ("open", "merged", "closed", "unknown")
    }
    repo.update(
        dependencies(conn, m.repositories, m.metadata.tables["repository_dependencies"], uid, limit)
    )
    if depth == 2:
        task_ids = (
            select(m.task_branches.c.task_id)
            .where(m.task_branches.c.branch_id.in_(branch_ids))
            .union(
                select(m.task_merge_requests.c.task_id).where(
                    m.task_merge_requests.c.merge_request_id.in_(mr_ids)
                )
            )
        )
        repo["tasks"] = context_rows(
            conn, select(m.tasks).where(m.tasks.c.id.in_(task_ids)).order_by(m.tasks.c.id), limit
        )
    return repo
