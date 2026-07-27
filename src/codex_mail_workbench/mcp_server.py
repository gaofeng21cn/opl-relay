from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .context import ContextBuilder
from .memory import MemoryStore
from .message import extract_text_body
from .paths import (
    default_db_path,
    default_memory_db_path,
    default_sources_config_path,
)
from .store import (
    connect_email_store,
    fetch_raw_email_by_storage_ref,
    get_message_by_storage_ref,
    list_messages,
)


TOOLS = [
    {
        "name": "mail_recent",
        "description": "List recent local mail messages from the Codex Mail Workbench SQLite store.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "account": {"type": "string"},
                "folder": {"type": "string"},
                "since": {"type": "string"},
                "until": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
        },
    },
    {
        "name": "mail_search",
        "description": "Search local mail metadata by subject, sender, recipient, or message id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "account": {"type": "string"},
                "folder": {"type": "string"},
                "since": {"type": "string"},
                "until": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "required": ["query"],
        },
    },
    {
        "name": "mail_read",
        "description": "Read one message body by storage_ref.",
        "inputSchema": {
            "type": "object",
            "properties": {"storage_ref": {"type": "string"}},
            "required": ["storage_ref"],
        },
    },
    {
        "name": "memory_search",
        "description": "Search approved private memories with evidence references.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "entity": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
        },
    },
    {
        "name": "context_build",
        "description": "Build a bounded drafting context from approved memory, mail evidence, and indexed knowledge.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "person": {"type": "string"},
                "project": {"type": "string"},
                "query": {"type": "string"},
                "mail_limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "memory_limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "knowledge_limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "anyOf": [
                {"required": ["person"]},
                {"required": ["project"]},
                {"required": ["query"]},
            ],
        },
    },
]


def text_result(payload: object) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=False, indent=2),
            }
        ]
    }


def dispatch_tool(
    name: str,
    arguments: dict[str, Any],
    db_path: Path,
    *,
    memory_db_path: Path | None = None,
    sources_config_path: Path | None = None,
) -> dict[str, Any]:
    memory_path = memory_db_path or default_memory_db_path()
    sources_path = sources_config_path or default_sources_config_path()
    if name in {"mail_recent", "mail_search", "mail_read"}:
        conn = connect_email_store(db_path)
        try:
            if name == "mail_recent":
                messages = list_messages(
                    conn,
                    account_ids=[arguments["account"]] if arguments.get("account") else None,
                    folder_slug=str(arguments.get("folder") or "") or None,
                    since=str(arguments.get("since") or "") or None,
                    until=str(arguments.get("until") or "") or None,
                    limit=int(arguments.get("limit") or 20),
                )
                return text_result({"ok": True, "messages": messages})
            if name == "mail_search":
                messages = list_messages(
                    conn,
                    account_ids=[arguments["account"]] if arguments.get("account") else None,
                    folder_slug=str(arguments.get("folder") or "") or None,
                    query=str(arguments["query"]),
                    since=str(arguments.get("since") or "") or None,
                    until=str(arguments.get("until") or "") or None,
                    limit=int(arguments.get("limit") or 20),
                )
                return text_result({"ok": True, "messages": messages})
            storage_ref = str(arguments["storage_ref"])
            meta = get_message_by_storage_ref(conn, storage_ref)
            raw = fetch_raw_email_by_storage_ref(conn, storage_ref)
            if not meta or raw is None:
                return text_result({"ok": False, "error": "message not found"})
            meta["body_text"] = extract_text_body(raw)
            return text_result({"ok": True, "message": meta})
        finally:
            conn.close()
    if name == "memory_search":
        memories = MemoryStore(memory_path).list_memories(
            entity=str(arguments.get("entity") or ""),
            statuses=("approved",),
            query=str(arguments.get("query") or ""),
            limit=int(arguments.get("limit") or 20),
        )
        return text_result({"ok": True, "memories": memories})
    if name == "context_build":
        payload = ContextBuilder(
            mail_db_path=db_path,
            memory_db_path=memory_path,
            sources_config_path=sources_path,
        ).build(
            person=str(arguments.get("person") or ""),
            project=str(arguments.get("project") or ""),
            query=str(arguments.get("query") or ""),
            mail_limit=int(arguments.get("mail_limit") or 6),
            memory_limit=int(arguments.get("memory_limit") or 20),
            knowledge_limit=int(arguments.get("knowledge_limit") or 6),
        )
        return text_result(payload)
    return text_result({"ok": False, "error": f"unknown tool: {name}"})


def handle_request(
    request: dict[str, Any],
    db_path: Path,
    *,
    memory_db_path: Path | None = None,
    sources_config_path: Path | None = None,
) -> dict[str, Any]:
    req_id = request.get("id")
    method = request.get("method")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "codex-mail-workbench", "version": __version__},
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = request.get("params") or {}
        result = dispatch_tool(
            str(params.get("name") or ""),
            params.get("arguments") if isinstance(params.get("arguments"), dict) else {},
            db_path,
            memory_db_path=memory_db_path,
            sources_config_path=sources_config_path,
        )
        return {"jsonrpc": "2.0", "id": req_id, "result": result}
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-mail-mcp")
    parser.add_argument("--db", default=str(default_db_path()))
    parser.add_argument("--memory-db", default=str(default_memory_db_path()))
    parser.add_argument("--sources-config", default=str(default_sources_config_path()))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db_path = Path(args.db).expanduser()
    memory_db_path = Path(args.memory_db).expanduser()
    sources_config_path = Path(args.sources_config).expanduser()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            response = handle_request(
                request,
                db_path,
                memory_db_path=memory_db_path,
                sources_config_path=sources_config_path,
            )
        except Exception as exc:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32000, "message": str(exc)},
            }
        print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
