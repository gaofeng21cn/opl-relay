from __future__ import annotations

import os
from pathlib import Path


def default_state_dir() -> Path:
    # CODEX_MAIL_HOME intentionally supports repo-local ignored profiles such
    # as ./local while keeping the package default outside the repository.
    configured = os.environ.get("CODEX_MAIL_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".codex-mail-workbench"


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
