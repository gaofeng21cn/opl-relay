from pathlib import Path

from codex_mail_workbench.knowledge import FrameworkKnowledge, KnowledgeIndex


class FakeFramework:
    def __init__(self):
        self.items = [{
            "knowledge_id": "relay.project",
            "title": "Professor Example",
            "source_id": "profile",
            "source_ref": "profile/people/example.md",
            "sha256": "sha256:" + "a" * 64,
            "body_ref": {"source_id": "profile", "ref": "people/example.md", "sha256": "sha256:" + "a" * 64},
            "scope": {"kind": "owner", "id": "gaofeng"},
            "content_kind": "state", "status": "accepted",
        }]
        self.revoked = False

    def sources(self):
        return [] if self.revoked else self.items

    def search(self, query, *, limit=10):
        if self.revoked:
            return []
        return [{**self.items[0], "excerpt": "Works with the Example Consortium."}] if "Example" in query else []


def test_metadata_catalog_rebuilds_without_source_bodies(tmp_path: Path):
    framework = FakeFramework()
    index = KnowledgeIndex(tmp_path / "memory.sqlite")
    result = index.rebuild(framework)
    assert result == {"indexed": 1, "authority": "opl-framework", "derived": True, "contains_source_bodies": False}
    assert index.source_counts() == {"profile": 1}
    found = index.search("Example Consortium", framework=framework)
    assert found[0]["knowledge_id"] == "relay.project"
    with index.path.open("rb") as stream:
        assert b"Example Consortium" not in stream.read()


def test_framework_search_rechecks_revocation_and_ignores_catalog_cache(tmp_path: Path):
    framework = FakeFramework()
    index = KnowledgeIndex(tmp_path / "memory.sqlite")
    index.rebuild(framework)
    framework.revoked = True
    assert index.search("Example Consortium", framework=framework) == []


def test_framework_adapter_fails_closed_for_invalid_command(tmp_path: Path):
    framework = FrameworkKnowledge(tmp_path, command=[str(tmp_path / "missing-opl")])
    try:
        framework.sources()
    except ValueError as exc:
        assert str(exc) == "framework_knowledge_unavailable"
    else:
        raise AssertionError("missing Framework command must fail closed")
