from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any


WORKSPACE_SCHEMA = "opl_profile_workspace.v1"
MARKER_NAME = ".opl-profile-workspace.json"
LEGACY_MARKER_NAMES = (".opl-relay-workspace.json",)
ROOT_FILES = ("AGENTS.md", "profile.md")
CONTENT_DIRS = ("profile", "policies", "context", "templates")
LEGACY_CONTENT_DIRS = ("skills", "notes")
MANAGED_DIRS = (*CONTENT_DIRS, "exports", "data/relay", "data/persona")
PRIVATE_STATE_NAMES = {
    "accounts.toml",
    "drafts.sqlite",
    "mail.sqlite",
    "memory.sqlite",
    "sources.toml",
    "sync-state",
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
    legacy_marker = next(
        (root / name for name in LEGACY_MARKER_NAMES if (root / name).is_file()),
        None,
    )
    active_marker = marker if marker.is_file() else legacy_marker
    marker_error = ""
    schema = ""
    if active_marker is not None:
        try:
            payload = json.loads(active_marker.read_text(encoding="utf-8"))
            schema = str(payload.get("schema") or "")
            if schema not in {WORKSPACE_SCHEMA, "opl_relay_workspace.v1"}:
                marker_error = f"unsupported workspace schema: {schema or '<missing>'}"
        except (OSError, json.JSONDecodeError) as exc:
            marker_error = str(exc)

    present = [
        name
        for name in (*ROOT_FILES, *MANAGED_DIRS, *LEGACY_CONTENT_DIRS)
        if (root / name).exists()
    ]
    return {
        "path": str(root),
        "exists": root.is_dir(),
        "marker_path": str(marker),
        "marker_exists": marker.is_file(),
        "legacy_marker_path": str(legacy_marker) if legacy_marker else "",
        "legacy_marker_exists": legacy_marker is not None,
        "schema": schema,
        "marker_error": marker_error,
        "ready": root.is_dir() and active_marker is not None and not marker_error,
        "present": present,
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
    return inspect_workspace(root)


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
        if item.name in (*CONTENT_DIRS, *LEGACY_CONTENT_DIRS) and item.is_dir():
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
            if target_path.exists():
                continue
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
