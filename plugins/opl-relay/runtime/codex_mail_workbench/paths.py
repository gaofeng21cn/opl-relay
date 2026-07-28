from __future__ import annotations

import os
from pathlib import Path


PROFILE_WORKSPACE_ENV = "OPL_PROFILE_WORKSPACE"


def default_profile_workspace() -> Path:
    configured = os.environ.get(PROFILE_WORKSPACE_ENV)
    if configured:
        return Path(configured).expanduser()
    profile_name = Path.home().name or "default"
    return Path.home() / "OPL" / "profiles" / profile_name


def profile_workspace_source() -> str:
    return PROFILE_WORKSPACE_ENV if os.environ.get(PROFILE_WORKSPACE_ENV) else "profile_default"


def default_state_dir() -> Path:
    return default_profile_workspace() / "data" / "relay"


def state_dir_source() -> str:
    return profile_workspace_source()


def default_workspace_dir() -> Path:
    return default_profile_workspace()


def workspace_dir_source() -> str:
    return profile_workspace_source()


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
