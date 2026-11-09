import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from codex_mail_workbench import cli
from codex_mail_workbench.knowledge import FrameworkKnowledge
from codex_mail_workbench.memory import MemoryStore


class KnowledgeOwner:
    def __init__(self):
        self.body = "# Mail policy\n\nUse concise updates.\n"
        self.digest = "sha256:" + hashlib.sha256(self.body.encode()).hexdigest()
        self.revoked = False
        self.source = {"knowledge_id": "owner.mail", "sha256": self.digest,
                       "content": self.body, "source_id": "profile", "source_ref": "policies/mail.md",
                       "scope": {"kind": "owner", "id": "test"}, "content_kind": "policy"}

    def read_source(self, knowledge_id):
        if self.revoked:
            raise ValueError("knowledge_source_revoked")
        assert knowledge_id == "owner.mail"
        return self.source

    def verify_dependencies(self, versions):
        return {"valid": not self.revoked and versions == {"owner.mail": self.digest}, "issues": []}

    def sources(self):
        return [{**self.source, "body_ref": {"source_id": "profile", "ref": "policies/mail.md", "sha256": self.digest},
                 "reviewed_at": "2026-01-01T00:00:00Z", "next_review_at": "2099-01-01T00:00:00Z"}]


def seed_memory(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite")
    entity = store.upsert_entity(kind="person", canonical_name="Example")
    memory = store.propose_memory(entity_ref=entity["entity_ref"], category="preference",
                                  content="Use concise updates.",
                                  sources=[{"source_ref": "user://confirmed/preference", "source_kind": "user"}])
    return store, memory


def test_export_promote_and_link_preserve_evidence_without_writing_body(tmp_path, monkeypatch, capsys):
    owner = KnowledgeOwner()
    monkeypatch.setattr(cli, "framework_knowledge", lambda args: owner)
    store, memory = seed_memory(tmp_path)
    prefix = ["--json", "--memory-db", str(store.path), "--db", str(tmp_path / "mail.sqlite")]
    export = tmp_path / "migration.json"
    assert cli.main([*prefix, "memory", "export", "--output", str(export)]) == 0
    assert json.loads(capsys.readouterr().out)["count"] == 1
    assert json.loads(export.read_text())["memories"][0]["sources"] == memory["sources"]
    assert export.stat().st_mode & 0o077 == 0
    proposal = tmp_path / "proposal.json"
    assert cli.main([*prefix, "memory", "promote", memory["memory_ref"], "--knowledge-id", "owner.mail", "--output", str(proposal)]) == 0
    capsys.readouterr()
    handoff = json.loads(proposal.read_text())
    assert handoff["proposal_only"] is True
    assert handoff["target_knowledge_id"] == "owner.mail"
    assert store.get_memory(memory["memory_ref"])["knowledge_id"] == ""
    assert cli.main([*prefix, "memory", "approve", memory["memory_ref"]]) == 0
    capsys.readouterr()
    assert cli.main([*prefix, "memory", "link", memory["memory_ref"], "--knowledge-id", "owner.mail"]) == 0
    linked = json.loads(capsys.readouterr().out)["memory"]
    assert linked["knowledge_id"] == "owner.mail"
    assert linked["source_currentness"]["status"] == "current"
    assert linked["sources"] == memory["sources"]
    assert [event["event_kind"] for event in linked["knowledge_events"]] == ["promotion_receipt", "canonical_link"]
    owner.revoked = True
    assert cli.main([*prefix, "memory", "search"]) == 0
    assert json.loads(capsys.readouterr().out)["memories"] == []
    # Existing export is a protected review artifact, not an overwrite target.
    assert cli.main([*prefix, "memory", "export", "--output", str(export)]) != 0
    capsys.readouterr()
    assert len(json.loads(export.read_text())["memories"]) == 1


def test_adapter_passes_profile_project_and_private_dependency_shape(tmp_path, monkeypatch):
    calls = []
    dependency_path = None

    def run(command, **kwargs):
        nonlocal dependency_path
        calls.append(command)
        assert command[:2] == ["node", "framework.js"]
        assert command[command.index("--workspace") + 1] == str(tmp_path)
        assert command[command.index("--project") + 1] == str(tmp_path / "project")
        assert command[command.index("--domain") + 1] == "relay"
        assert kwargs["check"] is False and "shell" not in kwargs
        dependency_path = Path(command[command.index("--dependencies") + 1])
        assert json.loads(dependency_path.read_text()) == [{"knowledge_id": "owner.mail", "sha256": "sha256:" + "a" * 64}]
        assert dependency_path.stat().st_mode & 0o077 == 0
        return SimpleNamespace(returncode=0, stdout=json.dumps({"knowledge_dependencies": {"valid": True, "issues": []}}))

    monkeypatch.setattr("codex_mail_workbench.knowledge.subprocess.run", run)
    owner = FrameworkKnowledge(tmp_path, tmp_path / "project", command=["node", "framework.js"])
    assert owner.verify_dependencies({"owner.mail": "sha256:" + "a" * 64})["valid"]
    assert dependency_path is not None and not dependency_path.exists()
