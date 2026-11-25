from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from time import monotonic
from typing import Annotated, Any, cast

import typer
from alembic.util import CommandError
from rich.console import Console
from sqlalchemy.exc import SQLAlchemyError

from . import models as m
from . import services as svc
from .batch import batch_import
from .database import (
    CatalogError,
    backup,
    default_path,
    head,
    migrate_database,
    revision,
    transaction,
)
from .exporters import export_json
from .extraction import confirm_document, document_list, inspect_document
from .importers import Format, Kind, detect_format, import_document, preserve_document
from .retrieval import search
from .schemas import MergeRequest, Repository, Task

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
db = typer.Typer(no_args_is_help=True)
repo = typer.Typer(no_args_is_help=True)
task = typer.Typer(no_args_is_help=True)
mr = typer.Typer(no_args_is_help=True)
imports = typer.Typer(no_args_is_help=True)
document = typer.Typer(no_args_is_help=True)
for name, group in (
    ("db", db),
    ("repo", repo),
    ("task", task),
    ("mr", mr),
    ("import", imports),
    ("document", document),
):
    app.add_typer(group, name=name)


def output(data: Any) -> None:
    Console().print_json(data=data, indent=2, ensure_ascii=False)


def required(value: str | None, prompt: str) -> str:
    return value if value is not None else typer.prompt(prompt)


def provided(**fields: Any) -> svc.Record:
    return {k: v for k, v in fields.items() if v is not None}


@app.callback()
def root(
    ctx: typer.Context,
    database: Annotated[Path | None, typer.Option(envvar="KDE_KB_DATABASE")] = None,
) -> None:
    """Manage a local knowledge base for KDE development."""
    ctx.obj = (database or default_path()).expanduser()


@app.command("init")
@db.command("migrate")
def migrate(ctx: typer.Context) -> None:
    """Create the database or apply pending migrations."""
    migrate_database(ctx.obj)
    output({"database": str(ctx.obj), "revision": head()})


@db.command("status")
def database_status(ctx: typer.Context) -> None:
    current = None
    if ctx.obj.is_file():
        with transaction(ctx.obj, check_revision=False) as conn:
            current = revision(conn)
    output(
        {
            "database": str(ctx.obj),
            "revision": current,
            "head": head(),
            "initialized": current is not None,
        }
    )


@db.command("backup")
def database_backup(
    ctx: typer.Context, output_path: Annotated[Path | None, typer.Option("--output")] = None
) -> None:
    """Write a consistent SQLite backup to a new file."""
    target = output_path or ctx.obj.with_name(
        f"{ctx.obj.stem}-{datetime.now():%Y%m%d-%H%M%S-%f}.sqlite3"
    )
    backup(ctx.obj, target)
    output({"backup": str(target)})


@repo.command("add")
def repo_add(
    ctx: typer.Context,
    name: str | None = None,
    url: str | None = None,
    namespace: str | None = None,
    description: str | None = None,
    default_branch: str | None = None,
) -> None:
    item = Repository.model_validate(
        provided(
            name=required(name, "Repository name"),
            url=required(url, "Repository URL"),
            namespace=namespace,
            description=description,
            default_branch=default_branch,
        )
    )
    with transaction(ctx.obj, write=True) as conn:
        output_id = svc.store(conn, item, create_only=True)
    output({"id": output_id})


@repo.command("list")
def repo_list(ctx: typer.Context) -> None:
    with transaction(ctx.obj) as conn:
        output(svc.rows(conn, m.repositories))


@repo.command("show")
def repo_show(
    ctx: typer.Context, ref: str, full: bool = False, depth: int = 2, limit: int = 20
) -> None:
    with transaction(ctx.obj) as conn:
        output(
            svc.repo_detail(conn, ref, depth, limit)
            if full
            else svc.with_documents(conn, m.repositories, svc.resolve_repo(conn, ref))
        )


@repo.command("update")
def repo_update(
    ctx: typer.Context,
    ref: str,
    name: str | None = None,
    description: str | None = None,
    default_branch: str | None = None,
    local_path: str | None = None,
    architecture_summary: str | None = None,
    build_instructions: str | None = None,
    test_instructions: str | None = None,
) -> None:
    with transaction(ctx.obj, write=True) as conn:
        svc.update_fields(
            conn,
            m.repositories,
            svc.resolve_repo(conn, ref),
            provided(
                name=name,
                description=description,
                default_branch=default_branch,
                local_path=local_path,
                architecture_summary=architecture_summary,
                build_instructions=build_instructions,
                test_instructions=test_instructions,
            ),
        )


@repo.command("delete")
def repo_delete(ctx: typer.Context, ref: str) -> None:
    with transaction(ctx.obj, write=True) as conn:
        svc.delete_record(conn, m.repositories, svc.resolve_repo(conn, ref))


@task.command("add")
def task_add(
    ctx: typer.Context,
    title: str | None = None,
    source: str | None = None,
    external_id: str | None = None,
    description: str | None = None,
    status: str | None = None,
    task_type: Annotated[str | None, typer.Option("--type")] = None,
) -> None:
    item = Task.model_validate(
        provided(
            title=required(title, "Task title"),
            source=source,
            external_id=external_id,
            description=description,
            status=status,
            type=task_type,
        )
    )
    with transaction(ctx.obj, write=True) as conn:
        output_id = svc.store(conn, item, create_only=True)
    output({"id": output_id})


@task.command("list")
def task_list(ctx: typer.Context) -> None:
    with transaction(ctx.obj) as conn:
        output(svc.rows(conn, m.tasks))


@task.command("show")
def task_show(ctx: typer.Context, ref: str, depth: int = 2, limit: int = 20) -> None:
    with transaction(ctx.obj) as conn:
        output(svc.task_detail(conn, ref, depth, limit))


@task.command("history")
def task_history(ctx: typer.Context, ref: str) -> None:
    with transaction(ctx.obj) as conn:
        output(svc.rows(conn, m.task_events, task_id=svc.resolve_task(conn, ref)["id"]))


@task.command("link-mr")
def task_link_mr(ctx: typer.Context, ref: str, repository: str, number: int) -> None:
    with transaction(ctx.obj, write=True) as conn:
        svc.link_mr(
            conn,
            svc.resolve_task(conn, ref)["id"],
            svc.mr_record(conn, repository, number)["id"],
            "manual",
        )


@task.command("update")
def task_update(
    ctx: typer.Context,
    ref: str,
    title: str | None = None,
    description: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    solution_summary: str | None = None,
) -> None:
    with transaction(ctx.obj, write=True) as conn:
        svc.update_fields(
            conn,
            m.tasks,
            svc.resolve_task(conn, ref),
            provided(
                title=title,
                description=description,
                status=status,
                priority=priority,
                solution_summary=solution_summary,
            ),
        )


@task.command("delete")
def task_delete(ctx: typer.Context, ref: str) -> None:
    with transaction(ctx.obj, write=True) as conn:
        svc.delete_record(conn, m.tasks, svc.resolve_task(conn, ref))


@task.command("link-branch")
def task_link_branch(
    ctx: typer.Context, ref: str, repository: str, name: str, source_url: str | None = None
) -> None:
    with transaction(ctx.obj, write=True) as conn:
        branch_id = svc.link_branch(conn, ref, repository, name, source_url)
    output({"branch_id": branch_id})


@mr.command("add")
def mr_add(
    ctx: typer.Context,
    repo: str | None = None,
    number: int | None = None,
    title: str | None = None,
    source_branch: str | None = None,
    source_url: str | None = None,
    target_branch: str | None = None,
    description: str | None = None,
    status: str | None = None,
) -> None:
    item = MergeRequest.model_validate(
        provided(
            repo=required(repo, "Target repository"),
            external_id=number if number is not None else typer.prompt("MR number", type=int),
            title=required(title, "Title"),
            source_branch=required(source_branch, "Source branch"),
            source_repository_url=required(source_url, "Source repository URL"),
            target_branch=target_branch,
            description=description,
            status=status,
        )
    )
    with transaction(ctx.obj, write=True) as conn:
        output_id = svc.store(conn, item, create_only=True)
    output({"id": output_id})


@mr.command("list")
def mr_list(ctx: typer.Context, repo: str | None = None) -> None:
    with transaction(ctx.obj) as conn:
        filters = {"target_repository_id": svc.resolve_repo(conn, repo)["id"]} if repo else {}
        output(svc.rows(conn, m.merge_requests, **filters))


@mr.command("show")
def mr_show(ctx: typer.Context, repository: str, number: int, local: bool = False) -> None:
    with transaction(ctx.obj) as conn:
        output(svc.mr_detail(conn, repository, number, local=local))


@mr.command("update")
def mr_update(
    ctx: typer.Context,
    repository: str,
    number: int,
    title: str | None = None,
    status: str | None = None,
    description: str | None = None,
    resolution_summary: str | None = None,
) -> None:
    with transaction(ctx.obj, write=True) as conn:
        svc.update_fields(
            conn,
            m.merge_requests,
            svc.mr_record(conn, repository, number),
            provided(
                title=title,
                status=status,
                description=description,
                resolution_summary=resolution_summary,
            ),
        )


@mr.command("delete")
def mr_delete(ctx: typer.Context, repository: str, number: int) -> None:
    with transaction(ctx.obj, write=True) as conn:
        svc.delete_record(conn, m.merge_requests, svc.mr_record(conn, repository, number))


class FileFormat(StrEnum):
    json = "json"
    yaml = "yaml"
    markdown = "markdown"
    text = "text"
    diff = "diff"


def import_progress() -> Callable[[str], None]:
    last = 0.0

    def report(message: str) -> None:
        nonlocal last
        current = monotonic()
        if "characters" in message and current - last < 0.5:
            return
        typer.echo(message, err=True)
        last = current

    return report


def import_command(kind: Kind) -> Callable[..., None]:
    def run(
        ctx: typer.Context,
        file: Path | None = None,
        stdin: bool = False,
        fmt: Annotated[FileFormat | None, typer.Option("--format")] = None,
        extract: bool = False,
        no_llm: bool = False,
        source_url: str | None = None,
    ) -> None:
        """Import a UTF-8 file or pasted text from stdin."""
        if (file is not None) == stdin:
            raise CatalogError("Choose exactly one of --file and --stdin")
        content = (file.read_bytes() if file is not None else sys.stdin.buffer.read()).decode(
            "utf-8"
        )
        filename = file.name if file is not None else None
        file_format = cast(Format, fmt.value) if fmt else detect_format(filename, kind, content)
        if extract or no_llm:
            with transaction(ctx.obj, write=True) as conn:
                uid = preserve_document(conn, kind, file_format, content, source_url, filename)
            typer.echo(f"Original document: {uid}", err=True)
            output(inspect_document(ctx.obj, uid, no_llm=no_llm, progress=import_progress()))
            return
        with transaction(ctx.obj, write=True) as conn:
            result = import_document(conn, kind, file_format, content, source_url, filename)
        output(asdict(result))

    return run


for kind in ("repo", "task", "mr", "diff"):
    imports.command(kind)(import_command(cast(Kind, kind)))


@imports.command("directory")
def import_directory(
    ctx: typer.Context,
    path: Annotated[Path, typer.Option("--path")],
    extract: bool = False,
    no_llm: bool = False,
    dry_run: bool = False,
) -> None:
    if extract and no_llm:
        raise CatalogError("--extract and --no-llm are mutually exclusive")
    output(
        batch_import(
            ctx.obj,
            path,
            extract=extract and not no_llm,
            dry_run=dry_run,
            progress=lambda value: typer.echo(value, err=True),
        )
    )


class Entity(StrEnum):
    repo = "repo"
    task = "task"
    mr = "mr"


@document.command("list")
def list_documents(ctx: typer.Context, unprocessed: bool = False) -> None:
    with transaction(ctx.obj) as conn:
        output(document_list(conn, unprocessed))


@document.command("extract")
def extract_document(
    ctx: typer.Context,
    document_id: int,
    dry_run: bool = False,
    confirm: bool = False,
    refresh: bool = False,
    no_llm: bool = False,
    repo: str | None = None,
) -> None:
    if repo and not confirm:
        raise CatalogError("--repo requires --confirm")
    if confirm and (dry_run or refresh):
        raise CatalogError("Use --confirm on its own to apply the saved proposal")
    output(
        confirm_document(ctx.obj, document_id, repo=repo)
        if confirm
        else inspect_document(
            ctx.obj,
            document_id,
            dry_run=dry_run,
            refresh=refresh,
            no_llm=no_llm,
            progress=import_progress(),
        )
    )


@document.command("link")
def document_link(ctx: typer.Context, document_id: int, kind: Entity, entity_id: int) -> None:
    """Attach an original document to a local entity ID."""
    table = {Entity.repo: m.repositories, Entity.task: m.tasks, Entity.mr: m.merge_requests}[kind]
    with transaction(ctx.obj, write=True) as conn:
        svc.attach(conn, document_id, table, entity_id)


@document.command("show")
def document_show(ctx: typer.Context, document_id: int) -> None:
    """Write the exact original UTF-8 content."""
    with transaction(ctx.obj) as conn:
        original = svc.one(conn, m.raw_documents, id=document_id)
    sys.stdout.buffer.write(original["content"].encode("utf-8"))


@app.command("search")
def search_command(ctx: typer.Context, query: str, limit: int = 20) -> None:
    with transaction(ctx.obj) as conn:
        output(search(conn, query, limit))


@app.command("export")
def export_command(
    ctx: typer.Context,
    output_path: Annotated[Path, typer.Option("--output")],
    fmt: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    if fmt != "json":
        raise CatalogError("Supported export format: json")
    with transaction(ctx.obj) as conn:
        export_json(conn, output_path)


def main() -> None:
    try:
        app()
    except (OSError, ValueError, SQLAlchemyError, sqlite3.Error, CommandError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc
