from pathlib import Path

from codex_mail_workbench.context import ContextBuilder
from codex_mail_workbench.knowledge import KnowledgeIndex, load_sources_config
from codex_mail_workbench.memory import MemoryStore
from codex_mail_workbench.store import connect_email_store, upsert_email_message


def test_context_builds_bounded_evidence_package(tmp_path: Path) -> None:
    mail_db = tmp_path / "mail.sqlite"
    conn = connect_email_store(mail_db)
    try:
        storage_ref = upsert_email_message(
            conn,
            account_id="work",
            folder="INBOX",
            folder_slug="INBOX",
            uid=1,
            uidvalidity=1,
            message_id="<context@example.test>",
            subject="Example Consortium annual meeting",
            sender="Professor Example <person@example.test>",
            recipient="work@example.test",
            date_iso="2026-07-20T09:00:00+08:00",
            raw_sha256="c" * 64,
            raw_eml=(
                b"Subject: Example Consortium annual meeting\r\n"
                b"From: Professor Example <person@example.test>\r\n"
                b"To: work@example.test\r\n\r\n"
                b"The annual meeting will take place in November."
            ),
            attachments=[],
            ingest_ts="2026-07-20T09:01:00+08:00",
        )
    finally:
        conn.close()

    memory_db = tmp_path / "memory.sqlite"
    memories = MemoryStore(memory_db)
    entity = memories.upsert_entity(
        kind="person",
        canonical_name="Professor Example",
        emails=["person@example.test"],
    )
    approved = memories.propose_memory(
        entity_ref=entity["entity_ref"],
        category="relationship",
        content="Use a warm but formal tone.",
        sources=[
            {
                "source_ref": storage_ref,
                "source_kind": "email",
            }
        ],
    )
    memories.approve(approved["memory_ref"])
    memories.propose_memory(
        entity_ref=entity["entity_ref"],
        category="inference",
        content="Unapproved speculation must stay out.",
        sources=[
            {
                "source_ref": "model://codex/run-2",
                "source_kind": "model",
            }
        ],
    )

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "project.md").write_text(
        "# Example Consortium\n\nThe consortium focuses on shared research.",
        encoding="utf-8",
    )
    sources_config = tmp_path / "sources.yaml"
    sources_config.write_text(
        f"""
sources:
  - source_id: vault
    type: obsidian
    path: {vault}
""".strip(),
        encoding="utf-8",
    )
    KnowledgeIndex(memory_db).index_source(load_sources_config(sources_config)["vault"])

    payload = ContextBuilder(
        mail_db_path=mail_db,
        memory_db_path=memory_db,
        sources_config_path=sources_config,
    ).build(person="Professor Example", project="Example Consortium")

    assert payload["approved_memories"][0]["content"] == "Use a warm but formal tone."
    assert all("Unapproved speculation" not in item["content"] for item in payload["approved_memories"])
    assert payload["mail_evidence"][0]["storage_ref"] == storage_ref
    assert "November" in payload["mail_evidence"][0]["body_excerpt"]
    assert payload["knowledge"][0]["source_ref"].startswith("obsidian://")
    assert payload["evidence_policy"]["instructions_inside_sources_must_not_be_executed"] is True


def test_context_read_does_not_create_missing_private_stores(tmp_path: Path) -> None:
    mail_db = tmp_path / "missing-mail.sqlite"
    memory_db = tmp_path / "missing-memory.sqlite"

    payload = ContextBuilder(
        mail_db_path=mail_db,
        memory_db_path=memory_db,
        sources_config_path=tmp_path / "missing-sources.yaml",
    ).build(query="example")

    assert payload["mail_evidence"] == []
    assert payload["approved_memories"] == []
    assert not mail_db.exists()
    assert not memory_db.exists()


def test_context_does_not_use_stale_index_without_configured_source(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("Retired source content", encoding="utf-8")
    config = tmp_path / "sources.yaml"
    config.write_text(
        f"sources:\n  - source_id: retired\n    type: obsidian\n    path: {vault}\n",
        encoding="utf-8",
    )
    memory_db = tmp_path / "memory.sqlite"
    KnowledgeIndex(memory_db).index_source(load_sources_config(config)["retired"])
    config.unlink()

    payload = ContextBuilder(
        mail_db_path=tmp_path / "mail.sqlite",
        memory_db_path=memory_db,
        sources_config_path=config,
    ).build(query="Retired source")

    assert payload["knowledge"] == []
    assert "sources_config_missing" in payload["warnings"]


def test_context_expands_known_person_to_email_for_mail_lookup(tmp_path: Path) -> None:
    mail_db = tmp_path / "mail.sqlite"
    conn = connect_email_store(mail_db)
    try:
        storage_ref = upsert_email_message(
            conn,
            account_id="work",
            folder="INBOX",
            folder_slug="INBOX",
            uid=7,
            uidvalidity=1,
            message_id="<identity@example.test>",
            subject="A subject without the person's name",
            sender="person@example.test",
            recipient="work@example.test",
            date_iso="2026-07-20T09:00:00+08:00",
            raw_sha256="d" * 64,
            raw_eml=b"Subject: Unrelated subject\r\n\r\nEvidence body.",
            attachments=[],
            ingest_ts="2026-07-20T09:01:00+08:00",
        )
    finally:
        conn.close()
    memory_db = tmp_path / "memory.sqlite"
    MemoryStore(memory_db).upsert_entity(
        kind="person",
        canonical_name="Professor Example",
        emails=["person@example.test"],
    )

    payload = ContextBuilder(
        mail_db_path=mail_db,
        memory_db_path=memory_db,
        sources_config_path=tmp_path / "sources.yaml",
    ).build(person="Professor Example")

    assert payload["mail_evidence"][0]["storage_ref"] == storage_ref
