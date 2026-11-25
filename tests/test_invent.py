import json
from pathlib import Path

from kde_kb import models as m
from kde_kb import services as svc
from kde_kb.database import migrate_database, transaction
from kde_kb.extraction import confirm_document, inspect_document
from kde_kb.import_stages import blocks
from kde_kb.importers import preserve_document
from kde_kb.invent import Lines, SourceComment, parse_page
from kde_kb.providers import MockProvider, settings

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


def test_resume_reuse_and_metrics(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "catalog"
    config = tmp_path / "config.toml"
    config.write_text("[llm]\nattempts=1\nblock_size=8192\n")
    monkeypatch.setenv("KDE_KB_CONFIG", str(config))
    migrate_database(path)
    source = SAMPLE_TEXT
    with transaction(path, write=True) as conn:
        uid = preserve_document(conn, "mr", "text", source, URL)
    first = MockProvider('{"labels":[]}', '{"labels":[]}', '{"labels":[]}')
    failed = inspect_document(path, uid, provider=first)
    assert failed["metrics"]["calls"] == 3
    assert failed["metrics"]["failed_comments"] == 2
    assert failed["metrics"]["input_tokens"] > 0 and failed["metrics"]["output_tokens"] > 0
    with transaction(path) as conn:
        assert not [
            row for row in svc.rows(conn, m.import_checkpoints) if row["stage"] == "classify"
        ]
        assert [row["stage"] for row in svc.rows(conn, m.import_checkpoints)] == ["parse"]
    second = MockProvider('{"labels":[1,4]}')
    preview = inspect_document(path, uid, provider=second, refresh=True)
    stats = preview["metrics"]
    assert stats["model"] == "mock"
    assert stats["cached_blocks"] == 0 and stats["reused_input_tokens"] == 0
    assert stats["input_tokens"] > 0 and stats["output_tokens"] > 0
    assert stats["estimated_calls"] == 1 and len(second.requests) == 1
    assert [c["categories"] for c in preview["proposal"]["source_comments"]] == [
        ["change-request"],
        ["resolution"],
    ]
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
    third = MockProvider()
    updated = inspect_document(path, uid2, provider=third)
    assert uid2 != uid and not third.requests and updated["metrics"]["cached_blocks"] == 1


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


def test_invalid_label_count_splits_and_caches_successful_subblocks(tmp_path, monkeypatch):
    path = tmp_path / "catalog"
    config = tmp_path / "config.toml"
    config.write_text("[llm]\nattempts=1\nblock_size=8192\nnum_predict=64\n")
    monkeypatch.setenv("KDE_KB_CONFIG", str(config))
    migrate_database(path)
    with transaction(path, write=True) as conn:
        uid = preserve_document(conn, "mr", "text", SAMPLE_TEXT, URL)

    model = MockProvider(
        '{"labels":[1]}',
        '{"labels":[1]}',
        '{"labels":[2]}',
    )
    preview = inspect_document(path, uid, provider=model)
    assert preview["metrics"]["calls"] == 3
    assert preview["metrics"]["failed_comments"] == 0
    assert preview["metrics"]["input_tokens"] > 0
    assert preview["metrics"]["output_tokens"] > 0
    assert [c["categories"] for c in preview["proposal"]["source_comments"]] == [
        ["change-request"],
        ["question"],
    ]
    assert [request.schema["properties"]["labels"]["minItems"] for request in model.requests] == [
        2,
        1,
        1,
    ]
    assert [
        f"exactly {count} " in request.system
        for request, count in zip(model.requests, [2, 1, 1], strict=True)
    ] == [True, True, True]
    with transaction(path) as conn:
        assert (
            len([r for r in svc.rows(conn, m.import_checkpoints) if r["stage"] == "classify"]) == 2
        )

    retry = MockProvider('{"labels":[]}')
    resumed = inspect_document(path, uid, provider=retry, refresh=True)
    assert resumed["metrics"]["calls"] == 1
    assert resumed["metrics"]["cached_blocks"] == 2
    assert resumed["metrics"]["input_tokens"] > 0


def test_unclassifiable_single_excerpt_keeps_raw_and_continues(tmp_path, monkeypatch):
    path = tmp_path / "catalog"
    config = tmp_path / "config.toml"
    config.write_text("[llm]\nattempts=2\nblock_size=8192\nnum_predict=64\n")
    monkeypatch.setenv("KDE_KB_CONFIG", str(config))
    migrate_database(path)
    one_comment = SAMPLE_TEXT.split("Méven Car\nMéven Car\n@meven", 1)[0]
    with transaction(path, write=True) as conn:
        uid = preserve_document(conn, "mr", "text", one_comment, URL)

    model = MockProvider('{"labels":[]}', '{"labels":[1,2]}')
    preview = inspect_document(path, uid, provider=model)
    assert preview["metrics"]["calls"] == 2
    assert preview["metrics"]["failed_comments"] == 1
    assert preview["metrics"]["input_tokens"] > 0
    assert preview["metrics"]["output_tokens"] > 0
    assert not preview["proposal"]["source_comments"][0].get("categories")
    with transaction(path) as conn:
        saved = svc.one(conn, m.raw_documents, id=uid)
        assert "Please preserve the original error." in saved["content"]
