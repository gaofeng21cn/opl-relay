import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_mail_workbench import cli
from codex_mail_workbench.context import ContextBuilder
from codex_mail_workbench.knowledge import KnowledgeIndex, load_sources_config, resolve_source_ref
from codex_mail_workbench.memory import MemoryStore
from codex_mail_workbench.memory_evidence import MemoryEvidence
from codex_mail_workbench.people import read_memory_evidence, read_people
from test_app_contribution_cli import invoke


def seed_source(tmp_path: Path, *, kind="obsidian", source_hash=True, **memory_fields):
    profile = tmp_path / "profile"
    state = profile / "data" / "relay"
    state.mkdir(parents=True)
    vault = tmp_path / "notes"
    vault.mkdir()
    note = vault / "note.md"
    note.write_bytes(b"# Colleague\r\n\r\nOriginal source evidence.")
    config = state / "sources.toml"
    config.write_text(f'[[sources]]\nsource_id="notes"\ntype="{kind}"\npath="{vault}"\n')
    sources = load_sources_config(config)
    index = KnowledgeIndex(state / "memory.sqlite")
    index.index_source(sources["notes"])
    store = MemoryStore(index.path)
    entity = store.upsert_entity(kind="person", canonical_name="Colleague")
    memory = store.propose_memory(
        entity_ref=entity["entity_ref"], category="fact", content="SOURCE-BOUND APPROVED SUMMARY",
        sources=[{"source_ref": f"{kind}://notes/note.md", "source_kind": kind,
                  "source_sha256": hashlib.sha256(note.read_bytes()).hexdigest() if source_hash else ""}],
        **memory_fields,
    )
    store.approve(memory["memory_ref"])
    return profile, state, note, config, store, entity, memory


@pytest.mark.parametrize("change", ["edit", "delete", "disable", "exclude", "include", "symlink"])
def test_source_changes_are_excluded_from_context_and_public_reads_without_deleting_history(tmp_path, change):
    profile, state, note, config, store, _, memory = seed_source(tmp_path)
    before = store.path.read_bytes()
    for ref in ("personal.memory.v1#people", "personal.memory.v1#search"):
        code, result = invoke(profile, ref)
        assert code == 0 and "SOURCE-BOUND APPROVED SUMMARY" in json.dumps(result)
        assert result["result"]["data"]["source_policy"]["source_currentness_checked"] is True
    if change == "edit":
        note.write_text("Changed source evidence")
    elif change == "delete":
        note.unlink()
    elif change == "disable":
        config.write_text(config.read_text() + "enabled=false\n")
    elif change in {"exclude", "include"}:
        pattern = "note.md" if change == "exclude" else "other/*.md"
        config.write_text(config.read_text() + f'{change}=["{pattern}"]\n')
    else:
        external = tmp_path / "private.md"
        external.write_bytes(note.read_bytes())
        note.unlink()
        note.symlink_to(external)
    for ref in ("personal.memory.v1#people", "personal.memory.v1#search"):
        code, result = invoke(profile, ref)
        assert code == 0 and "SOURCE-BOUND APPROVED SUMMARY" not in json.dumps(result)
        code, result = invoke(profile, ref, payload={"query": "SOURCE-BOUND"})
        assert code == 0 and result["result"]["data"]["items"] == []
    context = ContextBuilder(mail_db_path=state / "mail.sqlite", memory_db_path=store.path,
                             sources_config_path=config).build(person="Colleague", query="Original")
    assert context["approved_memories"] == context["knowledge"] == []
    code, result = invoke(profile, "personal.memory.v1#inspect", operation="execute",
                          payload={"memory_ref": memory["memory_ref"]})
    assert code == 0
    assert result["result"]["memory"]["source_currentness"]["status"] == "needs_review"
    assert store.get_memory(memory["memory_ref"])["status"] == "approved"
    assert store.path.read_bytes() == before


def test_markdown_source_and_raw_byte_revision_are_supported(tmp_path):
    _, _, note, config, store, _, memory = seed_source(tmp_path, kind="markdown")
    result = read_memory_evidence(store)
    assert result["items"][0]["memory_ref"] == memory["memory_ref"]
    sources = load_sources_config(config)
    found = KnowledgeIndex(store.path).search("Original", sources=sources)
    assert found[0]["content_sha256"] == hashlib.sha256(note.read_bytes()).hexdigest()
    assert found[0]["source_ref"] == "markdown://notes/note.md"


def test_index_repairs_source_type_changes_and_does_not_trust_modified_cached_content(tmp_path):
    _, _, _, config, store, _, _ = seed_source(tmp_path)
    index = KnowledgeIndex(store.path)
    config.write_text(config.read_text().replace('type="obsidian"', 'type="markdown"'))
    sources = load_sources_config(config)
    assert index.search("Original", sources=sources) == []
    assert index.index_source(sources["notes"])["indexed"] == 1
    assert index.search("Original", sources=sources)[0]["source_ref"] == "markdown://notes/note.md"
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE knowledge_documents SET content='Injected cached assertion'")
    assert index.search("Injected", sources=sources) == []
    assert index.index_source(sources["notes"])["indexed"] == 1
    assert index.search("Original", sources=sources)


@pytest.mark.parametrize("fields,reason", [
    ({"valid_until": "2000-01-01T00:00:00Z"}, "memory_expired"),
    ({"valid_from": "2100-01-01T00:00:00Z"}, "memory_not_yet_valid"),
    ({"valid_until": "not-a-date"}, "memory_validity_invalid"),
    ({"source_hash": False}, "source_revision_missing"),
])
def test_approved_status_does_not_override_validity_or_missing_source_revision(tmp_path, fields, reason):
    _, _, _, _, store, _, memory = seed_source(tmp_path, **fields)
    assert read_memory_evidence(store)["items"] == []
    check = MemoryEvidence.beside_store(store.path).check(store.get_memory(memory["memory_ref"]))
    assert reason in check["reasons"]
    assert store.get_memory(memory["memory_ref"])["status"] == "approved"


def test_proposal_captures_revision_and_approval_does_not_rebase_changed_evidence(tmp_path, capsys):
    _, state, note, config, store, entity, original = seed_source(tmp_path)
    args = SimpleNamespace(db=str(state / "mail.sqlite"), memory_db=str(store.path),
                           sources_config=str(config), source=["obsidian://notes/note.md"],
                           source_excerpt=[], memory_ref=original["memory_ref"], json=True)
    original_hash = original["sources"][0]["source_sha256"]
    note.write_text("New evidence requiring a separate acceptance")
    with pytest.raises(ValueError, match="source_revision_changed"):
        cli.cmd_memory_approve(args)
    assert store.get_memory(original["memory_ref"])["sources"][0]["source_sha256"] == original_hash
    sources = cli._memory_sources(args)
    assert sources[0]["source_sha256"] == hashlib.sha256(note.read_bytes()).hexdigest()
    proposal = store.propose_memory(entity_ref=entity["entity_ref"], category="fact",
                                    content=original["content"], sources=sources)
    assert proposal["memory_ref"] != original["memory_ref"]
    assert proposal["status"] == "candidate" and proposal["supersedes_ref"] == original["memory_ref"]
    assert store.get_memory(original["memory_ref"])["sources"][0]["source_sha256"] == original_hash
    args.memory_ref = proposal["memory_ref"]
    assert cli.cmd_memory_approve(args) == 0
    capsys.readouterr()
    assert store.get_memory(original["memory_ref"])["status"] == "superseded"


def test_reviewed_revisions_use_complete_evidence_validity_and_current_predecessor(tmp_path):
    _, _, _, _, store, entity, original = seed_source(tmp_path)
    user = {"source_ref": "user://statement/confirmation", "source_kind": "user"}
    def propose(sources, **kwargs):
        return store.propose_memory(entity_ref=entity["entity_ref"], category="fact",
                                    content=original["content"], sources=sources, **kwargs)
    second = propose([*original["sources"], user])
    store.approve(second["memory_ref"])
    removed = propose([user])
    assert removed["sources"] == [{**user, "excerpt": "", "source_sha256": ""}]
    assert removed["supersedes_ref"] == second["memory_ref"]
    store.approve(removed["memory_ref"])
    dated = propose([user], valid_until="2100-01-01T00:00:00Z")
    assert dated["memory_ref"] != removed["memory_ref"]
    assert dated["supersedes_ref"] == removed["memory_ref"]
    store.approve(dated["memory_ref"])
    repeated = propose([user], valid_until="2100-01-01T00:00:00Z")
    assert repeated == store.get_memory(dated["memory_ref"])
    current = read_memory_evidence(store)["items"]
    assert [item["memory_ref"] for item in current] == [dated["memory_ref"]]
    assert store.get_memory(removed["memory_ref"])["valid_until"] == ""


def test_competing_revision_requires_fresh_review_after_predecessor_changes(tmp_path):
    _, _, _, _, store, entity, original = seed_source(tmp_path)
    def propose(excerpt):
        return store.propose_memory(entity_ref=entity["entity_ref"], category="fact",
                                    content=original["content"], sources=[{
                                        "source_ref": "user://statement/confirmed",
                                        "source_kind": "user", "excerpt": excerpt}])
    first = propose("First")
    competing = propose("Competing")
    store.approve(first["memory_ref"])
    with pytest.raises(ValueError, match="propose a fresh revision"):
        store.approve(competing["memory_ref"])
    fresh = propose("Competing")
    assert fresh["supersedes_ref"] == first["memory_ref"]
    assert fresh["memory_ref"] != competing["memory_ref"]
    store.approve(fresh["memory_ref"])
    assert [m["memory_ref"] for m in read_memory_evidence(store)["items"]] == [fresh["memory_ref"]]


def test_competing_initial_candidates_do_not_create_two_accepted_versions(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite")
    entity = store.upsert_entity(kind="person", canonical_name="Colleague")
    def propose(excerpt):
        return store.propose_memory(entity_ref=entity["entity_ref"], category="fact",
                                    content="Confirmed assertion", sources=[{
                                        "source_ref": "user://statement/confirmed",
                                        "source_kind": "user", "excerpt": excerpt}])
    first, second = propose("First"), propose("Second")
    assert first["supersedes_ref"] == second["supersedes_ref"] == ""
    store.approve(first["memory_ref"])
    with pytest.raises(ValueError, match="predecessor changed"):
        store.approve(second["memory_ref"])
    renewed = propose("Second")
    store.approve(renewed["memory_ref"])
    assert [m["memory_ref"] for m in read_memory_evidence(store)["items"]] == [renewed["memory_ref"]]


def test_traversal_and_parent_symlinks_cannot_expand_source_scope(tmp_path):
    _, _, note, config, _, _, _ = seed_source(tmp_path)
    sources = load_sources_config(config)
    external = tmp_path / "outside"
    external.mkdir()
    (external / "secret.md").write_text("Private")
    (note.parent / "linked").symlink_to(external, target_is_directory=True)
    for ref in ("obsidian://notes/../outside/secret.md", "obsidian://notes/%2e%2e/outside/secret.md",
                "obsidian://notes/linked/secret.md", "obsidian://notes//note.md"):
        with pytest.raises((ValueError, OSError)):
            resolve_source_ref(ref, sources)


def test_filtering_precedes_result_limits_and_invalid_multisource_memory_is_not_used(tmp_path):
    _, _, note, _, store, entity, original = seed_source(tmp_path)
    note.unlink()
    other = store.upsert_entity(kind="person", canonical_name="Zed")
    fresh = store.propose_memory(entity_ref=other["entity_ref"], category="fact",
                                 content=original["content"],
                                 sources=[{"source_ref": "user://statement/zed", "source_kind": "user"}])
    store.approve(fresh["memory_ref"])
    assert read_people(store, query="SOURCE-BOUND", limit=1)["items"][0]["entity_ref"] == other["entity_ref"]
    assert read_memory_evidence(store, query="SOURCE-BOUND", limit=1)["items"][0]["memory_ref"] == fresh["memory_ref"]
    mixed = store.propose_memory(entity_ref=entity["entity_ref"], category="fact", content="Mixed assertion",
                                 sources=[{"source_ref": "user://statement/mixed", "source_kind": "user"},
                                          *original["sources"]])
    store.approve(mixed["memory_ref"])
    assert read_memory_evidence(store, query="Mixed")["items"] == []
