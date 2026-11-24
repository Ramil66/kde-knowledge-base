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
