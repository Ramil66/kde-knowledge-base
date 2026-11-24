import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy.exc import IntegrityError

from kde_kb import models as m
from kde_kb import services as s
from kde_kb.database import CatalogError, backup, config, migrate_database, revision, transaction
from kde_kb.exporters import export_json
from kde_kb.importers import detect_format, import_document
from kde_kb.retrieval import search
from kde_kb.schemas import Repository, Task

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "catalog.sqlite3"
    migrate_database(path)
    return path


def test_migrations(db: Path) -> None:
    migrate_database(db)
    with transaction(db, write=True) as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        settings = config()
        settings.attributes["connection"] = conn
        command.downgrade(settings, "base")
        assert revision(conn) is None
        assert not conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='trigger'").all()
    migrate_database(db)
    with transaction(db) as conn:
        assert revision(conn) == "0001"


def test_examples_and_replay(db: Path) -> None:
    with transaction(db, write=True) as conn:
        for kind, filename in (("repo", "repository.json"), ("task", "task.yaml"), ("mr", "mr.md")):
            original = (EXAMPLES / filename).read_bytes().decode()
            fmt = detect_format(filename, kind, original)
            first = import_document(conn, kind, fmt, original)
            assert first.entity_id is not None, first.message
            replay = import_document(conn, kind, fmt, original)
            assert (first.document_id, first.entity_id) == (replay.document_id, replay.entity_id)
        detail = s.mr_detail(conn, "kio", 2214)
        assert detail["document_ids"] == [first.document_id]
        assert detail["branch"]["source_repository_url"].endswith("/example/kio")
        comment = detail["discussions"][0]["comments"][0]
        assert comment["author"]["username"] == "example-user"
        assert comment["positions"][0]["new_line"] == 42
        assert {"comment", "document"} <= {r["kind"] for r in search(conn, "filename boundary")}
        s.link_branch(
            conn,
            "527726",
            "kio",
            "fix/file-name-too-long",
            detail["branch"]["source_repository_url"],
        )
        assert s.task_detail(conn, "example:527726")["branches"][0]["id"] == detail["branch_id"]
        with pytest.raises(IntegrityError), conn.begin_nested():
            s.delete_record(conn, m.repositories, s.resolve_repo(conn, "kio"))
        s.delete_record(conn, m.merge_requests, detail)
        assert not [r for r in search(conn, "filename") if r["kind"] == "comment"]
        assert s.one(conn, m.raw_documents, id=first.document_id)["content"] == original


def test_unstructured_and_retry(db: Path) -> None:
    original = "  first\r\n\r\nlast\r\n"
    with transaction(db, write=True) as conn:
        result = import_document(conn, "mr", "text", original)
        assert result.entity_id is None
        assert s.one(conn, m.raw_documents, id=result.document_id)["content"] == original
        content = (EXAMPLES / "mr.md").read_text()
        missing = import_document(conn, "mr", "markdown", content)
        assert missing.entity_id is None
        import_document(conn, "repo", "json", (EXAMPLES / "repository.json").read_text())
        retry = import_document(conn, "mr", "markdown", content)
        assert retry.document_id == missing.document_id and retry.entity_id is not None
        s.attach(conn, result.document_id, m.merge_requests, retry.entity_id)
        assert set(s.mr_detail(conn, "kio", 2214)["document_ids"]) == {
            result.document_id,
            retry.document_id,
        }


def test_partial_import_rolls_back(db: Path) -> None:
    with transaction(db, write=True) as conn:
        import_document(conn, "repo", "json", (EXAMPLES / "repository.json").read_text())
        content = (
            (EXAMPLES / "mr.md")
            .read_text()
            .replace("body:", "parent_external_id: missing\n        body:")
        )
        result = import_document(conn, "mr", "markdown", content)
        assert result.entity_id is None
        for table in (
            m.branches,
            m.merge_requests,
            m.users,
            m.comments,
            m.discussions,
            m.code_positions,
        ):
            assert not s.rows(conn, table)
        assert s.one(conn, m.raw_documents, id=result.document_id)["content"] == content


def test_update_search_export_backup(db: Path, tmp_path: Path) -> None:
    with transaction(db, write=True) as conn:
        item = Task(title="Unicode задача")
        task_id = s.store(conn, item)
        assert search(conn, "задача")[0]["entity_id"] == task_id
        s.update_fields(conn, m.tasks, s.resolve_task(conn, f"#{task_id}"), {"title": "Проверка"})
        assert not search(conn, "задача")
        assert search(conn, "проверка")
        with pytest.raises(CatalogError):
            search(conn, '"!')
    export = tmp_path / "data.json"
    with transaction(db) as conn:
        export_json(conn, export)
        with pytest.raises(FileExistsError):
            export_json(conn, export)
    assert json.loads(export.read_text())["tables"]["tasks"][0]["title"] == "Проверка"
    snapshot = tmp_path / "backup.sqlite3"
    backup(db, snapshot)
    with pytest.raises(FileExistsError):
        backup(db, snapshot)
    with transaction(snapshot) as conn:
        assert (
            search(conn, "проверка") and not conn.exec_driver_sql("PRAGMA foreign_key_check").all()
        )
    with transaction(db, write=True) as conn:
        s.delete_record(conn, m.tasks, s.resolve_task(conn, str(task_id)))
        assert not search(conn, "проверка")


def test_identity_and_input(db: Path) -> None:
    with transaction(db, write=True) as conn:
        for url in (
            "https://Invent.KDE.org/frameworks/kio.git/",
            "git@invent.kde.org:frameworks/kio.git",
        ):
            s.store(conn, Repository(name="kio", url=url))
        assert len(s.rows(conn, m.repositories)) == 1
        s.store(conn, Repository(name="kio", url="https://invent.kde.org/fork/kio"))
        with pytest.raises(CatalogError, match="Use its URL"):
            s.resolve_repo(conn, "kio")
        for source in ("invent", "bugzilla"):
            s.store(conn, Task(title="Example", external_id="42", source=source))
        with pytest.raises(CatalogError, match="Ambiguous"):
            s.resolve_task(conn, "42")
        assert s.resolve_task(conn, "bugzilla:42")["source"] == "bugzilla"
        for kind, fmt, content in (
            ("task", "json", "{"),
            ("task", "json", '{"title":"first","title":"second"}'),
            ("task", "yaml", "title: first\ntitle: second"),
            ("repo", "yaml", "!!python/object:evil {}"),
            ("diff", "diff", "diff --git a/a b/a\n"),
        ):
            result = import_document(conn, kind, fmt, content)
            assert result.entity_id is None
            assert s.one(conn, m.raw_documents, id=result.document_id)["content"] == content


def test_concurrent_import(db: Path) -> None:
    def add(_: int) -> tuple[int, int | None]:
        with transaction(db, write=True) as conn:
            result = import_document(conn, "task", "json", '{"title": "Local task"}')
            return result.document_id, result.entity_id

    with ThreadPoolExecutor(4) as pool:
        assert set(pool.map(add, range(8))) == {(1, 1)}
    with transaction(db) as conn:
        assert len(s.rows(conn, m.tasks)) == len(s.rows(conn, m.raw_documents)) == 1


def test_incremental_history_and_relations(db: Path) -> None:
    with transaction(db, write=True) as conn:
        s.store(conn, Repository(name="base", url="https://invent.kde.org/example/base"))
        repo = import_document(
            conn,
            "repo",
            "json",
            json.dumps(
                {
                    "name": "kio",
                    "url": "https://invent.kde.org/frameworks/kio",
                    "depends_on": ["base"],
                }
            ),
        )
        assert repo.entity_id
        s.store(conn, Task(title="Earlier task", source="invent", external_id="20"))
        payload = {
            "title": "New task",
            "events": [{"event_type": "tested", "description": "Passed"}],
            "depends_on": ["invent:20"],
        }
        result = import_document(conn, "task", "json", json.dumps(payload))
        assert s.task_detail(conn, f"#{result.entity_id}")["events"][0]["event_type"] == "tested"
        payload = {
            "repo": "kio",
            "external_id": 1,
            "title": "First",
            "source_branch": "topic",
            "source_repository_url": "https://invent.kde.org/example/kio",
        }
        first = import_document(conn, "mr", "json", json.dumps(payload))
        payload.update(
            external_id=2,
            depends_on=[{"repo": "kio", "external_id": 1}],
            test_runs=[{"command": "ctest", "result": "passed"}],
            commits=[
                {
                    "sha": "01234567",
                    "repository": "kio",
                    "changed_files": [{"new_path": "a.cpp", "diff": "+text"}],
                }
            ],
        )
        second = import_document(conn, "mr", "json", json.dumps(payload))
        assert first.entity_id != second.entity_id
        payload["title"] = "Second version"
        updated = import_document(conn, "mr", "json", json.dumps(payload))
        assert updated.entity_id == second.entity_id and updated.document_id != second.document_id
        detail = s.mr_detail(conn, "kio", 2)
        assert detail["commits"][0]["changed_files"][0]["diff"] == "+text"
        assert len(detail["test_runs"]) == 1
        assert detail["title"] == "Second version"
        for table in ("repository_dependencies", "task_dependencies", "mr_dependencies"):
            assert len(s.rows(conn, m.metadata.tables[table])) == 1
