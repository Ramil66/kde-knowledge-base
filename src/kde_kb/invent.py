"""Parse the visible text of an Invent MR. Source positions are one-based and inclusive."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from pydantic import Field, PositiveInt

from .database import CatalogError
from .schemas import Input, ReviewLocation, User

PARSER_VERSION = "invent-1"
LINK = re.compile(r"https?://[^\s<>\"\]]+")
MR_URL = re.compile(
    r"https://invent\.kde\.org/([\w.-]+(?:/[\w.-]+)+)/-/merge_requests/(\d+)/?(?:[?#].*)?$"
)
HANDLE = r"[A-Za-z0-9_][\w.-]*"
TIME = r"(?:(?:\d+|an?|one)\s+\w+\s+ago|yesterday|today|just now|\d{4}-\d{2}-\d{2}.*)"
ROLES = {"Author", "Developer", "Contributor", "Maintainer", "Owner", "Member", "Guest"}
EVENT = re.compile(
    r"^(?:Edited .+ ago by |.* (?:mentioned in merge request |added (?:\d+ commits?\b|$)|(?:un)?approved this merge request|resolved all threads|changed this line in version |started a thread on ))"
)
REPLY = {"Reply…", "Reply...", "Reply"}
UI_ONLY = {"Suggested change", "Applied", "Compare with previous version", *REPLY}
BRANCH = r"[\w./+-]+"


class Lines(Input):
    first: PositiveInt
    last: PositiveInt

    def read(self, lines: list[str]) -> str:
        if not self.first <= self.last <= len(lines):
            raise CatalogError("Invalid source line range")
        return (
            "".join(
                line for line in lines[self.first - 1 : self.last] if line.strip() not in UI_ONLY
            )
            .removesuffix("\n")
            .removesuffix("\r")
        )


class SourceComment(Input):
    id: PositiveInt
    thread: PositiveInt
    lines: Lines
    author: User
    resolved: bool | None = None
    external_id: str | None = None
    url: str | None = None
    categories: list[str] = Field(default_factory=list)
    review_locations: list[ReviewLocation] = Field(default_factory=list)


@dataclass(frozen=True)
class Header:
    start: int
    body: int
    author: User
    roles: frozenset[str]
    resolved: bool
    note_url: str | None


def _headers(lines: list[str], begin: int) -> list[Header]:
    headers = []
    for i in range(begin, len(lines)):
        handle = re.fullmatch(rf"@({HANDLE})", lines[i])
        if handle and i + 1 < len(lines) and re.fullmatch(TIME, lines[i + 1], re.IGNORECASE):
            start = i
            name = None
            if (
                i > begin
                and lines[i - 1]
                and not lines[i - 1].startswith("@")
                and (
                    i >= 2
                    and lines[i - 1] == lines[i - 2]
                    or re.fullmatch(r"[^\W\d_][\w.'’ -]{0,60}", lines[i - 1])
                )
            ):
                name = lines[i - 1]
                start -= 1
                if start > begin and lines[start - 1] == name:
                    start -= 1
            user, body = handle[1], i + 2
        else:
            combined = re.fullmatch(
                rf"(?:(.+?)\s+)?@({HANDLE})\s+(?:commented|replied)(?:\s+.*)?", lines[i]
            )
            if not combined:
                continue
            name, user = combined.groups()
            start, body = i, i + 1
        roles = set()
        resolved = False
        note_url = None
        while body < len(lines):
            if lines[body] in ROLES:
                roles.add(lines[body])
            elif lines[body].startswith("Resolved ") and " by " in lines[body]:
                resolved = True
            elif re.fullmatch(
                r"https://invent\.kde\.org/\S+/#?note_\d+|https://invent\.kde\.org/\S+#note_\d+",
                lines[body],
            ):
                note_url = lines[body]
            else:
                break
            body += 1
        headers.append(
            Header(
                start,
                body,
                User(username=user, platform="invent", display_name=name),
                frozenset(roles),
                resolved,
                note_url,
            )
        )
    return headers


def _span(lines: list[str], start: int, stop: int) -> Lines | None:
    while start < stop and not lines[start]:
        start += 1
    while start < stop and not lines[stop - 1]:
        stop -= 1
    return Lines(first=start + 1, last=stop) if start < stop else None


def parse_page(content: str, source_url: str | None) -> dict:
    lines = [line.strip() for line in content.splitlines()]
    record = {}
    proposal = {"record": record}
    activity = next((i for i, v in enumerate(lines) if v == "Activity"), len(lines))
    header = lines[:activity]
    urls = list(dict.fromkeys(match.group().rstrip(".,;)") for match in LINK.finditer(content)))
    proposal["links"] = urls
    proposal["bugzilla_urls"] = [
        url for url in urls if url.startswith("https://bugs.kde.org/show_bug.cgi?")
    ]
    proposal["related_mrs"] = list(
        dict.fromkeys(re.findall(r"\b[\w.-]+(?:/[\w.-]+)+!\d+\b", content))
    )
    proposal["observed_commits"] = list(
        dict.fromkeys(re.findall(r"^\s*([a-f0-9]{7,40}) - ", content, re.MULTILINE))
    )
    proposal["ci_results"] = [
        line for line in header if re.match(r"Merge request pipeline\b", line)
    ]
    pipeline = next(
        (
            re.search(r"\b(passed|failed|running|canceled)\b", line)
            for line in proposal["ci_results"]
            if re.search(r"\b(passed|failed|running|canceled)\b", line)
        ),
        None,
    )
    if pipeline:
        record["pipeline_status"] = pipeline[1]

    # Identify the subject only from its page address or heading, never activity links.
    subject = MR_URL.fullmatch(source_url or "")
    if not subject:
        subject = next((m for line in header[:4] if (m := MR_URL.fullmatch(line))), None)
    if subject:
        project = subject[1]
        url = "https://invent.kde.org/" + project
        proposal["repository"] = {"name": project.rsplit("/", 1)[-1], "url": url}
        record.update(
            repo=url, external_id=int(subject[2]), url=f"{url}/-/merge_requests/{subject[2]}"
        )
    for i, line in enumerate(header[:16]):
        if line.lower() in {"open", "opened", "merged", "closed"}:
            record["status"] = line
            if i and i + 1 < len(header) and header[i - 1] == header[i + 1]:
                record["title"] = header[i - 1]
        if match := re.fullmatch(r"!(\d+)\s+(.+)", line):
            record.setdefault("external_id", int(match[1]))
            record.setdefault("title", match[2])
        if line.startswith("Title: "):
            record.setdefault("title", line[7:].strip())
    for i, line in enumerate(header):
        if line.lower() == "into" and i and i + 1 < len(header):
            source, target = header[i - 1], header[i + 1]
        else:
            pair = re.fullmatch(rf"((?:{BRANCH}:)?{BRANCH})\s+(?:into|→)\s+({BRANCH})", line)
            if not pair:
                continue
            source, target = pair.groups()
        if re.fullmatch(rf"(?:{BRANCH}:)?{BRANCH}", source) and re.fullmatch(BRANCH, target):
            project, _, branch = source.rpartition(":")
            record.update(source_branch=branch, target_branch=target)
            if "/" in project:
                record["source_repository_url"] = "https://invent.kde.org/" + project
            break
    # The description follows tab counters and precedes the reports section.
    changes = next((i for i, line in enumerate(header) if line == "Changes"), None)
    if changes is not None:
        start = changes + 1
        while start < len(header) and (
            not header[start] or re.fullmatch(r"\d+(?: open threads?)?", header[start])
        ):
            start += 1
        stop = next(
            (
                i
                for i in range(start, len(header))
                if header[i] in {"Merge request reports", "Merge details"}
            ),
            len(header),
        )
        if span := _span(lines, start, stop):
            proposal["description_lines"] = span.model_dump()
    begin = activity + 1 if activity < len(lines) else 0
    headers = _headers(lines, begin)
    comments = []
    thread = 0
    active_thread = False
    previous_stop = begin
    thread_locations = {}
    path_pattern = re.compile(
        r"^\s*((?:[\w.@+-]+/)+[\w.@+-]+\.(?:c|cc|cpp|h|hh|hpp|qml|js|py|cmake|txt))\s*(?:[\d+→ -]*)$",
        re.IGNORECASE,
    )
    for index, h in enumerate(headers):
        gap = lines[previous_stop : h.start]
        if any(line in REPLY or EVENT.match(line) for line in gap):
            active_thread = False
        if not active_thread:
            thread += 1
        active_thread |= h.resolved or any("started a thread on " in line for line in gap)
        path_match = next(
            (
                (offset, match)
                for offset, line in enumerate(gap)
                if (match := path_pattern.fullmatch(line))
            ),
            None,
        )
        if path_match:
            offset, path = path_match
            range_match = re.search(
                r"Comment on lines\s+[+-]?(\d+)(?:\s+to\s+[+-]?(\d+))?",
                "\n".join(gap),
                re.IGNORECASE,
            )
            context_lines = []
            for line in gap[offset + 1 :]:
                if not line.strip() or line in REPLY or EVENT.match(line):
                    break
                context_lines.append(line)
                if len(context_lines) == 12:
                    break
            location = ReviewLocation(
                path=path[1],
                start_line=int(range_match[1]) if range_match else None,
                end_line=int(range_match[2] or range_match[1]) if range_match else None,
                code_context="\n".join(context_lines) or None,
            )
            thread_locations.setdefault(thread, []).append(location)
        end = headers[index + 1].start if index + 1 < len(headers) else len(lines)
        stop = end
        fenced = False
        for j in range(h.body, end):
            line = lines[j]
            if line.startswith("```"):
                fenced = not fenced
            if not fenced and (
                line in REPLY
                or EVENT.match(line)
                or line.startswith("changed this line in version ")
            ):
                stop = j
                if j > h.body and line.startswith("changed this line") and j and lines[j - 1]:
                    stop -= 1
                break
        previous_stop = stop
        if span := _span(lines, h.body, stop):
            ref = SourceComment(
                id=len(comments) + 1,
                thread=thread,
                lines=span,
                author=h.author,
                resolved=True
                if h.resolved or any(line.startswith("Resolved ") for line in gap)
                else None,
                url=h.note_url,
                external_id=h.note_url.split("note_")[-1] if h.note_url else None,
                review_locations=thread_locations.get(thread, []),
            )
            comments.append(ref)
        if "Author" in h.roles:
            record["author"] = h.author.model_dump(exclude_none=True)
    proposal["source_comments"] = [
        c.model_dump(exclude_none=True, exclude_defaults=True) for c in comments
    ]
    proposal["participants"] = list(dict.fromkeys(c.author.username for c in comments))
    return proposal


def hydrate(
    record: dict, description: Lines | None, comments: list[SourceComment], content: str
) -> None:
    lines = content.splitlines(keepends=True)
    if description:
        record["description"] = description.read(lines)
    if not comments:
        return
    discussions = defaultdict(list)
    resolved = set()
    for ref in comments:
        group = discussions[ref.thread]
        comment = {
            "body": ref.lines.read(lines),
            "author": ref.author.model_dump(exclude_none=True),
            "source_start_line": ref.lines.first,
            "source_end_line": ref.lines.last,
            "review_locations": [
                location.model_dump(exclude_none=True) for location in ref.review_locations
            ],
        }
        if ref.external_id:
            comment["external_id"] = ref.external_id
        if ref.url:
            comment["url"] = ref.url
        if ref.categories:
            comment["category"] = ", ".join(ref.categories)
        if group:
            comment["parent_index"] = 0
        group.append(comment)
        if ref.resolved:
            resolved.add(ref.thread)
    record["discussions"] = [
        {
            "external_id": f"source-thread:{thread}",
            "comments": group,
            **({"is_resolved": True} if thread in resolved else {}),
        }
        for thread, group in discussions.items()
    ]
