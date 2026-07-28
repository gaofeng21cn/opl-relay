import json
from pathlib import Path

import pytest

from codex_mail_workbench.workspace import (
    MARKER_NAME,
    WORKSPACE_SCHEMA,
    initialize_workspace,
    inspect_workspace,
    migrate_workspace,
    setup_status,
)


def test_initialize_and_inspect_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"

    result = initialize_workspace(workspace)

    assert result["ready"] is True
    assert result["schema"] == WORKSPACE_SCHEMA
    assert (workspace / "policies").is_dir()
    assert (workspace / "exports").is_dir()
    assert (workspace / "data" / "relay" / "accounts.toml").is_file()
    assert (workspace / "data" / "relay" / "sources.toml").is_file()
    assert json.loads((workspace / MARKER_NAME).read_text())["schema"] == WORKSPACE_SCHEMA


def test_initialize_is_idempotent_for_user_templates(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    initialize_workspace(workspace)
    identity = workspace / "profile" / "identity.md"
    identity.write_text("# Custom identity\n", encoding="utf-8")

    result = initialize_workspace(workspace)

    assert identity.read_text(encoding="utf-8") == "# Custom identity\n"
    assert result["created"] == []


def test_setup_status_is_actionable_before_and_after_init(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    before = setup_status(workspace)
    assert before["readiness"] == "unconfigured"
    assert "relay.accounts" in {step["id"] for step in before["steps"]}

    initialize_workspace(workspace)
    after = setup_status(workspace)
    assert after["readiness"] == "partial"
    assert after["accounts_configured"] == 0


def test_migrate_workspace_is_plan_only_by_default(tmp_path: Path) -> None:
    source = tmp_path / "local"
    target = tmp_path / "workspace"
    (source / "policies").mkdir(parents=True)
    (source / "profile.md").write_text("private profile\n", encoding="utf-8")
    (source / "policies" / "mail.md").write_text("policy\n", encoding="utf-8")
    (source / "mail.sqlite").touch()

    plan = migrate_workspace(source, target)

    assert plan["applied"] is False
    assert plan["copy"] == ["policies/mail.md", "profile.md"]
    assert plan["skipped_private_state"] == ["mail.sqlite"]
    assert not target.exists()


def test_migrate_workspace_copies_overlay_and_reads_back(tmp_path: Path) -> None:
    source = tmp_path / "local"
    target = tmp_path / "workspace"
    (source / "context").mkdir(parents=True)
    (source / "AGENTS.md").write_text("instructions\n", encoding="utf-8")
    (source / "context" / "people.md").write_text("people\n", encoding="utf-8")

    result = migrate_workspace(source, target, apply=True)

    assert result["ok"] is True
    assert result["workspace"]["ready"] is True
    assert (target / "AGENTS.md").read_text() == "instructions\n"
    assert (target / "context" / "people.md").read_text() == "people\n"
    assert inspect_workspace(target)["ready"] is True


def test_migrate_workspace_refuses_conflicting_bytes(tmp_path: Path) -> None:
    source = tmp_path / "local"
    target = tmp_path / "workspace"
    source.mkdir()
    target.mkdir()
    (source / "profile.md").write_text("source\n", encoding="utf-8")
    (target / "profile.md").write_text("target\n", encoding="utf-8")

    with pytest.raises(ValueError, match="conflicts"):
        migrate_workspace(source, target, apply=True)
