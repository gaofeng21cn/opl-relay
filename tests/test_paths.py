from pathlib import Path

from codex_mail_workbench import paths


def test_new_environment_variables_take_precedence(monkeypatch, tmp_path: Path) -> None:
    new_home = tmp_path / "relay-home"
    old_home = tmp_path / "legacy-home"
    workspace = tmp_path / "project"
    monkeypatch.setenv("OPL_RELAY_HOME", str(new_home))
    monkeypatch.setenv("CODEX_MAIL_HOME", str(old_home))
    monkeypatch.setenv("OPL_RELAY_WORKSPACE", str(workspace))

    assert paths.default_state_dir() == new_home
    assert paths.state_dir_source() == "OPL_RELAY_HOME"
    assert paths.default_workspace_dir() == workspace
    assert paths.workspace_dir_source() == "OPL_RELAY_WORKSPACE"


def test_legacy_environment_variable_remains_compatible(monkeypatch, tmp_path: Path) -> None:
    legacy = tmp_path / "legacy-home"
    monkeypatch.delenv("OPL_RELAY_HOME", raising=False)
    monkeypatch.setenv("CODEX_MAIL_HOME", str(legacy))

    assert paths.default_state_dir() == legacy
    assert paths.state_dir_source() == "CODEX_MAIL_HOME"


def test_existing_legacy_default_is_not_abandoned(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("OPL_RELAY_HOME", raising=False)
    monkeypatch.delenv("CODEX_MAIL_HOME", raising=False)
    monkeypatch.delenv("OPL_RELAY_WORKSPACE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    legacy = tmp_path / ".codex-mail-workbench"
    legacy.mkdir()
    (legacy / "mail.sqlite").touch()

    assert paths.default_state_dir() == legacy
    assert paths.state_dir_source() == "legacy_default"
    assert paths.default_workspace_dir() == tmp_path / ".opl-relay" / "workspaces" / "default"


def test_new_install_uses_relay_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("OPL_RELAY_HOME", raising=False)
    monkeypatch.delenv("CODEX_MAIL_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert paths.default_state_dir() == tmp_path / ".opl-relay"
    assert paths.state_dir_source() == "current_default"
