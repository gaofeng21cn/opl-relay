from __future__ import annotations

import hashlib
import json
import os
import shlex
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .paths import default_profile_workspace
from .schema import ensure_memory_schema

try:
    from opl_client import OplClient, OplClientError
except ImportError:  # pragma: no cover - optional thin SDK on PYTHONPATH/site-packages
    OplClient = None  # type: ignore[assignment]
    OplClientError = Exception  # type: ignore[assignment,misc]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_v2_source(item: dict[str, Any]) -> dict[str, Any]:
    """Normalize an opld v2 workspace knowledge source record into Relay metadata shape."""
    if "knowledge_id" in item and "body_ref" in item:
        return item
    source_id = str(item.get("source_id") or item.get("id") or "")
    knowledge_id = str(item.get("knowledge_id") or source_id)
    source_ref = str(item.get("source_ref") or item.get("root_path") or source_id)
    raw_sha = item.get("sha256") or item.get("snapshot_digest") or ""
    if not raw_sha:
        material = json.dumps(
            {
                "knowledge_id": knowledge_id,
                "source_id": source_id,
                "source_ref": source_ref,
                "revision": item.get("revision"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        raw_sha = "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()
    elif not str(raw_sha).startswith("sha256:"):
        raw_sha = "sha256:" + str(raw_sha)
    scope = item.get("scope")
    if isinstance(scope, str):
        scope_obj: dict[str, Any] = {"kind": scope, "id": source_id}
    elif isinstance(scope, dict):
        scope_obj = scope
    else:
        scope_obj = {"kind": "personal_profile", "id": source_id}
    return {
        "knowledge_id": knowledge_id,
        "title": item.get("title") or source_id or knowledge_id,
        "source_id": source_id,
        "source_ref": source_ref,
        "sha256": raw_sha,
        "body_ref": item.get("body_ref")
        or {"source_id": source_id, "ref": source_ref, "sha256": raw_sha},
        "scope": scope_obj,
        "content_kind": item.get("content_kind") or "state",
        "status": item.get("status") or "accepted",
        "reviewed_at": item.get("reviewed_at") or utc_now(),
        "next_review_at": item.get("next_review_at") or "2099-01-01T00:00:00Z",
        **({k: v for k, v in item.items() if k in {"content", "excerpt"}}),
    }


class FrameworkKnowledge:
    """Thin v2 client: OPL Workspace (via opl_client or `opl workspace`) owns source grants and revisions."""

    def __init__(
        self,
        workspace_root: Path | None = None,
        project_root: Path | None = None,
        *,
        command: list[str] | None = None,
    ):
        self.workspace_root = (workspace_root or default_profile_workspace()).expanduser().resolve()
        self.project_root = project_root.expanduser().resolve() if project_root else None
        self._explicit_command = command is not None
        self.command = command or shlex.split(os.environ.get("OPL_COMMAND", "opl"))
        if not self.command:
            raise ValueError("framework_command_missing")

    def _session_file(self) -> Path:
        state_dir = Path(os.environ.get("OPL_STATE_DIR") or (Path.home() / ".opl" / "state")).expanduser()
        return state_dir / "run" / "opld.session.json"

    def _select_workspace_id(self, workspaces: list[dict[str, Any]]) -> str | None:
        targets = [str(p) for p in (self.project_root, self.workspace_root) if p is not None]
        for ws in workspaces:
            root = ws.get("root_path")
            if isinstance(root, str) and str(Path(root).expanduser().resolve()) in targets:
                ws_id = ws.get("workspace_id")
                if isinstance(ws_id, str) and ws_id:
                    return ws_id
        if len(workspaces) == 1 and isinstance(workspaces[0].get("workspace_id"), str):
            return workspaces[0]["workspace_id"]
        return None

    def _call_opl_client(self, action: str, *, workspace_id: str | None = None, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if self._explicit_command or OplClient is None:
            return None
        session_path = self._session_file()
        if not session_path.is_file():
            return None
        try:
            with OplClient(session_file=session_path) as client:
                if action == "workspace.list":
                    return client.workspace_list()
                if workspace_id is None:
                    listed = client.workspace_list()
                    workspaces = listed.get("workspaces") if isinstance(listed, dict) else None
                    if not isinstance(workspaces, list):
                        return {"sources": []}
                    workspace_id = self._select_workspace_id(workspaces)
                    if not workspace_id:
                        return {"sources": []}
                call_params = {"workspace_id": workspace_id, **(params or {})}
                return client.call(action, call_params)
        except OplClientError as exc:
            raise ValueError("framework_knowledge_unavailable") from exc
        except Exception as exc:
            raise ValueError("framework_knowledge_unavailable") from exc

    def _call_cli(self, *subcommand: str, action_label: str) -> dict[str, Any]:
        command = [*self.command, "workspace", *subcommand, "--json"]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=45, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("framework_knowledge_unavailable") from exc
        if result.returncode:
            raise ValueError("framework_knowledge_rejected:" + action_label)
        try:
            payload = json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            raise ValueError("framework_knowledge_response_invalid") from exc
        if not isinstance(payload, dict):
            raise ValueError("framework_knowledge_response_invalid")
        return payload

    def _resolve_cli_workspace_id(self) -> str | None:
        listed = self._call_cli("list", action_label="list")
        workspaces = listed.get("workspaces")
        if not isinstance(workspaces, list):
            return None
        return self._select_workspace_id([w for w in workspaces if isinstance(w, dict)])

    def sources(self) -> list[dict[str, Any]]:
        rpc_payload = self._call_opl_client("workspace.knowledge.list")
        if rpc_payload is not None:
            raw_items = rpc_payload.get("sources")
            if raw_items is None and isinstance(rpc_payload.get("knowledge_sources"), dict):
                raw_items = rpc_payload["knowledge_sources"].get("items")
        else:
            ws_id = self._resolve_cli_workspace_id()
            cli_args = ["sources", "list"]
            if ws_id:
                cli_args.extend(["--workspace", ws_id])
            payload = self._call_cli(*cli_args, action_label="sources")
            raw_items = payload.get("sources")
            if raw_items is None and isinstance(payload.get("knowledge_sources"), dict):
                raw_items = payload["knowledge_sources"].get("items")
        if not isinstance(raw_items, list) or any(not isinstance(item, dict) for item in raw_items):
            raise ValueError("framework_knowledge_sources_invalid")
        return [_normalize_v2_source(item) for item in raw_items]

    def read_source(self, knowledge_id: str) -> dict[str, Any]:
        match = next((item for item in self.sources() if item.get("knowledge_id") == knowledge_id), None)
        if not isinstance(match, dict) or match.get("knowledge_id") != knowledge_id:
            raise ValueError("framework_knowledge_source_invalid")
        if "content" in match:
            content = match["content"]
            if not isinstance(content, str) or match.get("sha256") != "sha256:" + hashlib.sha256(content.encode()).hexdigest():
                raise ValueError("framework_knowledge_integrity_mismatch")
        return match

    def verify_dependencies(self, versions: dict[str, str]) -> dict[str, Any]:
        by_id = {item["knowledge_id"]: item for item in self.sources()}
        issues: list[str] = []
        for knowledge_id, expected_sha in versions.items():
            item = by_id.get(knowledge_id)
            if item is None:
                issues.append(f"knowledge_source_unauthorized:{knowledge_id}")
            elif item.get("sha256") != expected_sha:
                issues.append(f"knowledge_source_revision_changed:{knowledge_id}")
        return {"valid": len(issues) == 0, "issues": issues}

    def search(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        cleaned = query.strip()
        if not cleaned:
            return []
        needle = cleaned.casefold()
        capped = max(1, min(limit, 20))
        matches: list[dict[str, Any]] = []
        for item in self.sources():
            haystack = " ".join(
                str(item.get(field, "")) for field in ("knowledge_id", "title", "source_id", "source_ref")
            ).casefold()
            if needle in haystack:
                matches.append(
                    {
                        **item,
                        "source_currentness": {"status": "current", "checked_at": utc_now()},
                        "authority": "opl-framework",
                        "accepted": True,
                    }
                )
                if len(matches) >= capped:
                    break
        return matches

    def discover(self, query: str, *, limit: int = 10) -> dict[str, Any]:
        capped = max(1, min(limit, 20))
        cleaned = query.strip().casefold()
        candidates = [
            item
            for item in self.sources()
            if not cleaned
            or cleaned
            in " ".join(
                str(item.get(field, "")) for field in ("knowledge_id", "title", "source_id", "source_ref")
            ).casefold()
        ][:capped]
        return {
            "discovery": {"query": query, "candidates": candidates},
            "accepted": False,
            "authority": "opl-framework",
        }


class KnowledgeIndex:
    """Rebuildable metadata catalog. Never serves cached private source bodies."""

    def __init__(self, path: Path):
        self.path = path

    def rebuild(self, framework: FrameworkKnowledge) -> dict[str, Any]:
        items = framework.sources()  # Validate before mutating the derived catalog.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as conn:
            ensure_memory_schema(conn)
            conn.execute("DELETE FROM knowledge_catalog")
            # Only the retired, reconstructible source-body index is removed.
            # memories, memory_sources and original mail are retained.
            conn.execute("DROP TABLE IF EXISTS knowledge_documents")
            for item in items:
                conn.execute(
                    "INSERT INTO knowledge_catalog VALUES (?, ?, ?, ?)",
                    (
                        item["knowledge_id"],
                        item["body_ref"]["source_id"],
                        json.dumps(item, ensure_ascii=False),
                        utc_now(),
                    ),
                )
        return {
            "indexed": len(items),
            "authority": "opl-framework",
            "derived": True,
            "contains_source_bodies": False,
        }

    def source_counts(self) -> dict[str, int]:
        if not self.path.exists():
            return {}
        with sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            try:
                return dict(conn.execute("SELECT source_id, COUNT(*) FROM knowledge_catalog GROUP BY source_id"))
            except sqlite3.OperationalError:
                return {}

    def search(
        self,
        query: str,
        *,
        framework: FrameworkKnowledge,
        source_ids: Iterable[str] = (),
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        selected = set(source_ids)
        # Framework rechecks authorization and accepted bytes on every query;
        # a stale local catalog cannot resurrect revoked source text.
        return [
            item
            for item in framework.search(query, limit=20 if selected else limit)
            if not selected or item.get("source_id") in selected
        ][: max(1, min(limit, 20))]
