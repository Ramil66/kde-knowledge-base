from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import select

from kde_kb import models as m
from kde_kb import services as svc
from kde_kb.batch import _resolve, batch_import
from kde_kb.database import config, migrate_database, transaction
from kde_kb.import_documents import parse_mr, parse_task, resolve_mr_ref
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
    assert parsed["record"]
    if path.parent.name == "mr":
        assert parsed["record"]["external_id"] > 0
        assert parsed["record"]["commits"] is not None
    else:
        assert parsed["metadata"]["id"].startswith("task-")


def test_big_page_and_historical_commits():
    parsed = parse_mr(
        (ROOT / "mr/plasma-workspace-6958.txt").read_text(), "plasma-workspace-6958.txt"
    )
    assert len(parsed["record"]["commits"]) == 40
    assert all(commit.get("author") for commit in parsed["record"]["commits"])
    assert parsed["bug_ids"] == [384782]
    assert "frameworks/kirigami!2156" in parsed["related_mrs"]
    assert {
        "reference": "frameworks/kirigami!2156",
        "type": "depends_on",
    } in parsed["mr_references"]
    assert parsed["record"]["external_id"] == 6958
    discussions = parsed["record"]["discussions"]
    assert discussions
    assert discussions[0]["is_resolved"] is True
    comment = discussions[0]["comments"][0]
    assert comment["author"]["username"]
    assert comment["source_start_line"] <= comment["source_end_line"]
    original_lines = (ROOT / "mr/plasma-workspace-6958.txt").read_text().splitlines()
    assert original_lines[comment["source_start_line"] - 1].strip()


@pytest.mark.parametrize("filename", ["kio-2173.txt", "kio-2175.txt", "kcodecs-110.txt"])
def test_empty_url_mr_uses_filename_without_fabricating_source_data(filename):
    parsed = parse_mr("URL: \nCOPIED: 2026-11-25\n\n", filename)
    number = int(filename.rsplit("-", 1)[1].removesuffix(".txt"))
    assert parsed["record"]["external_id"] == number
    assert parsed["record"]["title"] == f"!{number} (title unavailable)"
    assert parsed["record"]["commits"] == []


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
        assert len(conn.execute(select(m.merge_requests)).all()) == 4
        assert len(conn.execute(select(m.tasks)).all()) == 2
        assert len(conn.execute(select(m.bug_reports)).all()) == 2
        assert len(conn.execute(select(m.mr_bug_reports)).all()) == 1
        assert len(conn.execute(select(m.task_bug_reports)).all()) == 2
        assert len(conn.execute(select(m.task_merge_requests)).all()) == 4
        pending = conn.execute(select(m.unresolved_links)).mappings().one()
        assert pending["link_type"] == "depends_on"
        assert len(conn.execute(select(m.comments)).all()) > 0
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    with transaction(db, write=True) as conn:
        repo_url = "https://invent.kde.org/frameworks/kirigami"
        svc.store(conn, Repository(name="kirigami", url=repo_url))
        svc.store(
            conn,
            MergeRequest(repo=repo_url, external_id=2156, title="Fixture target MR"),
        )
        assert _resolve(conn) == 1
        assert len(conn.execute(select(m.unresolved_links)).all()) == 0
        relation = conn.execute(select(m.mr_relations)).mappings().one()
        assert relation["relation_type"] == "depends_on"
    changed = source / "mr" / "plasma-workspace-6958.txt"
    changed.write_text(changed.read_text() + "\n")
    update = batch_import(db, source)
    assert update["created"] == 1 and update["errors"] == 0
    with transaction(db) as conn:
        assert len(conn.execute(select(m.merge_requests)).all()) == 5
        assert len(conn.execute(select(m.raw_documents)).all()) == 7


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
