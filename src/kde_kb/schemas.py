from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit, urlunsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    PositiveInt,
    model_validator,
)


def nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("Expected non-empty text")
    return value


def canonical_url(value: str) -> str:
    """Normalize HTTPS clone URLs and the standard KDE Invent SSH clone forms."""
    value = value.strip()
    if value.startswith("git@invent.kde.org:"):
        value = "https://invent.kde.org/" + value.removeprefix("git@invent.kde.org:")
    if value.startswith("ssh://git@invent.kde.org/"):
        value = "https://invent.kde.org/" + value.removeprefix("ssh://git@invent.kde.org/")
    parts = urlsplit(value)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.query
        or parts.fragment
        or re.search(r"\s", value)
    ):
        raise ValueError("Expected an HTTP(S) repository URL or a KDE Invent SSH clone URL")
    path = parts.path.rstrip("/").removesuffix(".git")
    if not path.strip("/"):
        raise ValueError("Repository URL requires a project path")
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    if parts.port is not None and parts.port != {"http": 80, "https": 443}[parts.scheme]:
        host += f":{parts.port}"
    return urlunsplit((parts.scheme.lower(), host, path, "", ""))


Name = Annotated[str, AfterValidator(nonblank)]
RepositoryURL = Annotated[str, AfterValidator(canonical_url)]
TaskType = Literal["bug", "feature", "refactoring", "documentation", "research", "other"]


def parse_date(value: object) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("Expected an ISO 8601 timestamp with a time zone")
    return value.astimezone(UTC)


Date = Annotated[
    datetime,
    BeforeValidator(parse_date),
    PlainSerializer(
        lambda value: value.isoformat().replace("+00:00", "Z"), return_type=str, when_used="json"
    ),
]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", coerce_numbers_to_str=True)


class TaskEvent(Input):
    event_type: Name
    description: str | None = None
    created_at: Date | None = None


class TestRun(Input):
    command: Name
    result: Name
    revision: str | None = None
    output: str | None = None
    created_at: Date | None = None


class MRReference(Input):
    repo: Name
    external_id: Annotated[PositiveInt, Field(le=2**63 - 1)]


class Repository(Input):
    depends_on: list[Name] = Field(default_factory=list)
    created_at: Date | None = None
    updated_at: Date | None = None
    name: Name
    url: RepositoryURL
    namespace: str | None = None
    description: str | None = None
    default_branch: Name | None = None
    local_path: str | None = None
    architecture_summary: str | None = None
    build_instructions: str | None = None
    test_instructions: str | None = None


class Task(Input):
    depends_on: list[Name] = Field(default_factory=list)
    events: list[TaskEvent] = Field(default_factory=list)
    created_at: Date | None = None
    updated_at: Date | None = None
    title: Name
    description: str | None = None
    external_id: Name | None = None
    source: Name | None = None
    external_url: str | None = None
    type: TaskType | None = None
    status: Name | None = None
    priority: Name | None = None
    reproduction_steps: str | None = None
    actual_behavior: str | None = None
    expected_behavior: str | None = None
    solution_summary: str | None = None
    completed_at: Date | None = None

    @model_validator(mode="after")
    def check_source(self) -> Self:
        if self.external_id is not None and self.source is None:
            raise ValueError("An external task ID requires its source")
        return self


class User(Input):
    username: Name
    platform: Name
    display_name: str | None = None
    profile_url: str | None = None
    description: str | None = None


class Position(Input):
    old_path: Name | None = None
    new_path: Name | None = None
    old_line: PositiveInt | None = None
    new_line: PositiveInt | None = None
    base_sha: Name | None = None
    start_sha: Name | None = None
    head_sha: Name | None = None
    code_context: str | None = None

    @model_validator(mode="after")
    def check_location(self) -> Self:
        if not (self.old_path or self.new_path) or not (
            self.base_sha or self.start_sha or self.head_sha
        ):
            raise ValueError("Code positions require a path and revision")
        return self


class Comment(Input):
    external_id: Name | None = None
    parent_index: Annotated[int, Field(ge=0)] | None = None
    body: str
    author: User | None = None
    parent_external_id: Name | None = None
    url: str | None = None
    created_at: Date | None = None
    updated_at: Date | None = None
    position: Position | None = None


class Discussion(Input):
    external_id: Name | None = None
    is_resolved: bool | None = None
    created_at: Date | None = None
    resolved_at: Date | None = None
    comments: list[Comment] = Field(default_factory=list)


class ChangedFile(Input):
    old_path: Name | None = None
    new_path: Name | None = None
    change_type: Name | None = None
    additions: Annotated[int, Field(ge=0)] | None = None
    deletions: Annotated[int, Field(ge=0)] | None = None
    diff: str | None = None

    @model_validator(mode="after")
    def check_path(self) -> Self:
        if not (self.old_path or self.new_path):
            raise ValueError("A changed file requires a path")
        return self


class Commit(Input):
    sha: Name
    repository: Name
    message: str | None = None
    author: User | None = None
    authored_at: Date | None = None
    committed_at: Date | None = None
    url: str | None = None
    summary: str | None = None
    changed_files: list[ChangedFile] = Field(default_factory=list)


class Revision(Input):
    complete: bool = False
    base_sha: Name | None = None
    start_sha: Name | None = None
    head_sha: Name | None = None


class MergeRequest(Input):
    repo: Name
    external_id: Annotated[PositiveInt, Field(le=2**63 - 1)] | None = None
    revision: Revision | None = None
    tasks: list[Name] = Field(default_factory=list)
    branch_status: Name | None = None
    depends_on: list[MRReference] = Field(default_factory=list)
    test_runs: list[TestRun] = Field(default_factory=list)
    title: Name
    source_branch: Name | None = None
    source_repository_url: RepositoryURL | None = None
    target_branch: Name | None = None
    url: str | None = None
    description: str | None = None
    status: Name | None = None
    is_draft: bool | None = None
    pipeline_status: str | None = None
    created_at: Date | None = None
    updated_at: Date | None = None
    merged_at: Date | None = None
    closed_at: Date | None = None
    resolution_summary: str | None = None
    author: User | None = None
    discussions: list[Discussion] = Field(default_factory=list)
    commits: list[Commit] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_snapshot(self) -> Self:
        if self.revision and self.revision.complete and "commits" not in self.model_fields_set:
            raise ValueError("A complete revision requires an explicit commits array")
        return self
