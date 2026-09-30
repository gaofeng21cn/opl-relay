import json
import sqlite3
from pathlib import Path

import pytest

from codex_mail_workbench.context import ContextBuilder
from codex_mail_workbench.memory import MemoryStore
from codex_mail_workbench.people import (
    ALIASES_LIMIT,
    MEMORIES_PER_ENTITY,
    SOURCE_REFS_LIMIT,
    SUMMARY_CHARS,
    read_memory_evidence,
    read_people,
)


def propose(store: MemoryStore, entity_ref: str, content: str, *, sources=None, **kwargs):
    return store.propose_memory(
        entity_ref=entity_ref,
        category="fact",
        content=content,
        sources=sources or [{"source_ref": "email-store://work/INBOX/1/abcdef", "source_kind": "email"}],
        **kwargs,
    )


def test_people_reuses_entity_identity_and_approved_evidence(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.sqlite")
    original = store.upsert_entity(kind="person", canonical_name="Original Name", emails=["person@example.test"])
    renamed = store.upsert_entity(
        kind="person", canonical_name="Current Name", aliases=["Colleague"], emails=["person@example.test"]
    )
    memory = propose(store, renamed["entity_ref"], "Prefers a concise update.")
    store.approve(memory["memory_ref"])
    result = read_people(store)
    assert result["count"] == 1
    person = result["items"][0]
    assert person["id"] == person["entity_ref"] == original["entity_ref"] == renamed["entity_ref"]
    assert person["name"] == "Current Name"
    assert set(person["aliases"]) == {"Original Name", "Current Name", "Colleague"}
    assert person["kind"] == "person"
    assert person["approved_memory_count"] == 1
    assert person["approved_memories"][0]["memory_ref"] == memory["memory_ref"]
    assert person["source_refs"] == ["email-store://work/INBOX/1/abcdef"]
    assert "emails" not in person
    assert "person@example.test" not in json.dumps(result)
    assert result["source_policy"]["read_only"] is True
    assert result["source_policy"]["candidate_review_only"] is True
    assert result["source_policy"]["candidate_in_default_context"] is False


def test_only_approved_memory_is_projected_or_matches_people_search(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.sqlite")
    entity = store.upsert_entity(kind="person", canonical_name="Colleague")
    candidate = propose(store, entity["entity_ref"], "candidate-only marker")
    rejected = propose(store, entity["entity_ref"], "rejected-only marker")
    store.reject(rejected["memory_ref"])
    forgotten = propose(store, entity["entity_ref"], "forgotten-only marker")
    store.approve(forgotten["memory_ref"])
    store.forget(forgotten["memory_ref"])
    superseded = propose(store, entity["entity_ref"], "superseded-only marker")
    store.approve(superseded["memory_ref"])
    current = propose(store, entity["entity_ref"], "current approved marker", supersedes_ref=superseded["memory_ref"])
    store.approve(current["memory_ref"])
    result = read_people(store)
    assert result["items"][0]["approved_memory_count"] == 1
    assert [item["memory_ref"] for item in result["items"][0]["approved_memories"]] == [current["memory_ref"]]
    assert [item["memory_ref"] for item in read_memory_evidence(store)["items"]] == [current["memory_ref"]]
    for marker in ("candidate-only", "rejected-only", "forgotten-only", "superseded-only"):
        assert read_people(store, query=marker)["count"] == 0
        assert marker not in json.dumps(result)
    assert store.get_memory(candidate["memory_ref"])["status"] == "candidate"

    context = ContextBuilder(
        memory_db_path=store.path, mail_db_path=tmp_path / "mail.sqlite",
        sources_config_path=tmp_path / "sources.toml",
    ).build(person="Colleague")
    assert [item["memory_ref"] for item in context["approved_memories"]] == [current["memory_ref"]]


def test_people_filters_before_limit_and_searches_all_existing_entities(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.sqlite")
    for index in range(501):
        store.upsert_entity(kind="person", canonical_name=f"A {index:03d}")
    last = store.upsert_entity(
        kind="organization", canonical_name="Z Institute", aliases=["Stra\u00dfe", "Percent_%"],
        emails=["institute@example.test"],
    )
    memory = propose(store, last["entity_ref"], "Approved collaboration topic")
    store.approve(memory["memory_ref"])
    for query in ("Z institute", "STRASSE", "institute@example.test", "collaboration", "_%"):
        result = read_people(store, query=query, limit=1)
        assert [item["entity_ref"] for item in result["items"]] == [last["entity_ref"]]
        assert result["has_more"] is False
    assert read_people(store, query="unknown")["count"] == 0
    assert read_people(store, limit=1)["has_more"] is True
    assert read_people(store, limit=500)["count"] == 500


def test_people_includes_entities_without_memories_and_existing_kinds(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.sqlite")
    for kind in ("person", "organization", "project"):
        store.upsert_entity(kind=kind, canonical_name=kind)
    result = read_people(store)
    assert {item["kind"] for item in result["items"]} == {"person", "organization", "project"}
    for item in result["items"]:
        assert item["approved_memory_count"] == 0
        assert item["approved_memories"] == item["source_refs"] == []
        field = "project" if item["kind"] == "project" else "person"
        assert item["actions"] == [{"action_ref": "personal.context.v1#build", "input": {field: item["name"]}}]


def test_people_and_evidence_bound_output_without_exporting_source_content(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.sqlite")
    entity = store.upsert_entity(
        kind="person", canonical_name="Colleague", aliases=[f"Alias {index}" for index in range(30)]
    )
    sources = [
        {"source_ref": f"email-store://work/INBOX/{index}/abcdef", "source_kind": "email",
         "excerpt": "PRIVATE SOURCE EXCERPT", "source_sha256": "f" * 64}
        for index in range(12)
    ]
    for index in range(7):
        memory = propose(store, entity["entity_ref"], f"Fact {index} " + "x" * 400, sources=sources)
        store.approve(memory["memory_ref"])
    person = read_people(store)["items"][0]
    assert person["approved_memory_count"] == 7
    assert len(person["approved_memories"]) == MEMORIES_PER_ENTITY
    assert person["approved_memories_truncated"] is True
    assert len(person["aliases"]) == ALIASES_LIMIT
    assert person["aliases_truncated"] is True
    assert len(person["source_refs"]) == SOURCE_REFS_LIMIT
    assert person["source_refs_truncated"] is True
    evidence = read_memory_evidence(store, entity=entity["entity_ref"], limit=2)
    assert evidence["count"] == 2
    for item in evidence["items"] + person["approved_memories"]:
        assert len(item["summary"]) == SUMMARY_CHARS
        assert item["summary_truncated"] is True
        assert item["source_count"] == 12
        assert len(item["source_refs"]) == SOURCE_REFS_LIMIT
        assert item["source_refs_truncated"] is True
        assert item["actions"] == [{"action_ref": "personal.memory.v1#inspect", "input": {"memory_ref": item["memory_ref"]}}]
    serialized = json.dumps({"person": person, "evidence": evidence})
    assert "PRIVATE SOURCE EXCERPT" not in serialized
    assert "f" * 64 not in serialized
    assert "content" not in person["approved_memories"][0]


def test_missing_store_does_not_create_files_or_directories(tmp_path: Path):
    store = MemoryStore(tmp_path / "missing" / "profile" / "memory.sqlite")
    for result in (read_people(store), read_memory_evidence(store, entity="unknown")):
        assert result["items"] == []
        assert result["count"] == 0
        assert result["store_state"] == "missing"
    assert not (tmp_path / "missing").exists()


def test_projection_uses_only_readonly_connections_and_preserves_database(tmp_path: Path, monkeypatch):
    store = MemoryStore(tmp_path / "memory.sqlite")
    store.upsert_entity(kind="person", canonical_name="Colleague")
    before = store.path.read_bytes()
    connect = store._connect
    calls = []

    def readonly_connect(*, create):
        calls.append(create)
        assert create is False
        return connect(create=create)

    monkeypatch.setattr(store, "_connect", readonly_connect)
    read_people(store)
    read_memory_evidence(store)
    assert calls == [False, False]
    assert store.path.read_bytes() == before


def test_people_does_not_initialize_schema_in_existing_empty_database(tmp_path: Path):
    path = tmp_path / "memory.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE unrelated (id TEXT)")
    before = path.read_bytes()
    assert read_people(MemoryStore(path))["store_state"] == "schema_missing"
    assert path.read_bytes() == before
    with sqlite3.connect(path) as conn:
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    assert tables == [("unrelated",)]


def test_projection_reads_committed_wal_and_connection_rejects_writes(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.sqlite")
    entity = store.upsert_entity(kind="person", canonical_name="Colleague")
    memory = propose(store, entity["entity_ref"], "Approved in WAL")
    writer = store._connect(create=True)
    try:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE memories SET status='approved' WHERE memory_ref=?", (memory["memory_ref"],))
        writer.commit()
        assert Path(str(store.path) + "-wal").stat().st_size > 0
        before = store.path.read_bytes()
        assert read_people(store)["items"][0]["approved_memory_count"] == 1
        assert read_memory_evidence(store)["items"][0]["memory_ref"] == memory["memory_ref"]
        readonly = store._connect(create=False)
        try:
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                readonly.execute("DELETE FROM memories")
        finally:
            readonly.close()
        assert store.path.read_bytes() == before
    finally:
        writer.close()


@pytest.mark.parametrize("limit", [0, -1, 501, True, "1"])
def test_people_rejects_invalid_limits_before_opening_store(tmp_path: Path, limit):
    path = tmp_path / "missing" / "memory.sqlite"
    with pytest.raises(ValueError, match="limit must be"):
        read_people(MemoryStore(path), limit=limit)
    assert not path.parent.exists()
