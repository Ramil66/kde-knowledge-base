from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

import yaml
from sqlalchemy import select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from . import models as m
from .database import CatalogError
from .schemas import MergeRequest, Repository, Task
from .services import attach, link_column, rows, save, store

type Kind = Literal["repo", "task", "mr", "diff"]
type Format = Literal["json", "yaml", "markdown", "text", "diff"]


@dataclass(frozen=True)
class ImportResult:
    document_id: int
    entity_id: int | None
    message: str


def unique_fields(pairs: Iterable[tuple[object, object]]) -> dict[str, object]:
    fields: dict[str, object] = {}
    for key, value in pairs:
        if not isinstance(key, str):
            raise CatalogError("Document field names must be strings")
        if key in fields:
            raise CatalogError(f"Duplicate document field: {key}")
        fields[key] = value
    return fields


class DocumentLoader(yaml.SafeLoader):
    """Preserve timestamp text and reject ambiguous mappings."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[str, object]:
        self.flatten_mapping(node)
        return unique_fields(self.construct_pairs(node, deep=deep))


DocumentLoader.yaml_implicit_resolvers = {
    key: [(tag, pattern) for tag, pattern in rules if tag != "tag:yaml.org,2002:timestamp"]
    for key, rules in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def parse(content: str, fmt: Format) -> object:
    if fmt == "json":
        return json.loads(content, object_pairs_hook=unique_fields)
    if fmt == "yaml":
        return yaml.load(content, Loader=DocumentLoader)
    if fmt == "markdown":
        front = re.match(
            r"\A---[ \t]*\r?\n(.*?)^---[ \t]*(?:\r?\n|$)", content, re.MULTILINE | re.DOTALL
        )
        if front:
            record = yaml.load(front[1], Loader=DocumentLoader)
            body = content[front.end() :]
            if isinstance(record, dict) and body.strip():
                record.setdefault("description", body)
            return record
    raise CatalogError("No structured fields. Attach this original with document link.")


def import_document(
    conn: Connection,
    kind: Kind,
    fmt: Format,
    content: str,
    source_url: str | None = None,
    filename: str | None = None,
) -> ImportResult:
    key = {
        "document_type": kind,
        "format": fmt,
        "source_url": source_url or None,
        "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }
    existing = conn.execute(select(m.raw_documents.c.id).filter_by(**key)).scalar_one_or_none()
    document_id = (
        existing
        if existing is not None
        else save(
            conn,
            m.raw_documents,
            key | {"content": content, "metadata": {"filename": filename} if filename else {}},
        )
    )
    if kind == "diff":
        return ImportResult(document_id, None, "Original diff stored")
    model, table = {
        "repo": (Repository, m.repositories),
        "task": (Task, m.tasks),
        "mr": (MergeRequest, m.merge_requests),
    }[kind]
    if existing is not None:
        for link in rows(conn, m.document_links, document_id=document_id):
            if link[link_column(table)] is not None:
                return ImportResult(document_id, link[link_column(table)], "Already linked")
    try:
        item = model.model_validate(parse(content, fmt))
        with conn.begin_nested():
            entity_id = store(conn, item)
            attach(conn, document_id, table, entity_id)
    except (ValueError, OverflowError, RecursionError, yaml.YAMLError, IntegrityError) as exc:
        logging.getLogger(__name__).info(
            "import_retained_original", extra={"document_id": document_id, "kind": kind}
        )
        return ImportResult(document_id, None, f"Original stored without structured record. {exc}")
    return ImportResult(document_id, entity_id, "Imported")


def detect_format(filename: str | None, kind: Kind, content: str) -> Format:
    suffix = filename.rsplit(".", 1)[-1].lower() if filename else ""
    match suffix:
        case "json":
            return "json"
        case "yaml" | "yml":
            return "yaml"
        case "md" | "markdown":
            return "markdown"
        case "diff" | "patch":
            return "diff"
        case "txt":
            return "text"
    if kind == "diff":
        return "diff"
    if content.lstrip().startswith(("{", "[")):
        return "json"
    if content.startswith("---"):
        return "markdown"
    return "text"
