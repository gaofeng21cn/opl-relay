from __future__ import annotations

import os
from pathlib import Path


from ._opl_profile_workspace import PROFILE_WORKSPACE_ENV, default_profile_workspace


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


def default_sync_state_dir() -> Path:
    return default_state_dir() / "sync-state"
