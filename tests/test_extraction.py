import json
from pathlib import Path

import pytest

from kde_kb import models as m
from kde_kb import services as s
from kde_kb.database import CatalogError, migrate_database, transaction
from kde_kb.extraction import confirm_document, document_list, inspect_document
from kde_kb.importers import preserve_document
from kde_kb.providers import MockProvider, ProviderError
from kde_kb.schemas import Repository


@pytest.fixture
def catalog(tmp_path: Path, monkeypatch) -> Path:
    db = tmp_path / "catalog"
    config = tmp_path / "config.toml"
    config.write_text("[llm]\nattempts = 2\n")
    monkeypatch.setenv("KDE_KB_CONFIG", str(config))
    migrate_database(db)
    return db


def test_review_raw_invent(catalog: Path) -> None:
    content = (
        "!24 Fix errors\r\nopened\r\nActivity\r\nDev @dev commented yesterday\r\n"
        "Resolved yesterday by Dev\r\nfirst comment\r\nAuthor @author replied yesterday\r\nreply\r\nReply…\r\n"
    )
    with transaction(catalog, write=True) as conn:
        uid = preserve_document(
            conn, "mr", "text", content, "https://invent.kde.org/frameworks/kio/-/merge_requests/24"
        )
    model = MockProvider('{"labels":[0,4]}')
    inspect_document(catalog, uid, provider=model, dry_run=True)
    with transaction(catalog) as conn:
        assert not s.rows(conn, m.document_extractions) and not s.rows(conn, m.import_checkpoints)
        assert s.one(conn, m.raw_documents, id=uid)["content"] == content
    model = MockProvider("invalid", '{"labels":[0,4]}')
    preview = inspect_document(catalog, uid, provider=model)
    assert preview["status"] == "pending" and len(model.requests) == 2
    assert json.loads(model.requests[0].document) == ["first comment", "reply"]
    assert preview == inspect_document(catalog, uid, provider=MockProvider())
    with transaction(catalog) as conn:
        assert not s.rows(conn, m.merge_requests) and document_list(conn, True)
    for _ in range(2):
        assert confirm_document(catalog, uid)["entity_ids"] == [1]
    with transaction(catalog) as conn:
        detail = s.mr_detail(conn, "kio", 24)
        assert detail["branch"] is None
        assert detail["document_ids"] == [uid]
        assert detail["status"] == "open" and detail["external_status"] == "opened"
        a, b = detail["discussions"][0]["comments"]
        assert a["external_id"] is None and a["local_key"]
        assert b["parent_comment_id"] == a["id"]
        assert not document_list(conn, True)
        assert (
            preserve_document(
                conn,
                "mr",
                "text",
                content,
                "https://invent.kde.org/frameworks/kio/-/merge_requests/24",
            )
            == uid
        )


def test_local_ids_and_partial_records(catalog: Path) -> None:
    for title in ("First", "Second"):
        content = f"{title}\nOpen\n{title}\ntopic into target\n"
        with transaction(catalog, write=True) as conn:
            s.store(conn, Repository(name="r", url="https://example.org/r"))
            uid = preserve_document(conn, "mr", "text", content)
        inspect_document(catalog, uid, no_llm=True)
        assert confirm_document(catalog, uid, repo="r")["identity_origin"] == "local"
    with transaction(catalog) as conn:
        assert len(s.rows(conn, m.merge_requests)) == 2
        assert s.mr_detail(conn, "r", 2, local=True)["external_id"] is None


def test_model_errors_and_conflicts(catalog: Path) -> None:
    with transaction(catalog, write=True) as conn:
        uid = preserve_document(conn, "repo", "text", "New name https://example.org/r")
        s.store(conn, Repository(name="Reviewed", url="https://example.org/r"))
    for model in (
        MockProvider(),
        MockProvider("invalid", "null"),
        MockProvider(
            *[json.dumps({"record": {"name": "Made up", "url": "https://example.org/r"}})] * 2
        ),
    ):
        with pytest.raises(ProviderError):
            inspect_document(catalog, uid, provider=model)
    inspect_document(
        catalog,
        uid,
        provider=MockProvider(
            json.dumps({"record": {"name": "New name", "url": "https://example.org/r"}})
        ),
    )
    with pytest.raises(CatalogError, match="conflicts"):
        confirm_document(catalog, uid)
    with transaction(catalog) as conn:
        assert s.resolve_repo(conn, "Reviewed")
        assert s.rows(conn, m.document_extractions)[0]["status"] == "pending"
        assert not s.rows(conn, m.document_links)
        assert s.one(conn, m.raw_documents, id=uid)["content"].startswith("New name")


def test_disabled_and_manual_workflow(catalog: Path, monkeypatch, tmp_path: Path) -> None:
    config = tmp_path / "disabled.toml"
    config.write_text('[llm]\nprovider="disabled"\n')
    monkeypatch.setenv("KDE_KB_CONFIG", str(config))
    with transaction(catalog, write=True) as conn:
        uid = preserve_document(conn, "task", "text", "Bug 33: Fix title")
        s.store(conn, Repository(name="r", url="https://example.org/r"))
    with pytest.raises(ProviderError, match="disabled"):
        inspect_document(catalog, uid)
    inspect_document(
        catalog,
        uid,
        provider=MockProvider(
            '{"record":{"title":"Fix title","source":"bugzilla","external_id":"33"}}'
        ),
    )
    confirm_document(catalog, uid)
    with transaction(catalog) as conn:
        assert s.resolve_task(conn, "bugzilla:33")


def test_partial_preview_then_manual_link(catalog: Path) -> None:
    with transaction(catalog, write=True) as conn:
        uid = preserve_document(conn, "task", "text", "priority: high")
    draft = inspect_document(catalog, uid, provider=MockProvider('{"record":{"priority":"high"}}'))
    assert draft["id"] and draft["proposal"]["record"] == {"priority": "high"}
    with pytest.raises(ValueError, match="title"):
        confirm_document(catalog, uid)
    from kde_kb.schemas import Task

    with transaction(catalog, write=True) as conn:
        entity = s.store(conn, Task(title="Known task"))
        s.attach(conn, uid, m.tasks, entity)
    assert confirm_document(catalog, uid)["entity_ids"] == [entity]
    with transaction(catalog) as conn:
        assert s.resolve_task(conn, f"#{entity}")["priority"] == "high"
        assert len(s.rows(conn, m.tasks)) == 1


def test_unsupported_timestamp(catalog: Path) -> None:
    with transaction(catalog, write=True) as conn:
        uid = preserve_document(conn, "task", "text", "Title")
    fabricated = json.dumps({"record": {"title": "Title", "created_at": "2024-01-01T00:00:00Z"}})
    with pytest.raises(ProviderError, match="timestamp evidence"):
        inspect_document(catalog, uid, provider=MockProvider(fabricated, fabricated))


@pytest.mark.parametrize(
    "provider_name,endpoint", [("ollama", "/api/chat"), ("openai-compatible", "/chat/completions")]
)
def test_chat_protocol_and_transport_failure(
    monkeypatch, provider_name: str, endpoint: str
) -> None:
    from io import BytesIO
    from urllib.error import URLError

    from kde_kb import providers

    config = providers.Settings(
        provider=provider_name,
        base_url="http://example.test",
        model="test",
        temperature=0,
        timeout=9,
        attempts=1,
    )
    prompt = providers.GenerationRequest("system", "original", {"type": "object"}, "v1")
    seen = []
    monkeypatch.setenv("KDE_KB_LLM_API_KEY", "test-token")

    class Transport:
        def open(self, request, timeout):
            seen.append(request)
            assert timeout == 9
            payload = json.loads(request.data)
            assert payload["messages"] == [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "original"},
            ]
            if endpoint == "/api/chat":
                assert payload["format"] == prompt.schema and payload["stream"] is True
                assert request.get_header("Authorization") is None
                return BytesIO(
                    b'{"message":{"content":"{"},"done":false}\n{"message":{"content":"}"},"done":true,"eval_count":2,"prompt_eval_count":10}\n'
                )
            assert request.get_header("Authorization") == "Bearer test-token"
            return BytesIO(
                b'data: {"choices":[{"delta":{"content":"{}"}}]}\n\ndata: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":2}}\n\ndata: [DONE]\n\n'
            )

    monkeypatch.setattr(providers, "build_opener", lambda *_: Transport())
    provider = providers.configured_provider(config)
    generated = provider.generate(prompt)
    assert (generated.content, generated.input_tokens, generated.output_tokens) == ("{}", 10, 2)
    assert seen[0].full_url == "http://example.test" + endpoint

    def unavailable(*args, **kwargs):
        raise URLError("offline")

    monkeypatch.setattr(Transport, "open", unavailable)
    with pytest.raises(ProviderError, match="unavailable"):
        provider.generate(prompt)


def test_refresh_preserves_confirmed_preview(catalog: Path) -> None:
    with transaction(catalog, write=True) as conn:
        uid = preserve_document(conn, "task", "text", "Title\nPriority high")
    first = '{"record":{"title":"Title"}}'
    second = '{"record":{"title":"Title","priority":"high"}}'
    proposal = inspect_document(catalog, uid, provider=MockProvider(first))
    confirm_document(catalog, uid)
    assert (
        inspect_document(catalog, uid, refresh=True, provider=MockProvider(first))["id"]
        == proposal["id"]
    )
    next_proposal = inspect_document(catalog, uid, refresh=True, provider=MockProvider(second))
    assert next_proposal["id"] != proposal["id"]
    with transaction(catalog) as conn:
        assert s.resolve_task(conn, "#1")["priority"] is None
        assert len(document_list(conn)) == len(document_list(conn, True)) == 1
        assert s.one(conn, m.document_extractions, id=proposal["id"])["status"] == "confirmed"
    confirm_document(catalog, uid)
    with transaction(catalog) as conn:
        assert s.resolve_task(conn, "#1")["priority"] == "high"
        assert len(s.rows(conn, m.tasks)) == 1
        assert s.one(conn, m.document_extractions, id=proposal["id"])["status"] == "superseded"
