from __future__ import annotations

import hashlib
import json
import os
import shlex
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .paths import default_profile_workspace


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FrameworkKnowledge:
    """Thin client: Framework owns source identity, grants and accepted revisions."""

    def __init__(self, workspace_root: Path | None = None, project_root: Path | None = None,
                 *, command: list[str] | None = None):
        self.workspace_root = (workspace_root or default_profile_workspace()).expanduser().resolve()
        self.project_root = project_root.expanduser().resolve() if project_root else None
        self.command = command or shlex.split(os.environ.get("OPL_COMMAND", "opl"))
        if not self.command:
            raise ValueError("framework_command_missing")

    def _call(self, action: str, *args: str) -> dict[str, Any]:
        command = [*self.command, "knowledge", action, "--workspace", str(self.workspace_root),
                   "--domain", "relay", *args, "--json"]
        if self.project_root:
            command.extend(["--project", str(self.project_root)])
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=45, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("framework_knowledge_unavailable") from exc
        if result.returncode:
            # Provider output can contain private paths/content. Error details
            # belong to the owner CLI; consumers fail closed with a stable code.
            raise ValueError("framework_knowledge_rejected:" + action)
        try:
            payload = json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            raise ValueError("framework_knowledge_response_invalid") from exc
        if not isinstance(payload, dict):
            raise ValueError("framework_knowledge_response_invalid")
        return payload

    def sources(self) -> list[dict[str, Any]]:
        payload = self._call("sources").get("knowledge_sources", {})
        items = payload.get("items")
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ValueError("framework_knowledge_sources_invalid")
        return items

    def read_source(self, knowledge_id: str) -> dict[str, Any]:
        source = self._call("read-source", "--id", knowledge_id).get("knowledge_source")
        if not isinstance(source, dict) or source.get("knowledge_id") != knowledge_id:
            raise ValueError("framework_knowledge_source_invalid")
        content = source.get("content")
        if not isinstance(content, str) or source.get("sha256") != "sha256:" + hashlib.sha256(content.encode()).hexdigest():
            raise ValueError("framework_knowledge_integrity_mismatch")
        return source

    def verify_dependencies(self, versions: dict[str, str]) -> dict[str, Any]:
        # The owner API takes a private file, not a second persistent registry.
        with tempfile.TemporaryDirectory(prefix="relay-knowledge-") as directory:
            path = Path(directory) / "dependencies.json"
            path.write_text(json.dumps([{"knowledge_id": key, "sha256": value}
                                        for key, value in versions.items()]), encoding="utf-8")
            path.chmod(0o600)
            result = self._call("verify-dependencies", "--dependencies", str(path)).get("knowledge_dependencies")
        if not isinstance(result, dict) or not isinstance(result.get("valid"), bool):
            raise ValueError("framework_knowledge_dependencies_invalid")
        return result

    def search(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        if not query.strip():
            return []
        result = self._call("search", "--query", query, "--limit", str(max(1, min(limit, 20))),
                            "--excerpt-chars", "600").get("knowledge_search", {})
        items = result.get("matches")
        if not isinstance(items, list):
            raise ValueError("framework_knowledge_search_invalid")
        return [{**item, "source_currentness": {"status": "current", "checked_at": utc_now()},
                 "authority": "opl-framework", "accepted": True} for item in items]

    def discover(self, query: str, *, limit: int = 10) -> dict[str, Any]:
        result = self._call("discover", "--query", query, "--limit", str(max(1, min(limit, 20))),
                            "--excerpt-chars", "600").get("knowledge_discovery")
        if not isinstance(result, dict) or not isinstance(result.get("candidates"), list):
            raise ValueError("framework_knowledge_discovery_invalid")
        return {"discovery": result, "accepted": False, "authority": "opl-framework"}


class KnowledgeIndex:
    """Rebuildable metadata catalog. Never serves cached private source bodies."""

    def __init__(self, path: Path):
        self.path = path

    def rebuild(self, framework: FrameworkKnowledge) -> dict[str, Any]:
        items = framework.sources()  # Validate before mutating the derived catalog.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS knowledge_catalog (knowledge_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, metadata_json TEXT NOT NULL, indexed_at TEXT NOT NULL)")
            conn.execute("DELETE FROM knowledge_catalog")
            # Only the retired, reconstructible source-body index is removed.
            # memories, memory_sources and original mail are retained.
            conn.execute("DROP TABLE IF EXISTS knowledge_documents")
            for item in items:
                conn.execute("INSERT INTO knowledge_catalog VALUES (?, ?, ?, ?)",
                             (item["knowledge_id"], item["body_ref"]["source_id"],
                              json.dumps(item, ensure_ascii=False), utc_now()))
        return {"indexed": len(items), "authority": "opl-framework", "derived": True,
                "contains_source_bodies": False}

    def source_counts(self) -> dict[str, int]:
        if not self.path.exists():
            return {}
        with sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            try:
                return dict(conn.execute("SELECT source_id, COUNT(*) FROM knowledge_catalog GROUP BY source_id"))
            except sqlite3.OperationalError:
                return {}

    def search(self, query: str, *, framework: FrameworkKnowledge,
               source_ids: Iterable[str] = (), limit: int = 10) -> list[dict[str, Any]]:
        selected = set(source_ids)
        # Framework rechecks authorization and accepted bytes on every query;
        # a stale local catalog cannot resurrect revoked source text.
        return [item for item in framework.search(query, limit=20 if selected else limit)
                if not selected or item.get("source_id") in selected][:max(1, min(limit, 20))]
