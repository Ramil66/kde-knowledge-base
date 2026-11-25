from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import select

from kde_kb import models as m
from kde_kb import services as svc
from kde_kb.batch import _resolve, batch_import
from kde_kb.database import config, migrate_database, transaction
from kde_kb.import_documents import parse_mr, parse_task, resolve_mr_ref
from kde_kb.providers import MockProvider
from kde_kb.schemas import MergeRequest, Repository

ROOT = Path(__file__).resolve().parents[1] / "imports"
SIX = [
    ROOT / "mr" / "plasma-workspace-6958.txt",
    ROOT / "mr" / "kio-2173.txt",
    ROOT / "mr" / "kio-2175.txt",
    ROOT / "mr" / "kcodecs-110.txt",
    ROOT / "tasks" / "task-114.txt",
    ROOT / "tasks" / "task-120.txt",
]


@pytest.mark.parametrize("path", SIX)
def test_six_documents_parse(path):
    text = path.read_bytes().decode()
    parsed = parse_mr(text, path.name) if path.parent.name == "mr" else parse_task(text)
    if path.parent.name == "mr" and parsed.get("empty_template"):
        assert parsed["record"] is None
        return
    assert parsed["record"]
    if path.parent.name == "mr":
        assert parsed["record"]["external_id"] > 0
        assert parsed["record"]["commits"] is not None
    else:
        assert parsed["metadata"]["id"].startswith("task-")


def test_big_page_and_historical_commits():
    source = (ROOT / "mr/plasma-workspace-6958.txt").read_text()
    parsed = parse_mr(source, "plasma-workspace-6958.txt")
    assert len(parsed["record"]["commits"]) == 40
    assert all(commit.get("author") for commit in parsed["record"]["commits"])
    assert parsed["bug_ids"] == [384782]
    assert "frameworks/kirigami!2156" in parsed["related_mrs"]
    assert {
        "reference": "frameworks/kirigami!2156",
        "type": "depends_on",
    } in parsed["mr_references"]
    assert parsed["record"]["external_id"] == 6958
    assert parsed["record"]["author"]["username"] == "ramilnur"
    assert parsed["record"]["url"] == (
        "https://invent.kde.org/plasma/plasma-workspace/-/merge_requests/6958"
    )
    discussions = parsed["record"]["discussions"]
    assert len(discussions) > 1
    assert discussions[0]["is_resolved"] is True
    comment = discussions[0]["comments"][0]
    assert comment["author"]["username"]
    assert comment["source_start_line"] <= comment["source_end_line"]
    all_comments = [item for discussion in discussions for item in discussion["comments"]]
    assert any("parent_index" in item for item in all_comments)
    assert all("Reply…" not in item["body"] for item in all_comments)
    kio = parse_mr((ROOT / "mr/kio-2446.txt").read_text(), "kio-2446.txt")
    kio_comments = [
        item for discussion in kio["record"]["discussions"] for item in discussion["comments"]
    ]
    assert all("Suggested change" not in item["body"] for item in kio_comments)
    assert all("Applied" not in item["body"] for item in kio_comments)
    locations = [location for item in all_comments for location in item.get("review_locations", [])]
    assert any(location["path"].endswith("ConfigGeneral.qml") for location in locations)
    assert any(location.get("code_context") for location in locations)
    original_lines = source.splitlines()
    assert original_lines[comment["source_start_line"] - 1].strip()


@pytest.mark.parametrize("filename", ["kio-2173.txt", "kio-2175.txt", "kcodecs-110.txt"])
def test_empty_url_mr_uses_filename_without_fabricating_source_data(filename):
    parsed = parse_mr("URL: \nCOPIED: 2026-11-25\n\n", filename)
    assert parsed["empty_template"]
    assert parsed["record"] is None


@pytest.mark.parametrize(
    "ref,expected",
    [
        ("!9090", (None, 9090)),
        ("kscreen!432", ("https://invent.kde.org/kscreen", 432)),
        ("frameworks/kirigami!2156", ("https://invent.kde.org/frameworks/kirigami", 2156)),
    ],
)
def test_short_refs(ref, expected):
    assert resolve_mr_ref(ref) == expected


def test_batch_repeat_and_db_migration(tmp_path):
    db = tmp_path / "catalog.sqlite3"
    migrate_database(db)
    source = tmp_path / "imports"
    (source / "mr").mkdir(parents=True)
    (source / "tasks").mkdir()
    for src in SIX:
        target = source / src.parent.name / src.name
        target.write_bytes(src.read_bytes())
    first = batch_import(db, source, dry_run=False)
    assert first["errors"] == 0 and first["created"] == 6
    again = batch_import(db, source)
    assert again["skipped"] == 6 and again["created"] == 0
    with transaction(db) as conn:
        assert len(conn.execute(select(m.raw_documents)).all()) == 6
        assert len(conn.execute(select(m.merge_requests)).all()) == 1
        assert first["empty_templates"] == 3
        assert len(conn.execute(select(m.tasks)).all()) == 2
        assert len(conn.execute(select(m.bug_reports)).all()) == 2
        assert len(conn.execute(select(m.mr_bug_reports)).all()) == 1
        assert len(conn.execute(select(m.task_bug_reports)).all()) == 2
        assert len(conn.execute(select(m.task_merge_requests)).all()) == 1
        large = (
            conn.execute(
                select(m.raw_documents).where(
                    m.raw_documents.c.source_name == "plasma-workspace-6958.txt"
                )
            )
            .mappings()
            .one()
        )
        assert large["source_url"] == (
            "https://invent.kde.org/plasma/plasma-workspace/-/merge_requests/6958"
        )
        assert large["content"] == (ROOT / "mr/plasma-workspace-6958.txt").read_text()
        pending = conn.execute(select(m.unresolved_links)).mappings().all()
        assert any(item["link_type"] == "depends_on" for item in pending)
        stored_comments = conn.execute(select(m.comments)).mappings().all()
        assert len(stored_comments) == 176
        assert sum(comment["parent_comment_id"] is not None for comment in stored_comments) > 0
        assert any(comment["review_locations"] for comment in stored_comments)
        assert all("Reply…" not in comment["body"] for comment in stored_comments)
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    with transaction(db, write=True) as conn:
        repo_url = "https://invent.kde.org/frameworks/kirigami"
        svc.store(conn, Repository(name="kirigami", url=repo_url))
        svc.store(
            conn,
            MergeRequest(repo=repo_url, external_id=2156, title="Fixture target MR"),
        )
        assert _resolve(conn) == 1
        remaining = conn.execute(select(m.unresolved_links)).mappings().all()
        assert all(item["source_type"] == "task" for item in remaining)
        relation = conn.execute(select(m.mr_relations)).mappings().one()
        assert relation["relation_type"] == "depends_on"
    changed = source / "mr" / "plasma-workspace-6958.txt"
    changed.write_text(changed.read_text() + "\n")
    update = batch_import(db, source)
    assert update["created"] == 1 and update["errors"] == 0
    with transaction(db) as conn:
        assert len(conn.execute(select(m.merge_requests)).all()) == 2
        assert len(conn.execute(select(m.raw_documents)).all()) == 7
        assert len(conn.execute(select(m.comments)).all()) == 176


def test_upgrade_from_previous_revision_preserves_documents(tmp_path):
    db = tmp_path / "previous.sqlite3"
    with transaction(db, migrate=True) as conn:
        settings = config()
        settings.attributes["connection"] = conn
        command.upgrade(settings, "0005")
        conn.exec_driver_sql(
            "INSERT INTO raw_documents "
            "(document_type, format, content, content_hash) VALUES ('mr','text','old','hash')"
        )
    migrate_database(db)
    with transaction(db) as conn:
        old = conn.execute(select(m.raw_documents)).mappings().one()
        assert old["content"] == "old"
        assert old["source_name"] is None
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_batch_reclassifies_existing_document_without_duplicate_entities(tmp_path, monkeypatch):
    source_file = ROOT / "mr" / "kio-2446.txt"
    source = tmp_path / "imports"
    (source / "mr").mkdir(parents=True)
    (source / "mr" / source_file.name).write_bytes(source_file.read_bytes())
    config_file = tmp_path / "config.toml"
    config_file.write_text("[llm]\nattempts=1\nblock_size=8192\nnum_predict=64\n")
    monkeypatch.setenv("KDE_KB_CONFIG", str(config_file))
    db = tmp_path / "catalog.sqlite3"
    migrate_database(db)

    inserted = batch_import(db, source)
    parsed = parse_mr(source_file.read_text(), source_file.name)
    comment_count = sum(
        len(discussion["comments"]) for discussion in parsed["record"]["discussions"]
    )
    provider = MockProvider('{"labels":[' + ",".join(["1"] * comment_count) + "]}")
    classified = batch_import(db, source, extract=True, provider=provider)

    assert inserted["created"] == 1
    assert classified["skipped"] == 1
    assert classified["llm_calls"] == 1 and len(provider.requests) == 1
    assert classified["classified_comments"] == comment_count
    with transaction(db) as conn:
        assert len(conn.execute(select(m.merge_requests)).all()) == 1
        comments = conn.execute(select(m.comments)).mappings().all()
        assert len(comments) == comment_count
        assert all(comment["category"] == "change-request" for comment in comments)
