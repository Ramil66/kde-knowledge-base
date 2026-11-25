"""Deterministic parsers for locally collected Invent and task documents."""

from __future__ import annotations

import re
from typing import Any

import yaml

from .database import CatalogError
from .importers import DocumentLoader
from .schemas import Comment, Discussion, MergeRequest, Repository, Task, User

MR_URL = re.compile(r"https?://invent\.kde\.org/(.+?)/-/merge_requests/(\d+)")
SHORT_MR = re.compile(r"(?:(?P<project>[\w.-]+(?:/[\w.-]+)*)!)?(?P<number>\d+)$")
BUG_URL = re.compile(r"https?://bugs\.kde\.org/show_bug\.cgi\?id=(\d+)")
SHA = re.compile(r"^([0-9a-f]{7,40})\s+-\s+(.+)$")


def sectioned(text: str) -> tuple[dict[str, str], str]:
    header = {}
    for line in text.splitlines()[:8]:
        if ": " in line:
            key, value = line.split(": ", 1)
            if key in {"URL", "COPIED"}:
                header[key.lower()] = value.strip()
    sections = {}
    matches = list(re.finditer(r"(?m)^--- (OVERVIEW|ACTIVITY|COMMITS) ---\s*$", text))
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections[match[1].lower()] = text[match.end() : end]
    if not sections and not header:
        raise CatalogError("Expected MR header or OVERVIEW, ACTIVITY or COMMITS sections")
    prefix = text[: matches[0].start()] if matches else text
    return header, prefix + "\n".join(sections.values())


def parse_mr(text: str, filename: str | None = None) -> dict[str, Any]:
    # The filename is part of the source identity when a captured page has no URL.
    # Some source snapshots contain only URL/COPIED headers; preserve them as
    # identifiable, explicitly untitled records instead of inventing metadata.
    header, _source = sectioned(text)
    if filename:
        header["filename"] = filename
    url = header.get("url")
    match = MR_URL.search(url or "")
    if match:
        project, number = match[1], int(match[2])
    else:
        filename_match = re.search(r"(?:^|/)([\w.-]+)-(\d+)\.txt$", header.get("filename", ""))
        if not filename_match:
            raise CatalogError("MR URL is empty and filename does not identify project and number")
        project, number = filename_match[1], int(filename_match[2])
    repo_url = "https://invent.kde.org/" + project
    sections = dict(
        re.findall(
            r"(?ms)^--- (OVERVIEW|ACTIVITY|COMMITS) ---\s*\n(.*?)(?=^--- (?:OVERVIEW|ACTIVITY|COMMITS) ---\s*$|\Z)",
            text,
        )
    )
    overview = sections.get("OVERVIEW", "")
    lines = [line.strip() for line in overview.splitlines()]
    title = next(
        (x for x in lines if x and x.lower() not in {"open", "opened", "closed", "merged"}),
        f"!{number} (title unavailable)",
    )
    status = next(
        (x.lower() for x in lines if x.lower() in {"open", "opened", "closed", "merged"}), None
    )
    branch_match = re.search(
        r"^\s*([\w.-]+/[\w.-]+):([\w./+-]+)\s*$\s*into\s*^\s*([\w./+-]+)", overview, re.MULTILINE
    )
    if not branch_match:
        branch_match = re.search(
            r"^\s*([\w./+-]+)\s*into\s*^\s*([\w./+-]+)", overview, re.MULTILINE
        )
    source_branch = (
        branch_match[2]
        if branch_match and branch_match.lastindex == 3
        else branch_match[1]
        if branch_match
        else None
    )
    target_branch = (
        branch_match[3]
        if branch_match and branch_match.lastindex == 3
        else branch_match[2]
        if branch_match
        else None
    )
    fork_url = (
        "https://invent.kde.org/" + branch_match[1]
        if branch_match and branch_match.lastindex == 3
        else repo_url
        if branch_match
        else None
    )
    description_match = re.search(
        r"(?ms)^Changes\s*\n(?:\d+\s*\n?)*\s*(.*?)(?=^Merge request reports|^Merge details|\Z)",
        overview,
    )
    description = description_match[1].strip() if description_match else None
    commit_author = None
    author_match = re.search(r"(?m)^([^\n]+) authored? \d+ ", sections.get("COMMITS", ""))
    if author_match:
        commit_author = User(
            username=re.sub(r"[^\w.-]", "", author_match[1].split()[-1]), platform="invent"
        )
    commits = []
    commit_text = sections.get("COMMITS", "")
    commit_lines = commit_text.splitlines()
    for line_index, line in enumerate(commit_lines):
        found = SHA.match(line.strip())
        if found:
            commits.append({"sha": found[1], "message": found[2], "repository": repo_url})
        # The history list also renders each commit as title / author / SHA.
        # It contains more entries than the compact "added N commits" summary.
        if line_index + 1 < len(commit_lines) and re.search(r"\bauthored?\s+", line):
            sha = re.fullmatch(r"\s*([0-9a-f]{7,40})\s*", commit_lines[line_index + 1])
            if sha and line_index:
                author_name = re.split(r"\s+authored?\s+", line, maxsplit=1)[0].strip()
                username = re.sub(r"[^\w.-]", "", author_name.replace("'s avatar", ""))
                if username:
                    commits.append(
                        {
                            "sha": sha[1],
                            "message": commit_lines[line_index - 1].strip(),
                            "repository": repo_url,
                            "author": User(
                                username=username, platform="invent", display_name=author_name
                            ).model_dump(),
                        }
                    )
    # The compact summary and detailed history repeat current commit entries.
    commits = list({commit["sha"]: commit for commit in commits}.values())
    # Parse visible discussion entries into source spans, without copying them through an LLM.
    comments = []
    activity = sections.get("ACTIVITY", "")
    activity_lines = activity.splitlines()
    activity_marker = re.search(r"(?m)^--- ACTIVITY ---\s*$", text)
    activity_first_line = text[: activity_marker.end()].count("\n") + 2 if activity_marker else 1
    authors = [
        (i, re.fullmatch(r"@([\w.-]+)", line.strip())) for i, line in enumerate(activity_lines)
    ]
    authors = [
        (i, author)
        for i, author in authors
        if author
        and i + 1 < len(activity_lines)
        and re.search(r"ago|yesterday|today", activity_lines[i + 1], re.IGNORECASE)
    ]
    for index, (line_index, activity_author) in enumerate(authors):
        start = line_index + 2
        while start < len(activity_lines) and activity_lines[start].strip() in {
            "Developer",
            "Author",
            "Contributor",
            "Maintainer",
        }:
            start += 1
        end = authors[index + 1][0] if index + 1 < len(authors) else len(activity_lines)
        while start < end and not activity_lines[start].strip():
            start += 1
        while start < end and (
            not activity_lines[end - 1].strip()
            or activity_lines[end - 1].strip() in {"Reply…", "Reply..."}
        ):
            end -= 1
        body_lines = activity_lines[start:end]
        body = "\n".join(body_lines).strip()
        if body:
            start_line = activity_first_line + start
            end_line = start_line + len(body.splitlines()) - 1
            comments.append(
                Comment(
                    external_id=f"source:{activity_first_line + line_index}",
                    body=body,
                    author=User(username=activity_author[1], platform="invent"),
                    source_start_line=start_line,
                    source_end_line=end_line,
                ).model_dump(mode="json", exclude_none=True)
            )
    bugs = list(dict.fromkeys(int(n) for n in re.findall(r"\bBUG:\s*(\d+)", overview)))
    relation_by_ref = {}
    for line in overview.splitlines():
        normalized = line.casefold()
        relation_type = next(
            (
                kind
                for phrase, kind in (
                    ("depends on", "depends_on"),
                    ("related", "related"),
                    ("superseded by", "superseded_by"),
                )
                if phrase in normalized
            ),
            "mentions",
        )
        for reference in re.findall(r"\b(?:[\w.-]+/)*[\w.-]+!\d+\b|!\d+", line):
            relation_by_ref.setdefault(reference, relation_type)
    related = list(relation_by_ref)
    return {
        "record": MergeRequest(
            repo=repo_url,
            external_id=number,
            title=title,
            target_branch=target_branch,
            source_branch=source_branch,
            source_repository_url=fork_url,
            status=status,
            description=description,
            author=commit_author,
            discussions=(
                [
                    Discussion(
                        external_id="source:activity",
                        is_resolved=(
                            "All threads resolved!" in overview
                            or "All threads resolved!" in activity
                        ),
                        comments=comments,
                    )
                ]
                if comments
                else []
            ),
            commits=commits,
        ).model_dump(mode="json", exclude_none=True),
        "repository": Repository(name=project.rsplit("/", 1)[-1], url=repo_url).model_dump(),
        "bug_ids": bugs,
        "related_mrs": related,
        "mr_references": [
            {"reference": reference, "type": kind} for reference, kind in relation_by_ref.items()
        ],
        "sections": list(sections),
    }


def parse_task(text: str) -> dict[str, Any]:
    match = re.match(r"\A---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not match:
        raise CatalogError("Task document requires YAML front matter")
    metadata = yaml.load(match[1], Loader=DocumentLoader)
    if not isinstance(metadata, dict) or not metadata.get("id"):
        raise CatalogError("Task front matter requires id")
    rest = text[match.end() :]
    sections = {}
    found = list(re.finditer(r"(?m)^--- (TASK|LINKS|BUGZILLA) ---\s*$", rest))
    for index, section in enumerate(found):
        end = found[index + 1].start() if index + 1 < len(found) else len(rest)
        sections.setdefault(section[1], []).append(rest[section.end() : end].strip())
    body = "\n\n".join(sections.get("TASK", [])).strip()
    title = next((line.strip() for line in body.splitlines() if line.strip()), str(metadata["id"]))
    refs = []
    for value in sections.get("LINKS", []):
        refs.extend(BUG_URL.findall(value))
        refs.extend(re.findall(r"https?://invent\.kde\.org/[^\s]+/-/merge_requests/\d+", value))
        refs.extend(re.findall(r"(?<![\w/])(?:[\w.-]+(?:/[\w.-]+)*)?!\d+", value))
    bugs = []
    for section in sections.get("BUGZILLA", []):
        bugs.extend(re.findall(r"(?i)\bBug\s+(\d+)", section))
        bugs.extend(re.findall(r"(?i)\b(?:id=|of bug\s+)(\d+)", section))
    task = Task(
        title=title,
        description=body,
        source=str(metadata.get("source", "work")),
        external_id=str(metadata["id"]),
        type="other",
    )
    return {
        "record": task.model_dump(mode="json", exclude_none=True),
        "metadata": metadata,
        "links": list(dict.fromkeys(refs)),
        "bug_ids": list(dict.fromkeys(bugs)),
    }


def resolve_mr_ref(ref: str) -> tuple[str | None, int] | None:
    url = MR_URL.search(ref)
    if url:
        return "https://invent.kde.org/" + url[1], int(url[2])
    value = ref.strip().removeprefix("!")
    found = SHORT_MR.fullmatch(value)
    if not found:
        return None
    project = found["project"]
    return ("https://invent.kde.org/" + project if project else None, int(found["number"]))
