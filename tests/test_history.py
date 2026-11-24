import json
from pathlib import Path

import pytest
from alembic import command

from kde_kb import models as m
from kde_kb import services as s
from kde_kb.database import CatalogError, config, migrate_database, transaction
from kde_kb.importers import import_document
from kde_kb.schemas import Repository, Task


def test_migration_from_populated_mvp(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    with transaction(path, migrate=True) as conn:
        settings = config()
        settings.attributes["connection"] = conn
        command.upgrade(settings, "0001")
        for sql in (
            "INSERT INTO repositories(id,name,url) VALUES(1,'r','https://example.org/r')",
            "INSERT INTO branches(id,repository_id,name,source_repository_url) VALUES(1,1,'topic','https://example.org/r')",
            "INSERT INTO merge_requests(id,branch_id,target_repository_id,external_id,title,status) VALUES(1,1,1,12,'Title','Opened')",
            "INSERT INTO commits(id,repository_id,sha) VALUES(1,1,'abc')",
            "INSERT INTO mr_commits VALUES(1,1)",
            "INSERT INTO tasks(id,title,status,completed_at) VALUES(1,'Task','Custom','yesterday')",
            "INSERT INTO task_branches(task_id,branch_id) VALUES(1,1)",
        ):
            conn.exec_driver_sql(sql)
    migrate_database(path)
    migrate_database(path)
    with transaction(path) as conn:
        detail = s.mr_detail(conn, "r", 12)
        assert detail["external_status"] == "Opened" and detail["status"] == "open"
        assert detail["revisions"][0]["kind"] == "legacy"
        assert detail["revisions"][0]["commit_ids"] == [1]
        task = s.task_detail(conn, "#1")
        assert task["merge_requests"][0]["id"] == 1
        assert task["external_status"] == "Custom" and task["status"] == "unknown"
        assert task["created_at"].endswith("Z")
        assert s.rows(conn, m.legacy_values)[0]["original"] == "yesterday"
        assert not conn.exec_driver_sql("PRAGMA foreign_key_check").all()


def test_snapshots_and_partial_import(tmp_path: Path) -> None:
    path = tmp_path / "catalog"
    migrate_database(path)
    with transaction(path, write=True) as conn:
        s.store(conn, Repository(name="r", url="https://example.org/r"))
        mr = {
            "repo": "r",
            "title": "MR",
            "external_id": 1,
            "source_branch": "topic",
            "source_repository_url": "https://example.org/r",
        }
        for commits, complete in ((["a", "b"], True), (["c"], False), (["d"], True), ([], True)):
            mr["commits"] = [{"repository": "r", "sha": sha} for sha in commits]
            mr["revision"] = {"complete": complete}
            doc = import_document(conn, "mr", "json", json.dumps(mr))
            assert doc.entity_id, doc.message
            detail = s.mr_detail(conn, "r", 1)
            expected = commits if complete else ["a", "b", "c"]
            assert [r["sha"] for r in detail["commits"]] == expected
            assert detail["revisions"][-1]["source_document_id"] == doc.document_id
            import_document(conn, "mr", "json", json.dumps(mr))
        assert len(s.rows(conn, m.mr_revisions)) == len(s.rows(conn, m.commits)) == 4
        del mr["commits"]
        assert not import_document(conn, "mr", "json", json.dumps(mr)).entity_id


def test_lifecycle_and_context(tmp_path: Path) -> None:
    path = tmp_path / "catalog"
    migrate_database(path)
    with transaction(path, write=True) as conn:
        s.store(conn, Repository(name="r", url="https://example.org/r"))
        payload = {
            "title": "T",
            "external_id": "1",
            "source": "bugzilla",
            "status": "CONFIRMED",
            "created_at": "2026-11-01T13:00:00+03:00",
        }
        result = import_document(conn, "task", "json", json.dumps(payload))
        assert result.entity_id
        assert s.resolve_task(conn, "1")["created_at"] == "2026-11-01T10:00:00Z"
        for status in ("completed", "completed", "reopened"):
            s.update_fields(conn, m.tasks, s.resolve_task(conn, "1"), {"status": status})
        s.update_fields(
            conn,
            m.tasks,
            s.resolve_task(conn, "1"),
            {"priority": "high", "solution_summary": "covered"},
        )
        for _ in range(2):
            s.link_branch(conn, "1", "r", "topic", None)
        mr = {
            "repo": "r",
            "external_id": 1,
            "title": "MR",
            "source_branch": "topic",
            "source_repository_url": "https://example.org/r",
        }
        assert import_document(conn, "mr", "json", json.dumps(mr)).entity_id
        task = s.task_detail(conn, "1")
        events = [e["event_type"] for e in task["events"]]
        assert events == [
            "created",
            "completed",
            "reopened",
            "priority_changed",
            "solution_changed",
            "branch_linked",
            "mr_linked",
        ]
        assert task["events"][1]["previous_value"] == "open"
        assert task["sources"] and task["repositories"][0]["name"] == "r"
        assert s.repo_detail(conn, "r")["tasks"][0]["id"] == result.entity_id
        assert "events" not in s.task_detail(conn, "1", depth=0)
        assert len(s.task_detail(conn, "1", limit=1)["events"]) == 1
        with pytest.raises(CatalogError):
            s.update_fields(conn, m.tasks, s.resolve_task(conn, "1"), {"status": "typo"})
        for date in ("yesterday", "2026-11-01"):
            with pytest.raises(ValueError):
                Task(title="T", created_at=date)
