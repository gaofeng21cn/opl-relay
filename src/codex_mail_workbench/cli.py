from __future__ import annotations

import argparse
import json
import shutil
import sys
from email.utils import getaddresses
from pathlib import Path

from . import __version__
from .apple_mail import AppleMailProvider
from .config import (
    KEYCHAIN_SERVICE,
    load_account,
    load_accounts_config,
)
from .context import ContextBuilder
from .drafts import DraftLedger, DraftService, Recipient
from .knowledge import KnowledgeIndex, load_sources_config
from .memory import (
    MEMORY_CATEGORIES,
    MEMORY_STATUSES,
    SENSITIVITIES,
    MemoryStore,
)
from .message import extract_text_body
from .persona import (
    load_approved_persona_draft_context,
    load_persona_mail_context,
    validate_approved_persona_draft_context,
)
from .paths import (
    default_config_path,
    default_db_path,
    default_drafts_db_path,
    default_memory_db_path,
    default_profile_workspace,
    default_sources_config_path,
    default_state_dir,
    profile_workspace_source,
    state_dir_source,
)
from .store import (
    connect_email_store,
    fetch_raw_email_by_storage_ref,
    get_message_by_storage_ref,
    list_messages,
    search_messages,
)
from .sync import sync_account
from .triage import build_triage_evidence, validate_triage_evidence
from .workspace import initialize_workspace, inspect_workspace, migrate_workspace


APP_CONTRIBUTION_ABI = "opl-package-app-contribution-cli.v1"
APP_CONTRIBUTION_REQUEST_SCHEMA = "opl-package-app-contribution-request.v1"
APP_CONTRIBUTION_RESPONSE_SCHEMA = "opl-package-app-contribution-response.v1"

APP_CONTRIBUTION_DATA_CONTRACTS = {
    "communications.mail.v1#recent": {
        "operation": "read",
        "input": {
            "account": {"type": "string", "required": False},
            "folder": {"type": "string", "required": False},
            "since": {"type": "string", "required": False},
            "until": {"type": "string", "required": False},
            "limit": {"type": "integer", "required": False, "minimum": 1, "maximum": 2000},
        },
        "result": "communications.mail.v1#recent.result",
    },
    "communications.mail.v1#draft.inspect": {
        "operation": "read",
        "input": {
            "draft_ref": {"type": "string", "required": True},
        },
        "result": "communications.mail.v1#draft.inspect.result",
    },
    "communications.mail.v1#triage.evidence": {
        "operation": "read",
        "input": {
            "source_ref": {"type": "string", "required": True},
            "policy_refs": {"type": "string[]", "required": True, "min_items": 1},
        },
        "result": "communications.mail.v1#triage.evidence.result",
    },
    "personal.memory.v1#search": {
        "operation": "read",
        "input": {
            "entity": {"type": "string", "required": False},
            "query": {"type": "string", "required": False},
            "limit": {"type": "integer", "required": False, "minimum": 1, "maximum": 500},
        },
        "result": "personal.memory.v1#search.result",
    },
}

APP_CONTRIBUTION_ACTION_CONTRACTS = {
    "communications.mail.v1#sync.incremental": {
        "operation": "execute",
        "confirmation_required": False,
        "input": {
            "account": {"type": "string", "required": True},
            "limit_per_folder": {"type": "integer", "required": False, "minimum": 1},
            "dry_run": {"type": "boolean", "required": False},
        },
        "result": "communications.mail.v1#sync.incremental.result",
    },
    "communications.mail.v1#draft.create": {
        "operation": "execute",
        "confirmation_required": False,
        "input": {
            "account": {"type": "string", "required": True},
            "to": {"type": "string[]", "required": True, "min_items": 1},
            "cc": {"type": "string[]", "required": False},
            "bcc": {"type": "string[]", "required": False},
            "subject": {"type": "string", "required": True},
            "body": {"type": "string", "required": True},
            "open": {"type": "boolean", "required": False},
        },
        "result": "communications.mail.v1#draft.create.result",
    },
    "communications.mail.v1#draft.create_from_persona": {
        "operation": "execute",
        "confirmation_required": True,
        "input": {
            "proposal_bundle": {"type": "object", "required": True},
            "account": {"type": "string", "required": True},
            "to": {"type": "string[]", "required": True, "min_items": 1},
            "cc": {"type": "string[]", "required": False},
            "bcc": {"type": "string[]", "required": False},
            "open": {"type": "boolean", "required": False},
        },
        "result": "communications.mail.v1#draft.create_from_persona.result",
    },
    "communications.mail.v1#draft.inspect": {
        "operation": "execute",
        "confirmation_required": False,
        "input": {
            "draft_ref": {"type": "string", "required": True},
        },
        "result": "communications.mail.v1#draft.inspect.result",
    },
    "communications.mail.v1#draft.open": {
        "operation": "execute",
        "confirmation_required": False,
        "input": {
            "draft_ref": {"type": "string", "required": True},
        },
        "result": "communications.mail.v1#draft.open.result",
    },
    "communications.mail.v1#draft.send": {
        "operation": "execute",
        "confirmation_required": True,
        "input": {
            "draft_ref": {"type": "string", "required": True},
            "approval": {"type": "string", "required": True},
        },
        "result": "communications.mail.v1#draft.send.result",
    },
    "personal.context.v1#build": {
        "operation": "execute",
        "confirmation_required": False,
        "input": {
            "person": {"type": "string", "required": False},
            "project": {"type": "string", "required": False},
            "query": {"type": "string", "required": False},
            "mail_limit": {"type": "integer", "required": False, "minimum": 1, "maximum": 200},
            "memory_limit": {"type": "integer", "required": False, "minimum": 1, "maximum": 500},
            "knowledge_limit": {"type": "integer", "required": False, "minimum": 1, "maximum": 200},
        },
        "result": "personal.context.v1#build.result",
    },
    "personal.memory.v1#inspect": {
        "operation": "execute",
        "confirmation_required": False,
        "input": {
            "memory_ref": {"type": "string", "required": True},
        },
        "result": "personal.memory.v1#inspect.result",
    },
}


def emit(payload: object, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if isinstance(payload, dict):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(payload)


def fail(message: str, *, as_json: bool, code: int = 1) -> int:
    payload = {"ok": False, "error": message}
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
    else:
        print(f"ERROR: {message}", file=sys.stderr)
    return code


def _app_contribution_error(ref: str | None, code: str, message: str) -> dict[str, object]:
    return {
        "schema_version": APP_CONTRIBUTION_RESPONSE_SCHEMA,
        "ok": False,
        "ref": ref,
        "error": {"code": code, "message": message},
    }


def _app_contribution_response(ref: str, operation: str, result: object) -> dict[str, object]:
    return {
        "schema_version": APP_CONTRIBUTION_RESPONSE_SCHEMA,
        "ok": True,
        "ref": ref,
        "operation": operation,
        "result": result,
    }


def _app_contribution_input(value: object) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("input must be an object")
    return value


def _app_contribution_string(value: object, field: str, *, required: bool = False) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError(f"input.{field} must be a non-empty string")
    return value.strip()


def _app_contribution_bool(value: object, field: str, *, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"input.{field} must be a boolean")
    return value


def _app_contribution_integer(
    value: object,
    field: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"input.{field} must be an integer from {minimum} to {maximum}")
    return value


def _app_contribution_strings(value: object, field: str, *, required: bool = False) -> list[str]:
    if value is None:
        if required:
            raise ValueError(f"input.{field} must be a non-empty string array")
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"input.{field} must be a string array")
    values = [item.strip() for item in value]
    if required and not values:
        raise ValueError(f"input.{field} must be a non-empty string array")
    return values


def _app_contribution_input_keys(
    payload: dict[str, object],
    contract: dict[str, object],
) -> None:
    fields = contract["input"]
    assert isinstance(fields, dict)
    unexpected = sorted(set(payload) - set(fields))
    if unexpected:
        raise ValueError("input contains unsupported fields: " + ", ".join(unexpected))
    for name, schema in fields.items():
        assert isinstance(name, str)
        assert isinstance(schema, dict)
        required = schema.get("required") is True
        if name not in payload:
            if required:
                raise ValueError(f"input.{name} is required")
            continue
        value = payload[name]
        value_type = schema.get("type")
        if value_type == "string":
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"input.{name} must be a non-empty string")
        elif value_type == "boolean":
            if not isinstance(value, bool):
                raise ValueError(f"input.{name} must be a boolean")
        elif value_type == "integer":
            minimum = schema.get("minimum", -sys.maxsize)
            maximum = schema.get("maximum", sys.maxsize)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not isinstance(minimum, int)
                or not isinstance(maximum, int)
                or not minimum <= value <= maximum
            ):
                raise ValueError(f"input.{name} must be an integer from {minimum} to {maximum}")
        elif value_type == "string[]":
            if not isinstance(value, list) or not all(
                isinstance(item, str) and item.strip() for item in value
            ):
                raise ValueError(f"input.{name} must be a string array")
            minimum_items = schema.get("min_items", 0)
            if not isinstance(minimum_items, int) or len(value) < minimum_items:
                raise ValueError(f"input.{name} must contain at least {minimum_items} items")
        elif value_type == "object":
            if not isinstance(value, dict):
                raise ValueError(f"input.{name} must be an object")
        else:
            raise ValueError(f"unsupported input type for {name}")


def _app_contribution_data(args: argparse.Namespace, ref: str, payload: dict[str, object]) -> object:
    if ref == "communications.mail.v1#recent":
        conn = connect_email_store(Path(args.db).expanduser())
        try:
            return {
                "messages": list_messages(
                    conn,
                    account_ids=[_app_contribution_string(payload.get("account"), "account")]
                    if payload.get("account") is not None
                    else None,
                    folder_slug=_app_contribution_string(payload.get("folder"), "folder") or None,
                    since=_app_contribution_string(payload.get("since"), "since") or None,
                    until=_app_contribution_string(payload.get("until"), "until") or None,
                    limit=_app_contribution_integer(
                        payload.get("limit"), "limit", default=20, minimum=1, maximum=2000
                    ),
                )
            }
        finally:
            conn.close()
    if ref == "communications.mail.v1#draft.inspect":
        draft_ref = _app_contribution_string(payload.get("draft_ref"), "draft_ref", required=True)
        service, _ = draft_service(args)
        return {"draft": service.inspect(draft_ref)}
    if ref == "communications.mail.v1#triage.evidence":
        return {
            "evidence": build_triage_evidence(
                mail_db_path=Path(args.db).expanduser(),
                source_ref=_app_contribution_string(
                    payload.get("source_ref"), "source_ref", required=True
                ),
                policy_refs=_app_contribution_strings(
                    payload.get("policy_refs"), "policy_refs", required=True
                ),
            )
        }
    if ref == "personal.memory.v1#search":
        return {
            "memories": memory_store(args).list_memories(
                entity=_app_contribution_string(payload.get("entity"), "entity"),
                query=_app_contribution_string(payload.get("query"), "query"),
                limit=_app_contribution_integer(
                    payload.get("limit"), "limit", default=50, minimum=1, maximum=500
                ),
            )
        }
    raise ValueError("unsupported data ref")


def _app_contribution_action(args: argparse.Namespace, ref: str, payload: dict[str, object]) -> object:
    if ref == "communications.mail.v1#sync.incremental":
        return sync_account(
            config_path=Path(args.config).expanduser(),
            db_path=Path(args.db).expanduser(),
            account_id=_app_contribution_string(payload.get("account"), "account", required=True),
            mode="incremental",
            limit_per_folder=(
                _app_contribution_integer(
                    payload.get("limit_per_folder"),
                    "limit_per_folder",
                    default=1,
                    minimum=1,
                    maximum=100000,
                )
                if payload.get("limit_per_folder") is not None
                else None
            ),
            dry_run=_app_contribution_bool(payload.get("dry_run"), "dry_run", default=False),
        )
    if ref == "communications.mail.v1#draft.create":
        account = load_account(
            Path(args.config).expanduser(),
            _app_contribution_string(payload.get("account"), "account", required=True),
        )
        service, _ = draft_service(args)
        return {
            "draft": service.create(
                account_id=account.account_id,
                sender=account.email,
                to=parse_recipients(_app_contribution_strings(payload.get("to"), "to", required=True), required=True),
                cc=parse_recipients(_app_contribution_strings(payload.get("cc"), "cc")),
                bcc=parse_recipients(_app_contribution_strings(payload.get("bcc"), "bcc")),
                subject=_app_contribution_string(payload.get("subject"), "subject", required=True),
                body_text=_app_contribution_string(payload.get("body"), "body", required=True),
                attachments=[],
                visible=_app_contribution_bool(payload.get("open"), "open", default=True),
            )
        }
    if ref == "communications.mail.v1#draft.create_from_persona":
        return create_persona_review_draft(
            args,
            context=validate_approved_persona_draft_context(
                payload.get("proposal_bundle")
            ),
            account_id=_app_contribution_string(
                payload.get("account"),
                "account",
                required=True,
            ),
            to=_app_contribution_strings(payload.get("to"), "to", required=True),
            cc=_app_contribution_strings(payload.get("cc"), "cc"),
            bcc=_app_contribution_strings(payload.get("bcc"), "bcc"),
            visible=_app_contribution_bool(payload.get("open"), "open", default=True),
        )
    if ref == "communications.mail.v1#draft.inspect":
        service, _ = draft_service(args)
        return {
            "draft": service.inspect(
                _app_contribution_string(payload.get("draft_ref"), "draft_ref", required=True)
            )
        }
    if ref == "communications.mail.v1#draft.open":
        service, _ = draft_service(args)
        return {
            "draft": service.open(
                _app_contribution_string(payload.get("draft_ref"), "draft_ref", required=True)
            )
        }
    if ref == "communications.mail.v1#draft.send":
        service, _ = draft_service(args)
        return {
            "draft": service.send(
                _app_contribution_string(payload.get("draft_ref"), "draft_ref", required=True),
                approval=_app_contribution_string(payload.get("approval"), "approval", required=True),
            )
        }
    if ref == "personal.context.v1#build":
        return ContextBuilder(
            mail_db_path=Path(args.db).expanduser(),
            memory_db_path=Path(args.memory_db).expanduser(),
            sources_config_path=Path(args.sources_config).expanduser(),
        ).build(
            person=_app_contribution_string(payload.get("person"), "person"),
            project=_app_contribution_string(payload.get("project"), "project"),
            query=_app_contribution_string(payload.get("query"), "query"),
            mail_limit=_app_contribution_integer(
                payload.get("mail_limit"), "mail_limit", default=6, minimum=1, maximum=200
            ),
            memory_limit=_app_contribution_integer(
                payload.get("memory_limit"), "memory_limit", default=20, minimum=1, maximum=500
            ),
            knowledge_limit=_app_contribution_integer(
                payload.get("knowledge_limit"), "knowledge_limit", default=6, minimum=1, maximum=200
            ),
        )
    if ref == "personal.memory.v1#inspect":
        return {
            "memory": memory_store(args).get_memory(
                _app_contribution_string(payload.get("memory_ref"), "memory_ref", required=True)
            )
        }
    raise ValueError("unsupported action ref")


def cmd_app_contribution(args: argparse.Namespace) -> int:
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        unexpected = sorted(set(request) - {"schema_version", "operation", "ref", "input"})
        if unexpected:
            raise ValueError("request contains unsupported fields: " + ", ".join(unexpected))
        if request.get("schema_version") != APP_CONTRIBUTION_REQUEST_SCHEMA:
            raise ValueError(f"schema_version must be {APP_CONTRIBUTION_REQUEST_SCHEMA}")
        operation = request.get("operation")
        ref = request.get("ref")
        if not isinstance(operation, str) or operation not in {"describe", "read", "execute"}:
            raise ValueError("operation must be describe, read, or execute")
        if not isinstance(ref, str) or not ref:
            raise ValueError("ref must be a non-empty string")
        data_contract = APP_CONTRIBUTION_DATA_CONTRACTS.get(ref)
        action_contract = APP_CONTRIBUTION_ACTION_CONTRACTS.get(ref)
        if data_contract is None and action_contract is None:
            raise ValueError("ref is not declared by this package")
        if operation == "describe":
            if "input" in request:
                raise ValueError("describe does not accept input")
            emit(
                _app_contribution_response(
                    ref,
                    operation,
                    {
                        "abi": APP_CONTRIBUTION_ABI,
                        "request_schema": APP_CONTRIBUTION_REQUEST_SCHEMA,
                        "response_schema": APP_CONTRIBUTION_RESPONSE_SCHEMA,
                        "ref": ref,
                        "operations": [
                            contract
                            for contract in (data_contract, action_contract)
                            if contract is not None
                        ],
                    },
                ),
                as_json=True,
            )
            return 0
        contract = data_contract if operation == "read" else action_contract
        if contract is None:
            raise ValueError(f"{ref} does not support {operation}")
        payload = _app_contribution_input(request.get("input"))
        _app_contribution_input_keys(payload, contract)
        result = (
            _app_contribution_data(args, ref, payload)
            if operation == "read"
            else _app_contribution_action(args, ref, payload)
        )
        emit(_app_contribution_response(ref, operation, result), as_json=True)
        return 0
    except Exception as exc:
        ref = None
        try:
            if isinstance(locals().get("request"), dict) and isinstance(request.get("ref"), str):
                ref = request["ref"]
        except UnboundLocalError:
            pass
        emit(_app_contribution_error(ref, "invalid_request", str(exc)), as_json=True)
        return 2


def cmd_doctor(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser()
    db_path = Path(args.db).expanduser()
    drafts_db_path = Path(args.draft_db).expanduser()
    memory_db_path = Path(args.memory_db).expanduser()
    sources_config_path = Path(args.sources_config).expanduser()
    workspace = inspect_workspace(default_profile_workspace())
    accounts = {}
    config_error = ""
    sources_error = ""
    if config_path.exists():
        try:
            accounts = load_accounts_config(config_path)
        except Exception as exc:
            config_error = str(exc)
    if sources_config_path.exists():
        try:
            load_sources_config(sources_config_path)
        except Exception as exc:
            sources_error = str(exc)
    payload = {
        "ok": not config_error and not sources_error,
        "product": "opl-relay",
        "version": __version__,
        "command": shutil.which("opl-relay"),
        "commands": {
            "opl-relay": shutil.which("opl-relay"),
        },
        "state_dir": str(default_state_dir()),
        "state_dir_source": state_dir_source(),
        "profile_workspace": str(default_profile_workspace()),
        "profile_workspace_source": profile_workspace_source(),
        "workspace": workspace,
        "config_path": str(config_path),
        "config_exists": config_path.exists(),
        "config_error": config_error,
        "db_path": str(db_path),
        "db_exists": db_path.exists(),
        "draft_db_path": str(drafts_db_path),
        "draft_db_exists": drafts_db_path.exists(),
        "memory_db_path": str(memory_db_path),
        "memory_db_exists": memory_db_path.exists(),
        "sources_config_path": str(sources_config_path),
        "sources_config_exists": sources_config_path.exists(),
        "sources_config_error": sources_error,
        "apple_mail_available": sys.platform == "darwin" and bool(shutil.which("osascript")),
        "accounts": sorted(accounts.keys()),
        "keychain_services": [KEYCHAIN_SERVICE],
    }
    emit(payload, as_json=args.json)
    return 0 if payload["ok"] else 1


def cmd_workspace_inspect(args: argparse.Namespace) -> int:
    emit(
        {"ok": True, "workspace": inspect_workspace(default_profile_workspace())},
        as_json=args.json,
    )
    return 0


def cmd_workspace_init(args: argparse.Namespace) -> int:
    emit(
        {"ok": True, "workspace": initialize_workspace(default_profile_workspace())},
        as_json=args.json,
    )
    return 0


def cmd_workspace_migrate(args: argparse.Namespace) -> int:
    payload = migrate_workspace(
        Path(args.from_path),
        default_profile_workspace(),
        apply=args.apply,
    )
    emit(payload, as_json=args.json)
    return 0 if payload["ok"] else 1


def cmd_accounts(args: argparse.Namespace) -> int:
    accounts = load_accounts_config(Path(args.config).expanduser())
    emit(
        {
            "ok": True,
            "accounts": [
                {
                    "account_id": account.account_id,
                    "email": account.email,
                    "imap_host": account.imap.host,
                    "include_folders": account.include_folders,
                    "exclude_folders": account.exclude_folders,
                }
                for account in accounts.values()
            ],
        },
        as_json=args.json,
    )
    return 0


def cmd_recent(args: argparse.Namespace) -> int:
    conn = connect_email_store(Path(args.db).expanduser())
    try:
        rows = list_messages(
            conn,
            account_ids=[args.account] if args.account else None,
            folder_slug=args.folder,
            since=args.since,
            until=args.until,
            limit=args.limit,
        )
    finally:
        conn.close()
    emit({"ok": True, "messages": rows}, as_json=args.json)
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    conn = connect_email_store(Path(args.db).expanduser())
    try:
        rows = search_messages(
            conn,
            queries=[args.query],
            account_ids=[args.account] if args.account else None,
            folder_slug=args.folder,
            since=args.since,
            until=args.until,
            include_body=args.include_body,
            max_scan=args.max_scan,
            limit=args.limit,
        )
    finally:
        conn.close()
    emit({"ok": True, "messages": rows}, as_json=args.json)
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    conn = connect_email_store(Path(args.db).expanduser())
    try:
        meta = get_message_by_storage_ref(conn, args.storage_ref)
        raw = fetch_raw_email_by_storage_ref(conn, args.storage_ref)
    finally:
        conn.close()
    if not meta or raw is None:
        return fail("message not found", as_json=args.json, code=2)
    meta["body_text"] = extract_text_body(raw)
    if args.include_raw:
        meta["raw_eml"] = raw.decode("utf-8", errors="replace")
    emit({"ok": True, "message": meta}, as_json=args.json)
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    payload = sync_account(
        config_path=Path(args.config).expanduser(),
        db_path=Path(args.db).expanduser(),
        account_id=args.account,
        mode=args.mode,
        limit_per_folder=args.limit_per_folder,
        dry_run=args.dry_run,
    )
    emit(payload, as_json=args.json)
    return 0


def memory_store(args: argparse.Namespace) -> MemoryStore:
    return MemoryStore(Path(args.memory_db).expanduser())


def cmd_memory_entity_upsert(args: argparse.Namespace) -> int:
    entity = memory_store(args).upsert_entity(
        kind=args.kind,
        canonical_name=args.name,
        aliases=args.alias,
        emails=args.email,
    )
    emit({"ok": True, "entity": entity}, as_json=args.json)
    return 0


def cmd_memory_entity_list(args: argparse.Namespace) -> int:
    entities = memory_store(args).list_entities(query=args.query, limit=args.limit)
    emit({"ok": True, "entities": entities}, as_json=args.json)
    return 0


def _source_kind(source_ref: str) -> str:
    if source_ref.startswith("email-store://"):
        return "email"
    if source_ref.startswith("obsidian://"):
        return "obsidian"
    if source_ref.startswith("user://"):
        return "user"
    if source_ref.startswith("calendar://"):
        return "calendar"
    if source_ref.startswith("contact://"):
        return "contact"
    if source_ref.startswith("model://"):
        return "model"
    return "other"


def _memory_sources(args: argparse.Namespace) -> list[dict[str, str]]:
    source_refs = list(args.source)
    excerpts = list(args.source_excerpt)
    if excerpts and len(excerpts) != len(source_refs):
        raise ValueError("--source-excerpt must be omitted or supplied once per --source")

    email_meta: dict[str, dict[str, object]] = {}
    email_refs = [ref for ref in source_refs if ref.startswith("email-store://")]
    if email_refs:
        db_path = Path(args.db).expanduser()
        if not db_path.exists():
            raise FileNotFoundError(f"mail database not found: {db_path}")
        conn = connect_email_store(db_path)
        try:
            for source_ref in email_refs:
                meta = get_message_by_storage_ref(conn, source_ref)
                if not meta:
                    raise ValueError(f"email evidence not found: {source_ref}")
                email_meta[source_ref] = meta
        finally:
            conn.close()

    return [
        {
            "source_ref": source_ref,
            "source_kind": _source_kind(source_ref),
            "excerpt": excerpts[index] if excerpts else "",
            "source_sha256": str(email_meta.get(source_ref, {}).get("raw_sha256") or ""),
        }
        for index, source_ref in enumerate(source_refs)
    ]


def cmd_memory_propose(args: argparse.Namespace) -> int:
    store = memory_store(args)
    entity = store.resolve_entity(args.entity)
    memory = store.propose_memory(
        entity_ref=entity["entity_ref"],
        category=args.category,
        content=args.content,
        sources=_memory_sources(args),
        confidence=args.confidence,
        sensitivity=args.sensitivity,
        occurred_at=args.occurred_at,
        valid_from=args.valid_from,
        valid_until=args.valid_until,
        supersedes_ref=args.supersedes,
    )
    emit({"ok": True, "memory": memory}, as_json=args.json)
    return 0


def cmd_memory_candidates(args: argparse.Namespace) -> int:
    memories = memory_store(args).list_memories(
        entity=args.entity,
        statuses=("candidate",),
        query=args.query,
        limit=args.limit,
    )
    emit({"ok": True, "memories": memories}, as_json=args.json)
    return 0


def cmd_memory_search(args: argparse.Namespace) -> int:
    memories = memory_store(args).list_memories(
        entity=args.entity,
        statuses=args.status or ("approved",),
        query=args.query,
        limit=args.limit,
    )
    emit({"ok": True, "memories": memories}, as_json=args.json)
    return 0


def cmd_memory_inspect(args: argparse.Namespace) -> int:
    memory = memory_store(args).get_memory(args.memory_ref)
    if memory is None:
        return fail("memory not found", as_json=args.json, code=2)
    emit({"ok": True, "memory": memory}, as_json=args.json)
    return 0


def _memory_transition(args: argparse.Namespace, action: str) -> int:
    store = memory_store(args)
    memory = getattr(store, action)(args.memory_ref)
    emit({"ok": True, "memory": memory}, as_json=args.json)
    return 0


def cmd_memory_approve(args: argparse.Namespace) -> int:
    return _memory_transition(args, "approve")


def cmd_memory_reject(args: argparse.Namespace) -> int:
    return _memory_transition(args, "reject")


def cmd_memory_forget(args: argparse.Namespace) -> int:
    return _memory_transition(args, "forget")


def cmd_sources_list(args: argparse.Namespace) -> int:
    config_path = Path(args.sources_config).expanduser()
    sources = load_sources_config(config_path)
    counts = KnowledgeIndex(Path(args.memory_db).expanduser()).source_counts()
    emit(
        {
            "ok": True,
            "config_path": str(config_path),
            "sources": [
                {
                    "source_id": source.source_id,
                    "type": source.kind,
                    "path": str(source.path),
                    "include": list(source.include),
                    "exclude": list(source.exclude),
                    "indexed_documents": counts.get(source.source_id, 0),
                }
                for source in sources.values()
            ],
        },
        as_json=args.json,
    )
    return 0


def cmd_sources_index(args: argparse.Namespace) -> int:
    sources = load_sources_config(Path(args.sources_config).expanduser())
    selected = (
        [sources[args.source]]
        if args.source and args.source in sources
        else list(sources.values())
    )
    if args.source and args.source not in sources:
        raise KeyError(f"knowledge source not found: {args.source}")
    index = KnowledgeIndex(Path(args.memory_db).expanduser())
    results = [index.index_source(source) for source in selected]
    emit({"ok": True, "results": results}, as_json=args.json)
    return 0


def cmd_sources_search(args: argparse.Namespace) -> int:
    results = KnowledgeIndex(Path(args.memory_db).expanduser()).search(
        args.query,
        source_ids=args.source,
        limit=args.limit,
    )
    emit({"ok": True, "results": results}, as_json=args.json)
    return 0


def cmd_context_build(args: argparse.Namespace) -> int:
    payload = ContextBuilder(
        mail_db_path=Path(args.db).expanduser(),
        memory_db_path=Path(args.memory_db).expanduser(),
        sources_config_path=Path(args.sources_config).expanduser(),
    ).build(
        person=args.person,
        project=args.project,
        query=args.query,
        mail_limit=args.mail_limit,
        memory_limit=args.memory_limit,
        knowledge_limit=args.knowledge_limit,
    )
    emit(payload, as_json=args.json)
    return 0


def cmd_persona_draft_context(args: argparse.Namespace) -> int:
    emit(
        load_persona_mail_context(Path(args.input).expanduser()),
        as_json=args.json,
    )
    return 0


def create_persona_review_draft(
    args: argparse.Namespace,
    *,
    context: dict[str, object],
    account_id: str,
    to: list[str],
    cc: list[str],
    bcc: list[str],
    visible: bool,
) -> dict[str, object]:
    """Create an Apple Mail review draft without authorizing send."""
    account = load_account(Path(args.config).expanduser(), account_id)
    service, _ = draft_service(args)
    draft = service.create(
        account_id=account.account_id,
        sender=account.email,
        to=parse_recipients(to, required=True),
        cc=parse_recipients(cc),
        bcc=parse_recipients(bcc),
        subject=str(context["subject"]),
        body_text=str(context["body_text"]),
        attachments=[],
        visible=visible,
    )
    return {
        "proposal_id": context["proposal_id"],
        "approval_ref": context["approval_ref"],
        "source_refs": context["source_refs"],
        "draft": draft,
        "review_required": True,
        "send_allowed": False,
        "next_action": "inspect_and_review_in_apple_mail",
    }


def cmd_persona_draft_create(args: argparse.Namespace) -> int:
    context = load_approved_persona_draft_context(Path(args.input).expanduser())
    emit(
        {
            "ok": True,
            "handoff": create_persona_review_draft(
                args,
                context=context,
                account_id=args.account,
                to=args.to,
                cc=args.cc,
                bcc=args.bcc,
                visible=args.open,
            ),
        },
        as_json=args.json,
    )
    return 0


def cmd_triage_evidence(args: argparse.Namespace) -> int:
    payload = build_triage_evidence(
        mail_db_path=Path(args.db).expanduser(),
        source_ref=args.source_ref,
        policy_refs=list(args.policy_ref),
    )
    emit({"ok": True, "evidence": payload}, as_json=args.json)
    return 0


def _triage_evidence_from_cli_output(value: object) -> object:
    """Accept the exact successful wrapper emitted by ``triage evidence``."""
    if not isinstance(value, dict) or set(value) != {"ok", "evidence"}:
        return value
    if value["ok"] is not True:
        raise ValueError("triage evidence command result must be successful")
    if not isinstance(value["evidence"], dict):
        raise ValueError("triage evidence command result must contain an evidence object")
    return value["evidence"]


def cmd_triage_validate(args: argparse.Namespace) -> int:
    raw = sys.stdin.read() if args.input == "-" else Path(args.input).expanduser().read_text(
        encoding="utf-8"
    )
    payload = _triage_evidence_from_cli_output(json.loads(raw))
    emit(validate_triage_evidence(payload), as_json=args.json)
    return 0


def parse_recipients(values: list[str] | None, *, required: bool = False) -> list[Recipient]:
    parsed = [
        Recipient(address=address.strip(), name=name.strip())
        for name, address in getaddresses(values or [])
        if address.strip()
    ]
    invalid = [item.address for item in parsed if "@" not in item.address]
    if invalid:
        raise ValueError("无效邮件地址: " + ", ".join(invalid))
    if required and not parsed:
        raise ValueError("至少需要一个收件人")
    return parsed


def read_body(args: argparse.Namespace) -> str:
    if args.body_file == "-":
        return sys.stdin.read()
    if args.body_file:
        return Path(args.body_file).expanduser().read_text(encoding="utf-8-sig")
    return str(args.body or "")


def draft_service(args: argparse.Namespace) -> tuple[DraftService, AppleMailProvider]:
    provider = AppleMailProvider()
    ledger = DraftLedger(Path(args.draft_db).expanduser())
    return DraftService(ledger, provider), provider


def cmd_draft_create(args: argparse.Namespace) -> int:
    account = load_account(Path(args.config).expanduser(), args.account)
    attachments = [Path(value).expanduser() for value in args.attach]
    missing = [str(path) for path in attachments if not path.is_file()]
    if missing:
        raise FileNotFoundError("附件不存在: " + ", ".join(missing))
    service, _ = draft_service(args)
    payload = service.create(
        account_id=account.account_id,
        sender=account.email,
        to=parse_recipients(args.to, required=True),
        cc=parse_recipients(args.cc),
        bcc=parse_recipients(args.bcc),
        subject=args.subject,
        body_text=read_body(args),
        attachments=attachments,
        visible=args.open,
    )
    emit({"ok": True, "draft": payload}, as_json=args.json)
    return 0


def cmd_draft_adopt(args: argparse.Namespace) -> int:
    account = load_account(Path(args.config).expanduser(), args.account)
    service, provider = draft_service(args)
    provider_account = provider.resolve_account(account.email)
    payload = service.adopt(
        account_id=account.account_id,
        provider_account=provider_account,
        provider_uuid=args.apple_mail_uuid,
    )
    emit({"ok": True, "draft": payload}, as_json=args.json)
    return 0


def cmd_draft_inspect(args: argparse.Namespace) -> int:
    service, _ = draft_service(args)
    emit(
        {"ok": True, "draft": service.inspect(args.draft_ref)},
        as_json=args.json,
    )
    return 0


def cmd_draft_open(args: argparse.Namespace) -> int:
    service, _ = draft_service(args)
    emit(
        {"ok": True, "draft": service.open(args.draft_ref)},
        as_json=args.json,
    )
    return 0


def cmd_draft_send(args: argparse.Namespace) -> int:
    service, _ = draft_service(args)
    emit(
        {
            "ok": True,
            "draft": service.send(args.draft_ref, approval=args.approval),
        },
        as_json=args.json,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="opl-relay")
    parser.add_argument("--json", action="store_true", help="输出稳定 JSON")
    parser.add_argument("--config", default=str(default_config_path()), help="accounts.toml 路径")
    parser.add_argument("--db", default=str(default_db_path()), help="SQLite 邮件库路径")
    parser.add_argument(
        "--draft-db",
        default=str(default_drafts_db_path()),
        help="本地草稿审批 ledger 路径",
    )
    parser.add_argument(
        "--memory-db",
        default=str(default_memory_db_path()),
        help="私有记忆与知识索引 SQLite 路径",
    )
    parser.add_argument(
        "--sources-config",
        default=str(default_sources_config_path()),
        help="外部知识源 sources.toml 路径",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="检查配置、数据库和安装状态")
    doctor.set_defaults(func=cmd_doctor)

    app_contribution = sub.add_parser(
        "app-contribution",
        help="执行 Package-owned app contribution JSON ABI",
    )
    app_contribution.set_defaults(func=cmd_app_contribution)

    accounts = sub.add_parser("accounts", help="列出配置账号")
    accounts.set_defaults(func=cmd_accounts)

    recent = sub.add_parser("recent", help="列出最近邮件")
    recent.add_argument("--account", default="")
    recent.add_argument("--folder", default="")
    recent.add_argument("--since", default="", help="include messages at or after this ISO datetime")
    recent.add_argument("--until", default="", help="include messages before this ISO datetime")
    recent.add_argument("--limit", type=int, default=20)
    recent.set_defaults(func=cmd_recent)

    search = sub.add_parser("search", help="搜索本地邮件元数据")
    search.add_argument("query")
    search.add_argument("--account", default="")
    search.add_argument("--folder", default="")
    search.add_argument("--since", default="", help="include messages at or after this ISO datetime")
    search.add_argument("--until", default="", help="include messages before this ISO datetime")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--include-body", action=argparse.BooleanOptionalAction, default=True)
    search.add_argument("--max-scan", type=int, default=500)
    search.set_defaults(func=cmd_search)

    read = sub.add_parser("read", help="读取一封邮件正文")
    read.add_argument("storage_ref")
    read.add_argument("--include-raw", action="store_true")
    read.set_defaults(func=cmd_read)

    sync = sub.add_parser("sync", help="从 IMAP 同步邮件到本地 SQLite")
    sync.add_argument("--account", required=True)
    sync.add_argument("--mode", choices=["initial", "incremental"], default="incremental")
    sync.add_argument("--limit-per-folder", type=int, default=None)
    sync.add_argument("--dry-run", action="store_true")
    sync.set_defaults(func=cmd_sync)

    memory = sub.add_parser("memory", help="私有人物、项目与关系记忆")
    memory_actions = memory.add_subparsers(dest="memory_action", required=True)

    memory_entity = memory_actions.add_parser("entity", help="管理记忆主体")
    entity_actions = memory_entity.add_subparsers(dest="entity_action", required=True)
    entity_upsert = entity_actions.add_parser("upsert", help="新增或更新人物、机构或项目")
    entity_upsert.add_argument("--kind", choices=["person", "organization", "project"], required=True)
    entity_upsert.add_argument("--name", required=True)
    entity_upsert.add_argument("--alias", action="append", default=[])
    entity_upsert.add_argument("--email", action="append", default=[])
    entity_upsert.set_defaults(func=cmd_memory_entity_upsert)
    entity_list = entity_actions.add_parser("list", help="列出或搜索记忆主体")
    entity_list.add_argument("--query", default="")
    entity_list.add_argument("--limit", type=int, default=50)
    entity_list.set_defaults(func=cmd_memory_entity_list)

    memory_propose = memory_actions.add_parser("propose", help="提出带证据的记忆候选")
    memory_propose.add_argument("--entity", required=True)
    memory_propose.add_argument("--category", choices=sorted(MEMORY_CATEGORIES), required=True)
    memory_propose.add_argument("--content", required=True)
    memory_propose.add_argument("--source", action="append", required=True)
    memory_propose.add_argument("--source-excerpt", action="append", default=[])
    memory_propose.add_argument("--confidence", type=float, default=1.0)
    memory_propose.add_argument("--sensitivity", choices=sorted(SENSITIVITIES), default="private")
    memory_propose.add_argument("--occurred-at", default="")
    memory_propose.add_argument("--valid-from", default="")
    memory_propose.add_argument("--valid-until", default="")
    memory_propose.add_argument("--supersedes", default="")
    memory_propose.set_defaults(func=cmd_memory_propose)

    memory_candidates = memory_actions.add_parser("candidates", help="列出待审核记忆")
    memory_candidates.add_argument("--entity", default="")
    memory_candidates.add_argument("--query", default="")
    memory_candidates.add_argument("--limit", type=int, default=50)
    memory_candidates.set_defaults(func=cmd_memory_candidates)

    memory_search = memory_actions.add_parser("search", help="搜索结构化记忆")
    memory_search.add_argument("query", nargs="?", default="")
    memory_search.add_argument("--entity", default="")
    memory_search.add_argument(
        "--status",
        action="append",
        choices=sorted(MEMORY_STATUSES),
        default=None,
    )
    memory_search.add_argument("--limit", type=int, default=50)
    memory_search.set_defaults(func=cmd_memory_search)

    memory_inspect = memory_actions.add_parser("inspect", help="查看记忆及其证据")
    memory_inspect.add_argument("memory_ref")
    memory_inspect.set_defaults(func=cmd_memory_inspect)
    for name, handler, help_text in [
        ("approve", cmd_memory_approve, "批准记忆候选"),
        ("reject", cmd_memory_reject, "拒绝记忆候选"),
        ("forget", cmd_memory_forget, "停用但不物理删除记忆"),
    ]:
        action = memory_actions.add_parser(name, help=help_text)
        action.add_argument("memory_ref")
        action.set_defaults(func=handler)

    sources = sub.add_parser("sources", help="预配置并索引外部知识源")
    source_actions = sources.add_subparsers(dest="sources_action", required=True)
    source_list = source_actions.add_parser("list", help="列出知识源及索引状态")
    source_list.set_defaults(func=cmd_sources_list)
    source_index = source_actions.add_parser("index", help="建立或更新只读知识索引")
    source_index.add_argument("--source", default="")
    source_index.set_defaults(func=cmd_sources_index)
    source_search = source_actions.add_parser("search", help="搜索已建立的知识索引")
    source_search.add_argument("query")
    source_search.add_argument("--source", action="append", default=[])
    source_search.add_argument("--limit", type=int, default=10)
    source_search.set_defaults(func=cmd_sources_search)

    context = sub.add_parser("context", help="组装起草所需的最小证据上下文")
    context_actions = context.add_subparsers(dest="context_action", required=True)
    context_build = context_actions.add_parser("build", help="按人物、项目或任务组装上下文")
    context_build.add_argument("--person", default="")
    context_build.add_argument("--project", default="")
    context_build.add_argument("--query", default="")
    context_build.add_argument("--mail-limit", type=int, default=6)
    context_build.add_argument("--memory-limit", type=int, default=20)
    context_build.add_argument("--knowledge-limit", type=int, default=6)
    context_build.set_defaults(func=cmd_context_build)

    persona = sub.add_parser("persona", help="读取跨域 Persona 提案")
    persona_actions = persona.add_subparsers(dest="persona_action", required=True)
    persona_context = persona_actions.add_parser(
        "draft-context",
        help="读取 Persona 的邮件草稿上下文，不创建或发送草稿",
    )
    persona_context.add_argument("--input", required=True)
    persona_context.set_defaults(func=cmd_persona_draft_context)
    persona_draft_create = persona_actions.add_parser(
        "draft-create",
        help="从已批准 Persona 提案创建 Apple Mail 审核草稿；不发送",
    )
    persona_draft_create.add_argument("--input", required=True)
    persona_draft_create.add_argument("--account", required=True)
    persona_draft_create.add_argument("--to", action="append", required=True)
    persona_draft_create.add_argument("--cc", action="append", default=[])
    persona_draft_create.add_argument("--bcc", action="append", default=[])
    persona_draft_create.add_argument(
        "--open",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="创建后在 Apple Mail 中显示草稿",
    )
    persona_draft_create.set_defaults(func=cmd_persona_draft_create)

    triage = sub.add_parser("triage", help="生成或验证只读邮件 triage 证据")
    triage_actions = triage.add_subparsers(dest="triage_action", required=True)
    triage_evidence = triage_actions.add_parser(
        "evidence",
        help="从 email-store:// 邮件证据生成不可写邮箱的 triage envelope",
    )
    triage_evidence.add_argument("source_ref")
    triage_evidence.add_argument("--policy-ref", action="append", required=True)
    triage_evidence.set_defaults(func=cmd_triage_evidence)
    triage_validate = triage_actions.add_parser(
        "validate",
        help="验证 triage evidence 的 provenance、policy digest 与写入边界",
    )
    triage_validate.add_argument("--input", required=True)
    triage_validate.set_defaults(func=cmd_triage_validate)

    workspace = sub.add_parser("workspace", help="检查、初始化或迁移 Relay workspace")
    workspace_actions = workspace.add_subparsers(dest="workspace_action", required=True)
    workspace_inspect = workspace_actions.add_parser("inspect", help="只读检查 workspace")
    workspace_inspect.set_defaults(func=cmd_workspace_inspect)
    workspace_init = workspace_actions.add_parser("init", help="初始化 workspace 目录")
    workspace_init.set_defaults(func=cmd_workspace_init)
    workspace_migrate = workspace_actions.add_parser(
        "migrate",
        help="从旧 private overlay 复制到当前 workspace",
    )
    workspace_migrate.add_argument("--from", dest="from_path", required=True)
    workspace_migrate.add_argument(
        "--apply",
        action="store_true",
        help="执行复制；省略时只输出计划",
    )
    workspace_migrate.set_defaults(func=cmd_workspace_migrate)

    draft = sub.add_parser("draft", help="Apple Mail 草稿审核与受控发送")
    draft_actions = draft.add_subparsers(dest="draft_action", required=True)

    draft_create = draft_actions.add_parser("create", help="创建并保存 Apple Mail 草稿")
    draft_create.add_argument("--account", required=True)
    draft_create.add_argument("--to", action="append", required=True)
    draft_create.add_argument("--cc", action="append", default=[])
    draft_create.add_argument("--bcc", action="append", default=[])
    draft_create.add_argument("--subject", required=True)
    body_source = draft_create.add_mutually_exclusive_group(required=True)
    body_source.add_argument("--body")
    body_source.add_argument("--body-file")
    draft_create.add_argument("--attach", action="append", default=[])
    draft_create.add_argument(
        "--open",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="创建后在 Apple Mail 中显示草稿",
    )
    draft_create.set_defaults(func=cmd_draft_create)

    draft_adopt = draft_actions.add_parser("adopt", help="登记现有 Apple Mail 草稿")
    draft_adopt.add_argument("--account", required=True)
    draft_adopt.add_argument("--apple-mail-uuid", required=True)
    draft_adopt.set_defaults(func=cmd_draft_adopt)

    draft_inspect = draft_actions.add_parser("inspect", help="回读草稿并生成审批指纹")
    draft_inspect.add_argument("draft_ref")
    draft_inspect.set_defaults(func=cmd_draft_inspect)

    draft_open = draft_actions.add_parser("open", help="在 Apple Mail 中打开草稿")
    draft_open.add_argument("draft_ref")
    draft_open.set_defaults(func=cmd_draft_open)

    draft_send = draft_actions.add_parser("send", help="使用当前审批指纹单次发送")
    draft_send.add_argument("draft_ref")
    draft_send.add_argument("--approval", required=True)
    draft_send.set_defaults(func=cmd_draft_send)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        return fail(str(exc), as_json=getattr(args, "json", False))


if __name__ == "__main__":
    raise SystemExit(main())
