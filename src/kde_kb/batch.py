"""Idempotent directory importer for copied task and Invent MR documents."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from . import models as m
from . import services as svc
from .database import CatalogError, transaction
from .extraction import inspect_document
from .import_documents import parse_mr, parse_task
from .importers import preserve_document
from .invent import Lines
from .providers import LLMProvider
from .schemas import MergeRequest, Repository, Task


def batch_import(
    database: Path,
    root: Path,
    *,
    extract: bool = False,
    provider: LLMProvider | None = None,
    dry_run: bool = False,
    progress=lambda _: None,
) -> dict:
    start = time.monotonic()
    files = sorted([*root.glob("mr/*.txt"), *root.glob("tasks/*.txt")])
    stats = {
        "files": len(files),
        "created": 0,
        "skipped": 0,
        "errors": 0,
        "empty_templates": 0,
        "links": 0,
        "llm_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "failed_comments": 0,
        "classified_comments": 0,
        "elapsed_seconds": 0,
    }
    for file in files:
        try:
            content = file.read_bytes().decode("utf-8")
            kind = "mr" if file.parent.name == "mr" else "task"
            parsed = parse_mr(content, file.name) if kind == "mr" else parse_task(content)
            digest = hashlib.sha256(content.encode()).hexdigest()
            if dry_run:
                stats["created"] += 1
                continue
            with transaction(database, write=True) as conn:
                existing = conn.execute(
                    select(m.raw_documents.c.id).where(
                        m.raw_documents.c.content_hash == digest,
                        m.raw_documents.c.document_type == kind,
                        m.raw_documents.c.format == "text",
                        m.raw_documents.c.source_name == file.name,
                    )
                ).scalar_one_or_none()
                is_empty_template = kind == "mr" and parsed.get("empty_template", False)
                if existing:
                    stats["skipped"] += 1
                    uid = existing
                else:
                    source_url = (
                        parsed.get("record", {}).get("url") if parsed.get("record") else None
                    )
                    uid = preserve_document(conn, kind, "text", content, source_url, file.name)
                    stats["created"] += 1
                    if is_empty_template:
                        stats["empty_templates"] += 1
                    elif kind == "mr":
                        repo = parsed["repository"]
                        svc.store(conn, Repository.model_validate(repo))
                        parsed["record"]["repo"] = repo["url"]
                        item = MergeRequest.model_validate(parsed["record"])
                        entity_id = svc.store(conn, item, document_id=uid, source=f"document:{uid}")
                        svc.attach(conn, uid, m.merge_requests, entity_id)
                        for bug in parsed["bug_ids"]:
                            _link_bug(conn, "mr", entity_id, bug)
                        for ref in parsed["mr_references"]:
                            _store_or_link(
                                conn,
                                "mr",
                                entity_id,
                                ref["reference"],
                                stats,
                                relation_type=ref["type"],
                            )
                    else:
                        item = Task.model_validate(parsed["record"])
                        entity_id = svc.store(conn, item, document_id=uid, source=f"document:{uid}")
                        svc.attach(conn, uid, m.tasks, entity_id)
                        for bug in parsed["bug_ids"]:
                            _link_bug(conn, "task", entity_id, bug)
                        for ref in parsed["links"]:
                            if "merge_requests/" in ref or "!" in ref:
                                _store_or_link(conn, "task", entity_id, ref, stats)
            if is_empty_template:
                progress(f"{file}: saved empty template as source only")
                continue
            if extract:
                _classify(database, uid, provider, stats, progress)
            progress(str(file))
        except (ValueError, OSError, UnicodeError, CatalogError) as exc:
            stats["errors"] += 1
            progress(f"{file}: {exc}")
    with transaction(database, write=True) as conn:
        stats["links"] += _resolve(conn)
    stats["elapsed_seconds"] = round(time.monotonic() - start, 3)
    return stats


def _classify(database, uid, provider, stats, progress):
    preview = inspect_document(database, uid, provider=provider, refresh=True, progress=progress)
    metrics = preview.get("metrics", {})
    stats["llm_calls"] += metrics.get("calls", 0)
    stats["input_tokens"] += metrics.get("input_tokens", 0)
    stats["output_tokens"] += metrics.get("output_tokens", 0)
    stats["failed_comments"] += metrics.get("failed_comments", 0)
    stats["classified_comments"] += _apply_comment_categories(database, uid, preview)


def _apply_comment_categories(database, document_id, preview):
    proposal = preview.get("proposal", {})
    refs = proposal.get("source_comments", [])
    if not refs:
        return 0
    updated = 0
    with transaction(database, write=True) as conn:
        document = svc.one(conn, m.raw_documents, id=document_id)
        link = conn.execute(
            select(m.document_links.c.merge_request_id).where(
                m.document_links.c.document_id == document_id,
                m.document_links.c.merge_request_id.is_not(None),
            )
        ).scalar_one_or_none()
        if link is None:
            return 0
        lines = document["content"].splitlines(keepends=True)
        comments = m.comments.join(m.discussions, m.comments.c.discussion_id == m.discussions.c.id)
        comments = comments.join(m.users, m.comments.c.author_id == m.users.c.id)
        for ref in refs:
            if not ref.get("categories"):
                continue
            body = Lines.model_validate(ref["lines"]).read(lines)
            candidates = conn.execute(
                select(m.comments.c.id, m.comments.c.source_start_line)
                .select_from(comments)
                .where(
                    m.discussions.c.merge_request_id == link,
                    m.comments.c.body == body,
                    m.users.c.username == ref["author"]["username"],
                )
            ).all()
            source_line = ref["lines"]["first"]
            match = next((row for row in candidates if row.source_start_line == source_line), None)
            if match is None and len(candidates) == 1:
                match = candidates[0]
            if match:
                conn.execute(
                    m.comments.update()
                    .where(m.comments.c.id == match.id)
                    .values(category=", ".join(ref["categories"]))
                )
                updated += 1
    return updated


def _link_bug(conn, kind, uid, number):
    number = int(number)
    conn.execute(
        insert(m.bug_reports)
        .values(external_id=number, url=f"https://bugs.kde.org/show_bug.cgi?id={number}")
        .on_conflict_do_nothing()
    )
    bug_id = conn.execute(
        select(m.bug_reports.c.id).where(m.bug_reports.c.external_id == number)
    ).scalar_one()
    table = m.task_bug_reports if kind == "task" else m.mr_bug_reports
    col = "task_id" if kind == "task" else "merge_request_id"
    conn.execute(
        insert(table).values(**{col: uid, "bug_report_id": bug_id}).on_conflict_do_nothing()
    )


def _store_or_link(conn, kind, uid, reference, stats, *, relation_type="mr"):
    from .import_documents import resolve_mr_ref

    parsed = resolve_mr_ref(reference)
    if not parsed:
        return
    repo_url, number = parsed
    repo_id = (
        conn.execute(
            select(m.repositories.c.id).where(m.repositories.c.url == repo_url)
        ).scalar_one_or_none()
        if repo_url
        else None
    )
    mr_id = conn.execute(
        select(m.merge_requests.c.id).where(
            m.merge_requests.c.external_id == number,
            *([m.merge_requests.c.target_repository_id == repo_id] if repo_id else []),
        )
    ).scalar_one_or_none()
    if not mr_id:
        conn.execute(
            insert(m.unresolved_links)
            .values(
                source_type=kind,
                source_id=uid,
                link_type=relation_type,
                repository_url=repo_url,
                external_id=number,
            )
            .on_conflict_do_nothing()
        )
        return
    if kind == "task":
        was_linked = conn.execute(
            select(m.task_merge_requests.c.task_id).where(
                m.task_merge_requests.c.task_id == uid,
                m.task_merge_requests.c.merge_request_id == mr_id,
            )
        ).first()
        svc.link_mr(conn, uid, mr_id, "import")
        if was_linked is None:
            stats["links"] += 1
    else:
        result = conn.execute(
            insert(m.mr_relations)
            .values(
                merge_request_id=uid,
                related_merge_request_id=mr_id,
                relation_type=relation_type,
            )
            .on_conflict_do_nothing()
        )
        stats["links"] += bool(result.rowcount)
        if relation_type == "depends_on":
            conn.execute(
                insert(m.metadata.tables["mr_dependencies"])
                .values(entity_id=uid, depends_on_id=mr_id)
                .on_conflict_do_nothing()
            )


def _resolve(conn):
    pending = conn.execute(select(m.unresolved_links)).mappings().all()
    count = 0
    for link in pending:
        query = select(m.merge_requests.c.id).where(
            m.merge_requests.c.external_id == link["external_id"]
        )
        if link["repository_url"]:
            repo = conn.execute(
                select(m.repositories.c.id).where(m.repositories.c.url == link["repository_url"])
            ).scalar_one_or_none()
            if repo is None:
                continue
            query = query.where(m.merge_requests.c.target_repository_id == repo)
        matches = conn.execute(query).scalars().all()
        if len(matches) != 1:
            continue
        mr_id = matches[0]
        if link["source_type"] == "task":
            svc.link_mr(conn, link["source_id"], mr_id, "import")
        else:
            relation_type = link["link_type"] if link["link_type"] != "mr" else "depends_on"
            conn.execute(
                insert(m.mr_relations)
                .values(
                    merge_request_id=link["source_id"],
                    related_merge_request_id=mr_id,
                    relation_type=relation_type,
                )
                .on_conflict_do_nothing()
            )
            if relation_type == "depends_on":
                conn.execute(
                    insert(m.metadata.tables["mr_dependencies"])
                    .values(entity_id=link["source_id"], depends_on_id=mr_id)
                    .on_conflict_do_nothing()
                )
        conn.execute(m.unresolved_links.delete().where(m.unresolved_links.c.id == link["id"]))
        count += 1
    return count
