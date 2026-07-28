from pathlib import Path

from codex_mail_workbench import paths


def test_profile_workspace_is_the_only_runtime_root(monkeypatch, tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    monkeypatch.setenv("OPL_PROFILE_WORKSPACE", str(profile))
    monkeypatch.setenv("OPL_RELAY_HOME", str(tmp_path / "ignored-relay-home"))
    monkeypatch.setenv("CODEX_MAIL_HOME", str(tmp_path / "ignored-codex-home"))
    monkeypatch.setenv("OPL_RELAY_WORKSPACE", str(tmp_path / "ignored-workspace"))

    assert paths.default_profile_workspace() == profile
    assert paths.default_state_dir() == profile / "data" / "relay"
    assert paths.default_workspace_dir() == profile
    assert paths.state_dir_source() == "OPL_PROFILE_WORKSPACE"
    assert paths.workspace_dir_source() == "OPL_PROFILE_WORKSPACE"


def test_default_profile_workspace_remains_the_only_runtime_root(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("OPL_PROFILE_WORKSPACE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OPL_RELAY_HOME", str(tmp_path / "ignored-relay-home"))
    monkeypatch.setenv("CODEX_MAIL_HOME", str(tmp_path / "ignored-codex-home"))
    monkeypatch.setenv("OPL_RELAY_WORKSPACE", str(tmp_path / "ignored-workspace"))

    expected = tmp_path / "OPL" / "profiles" / tmp_path.name
    assert paths.default_profile_workspace() == expected
    assert paths.default_state_dir() == expected / "data" / "relay"
    assert paths.default_workspace_dir() == expected
    assert paths.state_dir_source() == "profile_default"
    assert paths.workspace_dir_source() == "profile_default"
