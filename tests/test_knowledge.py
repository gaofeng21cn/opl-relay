from pathlib import Path

from codex_mail_workbench.knowledge import KnowledgeIndex, load_sources_config


def test_obsidian_source_indexes_searches_and_removes_documents(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / "People").mkdir(parents=True)
    (vault / ".obsidian").mkdir()
    note = vault / "People" / "example.md"
    note.write_text(
        "# Professor Example\n\nWorks with the Example Consortium.",
        encoding="utf-8",
    )
    (vault / ".obsidian" / "private.md").write_text(
        "This should not be indexed.",
        encoding="utf-8",
    )
    config = tmp_path / "sources.toml"
    config.write_text(
        f"""
version = 1
[[sources]]
source_id = "personal-vault"
type = "obsidian"
path = "{vault}"
include = ["*.md", "**/*.md"]
exclude = [".obsidian/**", "**/.obsidian/**"]
""".strip(),
        encoding="utf-8",
    )

    source = load_sources_config(config)["personal-vault"]
    index = KnowledgeIndex(tmp_path / "memory.sqlite")
    first = index.index_source(source)
    results = index.search("Example Consortium")

    assert first["scanned"] == 1
    assert results[0]["source_ref"] == "obsidian://personal-vault/People/example.md"
    assert results[0]["title"] == "Professor Example"
    assert index.search("should not be indexed") == []

    note.unlink()
    second = index.index_source(source)
    assert second["deleted"] == 1
    assert index.search("Example Consortium") == []


def test_missing_sources_config_is_an_empty_optional_provider(tmp_path: Path) -> None:
    assert load_sources_config(tmp_path / "missing.toml") == {}


def test_source_include_patterns_do_not_cross_directories_without_double_star(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    (vault / "People").mkdir(parents=True)
    (vault / "Archive").mkdir()
    (vault / "root.md").write_text("Root note", encoding="utf-8")
    (vault / "People" / "person.md").write_text("Person note", encoding="utf-8")
    (vault / "Archive" / "old.md").write_text("Archived note", encoding="utf-8")
    config = tmp_path / "sources.toml"
    config.write_text(
        f"""
[[sources]]
source_id = "scoped"
type = "obsidian"
path = "{vault}"
include = ["*.md", "People/**/*.md"]
""".strip(),
        encoding="utf-8",
    )

    source = load_sources_config(config)["scoped"]
    index = KnowledgeIndex(tmp_path / "memory.sqlite")
    result = index.index_source(source)

    assert result["scanned"] == 2
    assert index.search("Root note")
    assert index.search("Person note")
    assert index.search("Archived note") == []
