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
