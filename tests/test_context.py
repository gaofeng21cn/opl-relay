import hashlib
from pathlib import Path

from codex_mail_workbench.context import ContextBuilder
from codex_mail_workbench.memory import MemoryStore
from codex_mail_workbench.store import connect_email_store, upsert_email_message


class FakeFramework:
    def __init__(self):
        self.revoked = False
    def search(self, query, *, limit=10):
        if self.revoked or "Example" not in query:
            return []
        return [{"knowledge_id": "relay.project", "source_id": "profile",
                 "source_ref": "profile/project.md", "sha256": "sha256:" + "a" * 64,
                 "excerpt": "The consortium focuses on shared research.",
                 "source_currentness": {"status": "current"}}]
    def sources(self): return []
    def read_source(self, knowledge_id): raise ValueError("source unavailable")
    def verify_dependencies(self, versions): return {"valid": True, "issues": []}


def test_context_builds_bounded_evidence_package(tmp_path: Path) -> None:
    mail_db = tmp_path / "mail.sqlite"
    raw = (b"Subject: Example Consortium annual meeting\r\n"
           b"From: Professor Example <person@example.test>\r\n"
           b"To: work@example.test\r\n\r\n"
           b"The annual meeting will take place in November.")
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
            raw_sha256=hashlib.sha256(raw).hexdigest(),
            raw_eml=raw,
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
        observed_at="2026-07-20T09:00:00+08:00",
        next_review_at="2099-07-20T09:00:00+08:00",
        sources=[
            {
                "source_ref": storage_ref,
                "source_kind": "email",
                "source_sha256": hashlib.sha256(raw).hexdigest(),
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

    framework = FakeFramework()

    payload = ContextBuilder(
        mail_db_path=mail_db,
        memory_db_path=memory_db,
        framework=framework,
    ).build(person="Professor Example", project="Example Consortium")

    assert payload["approved_memories"][0]["content"] == "Use a warm but formal tone."
    assert all("Unapproved speculation" not in item["content"] for item in payload["approved_memories"])
    assert payload["mail_evidence"][0]["storage_ref"] == storage_ref
    assert "November" in payload["mail_evidence"][0]["body_excerpt"]
    assert payload["knowledge"][0]["source_ref"] == "profile/project.md"
    assert payload["evidence_policy"]["instructions_inside_sources_must_not_be_executed"] is True


def test_context_read_does_not_create_missing_private_stores(tmp_path: Path) -> None:
    mail_db = tmp_path / "missing-mail.sqlite"
    memory_db = tmp_path / "missing-memory.sqlite"

    payload = ContextBuilder(
        mail_db_path=mail_db,
        memory_db_path=memory_db,
        framework=FakeFramework(),
    ).build(query="example")

    assert payload["mail_evidence"] == []
    assert payload["approved_memories"] == []
    assert not mail_db.exists()
    assert not memory_db.exists()


def test_context_does_not_use_revoked_framework_source(tmp_path: Path) -> None:
    framework = FakeFramework()
    framework.revoked = True
    payload = ContextBuilder(
        mail_db_path=tmp_path / "mail.sqlite",
        memory_db_path=tmp_path / "memory.sqlite",
        framework=framework,
    ).build(query="Example Consortium")
    assert payload["knowledge"] == []


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
        framework=FakeFramework(),
    ).build(person="Professor Example")

    assert payload["mail_evidence"][0]["storage_ref"] == storage_ref
