import json
from pathlib import Path

import pytest

from kde_kb import models as m
from kde_kb import services as svc
from kde_kb.database import migrate_database, transaction
from kde_kb.extraction import confirm_document, inspect_document
from kde_kb.import_stages import SYSTEM, blocks
from kde_kb.importers import preserve_document
from kde_kb.invent import Lines, SourceComment, parse_page
from kde_kb.providers import GenerationResponse, MockProvider, ProviderError, settings

SAMPLE_TEXT = """core: Add a reusable FuseMountJob
Open
core: Add a reusable FuseMountJob
ramilnur/kio:feature/fuse-mount-job
into
master
Overview
9
Commits
3
Changes
8
2
Merge request reports
Merge request pipeline passed
Activity
Ramil Nurmanov
Ramil Nurmanov
@ramilnur
1 day ago
Author
Contributor
Please preserve the original error.

Reply…
Méven Car
Méven Car
@meven
10 hours ago
Developer
Could we add a test?
"""
URL = "https://invent.kde.org/example/project/-/merge_requests/17"


def test_real_invent_page() -> None:
    source = SAMPLE_TEXT
    parsed = parse_page(source, None)
    record = parsed["record"]
    assert "repo" not in record and "external_id" not in record
    assert record["author"]["username"] == "ramilnur"
    assert (record["source_branch"], record["target_branch"]) == (
        "feature/fuse-mount-job",
        "master",
    )
    assert parsed["participants"] == ["ramilnur", "meven"]
    assert not parsed["related_mrs"]
    assert parsed["source_comments"][0]["thread"] != parsed["source_comments"][1]["thread"]
    spans = [(c["lines"]["first"], c["lines"]["last"]) for c in parsed["source_comments"]]
    assert spans == [(23, 23), (31, 31)]
    assert parsed["record"]["pipeline_status"] == "passed"


def test_offline_preview_and_confirm(tmp_path: Path) -> None:
    path = tmp_path / "catalog"
    migrate_database(path)
    source = SAMPLE_TEXT.replace("\n", "\r\n")
    with transaction(path, write=True) as conn:
        uid = preserve_document(conn, "mr", "text", source, URL)
    preview = inspect_document(path, uid, no_llm=True)
    assert preview["metrics"]["input_tokens"] == preview["metrics"]["output_tokens"] == 0
    proposal = preview["proposal"]
    assert "discussions" not in proposal["record"] and "description" not in proposal["record"]
    assert "Could we add a test?" not in json.dumps(proposal)
    confirm_document(path, uid)
    with transaction(path) as conn:
        assert svc.one(conn, m.raw_documents, id=uid)["content"] == source
        detail = svc.mr_detail(conn, "project", 17)
        notes = [c for d in detail["discussions"] for c in d["comments"]]
        assert [c["body"] for c in notes] == [
            Lines.model_validate(ref["lines"]).read(source.splitlines(keepends=True))
            for ref in proposal["source_comments"]
        ]
        assert notes[0]["body"] == "Please preserve the original error."
        assert notes[0]["parent_comment_id"] is None and notes[1]["parent_comment_id"] is None


class Classifier:
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    def generate(self, request):
        self.calls.append(request)
        if len(self.calls) == self.fail_on:
            raise ProviderError("Interrupted")
        values = json.loads(request.document)
        assert request.system == SYSTEM and all(isinstance(v, str) for v in values)
        assert len(request.document.encode()) <= 1024
        if request.on_chunk:
            request.on_chunk(5)
        return GenerationResponse(
            json.dumps({"labels": [1] * len(values)}), "mock-classifier", 100, 8
        )


def test_resume_reuse_and_metrics(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "catalog"
    config = tmp_path / "config.toml"
    config.write_text("[llm]\nattempts=1\nblock_size=180\n")
    monkeypatch.setenv("KDE_KB_CONFIG", str(config))
    migrate_database(path)
    source = SAMPLE_TEXT
    with transaction(path, write=True) as conn:
        uid = preserve_document(conn, "mr", "text", source, URL)
    messages = []
    first = Classifier(fail_on=1)
    with pytest.raises(ProviderError):
        inspect_document(path, uid, provider=first, progress=messages.append)
    with transaction(path) as conn:
        assert not svc.rows(conn, m.document_extractions)
        assert [row["stage"] for row in svc.rows(conn, m.import_checkpoints)] == ["parse"]
    second = Classifier()
    preview = inspect_document(path, uid, provider=second, progress=messages.append)
    stats = preview["metrics"]
    assert stats["model"] == "mock-classifier"
    assert stats["cached_blocks"] == 0 and stats["reused_input_tokens"] == 0
    assert (
        stats["input_tokens"] == len(second.calls) * 100
        and stats["output_tokens"] == len(second.calls) * 8
    )
    assert stats["estimated_calls"] == 0 and len(second.calls) == 1
    assert any("generated characters" in message for message in messages)
    assert all(
        c["categories"] == ["change-request"] for c in preview["proposal"]["source_comments"]
    )
    assert inspect_document(path, uid, provider=MockProvider())["id"] == preview["id"]
    # A changed activity feed, with the same comments, reuses all completed semantic jobs.
    with transaction(path, write=True) as conn:
        uid2 = preserve_document(
            conn,
            "mr",
            "text",
            source.replace("Merge request pipeline passed", "Merge request pipeline failed"),
            URL,
        )
    third = Classifier()
    updated = inspect_document(path, uid2, provider=third)
    assert uid2 != uid and not third.calls and updated["metrics"]["cached_blocks"] == 1


def test_long_unicode_excerpts_are_bounded(monkeypatch, tmp_path: Path) -> None:
    cfg = tmp_path / "small.toml"
    cfg.write_text("[llm]\nblock_size=40\n")
    monkeypatch.setenv("KDE_KB_CONFIG", str(cfg))
    body = '\t«🙂» "comment" ' * 30
    ref = SourceComment(
        id=1,
        thread=1,
        author={"username": "user", "platform": "invent"},
        lines=Lines(first=1, last=1),
    )
    jobs = list(blocks([ref], [body], settings()))
    assert "".join(piece for job in jobs for _, piece in job) == body
    assert all(
        len(json.dumps([p for _, p in job], ensure_ascii=False, separators=(",", ":")).encode())
        <= 40
        for job in jobs
    )
