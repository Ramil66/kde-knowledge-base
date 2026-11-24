import json
import os
import subprocess
import sys
from pathlib import Path


def test_workflow(tmp_path: Path) -> None:
    env = {**os.environ, "KDE_KB_DATABASE": str(tmp_path / "catalog.sqlite3"), "NO_COLOR": "1"}

    def run(*args: str, data: bytes = b"", check: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [sys.executable, "-m", "kde_kb", *args],
            input=data,
            env=env,
            capture_output=True,
            check=False,
        )

        if check:
            assert result.returncode == 0, result.stderr.decode()
        return result

    assert json.loads(run("db", "status").stdout)["initialized"] is False
    run("init")
    run("db", "migrate")
    run("repo", "add", data=b"kio\nhttps://invent.kde.org/frameworks/kio.git\n")
    assert json.loads(run("repo", "list").stdout)[0]["name"] == "kio"
    run("repo", "update", "kio", "--description", "Resource access")
    assert json.loads(run("search", "resource").stdout)[0]["kind"] == "repo"
    assert run(
        "repo",
        "add",
        "--name",
        "kio",
        "--url",
        "https://invent.kde.org/frameworks/kio",
        check=False,
    ).returncode
    raw = "Заметка\r\n\r\n".encode()
    imported = json.loads(run("import", "mr", "--stdin", data=raw).stdout)
    doc_id = str(imported["document_id"])
    assert imported["entity_id"] is None
    assert run("document", "show", doc_id).stdout == raw
    run("document", "link", doc_id, "repo", "1")
    assert json.loads(run("repo", "show", "kio").stdout)["document_ids"] == [int(doc_id)]
    run("db", "backup", "--output", str(tmp_path / "copy.sqlite3"))
    destination = tmp_path / "export.json"
    run("export", "--format", "json", "--output", str(destination))
    assert (
        json.loads(destination.read_text())["tables"]["raw_documents"][0]["content"] == raw.decode()
    )

    config = tmp_path / "offline.toml"
    config.write_text('[llm]\nprovider = "disabled"\n')
    env["KDE_KB_CONFIG"] = str(config)
    page = b"Bug 42: Sample title\r\n"
    failure = run("import", "task", "--stdin", "--extract", data=page, check=False)
    assert failure.returncode and b"disabled" in failure.stderr
    pending = json.loads(run("document", "list", "--unprocessed").stdout)
    uid = str(next(doc["id"] for doc in pending if doc["document_type"] == "task"))
    assert run("document", "show", uid).stdout == page
    run("task", "add", "--title", "Sample title", "--external-id", "42", "--source", "bugzilla")
    run("task", "update", "42", "--priority", "high")
    run("task", "link-branch", "42", "kio", "topic")
    assert [event["event_type"] for event in json.loads(run("task", "history", "42").stdout)] == [
        "created",
        "priority_changed",
        "branch_linked",
    ]
    assert (
        json.loads(run("repo", "show", "kio", "--full", "--limit", "1").stdout)["tasks"][0]["title"]
        == "Sample title"
    )
