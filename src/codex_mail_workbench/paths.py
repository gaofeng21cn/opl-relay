from __future__ import annotations

import os
from pathlib import Path


CURRENT_HOME_ENV = "OPL_RELAY_HOME"
LEGACY_HOME_ENV = "CODEX_MAIL_HOME"
WORKSPACE_ENV = "OPL_RELAY_WORKSPACE"
PROFILE_WORKSPACE_ENV = "OPL_PROFILE_WORKSPACE"


def default_profile_workspace() -> Path:
    configured = os.environ.get(PROFILE_WORKSPACE_ENV)
    if configured:
        return Path(configured).expanduser()
    profile_name = Path.home().name or "default"
    return Path.home() / "OPL" / "profiles" / profile_name


def profile_workspace_source() -> str:
    return PROFILE_WORKSPACE_ENV if os.environ.get(PROFILE_WORKSPACE_ENV) else "profile_default"


def current_state_dir() -> Path:
    return Path.home() / ".opl-relay"


def legacy_state_dir() -> Path:
    return Path.home() / ".codex-mail-workbench"


def _has_runtime_state(path: Path) -> bool:
    return any(
        (path / name).exists()
        for name in (
            "accounts.toml",
            "mail.sqlite",
            "drafts.sqlite",
            "memory.sqlite",
            "sources.toml",
            "sync-state",
        )
    )


def default_state_dir() -> Path:
    configured = os.environ.get(CURRENT_HOME_ENV)
    if configured:
        return Path(configured).expanduser()
    legacy_configured = os.environ.get(LEGACY_HOME_ENV)
    if legacy_configured:
        return Path(legacy_configured).expanduser()
    if os.environ.get(PROFILE_WORKSPACE_ENV):
        return default_profile_workspace() / "data" / "relay"

    current = current_state_dir()
    legacy = legacy_state_dir()
    if _has_runtime_state(current):
        return current
    if legacy.exists():
        return legacy
    return default_profile_workspace() / "data" / "relay"


def state_dir_source() -> str:
    if os.environ.get(CURRENT_HOME_ENV):
        return CURRENT_HOME_ENV
    if os.environ.get(LEGACY_HOME_ENV):
        return LEGACY_HOME_ENV
    if _has_runtime_state(current_state_dir()):
        return "current_default"
    if legacy_state_dir().exists():
        return "legacy_default"
    return "current_default"


def default_workspace_dir() -> Path:
    configured = os.environ.get(WORKSPACE_ENV)
    if configured:
        return Path(configured).expanduser()
    if os.environ.get(PROFILE_WORKSPACE_ENV):
        return default_profile_workspace()
    legacy_workspace = current_state_dir() / "workspaces" / "default"
    if legacy_workspace.exists():
        return legacy_workspace
    return default_profile_workspace()


def workspace_dir_source() -> str:
    if os.environ.get(WORKSPACE_ENV):
        return WORKSPACE_ENV
    if os.environ.get(PROFILE_WORKSPACE_ENV):
        return PROFILE_WORKSPACE_ENV
    if (current_state_dir() / "workspaces" / "default").exists():
        return "legacy_current_default"
    return "profile_default"


def default_config_path() -> Path:
    return default_state_dir() / "accounts.toml"


def default_db_path() -> Path:
    return default_state_dir() / "mail.sqlite"


def default_drafts_db_path() -> Path:
    return default_state_dir() / "drafts.sqlite"


def default_memory_db_path() -> Path:
    return default_state_dir() / "memory.sqlite"


def default_sources_config_path() -> Path:
    return default_state_dir() / "sources.toml"


def default_sync_state_dir() -> Path:
    return default_state_dir() / "sync-state"
