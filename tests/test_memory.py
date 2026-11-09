from pathlib import Path

from codex_mail_workbench.memory import MemoryStore


def test_entity_upsert_merges_aliases_by_email(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite")

    original = store.upsert_entity(
        kind="person",
        canonical_name="Professor Example",
        aliases=["Prof. Example"],
        emails=["person@example.test"],
    )
    updated = store.upsert_entity(
        kind="person",
        canonical_name="Example Sensei",
        aliases=["Example"],
        emails=["PERSON@example.test"],
    )

    assert updated["entity_ref"] == original["entity_ref"]
    assert updated["canonical_name"] == "Example Sensei"
    assert "Professor Example" in updated["aliases"]
    assert store.resolve_entity("person@example.test")["entity_ref"] == original["entity_ref"]


def test_memory_lifecycle_preserves_sources_and_supersedes(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite")
    entity = store.upsert_entity(kind="person", canonical_name="Professor Example")
    original = store.propose_memory(
        entity_ref=entity["entity_ref"],
        category="fact",
        content="The person chaired the working group in 2025.",
        confidence=0.9,
        sources=[
            {
                "source_ref": "email-store://work/INBOX/1/aaaaaaaaaaaaaaaa",
                "source_kind": "email",
                "source_sha256": "a" * 64,
            }
        ],
    )

    assert store.list_memories(statuses=("approved",)) == []
    approved = store.approve(original["memory_ref"])
    assert approved["status"] == "approved"
    assert approved["sources"][0]["source_ref"].startswith("email-store://")

    replacement = store.propose_memory(
        entity_ref=entity["entity_ref"],
        category="fact",
        content="The person became chair of the consortium in 2026.",
        sources=[
            {
                "source_ref": "user://statement/2026-07-27",
                "source_kind": "user",
                "excerpt": "The role has changed.",
            }
        ],
        supersedes_ref=original["memory_ref"],
    )
    store.approve(replacement["memory_ref"])

    assert store.get_memory(original["memory_ref"])["status"] == "superseded"
    active = store.list_memories(entity=entity["entity_ref"], statuses=("approved",))
    assert [item["memory_ref"] for item in active] == [replacement["memory_ref"]]


def test_rejected_and_forgotten_memory_are_not_active(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite")
    entity = store.upsert_entity(kind="project", canonical_name="Example Project")
    candidate = store.propose_memory(
        entity_ref=entity["entity_ref"],
        category="inference",
        content="The project may invite a new collaborator.",
        confidence=0.5,
        sources=[
            {
                "source_ref": "model://codex/run-1",
                "source_kind": "model",
            }
        ],
    )

    rejected = store.reject(candidate["memory_ref"])
    forgotten = store.forget(candidate["memory_ref"])

    assert rejected["status"] == "rejected"
    assert forgotten["status"] == "forgotten"
    assert store.list_memories(statuses=("approved",)) == []


def test_link_and_export_preserve_original_evidence_and_body_ownership(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite")
    entity = store.upsert_entity(kind="person", canonical_name="Colleague")
    original = store.propose_memory(
        entity_ref=entity["entity_ref"], category="fact", content="Works with the group.",
        sources=[{"source_ref": "user://statement/confirmation", "source_kind": "user",
                  "excerpt": "User-confirmed current collaboration"}],
        observed_at="2026-01-01T00:00:00Z", next_review_at="2027-01-01T00:00:00Z",
    )
    store.approve(original["memory_ref"])
    source_hash = "sha256:" + "a" * 64
    linked = store.link_memory(
        original["memory_ref"], knowledge_id="owner.colleague", source_versions={"owner.colleague": source_hash},
        canonical_target={"source_id": "owner-notes", "ref": "people/colleague.md", "sha256": source_hash},
        scope={"kind": "owner", "id": "owner"}, content_kind="state",
        observed_at=original["observed_at"], next_review_at=original["next_review_at"],
    )
    assert linked["content"] == original["content"]
    assert linked["sources"] == original["sources"]
    assert linked["status"] == "approved"
    assert linked["knowledge_id"] == "owner.colleague"
    assert linked["writeback_status"] == "applied"
    assert [event["event_kind"] for event in linked["knowledge_events"]] == ["canonical_link"]
    export = store.export_memories()
    assert export["memories"] == [linked]
    assert export["entities"][0]["entity_ref"] == entity["entity_ref"]
    assert export["body_authority"] == "framework_sources"
    assert not (tmp_path / "people" / "colleague.md").exists()


def test_old_store_read_is_nonmutating_and_first_link_adds_metadata(tmp_path: Path) -> None:
    import sqlite3

    store = MemoryStore(tmp_path / "memory.sqlite")
    entity = store.upsert_entity(kind="person", canonical_name="Legacy Colleague")
    memory = store.propose_memory(entity_ref=entity["entity_ref"], category="event", content="Met in 2024.",
                                 occurred_at="2024-01-01", sources=[{
                                     "source_ref": "user://history/meeting", "source_kind": "user"}])
    store.approve(memory["memory_ref"])
    with sqlite3.connect(store.path) as conn:
        conn.execute("DROP TABLE memory_semantics")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = store.path.read_bytes()
    old = store.get_memory(memory["memory_ref"])
    assert old["knowledge_id"] == "" and old["content_kind"] == "decision"
    assert store.export_memories()["memories"] == [old]
    assert store.path.read_bytes() == before
    promoted = store.record_promotion(memory["memory_ref"], proposal_ref="proposal://new-knowledge",
                                     knowledge_id="owner.history")
    assert promoted["knowledge_id"] == ""
    assert promoted["promotion_target_id"] == "owner.history"
    assert promoted["writeback_status"] == "proposed"
    assert promoted["status"] == "approved"
    assert promoted["content"] == old["content"] and promoted["sources"] == old["sources"]
    renewed = store.record_promotion(memory["memory_ref"], proposal_ref="proposal://newer-knowledge",
                                    knowledge_id="owner.history")
    assert [event["payload"]["proposal_ref"] for event in renewed["knowledge_events"]] == [
        "proposal://new-knowledge", "proposal://newer-knowledge"]


def test_complete_export_keeps_rejected_forgotten_and_all_entities(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite")
    entity = store.upsert_entity(kind="person", canonical_name="Colleague")
    refs = []
    for index in range(503):
        memory = store.propose_memory(entity_ref=entity["entity_ref"], category="event",
                                     content=f"Historical event {index}", sources=[{
                                         "source_ref": f"user://history/{index}", "source_kind": "user"}])
        refs.append(memory["memory_ref"])
    store.reject(refs[0])
    store.forget(refs[1])
    export = store.export_memories()
    assert export["count"] == 503
    assert {item["status"] for item in export["memories"]} == {"candidate", "rejected", "forgotten"}


def test_semantic_revision_requires_new_review_without_rewriting_prior_dates(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite")
    entity = store.upsert_entity(kind="person", canonical_name="Colleague")
    fields = {"entity_ref": entity["entity_ref"], "category": "fact", "content": "Current role",
              "sources": [{"source_ref": "user://statement/role", "source_kind": "user"}],
              "observed_at": "2026-01-01", "next_review_at": "2027-01-01"}
    first = store.propose_memory(**fields)
    store.approve(first["memory_ref"])
    revised = store.propose_memory(**{**fields, "next_review_at": "2027-02-01"})
    assert revised["status"] == "candidate" and revised["supersedes_ref"] == first["memory_ref"]
    assert store.get_memory(first["memory_ref"])["next_review_at"] == "2027-01-01"
