import hashlib
import sqlite3
from pathlib import Path

import pytest

from codex_mail_workbench.memory import MemoryStore
from codex_mail_workbench.memory_evidence import MemoryEvidence
from codex_mail_workbench.store import connect_email_store, upsert_email_message


class FrameworkFixture:
    """A typed owner response: Relay never opens the source's physical path."""

    def __init__(self):
        self.body = "# Colleague\n\nReviewed current collaboration."
        self.accepted_hash = "sha256:" + hashlib.sha256(self.body.encode()).hexdigest()
        self.active = True
        self.current = True
        self.identity = {"source_id": "notes", "source_ref": "people/colleague.md",
                         "scope": {"kind": "owner", "id": "owner"}, "content_kind": "state"}
        self.verify_calls = []

    def read_source(self, knowledge_id):
        if not self.active:
            raise ValueError("knowledge_source_unauthorized:" + knowledge_id)
        if not self.current:
            raise ValueError("knowledge_review_overdue:" + knowledge_id)
        return {"knowledge_id": knowledge_id, "sha256": self.accepted_hash,
                "content": self.body, **self.identity}

    def verify_dependencies(self, versions):
        self.verify_calls.append(dict(versions))
        if not self.active:
            return {"valid": False, "issues": ["knowledge_source_unauthorized:owner.colleague"]}
        if not self.current:
            return {"valid": False, "issues": ["knowledge_review_overdue:owner.colleague"]}
        if versions.get("owner.colleague") != self.accepted_hash:
            return {"valid": False, "issues": ["knowledge_source_revision_changed:owner.colleague"]}
        return {"valid": True, "issues": []}


def seed_memory(tmp_path, *, source_kind="knowledge", canonical=True, **fields):
    framework = FrameworkFixture()
    store = MemoryStore(tmp_path / "profile" / "data" / "relay" / "memory.sqlite")
    entity = store.upsert_entity(kind="person", canonical_name="Colleague")
    if source_kind == "knowledge":
        source = {"source_ref": "knowledge://owner.colleague", "source_kind": "knowledge",
                  "source_sha256": framework.accepted_hash.removeprefix("sha256:")}
    else:
        source = {"source_ref": "user://statement/colleague", "source_kind": "user"}
    kwargs = {"category": "fact", "observed_at": "2020-01-01T00:00:00Z",
              "next_review_at": "2100-01-01T00:00:00Z", **fields}
    memory = store.propose_memory(entity_ref=entity["entity_ref"], content="Reviewed collaboration",
                                  sources=[source], **kwargs)
    store.approve(memory["memory_ref"])
    if canonical:
        memory = store.link_memory(
            memory["memory_ref"], knowledge_id="owner.colleague",
            source_versions={"owner.colleague": framework.accepted_hash},
            canonical_target={"source_id": "notes", "ref": "people/colleague.md",
                              "sha256": framework.accepted_hash},
            scope=framework.identity["scope"], content_kind=framework.identity["content_kind"],
            observed_at=kwargs["observed_at"], next_review_at=kwargs["next_review_at"],
        )
    else:
        memory = store.get_memory(memory["memory_ref"])
    evidence = MemoryEvidence(mail_db_path=store.path.parent / "mail.sqlite", framework=framework)
    return store, memory, framework, evidence


@pytest.mark.parametrize("change,reason", [
    ("revoke", "knowledge_source_unauthorized"),
    ("accepted_revision", "knowledge_source_revision_changed"),
    ("overdue", "knowledge_review_overdue"),
    ("identity", "canonical_source_identity_changed"),
    ("scope", "canonical_source_semantics_changed"),
])
def test_framework_changes_exclude_memory_without_changing_history(tmp_path, change, reason):
    store, memory, framework, evidence = seed_memory(tmp_path, source_kind="user")
    before = store.export_memories()["memories"]
    assert evidence.current(memory)
    if change == "revoke":
        framework.active = False
    elif change == "accepted_revision":
        framework.accepted_hash = "sha256:" + "b" * 64
    elif change == "overdue":
        framework.current = False
    elif change == "identity":
        framework.identity["source_ref"] = "moved/colleague.md"
    else:
        framework.identity["scope"] = {"kind": "project", "id": "another"}
    assert store.list_memories(is_current=evidence.current) == []
    inspected = evidence.inspect(store.get_memory(memory["memory_ref"]))
    assert any(reason in item for item in inspected["source_currentness"]["reasons"])
    assert inspected["evidence_currentness"]["status"] == "needs_review"
    assert store.export_memories()["memories"] == before


@pytest.mark.parametrize("fields,reason", [
    ({"valid_until": "2000-01-01T00:00:00Z"}, "memory_expired"),
    ({"valid_from": "2100-01-01T00:00:00Z"}, "memory_not_yet_valid"),
    ({"valid_until": "not-a-date"}, "memory_validity_invalid"),
    ({"observed_at": ""}, "claim_observed_at_missing"),
    ({"next_review_at": "2001-01-01T00:00:00Z"}, "claim_review_overdue"),
    ({"next_review_at": ""}, "claim_review_deadline_missing"),
    ({"observed_at": "2120-01-01T00:00:00Z"}, "claim_observation_in_future"),
])
def test_current_evidence_does_not_make_an_expired_or_undated_claim_current(tmp_path, fields, reason):
    store, memory, _, evidence = seed_memory(tmp_path, **fields)
    result = evidence.check(memory)
    assert result["evidence_currentness"]["status"] == "current"
    assert result["claim_freshness"]["status"] == "needs_review"
    assert reason in result["reasons"]
    assert not evidence.current(memory)
    assert store.get_memory(memory["memory_ref"])["status"] == "approved"


def test_history_is_verifiable_but_not_current_state(tmp_path):
    _, memory, _, evidence = seed_memory(tmp_path, source_kind="user", canonical=False,
                                         category="event", occurred_at="2024-06-01T00:00:00Z")
    result = evidence.check(memory)
    assert result["status"] == "historical"
    assert result["evidence_currentness"]["status"] == "current"
    assert result["claim_freshness"]["status"] == "historical"
    assert not evidence.current(memory)
    # Evidence approval does not claim the event is a current role or preference.
    assert evidence.check(memory, approving=True)["status"] == "current"


def test_unlinked_user_claim_can_be_reviewed_but_not_injected_as_personal_truth(tmp_path):
    _, memory, _, evidence = seed_memory(tmp_path, source_kind="user", canonical=False)
    assert evidence.check(memory, approving=True)["status"] == "current"
    result = evidence.check(memory)
    assert result["status"] == "needs_review"
    assert "canonical_link_required" in result["reasons"]
    assert result["repair"]["requires_canonical_link"] is True


def test_registered_knowledge_reads_require_framework_and_exact_evidence_revision(tmp_path):
    _, memory, framework, evidence = seed_memory(tmp_path)
    assert evidence.source_digest("knowledge://owner.colleague", "knowledge") == framework.accepted_hash[7:]
    memory["sources"][0]["source_sha256"] = ""
    assert "source_revision_missing" in evidence.check(memory)["reasons"]
    memory["sources"][0]["source_sha256"] = "c" * 64
    assert "source_revision_changed" in evidence.check(memory)["reasons"]
    with pytest.raises(ValueError, match="source_requires_framework_registration"):
        evidence.source_digest("obsidian://notes/people/colleague.md", "obsidian")
    with pytest.raises(ValueError, match="source_requires_framework_registration"):
        evidence.source_digest("markdown://notes/people/colleague.md", "markdown")


def test_local_source_config_cannot_grant_access(tmp_path):
    config = tmp_path / "sources.toml"
    config.write_text('[[sources]]\nsource_id="notes"\npath="/"\nenabled=true\n')
    framework = FrameworkFixture()
    framework.active = False
    evidence = MemoryEvidence(mail_db_path=tmp_path / "mail.sqlite",
                              framework=framework)
    with pytest.raises(ValueError, match="knowledge_source_unauthorized"):
        evidence.source_digest("knowledge://owner.colleague", "knowledge")


def test_email_source_uses_original_raw_bytes_and_keeps_domain_authority(tmp_path):
    raw = b"Subject: Historic meeting\r\n\r\nThe meeting happened in 2024."
    db = tmp_path / "mail.sqlite"
    conn = connect_email_store(db)
    try:
        ref = upsert_email_message(conn, account_id="work", folder="INBOX", folder_slug="INBOX",
                                   uid=1, uidvalidity=1, message_id="<event@example.test>",
                                   subject="Historic meeting", sender="person@example.test", recipient="me@example.test",
                                   date_iso="2024-01-01T00:00:00Z", raw_sha256=hashlib.sha256(raw).hexdigest(),
                                   raw_eml=raw, attachments=[], ingest_ts="2024-01-02T00:00:00Z")
    finally:
        conn.close()
    evidence = MemoryEvidence(mail_db_path=db, framework=FrameworkFixture())
    assert evidence.source_digest(ref, "email") == hashlib.sha256(raw).hexdigest()
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE email_blobs SET raw_eml=?", (b"Modified original bytes",))
    with pytest.raises(ValueError, match="source_integrity_mismatch"):
        evidence.source_digest(ref, "email")
