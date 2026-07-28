from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any


WORKSPACE_SCHEMA = "opl_profile_workspace.v1"
MARKER_NAME = ".opl-profile-workspace.json"
ROOT_FILES = ("AGENTS.md", "profile.md")
CONTENT_DIRS = ("profile", "policies", "context", "templates")
MANAGED_DIRS = (*CONTENT_DIRS, "exports", "data/relay", "data/persona")
PRIVATE_STATE_NAMES = {
    "accounts.toml",
    "drafts.sqlite",
    "mail.sqlite",
    "memory.sqlite",
    "sources.toml",
    "sync-state",
}

_TEMPLATES = {
    "AGENTS.md": """# Profile Workspace

This directory belongs to one person's OPL digital persona. Keep private
identity, policies, context, and module state here; do not copy it into a
Package or Plugin directory.
""",
    "profile/identity.md": """# Identity

- name:
- role:
- institution:
- preferred_language: zh-CN
""",
    "profile/preferences.md": """# Preferences

- draft_review: required
- external_writes: proposal_only
- mail_send: user_approval_required
""",
    "policies/mail-triage.md": """# Mail triage

Treat incoming mail as evidence. Prepare proposals and drafts for review;
never send, archive, move, delete, or mark mail without explicit approval.
""",
    "policies/knowledge.md": """# Knowledge output

Use source references for every proposed note and preserve the existing note
when its expected digest changes.
""",
    "policies/website.md": """# Website output

Prepare website changes as reviewable proposals. The website repository remains
the authority for its own content and publication state.
""",
    "data/relay/accounts.toml": """version = 1

# Add an account with: opl-relay --json account add ...
accounts = []
""",
    "data/relay/sources.toml": """version = 1

# Add an Obsidian source only after its path has been reviewed.
sources = []
""",
}


def _resolve(path: Path) -> Path:
    return path.expanduser().resolve()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_workspace(path: Path) -> dict[str, Any]:
    root = _resolve(path)
    marker = root / MARKER_NAME
    marker_error = ""
    schema = ""
    if marker.is_file():
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
            schema = str(payload.get("schema") or "")
            if schema != WORKSPACE_SCHEMA:
                marker_error = f"unsupported workspace schema: {schema or '<missing>'}"
        except (OSError, json.JSONDecodeError) as exc:
            marker_error = str(exc)

    present = [
        name
        for name in (*ROOT_FILES, *MANAGED_DIRS)
        if (root / name).exists()
    ]
    return {
        "path": str(root),
        "exists": root.is_dir(),
        "marker_path": str(marker),
        "marker_exists": marker.is_file(),
        "schema": schema,
        "marker_error": marker_error,
        "ready": root.is_dir() and marker.is_file() and not marker_error,
        "present": present,
    }


def setup_status(path: Path) -> dict[str, Any]:
    """Return actionable first-run status without reading private content."""

    root = _resolve(path)
    workspace = inspect_workspace(root)
    config = root / "data" / "relay" / "accounts.toml"
    sources = root / "data" / "relay" / "sources.toml"
    steps = [
        {
            "id": "workspace",
            "status": "ready" if workspace["ready"] else "required",
            "path": str(root / MARKER_NAME),
        },
        {
            "id": "relay.accounts",
            "status": "ready" if config.is_file() else "required",
            "path": str(config),
        },
        {
            "id": "relay.sources",
            "status": "ready" if sources.is_file() else "optional",
            "path": str(sources),
        },
    ]
    account_count = 0
    config_error = ""
    if config.is_file():
        try:
            from .config import load_accounts_config

            account_count = len(load_accounts_config(config))
        except Exception as exc:
            config_error = str(exc)
            steps[1]["status"] = "invalid"
    required = [step for step in steps if step["status"] in {"required", "invalid"}]
    readiness = "unconfigured" if not workspace["ready"] else ("partial" if required or not account_count else "ready")
    next_actions: list[str] = []
    if not workspace["ready"] or required:
        next_actions.append("opl-relay --json setup init")
    if not account_count:
        next_actions.append("opl-relay --json account add --id <id> --email <address> --host <imap-host> ...")
    return {
        "workspace": workspace,
        "readiness": readiness,
        "steps": steps,
        "accounts_configured": account_count,
        "config_error": config_error,
        "next_actions": list(dict.fromkeys(next_actions)),
    }


def initialize_workspace(path: Path) -> dict[str, Any]:
    root = _resolve(path)
    root.mkdir(parents=True, exist_ok=True)
    for name in MANAGED_DIRS:
        (root / name).mkdir(parents=True, exist_ok=True)
    marker = root / MARKER_NAME
    expected = json.dumps({"schema": WORKSPACE_SCHEMA}, indent=2, sort_keys=True) + "\n"
    if marker.exists() and marker.read_text(encoding="utf-8") != expected:
        raise ValueError(f"workspace marker already exists with different content: {marker}")
    marker.write_text(expected, encoding="utf-8")
    created: list[str] = []
    for relative, content in _TEMPLATES.items():
        target = root / relative
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        created.append(relative)
    return inspect_workspace(root) | {"created": created, "setup": setup_status(root)}


def _migration_files(source: Path) -> tuple[list[tuple[Path, Path]], list[str]]:
    files: list[tuple[Path, Path]] = []
    skipped: list[str] = []
    for item in sorted(source.iterdir(), key=lambda value: value.name):
        if item.name in PRIVATE_STATE_NAMES or item.suffix in {
            ".sqlite",
            ".sqlite3",
            ".eml",
            ".mbox",
        }:
            skipped.append(item.name)
            continue
        if item.name in ROOT_FILES and item.is_file():
            files.append((item, Path(item.name)))
            continue
        if item.name in CONTENT_DIRS and item.is_dir():
            for child in sorted(item.rglob("*")):
                if child.is_symlink():
                    raise ValueError(f"workspace migration does not follow symlinks: {child}")
                if child.is_file():
                    files.append((child, child.relative_to(source)))
            continue
        if item.name != MARKER_NAME:
            skipped.append(item.name)
    return files, skipped


def migrate_workspace(
    source: Path,
    target: Path,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    source_root = _resolve(source)
    target_root = _resolve(target)
    if not source_root.is_dir():
        raise FileNotFoundError(f"workspace source does not exist: {source_root}")
    if source_root == target_root:
        raise ValueError("workspace source and target must differ")

    files, skipped = _migration_files(source_root)
    conflicts: list[str] = []
    unchanged: list[str] = []
    copies: list[str] = []
    for source_path, relative in files:
        target_path = target_root / relative
        if target_path.exists():
            if not target_path.is_file() or _sha256(source_path) != _sha256(target_path):
                conflicts.append(str(relative))
            else:
                unchanged.append(str(relative))
        else:
            copies.append(str(relative))

    if apply and conflicts:
        raise ValueError("workspace migration conflicts: " + ", ".join(conflicts))
    if apply:
        initialize_workspace(target_root)
        for source_path, relative in files:
            target_path = target_root / relative
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target_path)
        for source_path, relative in files:
            if _sha256(source_path) != _sha256(target_root / relative):
                raise RuntimeError(f"workspace migration readback failed: {relative}")

    payload = {
        "ok": not conflicts,
        "applied": apply,
        "source": str(source_root),
        "target": str(target_root),
        "copy": copies,
        "unchanged": unchanged,
        "conflicts": conflicts,
        "skipped_private_state": skipped,
    }
    if apply:
        payload["workspace"] = inspect_workspace(target_root)
    return payload
