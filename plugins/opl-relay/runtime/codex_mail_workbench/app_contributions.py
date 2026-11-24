"""Pydantic v2 contracts and handlers for OPL Relay's App Contribution JSON ABI."""

from __future__ import annotations

import copy
import json
import sqlite3
import sys
import types
from pathlib import Path
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    model_validator,
)
from pydantic_core import PydanticUndefined

from .context import ContextBuilder
from .message import extract_text_body
from .paths import default_profile_workspace
from .people import read_memory_evidence, read_people
from .persona import validate_approved_persona_draft_context
from .store import (
    SCOPES,
    connect_email_store_readonly,
    fetch_raw_email_by_storage_ref,
    get_message_by_storage_ref,
    list_messages,
    scope_filter,
)
from .triage import build_triage_evidence

APP_CONTRIBUTION_ABI = "opl-package-app-contribution-cli.v1"
APP_CONTRIBUTION_REQUEST_SCHEMA = "opl-package-app-contribution-request.v1"
APP_CONTRIBUTION_RESPONSE_SCHEMA = "opl-package-app-contribution-response.v1"


def _strip_non_empty(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("must be a non-empty string")
    return stripped


NonEmptyStr = Annotated[StrictStr, AfterValidator(_strip_non_empty)]


class _StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_nulls(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            raise ValueError("input must be an object")
        for key, value in data.items():
            if value is None:
                raise ValueError(f"input.{key} must not be null")
        return data


# ---------------------------------------------------------------------------
# Data contract models (read)
# ---------------------------------------------------------------------------


class RecentMailInput(_StrictInput):
    account: NonEmptyStr | None = None
    folder: NonEmptyStr | None = None
    since: NonEmptyStr | None = None
    until: NonEmptyStr | None = None
    query: NonEmptyStr | None = None
    scope: Literal["active", "history", "all"] = "active"
    status: Literal["all", "unreviewed", "open", "waiting", "closed", "none"] = "all"
    offset: Annotated[StrictInt, Field(ge=0, le=sys.maxsize)] = 0
    limit: Annotated[StrictInt, Field(ge=1, le=2000)] = 20


class DraftInspectReadInput(_StrictInput):
    draft_ref: NonEmptyStr | None = None
    query: NonEmptyStr | None = None
    status: Literal["active", "all", "draft", "sending", "unknown", "sent"] = "active"
    offset: Annotated[StrictInt, Field(ge=0, le=sys.maxsize)] = 0
    limit: Annotated[StrictInt, Field(ge=1, le=500)] = 50


class TriageEvidenceReadInput(_StrictInput):
    source_ref: NonEmptyStr | None = None
    policy_refs: Annotated[list[NonEmptyStr], Field(min_length=1)] | None = None


class MemorySearchReadInput(_StrictInput):
    entity: NonEmptyStr | None = None
    query: NonEmptyStr | None = None
    limit: Annotated[StrictInt, Field(ge=1, le=500)] | None = None
    include_stale: StrictBool = False


class PeopleReadInput(_StrictInput):
    query: NonEmptyStr | None = None
    limit: Annotated[StrictInt, Field(ge=1, le=500)] | None = None
    include_stale: StrictBool = False


# ---------------------------------------------------------------------------
# Action contract models (execute)
# ---------------------------------------------------------------------------


class WorkbenchOpenInput(_StrictInput):
    account: NonEmptyStr | None = None
    storage_ref: NonEmptyStr | None = None
    draft_id: NonEmptyStr | None = None
    request_id: NonEmptyStr | None = None


class SyncIncrementalInput(_StrictInput):
    account: NonEmptyStr
    limit_per_folder: Annotated[StrictInt, Field(ge=1)] | None = None
    dry_run: StrictBool | None = None


class DraftCreateInput(_StrictInput):
    account: NonEmptyStr
    to: Annotated[list[NonEmptyStr], Field(min_length=1)]
    cc: list[NonEmptyStr] | None = None
    bcc: list[NonEmptyStr] | None = None
    subject: NonEmptyStr
    body: NonEmptyStr
    open: StrictBool | None = None


class DraftCreateFromPersonaInput(_StrictInput):
    proposal_bundle: dict[str, Any]
    account: NonEmptyStr
    to: Annotated[list[NonEmptyStr], Field(min_length=1)]
    cc: list[NonEmptyStr] | None = None
    bcc: list[NonEmptyStr] | None = None
    open: StrictBool | None = None


class DraftReplyAllInput(_StrictInput):
    account: NonEmptyStr
    apple_mail_account: NonEmptyStr
    apple_mail_id: Annotated[StrictInt, Field(ge=1)]
    mailbox_path: NonEmptyStr
    body: NonEmptyStr
    open: StrictBool | None = None


class DraftRefInput(_StrictInput):
    draft_ref: NonEmptyStr


class DraftSendInput(_StrictInput):
    draft_ref: NonEmptyStr
    approval: NonEmptyStr


class ContextBuildInput(_StrictInput):
    person: NonEmptyStr | None = None
    project: NonEmptyStr | None = None
    query: NonEmptyStr | None = None
    mail_limit: Annotated[StrictInt, Field(ge=1, le=200)] | None = None
    memory_limit: Annotated[StrictInt, Field(ge=1, le=500)] | None = None
    knowledge_limit: Annotated[StrictInt, Field(ge=1, le=200)] | None = None


class MemoryInspectInput(_StrictInput):
    memory_ref: NonEmptyStr


# ---------------------------------------------------------------------------
# Request envelope & contract metadata derived from Pydantic models
# ---------------------------------------------------------------------------


class _AppContributionEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["opl-package-app-contribution-request.v1"]
    operation: Literal["describe", "read", "execute"]
    ref: NonEmptyStr
    input: Any = None


def _unwrap_optional(annotation: Any) -> tuple[Any, list[Any]]:
    metadata: list[Any] = []
    current = annotation
    while True:
        origin = get_origin(current)
        if origin is Annotated:
            args = get_args(current)
            current = args[0]
            metadata.extend(args[1:])
        elif origin in (Union, types.UnionType):
            non_none = [arg for arg in get_args(current) if arg is not type(None)]
            if len(non_none) == 1:
                current = non_none[0]
            else:
                break
        else:
            break
    return current, metadata


def _contract_input_schema(model_cls: type[BaseModel]) -> dict[str, dict[str, object]]:
    fields: dict[str, dict[str, object]] = {}
    for name, field_info in model_cls.model_fields.items():
        base_type, extra_meta = _unwrap_optional(field_info.annotation)
        meta = [*field_info.metadata, *extra_meta]
        origin = get_origin(base_type)
        entry: dict[str, object] = {}
        if origin is Literal:
            entry["type"] = "string"
            entry["required"] = field_info.is_required()
            entry["enum"] = list(get_args(base_type))
        elif origin is list:
            entry["type"] = "string[]"
            entry["required"] = field_info.is_required()
        elif origin is dict or base_type is dict:
            entry["type"] = "object"
            entry["required"] = field_info.is_required()
        elif base_type is bool:
            entry["type"] = "boolean"
            entry["required"] = field_info.is_required()
        elif base_type is int:
            entry["type"] = "integer"
            entry["required"] = field_info.is_required()
        else:
            entry["type"] = "string"
            entry["required"] = field_info.is_required()

        for item in meta:
            if hasattr(item, "ge") and item.ge is not None:
                entry["minimum"] = item.ge
            if hasattr(item, "le") and item.le is not None:
                entry["maximum"] = item.le
            if hasattr(item, "min_length") and item.min_length is not None:
                entry["min_items"] = item.min_length

        if field_info.default is not PydanticUndefined and field_info.default is not None:
            entry["default"] = field_info.default
        fields[name] = entry
    return fields


DATA_INPUT_MODELS: dict[str, type[_StrictInput]] = {
    "communications.mail.v1#recent": RecentMailInput,
    "communications.mail.v1#draft.inspect": DraftInspectReadInput,
    "communications.mail.v1#triage.evidence": TriageEvidenceReadInput,
    "personal.memory.v1#search": MemorySearchReadInput,
    "personal.memory.v1#people": PeopleReadInput,
}

ACTION_SPECS: dict[str, tuple[type[_StrictInput], bool]] = {
    "communications.mail.v1#workbench.open": (WorkbenchOpenInput, False),
    "communications.mail.v1#sync.incremental": (SyncIncrementalInput, False),
    "communications.mail.v1#draft.create": (DraftCreateInput, False),
    "communications.mail.v1#draft.create_from_persona": (DraftCreateFromPersonaInput, True),
    "communications.mail.v1#draft.reply_all": (DraftReplyAllInput, False),
    "communications.mail.v1#draft.inspect": (DraftRefInput, False),
    "communications.mail.v1#draft.open": (DraftRefInput, False),
    "communications.mail.v1#draft.send": (DraftSendInput, True),
    "personal.context.v1#build": (ContextBuildInput, False),
    "personal.memory.v1#inspect": (MemoryInspectInput, False),
}

APP_CONTRIBUTION_DATA_CONTRACTS: dict[str, dict[str, object]] = {
    ref: {
        "operation": "read",
        "input": _contract_input_schema(model_cls),
        "result": f"{ref}.result",
    }
    for ref, model_cls in DATA_INPUT_MODELS.items()
}

APP_CONTRIBUTION_ACTION_CONTRACTS: dict[str, dict[str, object]] = {
    ref: {
        "operation": "execute",
        "confirmation_required": confirmation_required,
        "input": _contract_input_schema(model_cls),
        "result": f"{ref}.result",
    }
    for ref, (model_cls, confirmation_required) in ACTION_SPECS.items()
}

APP_CONTRIBUTION_DATA_ACTIONS = {
    "communications.mail.v1#recent": ["communications.mail.v1#sync.incremental", "communications.mail.v1#workbench.open"],
    "communications.mail.v1#draft.inspect": [
        "communications.mail.v1#workbench.open",
        "communications.mail.v1#draft.create",
        "communications.mail.v1#draft.reply_all",
        "communications.mail.v1#draft.create_from_persona",
        "communications.mail.v1#draft.inspect",
        "communications.mail.v1#draft.open",
        "communications.mail.v1#draft.send",
    ],
    "communications.mail.v1#triage.evidence": [],
    "personal.memory.v1#people": ["personal.context.v1#build", "personal.memory.v1#inspect"],
    "personal.memory.v1#search": ["personal.memory.v1#inspect"],
}

APP_CONTRIBUTION_COLLECTION_ACTIONS = {
    "communications.mail.v1#recent": [
        ("communications.mail.v1#workbench.open", {"en-US": "Open mail workbench", "zh-CN": "打开邮件工作台"}),
        ("communications.mail.v1#sync.incremental", {"en-US": "Sync mail", "zh-CN": "同步邮件"}),
    ],
    "communications.mail.v1#draft.inspect": [
        ("communications.mail.v1#workbench.open", {"en-US": "Open mail workbench", "zh-CN": "打开邮件工作台"}),
        ("communications.mail.v1#draft.create", {"en-US": "New draft", "zh-CN": "新建草稿"}),
        ("communications.mail.v1#draft.reply_all", {"en-US": "Reply all draft", "zh-CN": "回复全部草稿"}),
        ("communications.mail.v1#draft.create_from_persona", {"en-US": "Draft from proposal", "zh-CN": "从提案创建草稿"}),
    ],
    "communications.mail.v1#triage.evidence": [],
    "personal.memory.v1#people": [
        ("personal.context.v1#build", {"en-US": "Build context", "zh-CN": "构建上下文"}),
    ],
    "personal.memory.v1#search": [],
}

# Keep SCOPES referenced for parity assertions.
assert list(SCOPES) == APP_CONTRIBUTION_DATA_CONTRACTS["communications.mail.v1#recent"]["input"]["scope"]["enum"]


def _format_validation_error(exc: ValidationError) -> str:
    first = exc.errors(include_url=False)[0]
    loc = ".".join(str(part) for part in first.get("loc", ()))
    msg = str(first.get("msg", "invalid input"))
    return f"{loc}: {msg}" if loc else msg


def _app_contribution_form_schema(fields: dict[str, dict[str, object]]) -> dict[str, object]:
    schema = copy.deepcopy(fields)
    labels = {
        "all": ("All", "全部"),
        "active": ("Active", "当前"),
        "history": ("History", "历史"),
        "unreviewed": ("Unreviewed", "未审阅"),
        "open": ("Open", "待处理"),
        "waiting": ("Waiting", "等待"),
        "closed": ("Closed", "已结束"),
        "none": ("No open task", "无待办"),
        "draft": ("Draft", "草稿"),
        "sending": ("Sending", "正在发送"),
        "unknown": ("Unknown", "待核实"),
        "sent": ("Sent", "已发送"),
    }
    for field in schema.values():
        if field["type"] == "string[]":
            field["type"] = "string_list"
        if "enum" in field:
            field["options"] = [
                {"value": value, "label_i18n": {"en-US": labels[value][0], "zh-CN": labels[value][1]}}
                for value in field["enum"]
            ]
    return schema


def _app_contribution_pagination(offset: int, limit: int, total: int) -> dict[str, object]:
    return {"offset": offset, "limit": limit, "total": total, "has_more": offset + limit < total}


def _app_contribution_collection(
    ref: str,
    data: dict[str, object],
    *,
    state: str = "ready",
    reason: str = "",
    input_schema: dict[str, object] | None = None,
    **legacy: object,
) -> dict[str, object]:
    command_inputs = {}
    for action_ref in APP_CONTRIBUTION_DATA_ACTIONS[ref]:
        contract = APP_CONTRIBUTION_ACTION_CONTRACTS[action_ref]
        command_inputs[action_ref] = {
            "input_schema": _app_contribution_form_schema(contract["input"]),
            "defaults": {},
            "confirmation_required": contract["confirmation_required"],
        }
    return {
        **legacy,
        "kind": "data",
        "state": state,
        "input_schema": input_schema if input_schema is not None else _app_contribution_form_schema(
            APP_CONTRIBUTION_DATA_CONTRACTS[ref]["input"]
        ),
        "data": {
            "collection_actions": [
                {"action_ref": action_ref, "input": {}, "label_i18n": label}
                for action_ref, label in APP_CONTRIBUTION_COLLECTION_ACTIONS[ref]
            ],
            **data,
            "command_inputs": command_inputs,
        },
        **({"reason": reason} if reason else {}),
    }


def _app_contribution_draft_item(draft: dict[str, object], *, inspection_required: bool) -> dict[str, object]:
    draft_ref = draft["draft_ref"]
    actions = [{"action_ref": "communications.mail.v1#draft.inspect", "input": {"draft_ref": draft_ref}}]
    if draft.get("state") == "draft":
        actions.extend(
            {"action_ref": action_ref, "input": {"draft_ref": draft_ref}}
            for action_ref in ("communications.mail.v1#draft.open", "communications.mail.v1#draft.send")
        )
    return {
        **draft,
        "id": draft_ref,
        "title": draft.get("subject") or draft_ref,
        "inspection_required": inspection_required,
        "read_input": {"draft_ref": draft_ref},
        "review_target": {
            key: draft[key]
            for key in (
                "draft_ref", "account_id", "state", "sender", "to", "cc", "bcc", "subject", "attachments",
            )
            if key in draft
        },
        "actions": actions,
    }


def _app_contribution_drafts(
    args: Any, *, query: str, status: str, offset: int, limit: int,
) -> dict[str, object]:
    path = Path(args.draft_db).expanduser()
    empty = {"items": [], "count": 0, "pagination": _app_contribution_pagination(offset, limit, 0), "has_more": False}
    if not path.exists():
        return {**empty, "store_state": "missing"}
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        where, params = [], []
        if status == "active":
            where.append("state IN ('draft', 'sending', 'unknown')")
        elif status != "all":
            where.append("state=?")
            params.append(status)
        if query:
            where.append(
                "(instr(lower(draft_ref),?) OR instr(lower(account_id),?) "
                "OR instr(lower(provider_account),?) OR instr(lower(provider_uuid),?))"
            )
            params.extend([query.lower()] * 4)
        predicate = " AND ".join(where) or "1=1"
        total = conn.execute(f"SELECT count(*) FROM mail_drafts WHERE {predicate}", params).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT draft_ref, account_id, provider_account, provider_uuid, state, created_at, updated_at
            FROM mail_drafts WHERE {predicate}
            ORDER BY updated_at DESC, draft_ref LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
        items = [_app_contribution_draft_item(dict(row), inspection_required=True) for row in rows]
        pagination = _app_contribution_pagination(offset, limit, total)
        return {
            "items": items,
            "count": len(items),
            "store_state": "present",
            "pagination": pagination,
            "has_more": pagination["has_more"],
        }
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return {**empty, "store_state": "schema_missing"}
    finally:
        conn.close()


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


def _app_contribution_recent(args: Any, validated: RecentMailInput) -> dict[str, object]:
    offset = validated.offset
    limit = validated.limit
    conn = connect_email_store_readonly(Path(args.db).expanduser())
    messages, items, total = [], [], 0
    try:
        if conn is not None:
            conn.execute("BEGIN")
            where, params = scope_filter(validated.scope, validated.folder)
            if validated.account is not None:
                where.append("email_messages.account_id=?")
                params.append(validated.account)
            for field_val, operator in ((validated.since, ">="), (validated.until, "<")):
                if field_val is not None:
                    where.append(f"datetime(date_iso){operator}datetime(?)")
                    params.append(field_val)
            if validated.query:
                if conn.execute(
                    f"SELECT 1 FROM email_messages WHERE {' AND '.join(where)} "
                    "AND raw_sha256 NOT IN (SELECT raw_sha256 FROM email_search) LIMIT 1",
                    params,
                ).fetchone():
                    raise ValueError("mail body index incomplete; run opl-relay index before searching")
                term = validated.query.casefold()
                where.append(
                    "(instr(lower(subject),?) OR instr(lower(sender),?) OR instr(lower(recipient),?) "
                    "OR instr(lower(message_id),?) OR email_messages.raw_sha256 IN "
                    "(SELECT raw_sha256 FROM email_search WHERE instr(body_text,?)))"
                )
                params.extend([term] * 5)
            if validated.status != "all":
                where.append("coalesce(r.status,'unreviewed')=?")
                params.append(validated.status)
            source = (
                "FROM email_messages LEFT JOIN mail_reviews r ON r.account_id=email_messages.account_id "
                "AND r.raw_sha256=email_messages.raw_sha256 " + f"WHERE {' AND '.join(where)}"
            )
            total = conn.execute("SELECT count(*) " + source, params).fetchone()[0]
            rows = conn.execute(
                "SELECT email_messages.storage_ref, coalesce(r.status,'unreviewed'), r.action, r.note "
                + source
                + " ORDER BY date_iso DESC, ingest_ts DESC, uid DESC, email_messages.storage_ref LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
            for storage_ref, review_state, action, note in rows:
                message = get_message_by_storage_ref(conn, storage_ref)
                raw = fetch_raw_email_by_storage_ref(conn, storage_ref)
                body = extract_text_body(raw) if raw is not None else ""
                messages.append(message)
                items.append({
                    **message,
                    "id": storage_ref,
                    "title": message["subject"],
                    "body_text": body[:20000],
                    "body_truncated": len(body) > 20000,
                    "status": review_state,
                    "review_target": {
                        "source_ref": storage_ref,
                        "status": review_state,
                        "action": action,
                        "note": note or "",
                    },
                    "actions": [{
                        "action_ref": "communications.mail.v1#sync.incremental",
                        "input": {"account": message["account_id"]},
                    }],
                })
        pagination = _app_contribution_pagination(offset, limit, total)
        return _app_contribution_collection(
            "communications.mail.v1#recent",
            {
                "items": items,
                "count": len(items),
                "pagination": pagination,
                "has_more": pagination["has_more"],
                "store_state": "present" if conn is not None else "missing",
                "collection_actions": [
                    {
                        "action_ref": action_ref,
                        "input": {"account": validated.account} if validated.account is not None else {},
                        "label_i18n": label,
                    }
                    for action_ref, label in APP_CONTRIBUTION_COLLECTION_ACTIONS["communications.mail.v1#recent"]
                ],
                "source_policy": {"owner": "EmailStore", "read_only": True, "local_snapshot_only": True},
            },
            messages=messages,
        )
    finally:
        if conn is not None:
            conn.close()


def _app_contribution_triage_schema(args: Any) -> dict[str, object]:
    schema = _app_contribution_form_schema(
        APP_CONTRIBUTION_DATA_CONTRACTS["communications.mail.v1#triage.evidence"]["input"]
    )
    schema["source_ref"].update(required=True, label_i18n={"en-US": "Source mail", "zh-CN": "来源邮件"}, options=[])
    schema["policy_refs"].update(required=True, label_i18n={"en-US": "Policy references", "zh-CN": "策略引用"}, options=[])
    conn = connect_email_store_readonly(Path(args.db).expanduser())
    try:
        if conn is not None:
            schema["source_ref"]["options"] = [
                {
                    "value": message["storage_ref"],
                    "label_i18n": {
                        "en-US": message["subject"] or message["storage_ref"],
                        "zh-CN": message["subject"] or message["storage_ref"],
                    },
                }
                for message in list_messages(conn, limit=20)
            ]
    finally:
        if conn is not None:
            conn.close()
    policies = (default_profile_workspace() / "policies").resolve()
    if policies.is_dir():
        schema["policy_refs"]["options"] = [
            {
                "value": path.as_uri(),
                "label_i18n": {
                    "en-US": path.relative_to(policies).as_posix(),
                    "zh-CN": path.relative_to(policies).as_posix(),
                },
            }
            for path in sorted(policies.rglob("*.md"))[:100]
            if path.is_file()
        ]
    return schema


def _dispatch_data(args: Any, ref: str, validated: _StrictInput) -> object:
    from . import cli

    if ref == "communications.mail.v1#recent":
        assert isinstance(validated, RecentMailInput)
        return _app_contribution_recent(args, validated)
    if ref == "communications.mail.v1#draft.inspect":
        assert isinstance(validated, DraftInspectReadInput)
        if not validated.draft_ref:
            data = _app_contribution_drafts(
                args,
                limit=validated.limit,
                offset=validated.offset,
                status=validated.status,
                query=validated.query or "",
            )
            data["source_policy"] = {
                "owner": "DraftLedger",
                "read_only": True,
                "metadata_only": True,
                "fresh_inspection_required_before_approval": True,
            }
            return _app_contribution_collection(ref, data, drafts=data["items"])
        service, _ = cli.draft_service(args)
        draft = service.inspect(validated.draft_ref)
        if draft.get("draft_ref") != validated.draft_ref:
            raise ValueError("draft inspection identity mismatch")
        item = _app_contribution_draft_item(draft, inspection_required=False)
        return _app_contribution_collection(
            ref,
            {"items": [item], "count": 1, "pagination": _app_contribution_pagination(0, 1, 1)},
            draft=draft,
        )
    if ref == "communications.mail.v1#triage.evidence":
        assert isinstance(validated, TriageEvidenceReadInput)
        schema = _app_contribution_triage_schema(args)
        if not validated.source_ref or not validated.policy_refs:
            return _app_contribution_collection(
                ref,
                {"items": [], "count": 0},
                state="input_required",
                reason="source_ref and policy_refs are required for mail triage evidence",
                input_schema=schema,
            )
        evidence = build_triage_evidence(
            mail_db_path=Path(args.db).expanduser(),
            source_ref=validated.source_ref,
            policy_refs=validated.policy_refs,
        )
        return _app_contribution_collection(
            ref,
            {
                "items": [{
                    "id": evidence["mail"]["source_ref"],
                    **evidence,
                    "title": evidence["mail"]["headers"]["subject"],
                    "body_text": evidence["mail"]["raw_readback"]["body_text"],
                    "review_target": {
                        "source_ref": evidence["mail"]["source_ref"],
                        "headers": evidence["mail"]["headers"],
                        "policy_refs": evidence["policy"]["policy_refs"],
                    },
                    "actions": [],
                }],
                "count": 1,
            },
            evidence=evidence,
            input_schema=schema,
        )
    if ref == "personal.memory.v1#people":
        assert isinstance(validated, PeopleReadInput)
        return _app_contribution_collection(
            ref,
            read_people(
                cli.memory_store(args),
                evidence=cli.memory_evidence(args),
                query=validated.query or "",
                limit=validated.limit if validated.limit is not None else 50,
                include_stale=validated.include_stale,
            ),
        )
    if ref == "personal.memory.v1#search":
        assert isinstance(validated, MemorySearchReadInput)
        return _app_contribution_collection(
            ref,
            read_memory_evidence(
                cli.memory_store(args),
                evidence=cli.memory_evidence(args),
                entity=validated.entity or "",
                query=validated.query or "",
                limit=validated.limit if validated.limit is not None else 50,
                include_stale=validated.include_stale,
            ),
        )
    raise ValueError("unsupported data ref")


def _dispatch_action(args: Any, ref: str, validated: _StrictInput) -> object:
    from . import cli

    if ref == "communications.mail.v1#workbench.open":
        assert isinstance(validated, WorkbenchOpenInput)
        from .workbench import open_workbench

        focus = {
            ("account_id" if name == "account" else name): value
            for name in ("account", "storage_ref", "draft_id", "request_id")
            if (value := getattr(validated, name))
        }
        return {"ok": True, "presentation": {"mode": "browser", **open_workbench(focus=focus)}}
    if ref == "communications.mail.v1#sync.incremental":
        assert isinstance(validated, SyncIncrementalInput)
        if validated.limit_per_folder is not None and validated.limit_per_folder > 100000:
            raise ValueError("input.limit_per_folder must be an integer from 1 to 100000")
        return cli.sync_account(
            config_path=Path(args.config).expanduser(),
            db_path=Path(args.db).expanduser(),
            account_id=validated.account,
            mode="incremental",
            limit_per_folder=validated.limit_per_folder,
            dry_run=bool(validated.dry_run) if validated.dry_run is not None else False,
        )
    if ref == "communications.mail.v1#draft.create":
        assert isinstance(validated, DraftCreateInput)
        account = cli.load_account(Path(args.config).expanduser(), validated.account)
        service, _ = cli.draft_service(args)
        return {
            "draft": service.create(
                account_id=account.account_id,
                sender=account.email,
                to=cli.parse_recipients(validated.to, required=True),
                cc=cli.parse_recipients(validated.cc or []),
                bcc=cli.parse_recipients(validated.bcc or []),
                subject=validated.subject,
                body_text=validated.body,
                attachments=[],
                visible=True if validated.open is None else validated.open,
            )
        }
    if ref == "communications.mail.v1#draft.create_from_persona":
        assert isinstance(validated, DraftCreateFromPersonaInput)
        return cli.create_persona_review_draft(
            args,
            context=validate_approved_persona_draft_context(validated.proposal_bundle),
            account_id=validated.account,
            to=validated.to,
            cc=validated.cc or [],
            bcc=validated.bcc or [],
            visible=True if validated.open is None else validated.open,
        )
    if ref == "communications.mail.v1#draft.reply_all":
        assert isinstance(validated, DraftReplyAllInput)
        if validated.apple_mail_id > sys.maxsize:
            raise ValueError(f"input.apple_mail_id must be an integer from 1 to {sys.maxsize}")
        account = cli.load_account(Path(args.config).expanduser(), validated.account)
        service, _ = cli.draft_service(args)
        return {
            "draft": service.reply_all(
                account_id=account.account_id,
                sender=account.email,
                provider_account=validated.apple_mail_account,
                source_message_id=validated.apple_mail_id,
                mailbox_path=validated.mailbox_path,
                body_text=validated.body,
                attachments=[],
                visible=True if validated.open is None else validated.open,
            )
        }
    if ref == "communications.mail.v1#draft.inspect":
        assert isinstance(validated, DraftRefInput)
        service, _ = cli.draft_service(args)
        return {"draft": service.inspect(validated.draft_ref)}
    if ref == "communications.mail.v1#draft.open":
        assert isinstance(validated, DraftRefInput)
        service, _ = cli.draft_service(args)
        return {"draft": service.open(validated.draft_ref)}
    if ref == "communications.mail.v1#draft.send":
        assert isinstance(validated, DraftSendInput)
        service, _ = cli.draft_service(args)
        return {"draft": service.send(validated.draft_ref, approval=validated.approval)}
    if ref == "personal.context.v1#build":
        assert isinstance(validated, ContextBuildInput)
        return ContextBuilder(
            mail_db_path=Path(args.db).expanduser(),
            memory_db_path=Path(args.memory_db).expanduser(),
            framework=cli.framework_knowledge(args),
        ).build(
            person=validated.person or "",
            project=validated.project or "",
            query=validated.query or "",
            mail_limit=validated.mail_limit if validated.mail_limit is not None else 6,
            memory_limit=validated.memory_limit if validated.memory_limit is not None else 20,
            knowledge_limit=validated.knowledge_limit if validated.knowledge_limit is not None else 6,
        )
    if ref == "personal.memory.v1#inspect":
        assert isinstance(validated, MemoryInspectInput)
        return {
            "memory": cli.memory_evidence(args).inspect(
                cli.memory_store(args).get_memory(validated.memory_ref)
            )
        }
    raise ValueError("unsupported action ref")


def cmd_app_contribution(args: Any) -> int:
    from . import cli

    raw_request: Any = None
    try:
        raw_request = json.load(sys.stdin)
        if not isinstance(raw_request, dict):
            raise ValueError("request must be an object")
        try:
            envelope = _AppContributionEnvelope.model_validate(raw_request)
        except ValidationError as exc:
            raise ValueError(_format_validation_error(exc)) from None
        ref = envelope.ref
        operation = envelope.operation
        data_contract = APP_CONTRIBUTION_DATA_CONTRACTS.get(ref)
        action_contract = APP_CONTRIBUTION_ACTION_CONTRACTS.get(ref)
        if data_contract is None and action_contract is None:
            raise ValueError("ref is not declared by this package")
        if operation == "describe":
            if "input" in raw_request:
                raise ValueError("describe does not accept input")
            cli.emit(
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
        if operation == "read":
            model_cls = DATA_INPUT_MODELS.get(ref)
            if model_cls is None:
                raise ValueError(f"{ref} does not support {operation}")
        else:
            spec = ACTION_SPECS.get(ref)
            if spec is None:
                raise ValueError(f"{ref} does not support {operation}")
            model_cls = spec[0]
        raw_input = {} if envelope.input is None else envelope.input
        if not isinstance(raw_input, dict):
            raise ValueError("input must be an object")
        try:
            validated = model_cls.model_validate(raw_input)
        except ValidationError as exc:
            raise ValueError(_format_validation_error(exc)) from None
        result = (
            _dispatch_data(args, ref, validated)
            if operation == "read"
            else _dispatch_action(args, ref, validated)
        )
        cli.emit(_app_contribution_response(ref, operation, result), as_json=True)
        return 0
    except Exception as exc:
        ref = raw_request["ref"] if isinstance(raw_request, dict) and isinstance(raw_request.get("ref"), str) else None
        cli.emit(_app_contribution_error(ref, "invalid_request", str(exc)), as_json=True)
        return 2
