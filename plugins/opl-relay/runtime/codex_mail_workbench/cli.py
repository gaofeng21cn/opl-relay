from __future__ import annotations

import getpass
import json
import os
import shutil
import sys
from email.utils import getaddresses
from enum import StrEnum
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any, Literal

import typer

from . import __version__
from .app_contributions import (
    APP_CONTRIBUTION_ABI,
    APP_CONTRIBUTION_ACTION_CONTRACTS,
    APP_CONTRIBUTION_COLLECTION_ACTIONS,
    APP_CONTRIBUTION_DATA_ACTIONS,
    APP_CONTRIBUTION_DATA_CONTRACTS,
    APP_CONTRIBUTION_REQUEST_SCHEMA,
    APP_CONTRIBUTION_RESPONSE_SCHEMA,
    cmd_app_contribution,
)
from .apple_mail import AppleMailProvider
from .config import (
    KEYCHAIN_SERVICE,
    add_account,
    keychain_has_secret,
    keychain_set_secret,
    load_account,
    load_accounts_config,
)
from .context import ContextBuilder
from .drafts import DraftLedger, DraftService, Recipient
from .knowledge import FrameworkKnowledge, KnowledgeIndex
from .mailbox import MailboxMoveFailure, move_messages
from .memory import (
    MemoryStore,
)
from .memory_evidence import MemoryEvidence
from .message import extract_text_body
from .paths import (
    default_config_path,
    default_db_path,
    default_drafts_db_path,
    default_memory_db_path,
    default_profile_workspace,
    default_state_dir,
    profile_workspace_source,
    state_dir_source,
)
from .persona import (
    load_approved_persona_draft_context,
    load_persona_mail_context,
)
from .store import (
    connect_email_store,
    connect_email_store_readonly,
    fetch_raw_email_by_storage_ref,
    folder_status,
    get_message_by_storage_ref,
    index_messages,
    inspect_mail_storage,
    list_messages,
    prune_mail_storage,
    record_reviews,
    review_pending,
    review_status,
    search_messages,
)
from .sync import sync_account
from .triage import build_triage_evidence, validate_triage_evidence
from .workspace import (
    initialize_workspace,
    inspect_workspace,
    migrate_workspace,
    setup_status,
)

__all__ = [
    "APP_CONTRIBUTION_ABI",
    "APP_CONTRIBUTION_ACTION_CONTRACTS",
    "APP_CONTRIBUTION_COLLECTION_ACTIONS",
    "APP_CONTRIBUTION_DATA_ACTIONS",
    "APP_CONTRIBUTION_DATA_CONTRACTS",
    "APP_CONTRIBUTION_REQUEST_SCHEMA",
    "APP_CONTRIBUTION_RESPONSE_SCHEMA",
    "app",
    "build_parser",
    "main",
]

ScopeLiteral = Literal["active", "history", "all"]
MemoryCategoryLiteral = Literal[
    "commitment", "event", "fact", "inference", "note", "preference", "relationship", "style"
]
class MemoryStatusChoice(StrEnum):
    approved = "approved"
    candidate = "candidate"
    forgotten = "forgotten"
    rejected = "rejected"
    superseded = "superseded"


SensitivityLiteral = Literal["private", "restricted", "sensitive"]
ContentKindLiteral = Literal["", "policy", "state", "decision", "mechanism"]


# ---------------------------------------------------------------------------
# Shared output, state, and domain helpers
# ---------------------------------------------------------------------------


def emit(payload: object, *, as_json: bool) -> None:
    if as_json or isinstance(payload, dict):
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


def _state(ctx: typer.Context) -> SimpleNamespace:
    if isinstance(ctx.obj, SimpleNamespace):
        return ctx.obj
    return SimpleNamespace(
        json=False,
        config=str(default_config_path()),
        db=str(default_db_path()),
        draft_db=str(default_drafts_db_path()),
        memory_db=str(default_memory_db_path()),
        knowledge_project="",
    )


def _require_exclusive(
    first_val: object,
    first_flag: str,
    second_val: object,
    second_flag: str,
    *,
    required: bool,
) -> None:
    has_first = first_val is not None
    has_second = second_val is not None
    if has_first and has_second:
        raise typer.BadParameter(f"{first_flag} and {second_flag} are mutually exclusive")
    if required and not has_first and not has_second:
        raise typer.BadParameter(f"one of {first_flag} or {second_flag} is required")


def memory_store(args: Any) -> MemoryStore:
    return MemoryStore(Path(args.memory_db).expanduser())


def framework_knowledge(args: Any) -> FrameworkKnowledge:
    project = getattr(args, "knowledge_project", "")
    return FrameworkKnowledge(project_root=Path(project) if project else None)


def memory_evidence(args: Any) -> MemoryEvidence:
    return MemoryEvidence(
        mail_db_path=Path(args.db).expanduser(),
        framework=framework_knowledge(args),
    )


def _source_kind(source_ref: str) -> str:
    for prefix, kind in (
        ("email-store://", "email"),
        ("knowledge://", "knowledge"),
        ("obsidian://", "obsidian"),
        ("markdown://", "markdown"),
        ("user://", "user"),
        ("calendar://", "calendar"),
        ("contact://", "contact"),
        ("model://", "model"),
    ):
        if source_ref.startswith(prefix):
            return kind
    return "other"


def _memory_sources(args: Any, sources: list[str], excerpts: list[str]) -> list[dict[str, str]]:
    if excerpts and len(excerpts) != len(sources):
        raise ValueError("--source-excerpt must be omitted or supplied once per --source")
    evidence = memory_evidence(args)
    return [
        {
            "source_ref": source_ref,
            "source_kind": _source_kind(source_ref),
            "excerpt": excerpts[index] if excerpts else "",
            "source_sha256": (
                evidence.source_digest(source_ref, _source_kind(source_ref))
                if _source_kind(source_ref) in {"email", "knowledge", "obsidian", "markdown"}
                else ""
            ),
        }
        for index, source_ref in enumerate(sources)
    ]


def _private_json_output(path: str, payload: object) -> None:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    # Export never overwrites source notes or an existing review artifact.
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def _triage_evidence_from_cli_output(value: object) -> object:
    """Accept the exact successful wrapper emitted by ``triage evidence``."""
    if not isinstance(value, dict) or set(value) != {"ok", "evidence"}:
        return value
    if value["ok"] is not True:
        raise ValueError("triage evidence command result must be successful")
    if not isinstance(value["evidence"], dict):
        raise ValueError("triage evidence command result must contain an evidence object")
    return value["evidence"]


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


def read_body_text(body: str | None, body_file: str | None) -> str:
    if body_file == "-":
        return sys.stdin.read()
    if body_file:
        return Path(body_file).expanduser().read_text(encoding="utf-8-sig")
    return str(body or "")


def draft_service(args: Any) -> tuple[DraftService, AppleMailProvider]:
    provider = AppleMailProvider()
    ledger = DraftLedger(Path(args.draft_db).expanduser())
    return DraftService(ledger, provider), provider


def create_persona_review_draft(
    args: Any,
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


def _memory_transition(args: Any, memory_ref: str, action: str) -> int:
    store = memory_store(args)
    if action == "approve":
        candidate = store.get_memory(memory_ref)
        if candidate is None:
            raise ValueError("memory not found")
        currentness = memory_evidence(args).check(candidate, approving=True)
        if currentness["status"] != "current":
            raise ValueError("memory evidence requires review: " + ", ".join(currentness["reasons"]))
    memory = getattr(store, action)(memory_ref)
    emit({"ok": True, "memory": memory}, as_json=args.json)
    return 0


def _apple_mail_provider() -> AppleMailProvider:
    return AppleMailProvider()


# ---------------------------------------------------------------------------
# Typer application tree
# ---------------------------------------------------------------------------


app = typer.Typer(
    name="opl-relay",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    add_completion=False,
)
workbench_app = typer.Typer(help="打开共享邮件工作台或启动 MCP App", no_args_is_help=True)
review_app = typer.Typer(help="本地逐封审阅进度，不更改服务器邮件", no_args_is_help=True)
mailbox_app = typer.Typer(help="逐封受控 IMAP 邮箱移动", no_args_is_help=True)
memory_app = typer.Typer(help="私有人物、项目与关系记忆", no_args_is_help=True)
memory_entity_app = typer.Typer(help="管理记忆主体", no_args_is_help=True)
sources_app = typer.Typer(help="读取 Framework 授权知识", no_args_is_help=True)
context_app = typer.Typer(help="组装起草所需的最小证据上下文", no_args_is_help=True)
persona_app = typer.Typer(help="读取跨域 Persona 提案", no_args_is_help=True)
triage_app = typer.Typer(help="生成或验证只读邮件 triage 证据", no_args_is_help=True)
workspace_app = typer.Typer(help="检查、初始化或迁移 Relay workspace", no_args_is_help=True)
setup_app = typer.Typer(help="首次配置 Profile Workspace 和邮箱", no_args_is_help=True)
account_app = typer.Typer(help="配置和检查一个邮箱账号", no_args_is_help=True)
credential_app = typer.Typer(help="将邮箱密码安全保存到 macOS Keychain", no_args_is_help=True)
draft_app = typer.Typer(help="服务器审核草稿、Apple Mail 草稿与受控发送", no_args_is_help=True)
apple_mail_app = typer.Typer(help="只读 Apple Mail 驱动接口（不提供删除或标记）", no_args_is_help=True)

memory_app.add_typer(memory_entity_app, name="entity")
app.add_typer(workbench_app, name="workbench")
app.add_typer(review_app, name="review")
app.add_typer(mailbox_app, name="mailbox")
app.add_typer(memory_app, name="memory")
app.add_typer(sources_app, name="sources")
app.add_typer(context_app, name="context")
app.add_typer(persona_app, name="persona")
app.add_typer(triage_app, name="triage")
app.add_typer(workspace_app, name="workspace")
app.add_typer(setup_app, name="setup")
app.add_typer(account_app, name="account")
app.add_typer(credential_app, name="credential")
app.add_typer(draft_app, name="draft")
app.add_typer(apple_mail_app, name="apple-mail")


@app.callback()
def _root_options(
    ctx: typer.Context,
    as_json: Annotated[bool, typer.Option("--json", help="输出稳定 JSON")] = False,
    config: Annotated[str | None, typer.Option("--config", help="accounts.toml 路径")] = None,
    db: Annotated[str | None, typer.Option("--db", help="SQLite 邮件库路径")] = None,
    draft_db: Annotated[str | None, typer.Option("--draft-db", help="本地草稿审批 ledger 路径")] = None,
    memory_db: Annotated[str | None, typer.Option("--memory-db", help="私有记忆与知识索引 SQLite 路径")] = None,
    knowledge_project: Annotated[
        str,
        typer.Option("--knowledge-project", help="Framework 知识授权的项目根目录；不是 context 的项目检索词"),
    ] = "",
) -> None:
    ctx.obj = SimpleNamespace(
        json=as_json,
        config=config or str(default_config_path()),
        db=db or str(default_db_path()),
        draft_db=draft_db or str(default_drafts_db_path()),
        memory_db=memory_db or str(default_memory_db_path()),
        knowledge_project=knowledge_project,
    )


# ---------------------------------------------------------------------------
# Top-level commands
# ---------------------------------------------------------------------------


@app.command("connect", help="通过 stdin JSON 暴露 Relay 邮箱连接 provider")
def cmd_connect(ctx: typer.Context) -> int:
    from .connect import create_provider

    args = _state(ctx)
    try:
        try:
            request = json.load(sys.stdin)
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {
                "ok": False,
                "disposition": "not_dispatched",
                "error": {"code": "INVALID_REQUEST", "message": "连接请求无效"},
            }
        else:
            provider = create_provider(
                config_path=Path(args.config).expanduser(),
                db_path=Path(args.db).expanduser(),
                ledger_path=Path(args.draft_db).expanduser(),
            )
            payload = provider.dispatch(request)
    except Exception:
        payload = {
            "ok": False,
            "disposition": "not_dispatched",
            "error": {"code": "PROVIDER_UNAVAILABLE", "message": "Relay 邮箱连接暂不可用"},
        }
    print(json.dumps(payload, ensure_ascii=False))
    return 0


@app.command("doctor", help="检查配置、数据库和安装状态")
def cmd_doctor(ctx: typer.Context) -> int:
    args = _state(ctx)
    config_path = Path(args.config).expanduser()
    db_path = Path(args.db).expanduser()
    drafts_db_path = Path(args.draft_db).expanduser()
    memory_db_path = Path(args.memory_db).expanduser()
    workspace = inspect_workspace(default_profile_workspace())
    setup = setup_status(default_profile_workspace())
    accounts = {}
    config_error = ""
    if config_path.exists():
        try:
            accounts = load_accounts_config(config_path)
        except Exception as exc:
            config_error = str(exc)
    payload = {
        "ok": not config_error,
        "product": "opl-relay",
        "version": __version__,
        "command": shutil.which("opl-relay"),
        "commands": {"opl-relay": shutil.which("opl-relay")},
        "state_dir": str(default_state_dir()),
        "state_dir_source": state_dir_source(),
        "profile_workspace": str(default_profile_workspace()),
        "profile_workspace_source": profile_workspace_source(),
        "workspace": workspace,
        "setup": setup,
        "readiness": setup["readiness"],
        "config_path": str(config_path),
        "config_exists": config_path.exists(),
        "config_error": config_error,
        "db_path": str(db_path),
        "db_exists": db_path.exists(),
        "draft_db_path": str(drafts_db_path),
        "draft_db_exists": drafts_db_path.exists(),
        "memory_db_path": str(memory_db_path),
        "memory_db_exists": memory_db_path.exists(),
        "knowledge_authority": "opl-framework",
        "knowledge_project": args.knowledge_project,
        "apple_mail_available": sys.platform == "darwin" and bool(shutil.which("osascript")),
        "accounts": sorted(accounts.keys()),
        "keychain_services": [KEYCHAIN_SERVICE],
    }
    emit(payload, as_json=args.json)
    return 0 if payload["ok"] else 1


@app.command("app-contribution", help="执行 Package-owned app contribution JSON ABI")
def cmd_app_contribution_entry(ctx: typer.Context) -> int:
    return cmd_app_contribution(_state(ctx))


@app.command("accounts", help="列出配置账号")
def cmd_accounts(ctx: typer.Context) -> int:
    args = _state(ctx)
    accounts = load_accounts_config(Path(args.config).expanduser())
    emit(
        {
            "ok": True,
            "accounts": [
                {
                    "account_id": account.account_id,
                    "email": account.email,
                    "provider": account.provider,
                    "backend": account.backend,
                    "imap_host": account.imap.host if account.imap else None,
                    "include_folders": account.include_folders,
                    "exclude_folders": account.exclude_folders,
                }
                for account in accounts.values()
            ],
        },
        as_json=args.json,
    )
    return 0


@app.command("recent", help="列出最近邮件")
def cmd_recent(
    ctx: typer.Context,
    account: Annotated[str, typer.Option("--account")] = "",
    folder: Annotated[str, typer.Option("--folder")] = "",
    since: Annotated[str, typer.Option("--since", help="include messages at or after this ISO datetime")] = "",
    until: Annotated[str, typer.Option("--until", help="include messages before this ISO datetime")] = "",
    limit: Annotated[int, typer.Option("--limit")] = 20,
    scope: Annotated[ScopeLiteral, typer.Option("--scope")] = "active",
) -> int:
    args = _state(ctx)
    conn = connect_email_store(Path(args.db).expanduser())
    try:
        rows = list_messages(
            conn,
            account_ids=[account] if account else None,
            folder_slug=folder,
            since=since,
            until=until,
            limit=limit,
            scope=scope,
        )
    finally:
        conn.close()
    emit({"ok": True, "messages": rows}, as_json=args.json)
    return 0


@app.command("search", help="搜索本地邮件元数据")
def cmd_search(
    ctx: typer.Context,
    query: Annotated[str, typer.Argument()],
    account: Annotated[str, typer.Option("--account")] = "",
    folder: Annotated[str, typer.Option("--folder")] = "",
    since: Annotated[str, typer.Option("--since", help="include messages at or after this ISO datetime")] = "",
    until: Annotated[str, typer.Option("--until", help="include messages before this ISO datetime")] = "",
    limit: Annotated[int, typer.Option("--limit")] = 20,
    scope: Annotated[ScopeLiteral, typer.Option("--scope")] = "active",
    remote: Annotated[
        bool,
        typer.Option("--remote", help="按需在服务器检索，只缓存命中的原文，不同步整个归档"),
    ] = False,
    include_body: Annotated[bool, typer.Option("--include-body/--no-include-body")] = True,
    max_scan: Annotated[
        int,
        typer.Option("--max-scan", help="compatibility option; indexed search always covers the full selected scope"),
    ] = 500,
) -> int:
    args = _state(ctx)
    if remote:
        if not account:
            raise ValueError("Remote search requires an explicit --account")
        from .remote_search import search_remote

        result = search_remote(
            account=load_account(Path(args.config).expanduser(), account),
            db_path=Path(args.db).expanduser(),
            query=query,
            scope=scope,
            folder=folder,
            limit=limit,
            since=since,
            until=until,
        )
        emit(result, as_json=args.json)
        return 0 if result["ok"] else 1
    conn = connect_email_store(Path(args.db).expanduser())
    try:
        rows = search_messages(
            conn,
            queries=[query],
            account_ids=[account] if account else None,
            folder_slug=folder,
            since=since,
            until=until,
            include_body=include_body,
            max_scan=max_scan,
            limit=limit,
            scope=scope,
        )
    finally:
        conn.close()
    emit(
        {
            "ok": True,
            "scope": scope,
            "messages": rows,
            "search_complete": True,
            "result_limit": limit,
        },
        as_json=args.json,
    )
    return 0


@app.command("read", help="读取一封邮件正文")
def cmd_read(
    ctx: typer.Context,
    storage_ref: Annotated[str, typer.Argument()],
    include_raw: Annotated[bool, typer.Option("--include-raw")] = False,
) -> int:
    args = _state(ctx)
    conn = connect_email_store(Path(args.db).expanduser())
    try:
        meta = get_message_by_storage_ref(conn, storage_ref)
        raw = fetch_raw_email_by_storage_ref(conn, storage_ref)
    finally:
        conn.close()
    if not meta or raw is None:
        return fail("message not found", as_json=args.json, code=2)
    meta["body_text"] = extract_text_body(raw)
    from .server_drafts import content_snapshot

    meta["headers"] = content_snapshot(raw)["headers"]
    if include_raw:
        meta["raw_eml"] = raw.decode("utf-8", errors="replace")
    emit({"ok": True, "message": meta}, as_json=args.json)
    return 0


@app.command("sync", help="从 IMAP 同步邮件到本地 SQLite")
def cmd_sync(
    ctx: typer.Context,
    account: Annotated[str, typer.Option("--account")],
    mode: Annotated[Literal["initial", "incremental"], typer.Option("--mode")] = "incremental",
    limit_per_folder: Annotated[int | None, typer.Option("--limit-per-folder")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    scope: Annotated[ScopeLiteral, typer.Option("--scope")] = "active",
    folder: Annotated[str, typer.Option("--folder", help="sync one configured folder")] = "",
    max_seconds: Annotated[
        float,
        typer.Option("--max-seconds", help="每轮 IMAP 同步的总预算（默认 120 秒，超时返回可续跑的部分结果）"),
    ] = 120.0,
) -> int:
    args = _state(ctx)
    payload = sync_account(
        config_path=Path(args.config).expanduser(),
        db_path=Path(args.db).expanduser(),
        account_id=account,
        mode=mode,
        limit_per_folder=limit_per_folder,
        dry_run=dry_run,
        scope=scope,
        folder=folder,
        max_duration_sec=max_seconds,
    )
    emit(payload, as_json=args.json)
    return 0 if payload.get("ok") else 1


@app.command("index", help="建立或补齐本地正文索引（按原文哈希去重）")
def cmd_index(ctx: typer.Context) -> int:
    args = _state(ctx)
    conn = connect_email_store(Path(args.db).expanduser())
    try:
        emit(index_messages(conn), as_json=args.json)
    finally:
        conn.close()
    return 0


@app.command("status", help="读取各文件夹最后一次 IMAP 对账与完整性")
def cmd_mail_status(
    ctx: typer.Context,
    account: Annotated[str, typer.Option("--account")] = "",
) -> int:
    args = _state(ctx)
    conn = connect_email_store(Path(args.db).expanduser())
    try:
        emit(
            {
                "ok": True,
                "folders": folder_status(conn, account),
                "counts_are": "last verified provider snapshot, not a live server query",
            },
            as_json=args.json,
        )
    finally:
        conn.close()
    return 0


@app.command("storage-stats", help="只读统计 mail.sqlite 体积、孤儿/已删正文占比与残留临时目录")
def cmd_storage_stats(ctx: typer.Context) -> int:
    args = _state(ctx)
    db_path = Path(args.db).expanduser()
    conn = connect_email_store_readonly(db_path)
    if conn is None:
        emit(
            {
                "ok": True,
                "db_path": str(db_path),
                "db_exists": False,
                "db_size_bytes": 0,
            },
            as_json=args.json,
        )
        return 0
    try:
        emit(
            inspect_mail_storage(conn, state_dir=default_state_dir(), db_path=db_path),
            as_json=args.json,
        )
    finally:
        conn.close()
    return 0


@app.command("storage-prune", help="安全清理孤儿邮件正文/临时目录（默认 dry-run，需显式 --execute）")
def cmd_storage_prune(
    ctx: typer.Context,
    execute: Annotated[bool, typer.Option("--execute", help="执行实际清理（默认仅 dry-run 预览）")] = False,
    include_deleted: Annotated[
        bool,
        typer.Option("--include-deleted", help="一并回收超过保留天数且已标记 deleted=1 的历史邮件正文"),
    ] = False,
    deleted_retention_days: Annotated[
        int,
        typer.Option("--deleted-retention-days", min=0, help="已删除邮件正文的保留天数（默认 30 天）"),
    ] = 30,
    vacuum: Annotated[bool, typer.Option("--vacuum", help="清理后执行 SQLite VACUUM 回收磁盘文件体积")] = False,
) -> int:
    args = _state(ctx)
    db_path = Path(args.db).expanduser()
    if not execute:
        conn = connect_email_store_readonly(db_path)
        if conn is None:
            emit({"ok": True, "dry_run": True, "db_exists": False}, as_json=args.json)
            return 0
    else:
        conn = connect_email_store(db_path)
    try:
        emit(
            prune_mail_storage(
                conn,
                state_dir=default_state_dir(),
                deleted_retention_days=deleted_retention_days,
                include_deleted=include_deleted,
                vacuum=vacuum,
                dry_run=not execute,
            ),
            as_json=args.json,
        )
    finally:
        conn.close()
    return 0


# ---------------------------------------------------------------------------
# workbench subcommands
# ---------------------------------------------------------------------------


@workbench_app.command("open")
def cmd_workbench_open(
    ctx: typer.Context,
    account: Annotated[str, typer.Option("--account")] = "",
    storage_ref: Annotated[str, typer.Option("--storage-ref")] = "",
    draft_id: Annotated[str, typer.Option("--draft-id")] = "",
    request_id: Annotated[str, typer.Option("--request-id")] = "",
    revision: Annotated[int | None, typer.Option("--revision")] = None,
) -> int:
    from .workbench import Workbench, open_workbench

    args = _state(ctx)
    focus = {
        key: value
        for key, value in {
            "account_id": account,
            "storage_ref": storage_ref,
            "draft_id": draft_id,
            "request_id": request_id,
            "revision": revision,
        }.items()
        if value is not None and value != ""
    }
    workbench = Workbench(config_path=args.config, db_path=args.db, ledger_path=args.draft_db)
    emit(open_workbench(focus=workbench.focus(**focus)["focus"]), as_json=True)
    return 0


@workbench_app.command("connection", help="生成当前宿主的连接配置或 DSH 原生配置包")
def cmd_workbench_connection(
    host: Annotated[Literal["generic", "codex", "dsh", "opl"], typer.Option("--host")] = "generic",
    output: Annotated[str, typer.Option("--output")] = "",
) -> int:
    from .workbench import connection_config

    emit(connection_config(host=host, output=output), as_json=True)
    return 0


@workbench_app.command("mcp")
def cmd_workbench_mcp() -> int:
    from .workbench import bundle_root

    os.execvp("node", ["node", str(bundle_root() / "server.mjs"), "--python", sys.executable])
    return 0


@workbench_app.command("action")
def cmd_workbench_action(ctx: typer.Context) -> int:
    from .workbench import Workbench

    args = _state(ctx)
    request = json.load(sys.stdin)
    if not isinstance(request, dict) or set(request) != {"action", "input"}:
        raise ValueError("workbench action expects action and input")
    workbench = Workbench(config_path=args.config, db_path=args.db, ledger_path=args.draft_db)
    try:
        emit(workbench.dispatch(request["action"], request["input"]), as_json=True)
        return 0
    except Exception as exc:
        emit(workbench.failure(request["action"], request["input"], exc), as_json=True)
        return 1


# ---------------------------------------------------------------------------
# review subcommands
# ---------------------------------------------------------------------------


@review_app.command("prepare")
def cmd_review_prepare(
    ctx: typer.Context,
    account: Annotated[str, typer.Option("--account")] = "",
    limit: Annotated[int, typer.Option("--limit")] = 50,
    sync_limit: Annotated[
        int,
        typer.Option("--sync-limit", help="每个文件夹每轮最多补抓的邮件数，优先新信"),
    ] = 200,
) -> int:
    args = _state(ctx)
    accounts = [account] if account else list(load_accounts_config(Path(args.config).expanduser()))
    prepared = []
    for account_id in accounts:
        try:
            sync_result = sync_account(
                config_path=Path(args.config).expanduser(),
                db_path=Path(args.db).expanduser(),
                account_id=account_id,
                scope="active",
                limit_per_folder=sync_limit,
            )
        except Exception as exc:
            sync_result = {"ok": False, "error": str(exc), "folders": []}
        conn = connect_email_store(Path(args.db).expanduser())
        try:
            queue = review_pending(conn, account=account_id, limit=limit)
            state = review_status(conn, account=account_id)
        finally:
            conn.close()
        validated = any(
            f.get("folder", "").lower() == "inbox"
            and "remote_count" in f
            and f.get("membership_verified", True)
            for f in sync_result.get("folders", [])
        )
        prepared.append({
            "account": account_id,
            "sync": sync_result,
            "available_for_review": validated,
            "pending": queue,
            "open_items": state["open_items"],
        })
    payload = {"ok": all(p["sync"].get("ok") for p in prepared), "accounts": prepared}
    emit(payload, as_json=args.json)
    return 0 if payload["ok"] else 1


@review_app.command("pending")
def cmd_review_pending(
    ctx: typer.Context,
    account: Annotated[str, typer.Option("--account")],
    limit: Annotated[int, typer.Option("--limit")] = 50,
) -> int:
    args = _state(ctx)
    conn = connect_email_store(Path(args.db).expanduser())
    try:
        emit(review_pending(conn, account=account, limit=limit), as_json=args.json)
    finally:
        conn.close()
    return 0


@review_app.command("status")
def cmd_review_status(
    ctx: typer.Context,
    account: Annotated[str, typer.Option("--account")],
) -> int:
    args = _state(ctx)
    conn = connect_email_store(Path(args.db).expanduser())
    try:
        emit(review_status(conn, account=account), as_json=args.json)
    finally:
        conn.close()
    return 0


@review_app.command("record")
def cmd_review_record(
    ctx: typer.Context,
    file: Annotated[str, typer.Option("--file", help="JSON records: storage_ref, action, status, note")],
) -> int:
    args = _state(ctx)
    conn = connect_email_store(Path(args.db).expanduser())
    try:
        records = json.loads(Path(file).expanduser().read_text(encoding="utf-8"))
        if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
            raise ValueError("review file must be a JSON array of records")
        emit(record_reviews(conn, records), as_json=args.json)
    finally:
        conn.close()
    return 0


# ---------------------------------------------------------------------------
# mailbox subcommands
# ---------------------------------------------------------------------------


@mailbox_app.command("move", help="只将经实时验证的精确邮件移动到已存在的 Archive、Trash、Bill 或精确指定的现有文件夹")
def cmd_mailbox_move(
    ctx: typer.Context,
    account: Annotated[str, typer.Option("--account")],
    storage_ref: Annotated[list[str], typer.Option("--storage-ref")],
    destination: Annotated[Literal["archive", "trash", "bill"] | None, typer.Option("--destination")] = None,
    destination_folder: Annotated[
        str,
        typer.Option("--destination-folder", help="精确指定一个已存在的现有文件夹（不创建新文件夹）"),
    ] = "",
    reconcile: Annotated[
        bool,
        typer.Option("--reconcile", help="只读核对 Graph 未知移动结果并保存回执，不重试远端操作"),
    ] = False,
    apply: Annotated[bool, typer.Option("--apply", help="执行移动；省略时仅做远端实时预检")] = False,
) -> int:
    args = _state(ctx)
    try:
        if reconcile:
            if apply:
                raise ValueError("--reconcile never accepts --apply")
            from .graph import reconcile_graph_moves

            acct = load_account(Path(args.config).expanduser(), account)
            if not acct.graph:
                raise ValueError("This reconciliation command currently requires a Graph account")
            payload = reconcile_graph_moves(
                account=acct,
                db_path=Path(args.db).expanduser(),
                destination=destination,
                exact_folder=destination_folder,
                storage_refs=storage_ref,
            )
            emit(payload, as_json=args.json)
            return 0 if payload["ok"] else 1
        payload = move_messages(
            config_path=Path(args.config).expanduser(),
            db_path=Path(args.db).expanduser(),
            account_id=account,
            destination=destination,
            exact_folder=destination_folder,
            storage_refs=storage_ref,
            apply=apply,
        )
    except (ValueError, LookupError, MailboxMoveFailure) as exc:
        return fail(str(exc), as_json=args.json, code=2)
    emit(payload, as_json=args.json)
    return 0 if payload.get("ok") is True else 1


# ---------------------------------------------------------------------------
# memory subcommands
# ---------------------------------------------------------------------------


@memory_entity_app.command("upsert", help="新增或更新人物、机构或项目")
def cmd_memory_entity_upsert(
    ctx: typer.Context,
    kind: Annotated[Literal["person", "organization", "project"], typer.Option("--kind")],
    name: Annotated[str, typer.Option("--name")],
    alias: Annotated[list[str] | None, typer.Option("--alias")] = None,
    email: Annotated[list[str] | None, typer.Option("--email")] = None,
) -> int:
    args = _state(ctx)
    entity = memory_store(args).upsert_entity(
        kind=kind,
        canonical_name=name,
        aliases=alias or [],
        emails=email or [],
    )
    emit({"ok": True, "entity": entity}, as_json=args.json)
    return 0


@memory_entity_app.command("list", help="列出或搜索记忆主体")
def cmd_memory_entity_list(
    ctx: typer.Context,
    query: Annotated[str, typer.Option("--query")] = "",
    limit: Annotated[int, typer.Option("--limit")] = 50,
) -> int:
    args = _state(ctx)
    entities = memory_store(args).list_entities(query=query, limit=limit)
    emit({"ok": True, "entities": entities}, as_json=args.json)
    return 0


@memory_app.command("propose", help="提出带证据的记忆候选")
def cmd_memory_propose(
    ctx: typer.Context,
    entity: Annotated[str, typer.Option("--entity")],
    category: Annotated[MemoryCategoryLiteral, typer.Option("--category")],
    content: Annotated[str, typer.Option("--content")],
    source: Annotated[list[str], typer.Option("--source")],
    source_excerpt: Annotated[list[str] | None, typer.Option("--source-excerpt")] = None,
    confidence: Annotated[float, typer.Option("--confidence")] = 1.0,
    sensitivity: Annotated[SensitivityLiteral, typer.Option("--sensitivity")] = "private",
    occurred_at: Annotated[str, typer.Option("--occurred-at")] = "",
    valid_from: Annotated[str, typer.Option("--valid-from")] = "",
    valid_until: Annotated[str, typer.Option("--valid-until")] = "",
    supersedes: Annotated[str, typer.Option("--supersedes")] = "",
    observed_at: Annotated[str, typer.Option("--observed-at")] = "",
    next_review_at: Annotated[str, typer.Option("--next-review-at")] = "",
    content_kind: Annotated[ContentKindLiteral, typer.Option("--content-kind")] = "",
) -> int:
    args = _state(ctx)
    store = memory_store(args)
    resolved = store.resolve_entity(entity)
    memory = store.propose_memory(
        entity_ref=resolved["entity_ref"],
        category=category,
        content=content,
        sources=_memory_sources(args, list(source), list(source_excerpt or [])),
        confidence=confidence,
        sensitivity=sensitivity,
        occurred_at=occurred_at,
        valid_from=valid_from,
        valid_until=valid_until,
        supersedes_ref=supersedes,
        observed_at=observed_at,
        next_review_at=next_review_at,
        content_kind=content_kind,
    )
    emit({"ok": True, "memory": memory}, as_json=args.json)
    return 0


@memory_app.command("candidates", help="列出待审核记忆")
def cmd_memory_candidates(
    ctx: typer.Context,
    entity: Annotated[str, typer.Option("--entity")] = "",
    query: Annotated[str, typer.Option("--query")] = "",
    limit: Annotated[int, typer.Option("--limit")] = 50,
) -> int:
    args = _state(ctx)
    memories = memory_store(args).list_memories(
        entity=entity,
        statuses=("candidate",),
        query=query,
        limit=limit,
    )
    emit({"ok": True, "memories": memories}, as_json=args.json)
    return 0


@memory_app.command("search", help="搜索结构化记忆")
def cmd_memory_search(
    ctx: typer.Context,
    query: Annotated[str, typer.Argument()] = "",
    entity: Annotated[str, typer.Option("--entity")] = "",
    status: Annotated[list[MemoryStatusChoice] | None, typer.Option("--status")] = None,
    limit: Annotated[int, typer.Option("--limit")] = 50,
    include_stale: Annotated[
        bool,
        typer.Option("--include-stale", help="查看保留的历史条目及来源复核状态，不作为当前上下文"),
    ] = False,
) -> int:
    args = _state(ctx)
    evidence = memory_evidence(args)
    memories = memory_store(args).list_memories(
        entity=entity,
        statuses=tuple(item.value for item in status) if status else ("approved",),
        query=query,
        limit=limit,
        is_current=None if include_stale else evidence.current,
    )
    if include_stale:
        memories = [evidence.inspect(memory) for memory in memories]
    emit({"ok": True, "memories": memories}, as_json=args.json)
    return 0


@memory_app.command("inspect", help="查看记忆及其证据")
def cmd_memory_inspect(ctx: typer.Context, memory_ref: Annotated[str, typer.Argument()]) -> int:
    args = _state(ctx)
    memory = memory_evidence(args).inspect(memory_store(args).get_memory(memory_ref))
    if memory is None:
        return fail("memory not found", as_json=args.json, code=2)
    emit({"ok": True, "memory": memory}, as_json=args.json)
    return 0


@memory_app.command("approve", help="批准记忆候选")
def cmd_memory_approve(ctx: typer.Context, memory_ref: Annotated[str, typer.Argument()]) -> int:
    return _memory_transition(_state(ctx), memory_ref, "approve")


@memory_app.command("reject", help="拒绝记忆候选")
def cmd_memory_reject(ctx: typer.Context, memory_ref: Annotated[str, typer.Argument()]) -> int:
    return _memory_transition(_state(ctx), memory_ref, "reject")


@memory_app.command("forget", help="停用但不物理删除记忆")
def cmd_memory_forget(ctx: typer.Context, memory_ref: Annotated[str, typer.Argument()]) -> int:
    return _memory_transition(_state(ctx), memory_ref, "forget")


@memory_app.command("export", help="保留原证据导出私有迁移文件")
def cmd_memory_export(
    ctx: typer.Context,
    output: Annotated[str, typer.Option("--output")],
    entity: Annotated[str, typer.Option("--entity")] = "",
) -> int:
    args = _state(ctx)
    payload = {**memory_store(args).export_memories(entity=entity), "authority": "opl-relay"}
    _private_json_output(output, payload)
    emit({"ok": True, "output": str(Path(output).expanduser()), "count": payload["count"]}, as_json=args.json)
    return 0


@memory_app.command("link", help="关联经 Framework 核验的唯一知识正文")
def cmd_memory_link(
    ctx: typer.Context,
    memory_ref: Annotated[str, typer.Argument()],
    knowledge_id: Annotated[str, typer.Option("--knowledge-id")],
    observed_at: Annotated[str, typer.Option("--observed-at")] = "",
    next_review_at: Annotated[str, typer.Option("--next-review-at")] = "",
) -> int:
    args = _state(ctx)
    framework = framework_knowledge(args)
    source = framework.read_source(knowledge_id)
    versions = {knowledge_id: source["sha256"]}
    if not framework.verify_dependencies(versions).get("valid"):
        raise ValueError("canonical_source_not_current")
    metadata = next((item for item in framework.sources() if item["knowledge_id"] == knowledge_id), None)
    if metadata is None or metadata["body_ref"]["sha256"] != source["sha256"]:
        raise ValueError("canonical_source_changed")
    linked = memory_store(args).link_memory(
        memory_ref,
        knowledge_id=knowledge_id,
        source_versions=versions,
        canonical_target={
            "source_id": source["source_id"],
            "ref": source["source_ref"],
            "sha256": source["sha256"],
        },
        scope=source["scope"],
        content_kind=source["content_kind"],
        observed_at=observed_at or metadata.get("reviewed_at", ""),
        next_review_at=next_review_at or metadata.get("next_review_at", ""),
    )
    emit({"ok": True, "memory": memory_evidence(args).inspect(linked)}, as_json=args.json)
    return 0


@memory_app.command("promote", help="导出交给 Persona 判断的长期知识晋升提案")
def cmd_memory_promote(
    ctx: typer.Context,
    memory_ref: Annotated[str, typer.Argument()],
    knowledge_id: Annotated[str, typer.Option("--knowledge-id")],
    output: Annotated[str, typer.Option("--output")],
) -> int:
    args = _state(ctx)
    store = memory_store(args)
    memory = store.get_memory(memory_ref)
    if memory is None:
        raise ValueError("memory not found")
    if memory["status"] not in {"candidate", "approved"}:
        raise ValueError("only candidate or approved memory can be proposed for promotion")
    payload = {
        "version": "relay-memory-promotion.v1",
        "proposal_only": True,
        "semantic_owner": "persona",
        "writeback_owner": "opl-framework",
        "target_knowledge_id": knowledge_id,
        "memory": memory_evidence(args).inspect(memory),
    }
    _private_json_output(output, payload)
    store.record_promotion(
        memory_ref,
        proposal_ref=str(Path(output).expanduser().resolve()),
        knowledge_id=knowledge_id,
    )
    emit({"ok": True, "proposal_only": True, "output": str(Path(output).expanduser())}, as_json=args.json)
    return 0


# ---------------------------------------------------------------------------
# sources / context / persona / triage subcommands
# ---------------------------------------------------------------------------


@sources_app.command("list", help="列出 Framework 已授权接受的知识元信息")
def cmd_sources_list(ctx: typer.Context) -> int:
    args = _state(ctx)
    sources = framework_knowledge(args).sources()
    emit({"ok": True, "authority": "opl-framework", "sources": sources}, as_json=args.json)
    return 0


@sources_app.command("index", help="重建可丢弃的知识元信息目录")
def cmd_sources_index(ctx: typer.Context) -> int:
    args = _state(ctx)
    result = KnowledgeIndex(Path(args.memory_db).expanduser()).rebuild(framework_knowledge(args))
    emit({"ok": True, "result": result}, as_json=args.json)
    return 0


@sources_app.command("search", help="通过 Framework 检索当前已接受知识")
def cmd_sources_search(
    ctx: typer.Context,
    query: Annotated[str, typer.Argument()],
    source: Annotated[list[str] | None, typer.Option("--source")] = None,
    limit: Annotated[int, typer.Option("--limit")] = 10,
) -> int:
    args = _state(ctx)
    results = KnowledgeIndex(Path(args.memory_db).expanduser()).search(
        query,
        framework=framework_knowledge(args),
        source_ids=source or [],
        limit=limit,
    )
    emit({"ok": True, "results": results}, as_json=args.json)
    return 0


@sources_app.command("discover", help="发现来源授权内的未审阅候选；不作为当前事实")
def cmd_sources_discover(
    ctx: typer.Context,
    query: Annotated[str, typer.Argument()],
    limit: Annotated[int, typer.Option("--limit")] = 10,
) -> int:
    args = _state(ctx)
    payload = framework_knowledge(args).discover(query, limit=limit)
    emit({"ok": True, **payload}, as_json=args.json)
    return 0


@context_app.command("build", help="按人物、项目或任务组装上下文")
def cmd_context_build(
    ctx: typer.Context,
    person: Annotated[str, typer.Option("--person")] = "",
    project: Annotated[str, typer.Option("--project")] = "",
    query: Annotated[str, typer.Option("--query")] = "",
    mail_limit: Annotated[int, typer.Option("--mail-limit")] = 6,
    memory_limit: Annotated[int, typer.Option("--memory-limit")] = 20,
    knowledge_limit: Annotated[int, typer.Option("--knowledge-limit")] = 6,
) -> int:
    args = _state(ctx)
    payload = ContextBuilder(
        mail_db_path=Path(args.db).expanduser(),
        memory_db_path=Path(args.memory_db).expanduser(),
        framework=framework_knowledge(args),
    ).build(
        person=person,
        project=project,
        query=query,
        mail_limit=mail_limit,
        memory_limit=memory_limit,
        knowledge_limit=knowledge_limit,
    )
    emit(payload, as_json=args.json)
    return 0


@persona_app.command("draft-context", help="读取 Persona 的邮件草稿上下文，不创建或发送草稿")
def cmd_persona_draft_context(
    ctx: typer.Context,
    input_path: Annotated[str, typer.Option("--input")],
) -> int:
    args = _state(ctx)
    emit(load_persona_mail_context(Path(input_path).expanduser()), as_json=args.json)
    return 0


@persona_app.command("draft-create", help="从已批准 Persona 提案创建 Apple Mail 审核草稿；不发送")
def cmd_persona_draft_create(
    ctx: typer.Context,
    input_path: Annotated[str, typer.Option("--input")],
    account: Annotated[str, typer.Option("--account")],
    to: Annotated[list[str], typer.Option("--to")],
    cc: Annotated[list[str] | None, typer.Option("--cc")] = None,
    bcc: Annotated[list[str] | None, typer.Option("--bcc")] = None,
    open_draft: Annotated[bool, typer.Option("--open/--no-open", help="创建后在 Apple Mail 中显示草稿")] = True,
) -> int:
    args = _state(ctx)
    context = load_approved_persona_draft_context(Path(input_path).expanduser())
    emit(
        {
            "ok": True,
            "handoff": create_persona_review_draft(
                args,
                context=context,
                account_id=account,
                to=list(to),
                cc=list(cc or []),
                bcc=list(bcc or []),
                visible=open_draft,
            ),
        },
        as_json=args.json,
    )
    return 0


@triage_app.command("evidence", help="从 email-store:// 邮件证据生成不可写邮箱的 triage envelope")
def cmd_triage_evidence(
    ctx: typer.Context,
    source_ref: Annotated[str, typer.Argument()],
    policy_ref: Annotated[list[str], typer.Option("--policy-ref")],
) -> int:
    args = _state(ctx)
    payload = build_triage_evidence(
        mail_db_path=Path(args.db).expanduser(),
        source_ref=source_ref,
        policy_refs=list(policy_ref),
    )
    emit({"ok": True, "evidence": payload}, as_json=args.json)
    return 0


@triage_app.command("validate", help="验证 triage evidence 的 provenance、policy digest 与写入边界")
def cmd_triage_validate(
    ctx: typer.Context,
    input_path: Annotated[str, typer.Option("--input")],
) -> int:
    args = _state(ctx)
    raw = sys.stdin.read() if input_path == "-" else Path(input_path).expanduser().read_text(encoding="utf-8")
    payload = _triage_evidence_from_cli_output(json.loads(raw))
    emit(validate_triage_evidence(payload), as_json=args.json)
    return 0


# ---------------------------------------------------------------------------
# workspace / setup / account / credential subcommands
# ---------------------------------------------------------------------------


@workspace_app.command("inspect", help="只读检查 workspace")
def cmd_workspace_inspect(ctx: typer.Context) -> int:
    emit({"ok": True, "workspace": inspect_workspace(default_profile_workspace())}, as_json=_state(ctx).json)
    return 0


@workspace_app.command("init", help="初始化 workspace 目录")
def cmd_workspace_init(ctx: typer.Context) -> int:
    emit({"ok": True, "workspace": initialize_workspace(default_profile_workspace())}, as_json=_state(ctx).json)
    return 0


@workspace_app.command("migrate", help="从旧 private overlay 复制到当前 workspace")
def cmd_workspace_migrate(
    ctx: typer.Context,
    from_path: Annotated[str, typer.Option("--from")],
    apply: Annotated[bool, typer.Option("--apply", help="执行复制；省略时只输出计划")] = False,
) -> int:
    args = _state(ctx)
    payload = migrate_workspace(Path(from_path), default_profile_workspace(), apply=apply)
    emit(payload, as_json=args.json)
    return 0 if payload["ok"] else 1


@setup_app.command("status", help="显示首次配置步骤")
def cmd_setup_status(ctx: typer.Context) -> int:
    emit({"ok": True, "setup": setup_status(default_profile_workspace())}, as_json=_state(ctx).json)
    return 0


@setup_app.command("init", help="创建缺失的 Profile 模板和空配置")
def cmd_setup_init(ctx: typer.Context) -> int:
    workspace_payload = initialize_workspace(default_profile_workspace())
    setup_payload = dict(workspace_payload["setup"])
    setup_payload["created"] = workspace_payload["created"]
    emit({"ok": True, "setup": setup_payload, "workspace": workspace_payload}, as_json=_state(ctx).json)
    return 0


@account_app.command("add", help="写入账号元数据，不保存密码")
def cmd_account_add(
    ctx: typer.Context,
    account_id: Annotated[str, typer.Option("--id")],
    email: Annotated[str, typer.Option("--email")],
    host: Annotated[str, typer.Option("--host")] = "",
    provider: Annotated[Literal["imap", "microsoft_graph"], typer.Option("--provider")] = "imap",
    client_id: Annotated[str, typer.Option("--client-id")] = "",
    tenant: Annotated[Literal["consumers", "common", "organizations"], typer.Option("--tenant")] = "consumers",
    port: Annotated[int, typer.Option("--port")] = 993,
    security: Annotated[Literal["ssl", "starttls", "plain"], typer.Option("--security")] = "ssl",
    username: Annotated[str, typer.Option("--username")] = "",
    credential_ref: Annotated[str, typer.Option("--credential-ref")] = "",
    backend: Annotated[Literal["himalaya"], typer.Option("--backend")] = "himalaya",
    include: Annotated[list[str] | None, typer.Option("--include")] = None,
    exclude: Annotated[list[str] | None, typer.Option("--exclude")] = None,
) -> int:
    args = _state(ctx)
    if provider == "imap" and not host:
        raise ValueError("--host is required for IMAP accounts")
    account = add_account(
        Path(args.config).expanduser(),
        account_id=account_id,
        email=email,
        host=host,
        port=port,
        security=security,
        username=username or email,
        credential_ref=credential_ref or f"keychain.{account_id}.{'oauth' if provider == 'microsoft_graph' else 'imap'}",
        include_folders=include or ["*"],
        exclude_folders=exclude or [],
        provider=provider,
        client_id=client_id,
        tenant=tenant,
        backend=backend,
    )
    emit(
        {
            "ok": True,
            "account": {
                "account_id": account.account_id,
                "email": account.email,
                "provider": account.provider,
                "imap_host": account.imap.host if account.imap else None,
                "credential_ref": account.imap.credential_ref if account.imap else account.graph.credential_ref,
                "include_folders": account.include_folders,
                "exclude_folders": account.exclude_folders,
                "credential_configured": False,
            },
            "next_actions": [
                f"opl-relay --json {'account login' if account.graph else 'credential set'} --account {account.account_id}",
                f"opl-relay --json account check --account {account.account_id}",
            ],
        },
        as_json=args.json,
    )
    return 0


@account_app.command("check", help="检查账号元数据和密码存在性")
def cmd_account_check(
    ctx: typer.Context,
    account: Annotated[str, typer.Option("--account")],
    connect: Annotated[bool, typer.Option("--connect", help="额外执行一次 IMAP 登录探测")] = False,
) -> int:
    args = _state(ctx)
    acct = load_account(Path(args.config).expanduser(), account)
    if acct.graph:
        from .graph import GraphClient
        from .keychain import get_oauth_secret

        credential_error = ""
        try:
            configured = bool(get_oauth_secret(acct.graph.credential_ref))
        except RuntimeError as exc:
            configured, credential_error = False, str(exc)
        payload: dict[str, object] = {
            "ok": True,
            "account": acct.account_id,
            "email": acct.email,
            "provider": acct.provider,
            "backend": acct.backend,
            "credential_configured": configured,
            "connection": {"status": "not_checked"},
        }
        if connect:
            try:
                if not configured:
                    raise ValueError(credential_error or "oauth_login_required")
                client = GraphClient(acct)
                client.identity()
                from .native import Himalaya

                Himalaya(acct, token=client._access_token()).check()
                payload["connection"] = {"status": "healthy", "identity_verified": True}
                payload["scopes"] = sorted(client.scopes)
            except Exception as exc:
                payload.update(ok=False, connection={"status": "unavailable", "reason": str(exc)})
        emit(payload, as_json=args.json)
        return 0 if payload["ok"] else 1
    configured = keychain_has_secret(
        acct.imap.credential_ref,
        fallback_keychain=acct.imap.fallback_keychain,
    )
    payload = {
        "ok": True,
        "account": acct.account_id,
        "email": acct.email,
        "imap_host": acct.imap.host,
        "credential_ref": acct.imap.credential_ref,
        "credential_configured": configured,
        "backend": acct.backend,
        "connection": {"status": "not_checked"},
    }
    if connect:
        if not configured:
            payload["connection"] = {"status": "blocked", "reason": "credential_not_configured"}
        else:
            try:
                from .native import Himalaya

                Himalaya(acct).check()
                payload["connection"] = {"status": "healthy", "server_response": "OK"}
            except Exception as exc:
                payload["connection"] = {"status": "unavailable", "reason": type(exc).__name__}
    emit(payload, as_json=args.json)
    return 0 if payload["connection"]["status"] not in {"blocked", "unavailable"} else 1


@account_app.command("login", help="微软 OAuth 登录；令牌仅存入 Keychain")
def cmd_account_login(
    ctx: typer.Context,
    account: Annotated[str, typer.Option("--account")],
    write: Annotated[bool, typer.Option("--write", help="授权邮件整理与草稿；不申请发送权限")] = False,
    timeout: Annotated[int, typer.Option("--timeout")] = 600,
) -> int:
    from .graph import login_graph

    args = _state(ctx)
    acct = load_account(Path(args.config).expanduser(), account)

    def notify(url: str) -> None:
        print(
            json.dumps({
                "status": "awaiting_microsoft_login",
                "account": acct.account_id,
                "authorization_url": url,
                "permissions": "Mail.ReadWrite" if write else "Mail.Read",
                "send_allowed": False,
            }),
            flush=True,
        )

    payload = login_graph(acct, write=write, notify=notify, timeout=timeout)
    emit(payload, as_json=args.json)
    return 0


def cmd_account_folders(args: Any) -> int:
    acct = load_account(Path(args.config).expanduser(), args.account)
    if acct.graph:
        from .graph import GraphClient

        client = GraphClient(acct)
        client.identity()
    else:
        from .native import Himalaya

        client = Himalaya(acct)
    folders = client.folders()
    if not acct.graph:
        folders = [{"name": box["name"], "flags": sorted(box["flags"])} for box in folders]
    emit({"ok": True, "account": acct.account_id, "folders": folders}, as_json=args.json)
    return 0


@account_app.command("folders", help="只读盘点 Microsoft Graph 文件夹")
def _cli_account_folders(
    ctx: typer.Context,
    account: Annotated[str, typer.Option("--account")],
) -> int:
    args = _state(ctx)
    return cmd_account_folders(SimpleNamespace(config=args.config, account=account, json=args.json))


@credential_app.command("set", help="交互式录入密码")
def cmd_credential_set(
    ctx: typer.Context,
    account_id: Annotated[str, typer.Option("--account")],
    secret_stdin: Annotated[
        bool,
        typer.Option("--secret-stdin", help="从 stdin 读取密码，避免密码出现在命令参数中"),
    ] = False,
) -> int:
    args = _state(ctx)
    account = load_account(Path(args.config).expanduser(), account_id)
    if account.graph:
        raise ValueError("Microsoft mail uses OAuth; run account login instead of entering a password")
    secret = (
        sys.stdin.read().rstrip("\r\n")
        if secret_stdin
        else getpass.getpass(f"IMAP password for {account.email}: ")
    )
    keychain_set_secret(account.imap.credential_ref, secret)
    emit(
        {
            "ok": True,
            "account": account.account_id,
            "credential_ref": account.imap.credential_ref,
            "credential_configured": True,
            "secret_persisted_to": KEYCHAIN_SERVICE,
        },
        as_json=args.json,
    )
    return 0


# ---------------------------------------------------------------------------
# draft subcommands
# ---------------------------------------------------------------------------


@draft_app.command("server-create", help="准备服务器草稿；--apply 保存至 IMAP Drafts，不发送")
def cmd_draft_server_create(
    ctx: typer.Context,
    account: Annotated[str, typer.Option("--account")],
    request_id: Annotated[str, typer.Option("--request-id")],
    signature_file: Annotated[str, typer.Option("--signature-file")],
    to: Annotated[list[str], typer.Option("--to")],
    subject: Annotated[str, typer.Option("--subject")],
    body: Annotated[str | None, typer.Option("--body")] = None,
    body_file: Annotated[str | None, typer.Option("--body-file")] = None,
    cc: Annotated[list[str] | None, typer.Option("--cc")] = None,
    attach: Annotated[list[str] | None, typer.Option("--attach")] = None,
    in_reply_to: Annotated[str | None, typer.Option("--in-reply-to", help="被回复原信的 Message-ID，包含尖括号")] = None,
    thread_source: Annotated[
        str | None,
        typer.Option("--thread-source", help="从本账号原信提取线程头，保留显式收件人和正文"),
    ] = None,
    references: Annotated[
        list[str] | None,
        typer.Option("--references", help="已有线程 Message-ID（空格分隔或重复传入）；与 --in-reply-to 配用"),
    ] = None,
    apply: Annotated[bool, typer.Option("--apply", help="仅写入服务器 Drafts；不会发送")] = False,
) -> int:
    from .server_drafts import server_draft

    _require_exclusive(body, "--body", body_file, "--body-file", required=True)
    _require_exclusive(in_reply_to, "--in-reply-to", thread_source, "--thread-source", required=False)
    args = _state(ctx)
    payload = server_draft(
        config_path=Path(args.config).expanduser(),
        db_path=Path(args.db).expanduser(),
        ledger_path=Path(args.draft_db).expanduser(),
        account_id=account,
        request_id=request_id,
        inspect=False,
        body=read_body_text(body, body_file),
        signature=Path(signature_file).expanduser().read_text(encoding="utf-8"),
        to=list(to),
        cc=list(cc or []),
        subject=subject,
        attachments=list(attach or []),
        thread_source_ref=thread_source,
        in_reply_to=in_reply_to,
        references=list(references or []),
        apply=apply,
    )
    emit({"ok": True, "draft": payload}, as_json=args.json)
    return 0


@draft_app.command("server-reply-all", help="按完整原邮件回复所有人，保存服务器草稿，不依赖桌面")
def cmd_draft_server_reply_all(
    ctx: typer.Context,
    account: Annotated[str, typer.Option("--account")],
    request_id: Annotated[str, typer.Option("--request-id")],
    signature_file: Annotated[str, typer.Option("--signature-file")],
    storage_ref: Annotated[str, typer.Option("--storage-ref")],
    body: Annotated[str | None, typer.Option("--body")] = None,
    body_file: Annotated[str | None, typer.Option("--body-file")] = None,
    attach: Annotated[list[str] | None, typer.Option("--attach")] = None,
    self_address: Annotated[list[str] | None, typer.Option("--self-address", help="额外已确认的本人邮箱别名")] = None,
    apply: Annotated[bool, typer.Option("--apply", help="仅写入服务器 Drafts；不会发送")] = False,
) -> int:
    from .server_drafts import server_draft

    _require_exclusive(body, "--body", body_file, "--body-file", required=True)
    args = _state(ctx)
    payload = server_draft(
        config_path=Path(args.config).expanduser(),
        db_path=Path(args.db).expanduser(),
        ledger_path=Path(args.draft_db).expanduser(),
        account_id=account,
        request_id=request_id,
        inspect=False,
        body=read_body_text(body, body_file),
        signature=Path(signature_file).expanduser().read_text(encoding="utf-8"),
        source_ref=storage_ref,
        attachments=list(attach or []),
        self_aliases=list(self_address or []),
        apply=apply,
    )
    emit({"ok": True, "draft": payload}, as_json=args.json)
    return 0


@draft_app.command("server-inspect", help="从服务器回读同一请求的草稿，不重建或覆盖")
def cmd_draft_server_inspect(
    ctx: typer.Context,
    account: Annotated[str, typer.Option("--account")],
    request_id: Annotated[str, typer.Option("--request-id")],
) -> int:
    from .server_drafts import server_draft

    args = _state(ctx)
    payload = server_draft(
        config_path=Path(args.config).expanduser(),
        db_path=Path(args.db).expanduser(),
        ledger_path=Path(args.draft_db).expanduser(),
        account_id=account,
        request_id=request_id,
        inspect=True,
    )
    emit({"ok": True, "draft": payload}, as_json=args.json)
    return 0


@draft_app.command("server-reconcile", help="对账 Drafts 与 Sent，清除已由已发件取代的残留服务器草稿")
def cmd_draft_server_reconcile(
    ctx: typer.Context,
    account: Annotated[str, typer.Option("--account")],
    request_id: Annotated[str | None, typer.Option("--request-id", help="只核对这一个 Relay 请求")] = None,
    sent_message_id: Annotated[
        str | None,
        typer.Option("--sent-message-id", help="在 Sent 中实时验证这一 Message-ID，避免扫描历史"),
    ] = None,
    apply: Annotated[bool, typer.Option("--apply", help="执行删除；默认只做只读预览")] = False,
) -> int:
    from .server_drafts import reconcile_server_drafts

    args = _state(ctx)
    payload = reconcile_server_drafts(
        config_path=Path(args.config).expanduser(),
        ledger_path=Path(args.draft_db).expanduser(),
        account_id=account,
        apply=apply,
        request_id=request_id,
        sent_message_id=sent_message_id,
    )
    emit({"ok": True, "reconcile": payload}, as_json=args.json)
    return 0


@draft_app.command("server-send", help="按已审核指纹单次发送服务器草稿并核对 Sent")
def cmd_server_send(
    ctx: typer.Context,
    account: Annotated[str, typer.Option("--account")],
    request_id: Annotated[str, typer.Option("--request-id")],
    approval: Annotated[str, typer.Option("--approval")],
    check: Annotated[bool, typer.Option("--check", help="只核对同一草稿的交付就绪状态，不发送或认领")] = False,
) -> int:
    from .server_send import send_server_draft

    args = _state(ctx)
    payload = send_server_draft(
        config_path=Path(args.config).expanduser(),
        ledger_path=Path(args.draft_db).expanduser(),
        account_id=account,
        request_id=request_id,
        approval=approval,
        check=check,
    )
    ok = payload["state"] != "unknown"
    emit({"ok": ok, "draft": payload}, as_json=args.json)
    return 0 if ok else 1


@draft_app.command("reply-context", help="一次读取回复原文、完整收件人及线程锚点")
def cmd_reply_context(
    ctx: typer.Context,
    account: Annotated[str, typer.Option("--account")],
    storage_ref: Annotated[str | None, typer.Option("--storage-ref")] = None,
    query: Annotated[str | None, typer.Option("--query", help="搜索当前活动邮箱；配合 --latest-sent 选择最新本人已发线程")] = None,
    latest_sent: Annotated[bool, typer.Option("--latest-sent", help="催办时选同线程中较新的本人已发邮件")] = False,
    sync: Annotated[bool, typer.Option("--sync", help="选择来源前只同步一次当前账户")] = False,
) -> int:
    from .reply_context import reply_context

    _require_exclusive(storage_ref, "--storage-ref", query, "--query", required=True)
    args = _state(ctx)
    result = None
    if sync:
        result = sync_account(
            config_path=Path(args.config).expanduser(),
            db_path=Path(args.db).expanduser(),
            account_id=account,
            scope="active",
            max_duration_sec=60,
        )
        if not result.get("ok") or any(not item.get("complete") for item in result.get("folders", [])):
            emit(
                {
                    "ok": False,
                    "sync": result,
                    "error": "Active sync is incomplete; inspect available mail before selecting a reply",
                },
                as_json=args.json,
            )
            return 1
    payload = reply_context(
        config_path=Path(args.config).expanduser(),
        db_path=Path(args.db).expanduser(),
        account_id=account,
        storage_ref=storage_ref,
        query=query,
        latest_sent=latest_sent,
    )
    if result is not None:
        payload["sync"] = result
        payload["coverage"]["freshness"] = "fresh active sync"
    emit({"ok": True, "reply_context": payload}, as_json=args.json)
    return 0


@draft_app.command("create", help="创建并保存 Apple Mail 草稿")
def cmd_draft_create(
    ctx: typer.Context,
    account: Annotated[str, typer.Option("--account")],
    to: Annotated[list[str], typer.Option("--to")],
    subject: Annotated[str, typer.Option("--subject")],
    body: Annotated[str | None, typer.Option("--body")] = None,
    body_file: Annotated[str | None, typer.Option("--body-file")] = None,
    cc: Annotated[list[str] | None, typer.Option("--cc")] = None,
    bcc: Annotated[list[str] | None, typer.Option("--bcc")] = None,
    attach: Annotated[list[str] | None, typer.Option("--attach")] = None,
    open_draft: Annotated[bool, typer.Option("--open/--no-open", help="创建后在 Apple Mail 中显示草稿")] = True,
) -> int:
    _require_exclusive(body, "--body", body_file, "--body-file", required=True)
    args = _state(ctx)
    acct = load_account(Path(args.config).expanduser(), account)
    attachments = [Path(value).expanduser() for value in (attach or [])]
    missing = [str(path) for path in attachments if not path.is_file()]
    if missing:
        raise FileNotFoundError("附件不存在: " + ", ".join(missing))
    service, _ = draft_service(args)
    payload = service.create(
        account_id=acct.account_id,
        sender=acct.email,
        to=parse_recipients(list(to), required=True),
        cc=parse_recipients(list(cc or [])),
        bcc=parse_recipients(list(bcc or [])),
        subject=subject,
        body_text=read_body_text(body, body_file),
        attachments=attachments,
        visible=open_draft,
    )
    emit({"ok": True, "draft": payload}, as_json=args.json)
    return 0


@draft_app.command("reply-all", help="按 Apple Mail 原生 Reply All 路由创建稳定审核草稿")
def cmd_draft_reply_all(
    ctx: typer.Context,
    account: Annotated[str, typer.Option("--account")],
    apple_mail_account: Annotated[str, typer.Option("--apple-mail-account")],
    apple_mail_id: Annotated[int, typer.Option("--apple-mail-id")],
    mailbox_path: Annotated[str, typer.Option("--mailbox-path")],
    body: Annotated[str | None, typer.Option("--body")] = None,
    body_file: Annotated[str | None, typer.Option("--body-file")] = None,
    attach: Annotated[list[str] | None, typer.Option("--attach")] = None,
    open_draft: Annotated[bool, typer.Option("--open/--no-open", help="创建后在 Apple Mail 中显示草稿")] = True,
) -> int:
    _require_exclusive(body, "--body", body_file, "--body-file", required=True)
    args = _state(ctx)
    acct = load_account(Path(args.config).expanduser(), account)
    attachments = [Path(value).expanduser() for value in (attach or [])]
    missing = [str(path) for path in attachments if not path.is_file()]
    if missing:
        raise FileNotFoundError("附件不存在: " + ", ".join(missing))
    service, _ = draft_service(args)
    payload = service.reply_all(
        account_id=acct.account_id,
        sender=acct.email,
        provider_account=apple_mail_account,
        source_message_id=apple_mail_id,
        mailbox_path=mailbox_path,
        body_text=read_body_text(body, body_file),
        attachments=attachments,
        visible=open_draft,
    )
    emit({"ok": True, "draft": payload}, as_json=args.json)
    return 0


@draft_app.command("adopt", help="登记现有 Apple Mail 草稿")
def cmd_draft_adopt(
    ctx: typer.Context,
    account: Annotated[str, typer.Option("--account")],
    apple_mail_uuid: Annotated[str, typer.Option("--apple-mail-uuid")],
) -> int:
    args = _state(ctx)
    acct = load_account(Path(args.config).expanduser(), account)
    service, provider = draft_service(args)
    provider_account = provider.resolve_account(acct.email)
    payload = service.adopt(
        account_id=acct.account_id,
        provider_account=provider_account,
        provider_uuid=apple_mail_uuid,
    )
    emit({"ok": True, "draft": payload}, as_json=args.json)
    return 0


@draft_app.command("inspect", help="回读草稿并生成审批指纹")
def cmd_draft_inspect(ctx: typer.Context, draft_ref: Annotated[str, typer.Argument()]) -> int:
    args = _state(ctx)
    service, _ = draft_service(args)
    emit({"ok": True, "draft": service.inspect(draft_ref)}, as_json=args.json)
    return 0


@draft_app.command("open", help="在 Apple Mail 中打开草稿")
def cmd_draft_open(ctx: typer.Context, draft_ref: Annotated[str, typer.Argument()]) -> int:
    args = _state(ctx)
    service, _ = draft_service(args)
    emit({"ok": True, "draft": service.open(draft_ref)}, as_json=args.json)
    return 0


@draft_app.command("send", help="使用当前审批指纹单次发送")
def cmd_draft_send(
    ctx: typer.Context,
    draft_ref: Annotated[str, typer.Argument()],
    approval: Annotated[str, typer.Option("--approval")],
) -> int:
    args = _state(ctx)
    service, _ = draft_service(args)
    emit({"ok": True, "draft": service.send(draft_ref, approval=approval)}, as_json=args.json)
    return 0


# ---------------------------------------------------------------------------
# Canonical read-only Apple Mail subcommands
# ---------------------------------------------------------------------------


@apple_mail_app.command("accounts", help="列出本机 Apple Mail 账户（只读）")
def cmd_apple_mail_accounts(ctx: typer.Context) -> int:
    emit({"ok": True, "accounts": _apple_mail_provider().list_accounts()}, as_json=_state(ctx).json)
    return 0


@apple_mail_app.command("mailboxes", help="列出指定 Apple Mail 账户下的邮箱目录（只读）")
def cmd_apple_mail_mailboxes(
    ctx: typer.Context,
    account: Annotated[str, typer.Option("--account")],
) -> int:
    emit(
        {"ok": True, "account": account, "mailboxes": _apple_mail_provider().list_mailboxes(provider_account=account)},
        as_json=_state(ctx).json,
    )
    return 0


@apple_mail_app.command("recent", help="列出 Apple Mail 指定目录最近邮件（只读）")
def cmd_apple_mail_recent(
    ctx: typer.Context,
    account: Annotated[str, typer.Option("--account")],
    mailbox_path: Annotated[str, typer.Option("--mailbox-path")] = "INBOX",
    limit: Annotated[int, typer.Option("--limit")] = 20,
    include_read: Annotated[bool, typer.Option("--include-read")] = False,
) -> int:
    messages = _apple_mail_provider().recent_messages(
        provider_account=account,
        mailbox_path=mailbox_path,
        limit=limit,
        include_read=include_read,
    )
    emit({"ok": True, "messages": messages}, as_json=_state(ctx).json)
    return 0


@apple_mail_app.command("read", help="读取 Apple Mail 单封邮件（只读）")
def cmd_apple_mail_read(
    ctx: typer.Context,
    account: Annotated[str, typer.Option("--account")],
    message_id: Annotated[int, typer.Option("--id")],
    mailbox_path: Annotated[str, typer.Option("--mailbox-path")] = "INBOX",
) -> int:
    message = _apple_mail_provider().read_message(
        provider_account=account,
        mailbox_path=mailbox_path,
        message_id=message_id,
    )
    emit({"ok": True, "message": message}, as_json=_state(ctx).json)
    return 0


@apple_mail_app.command("resolve-account", help="按发件邮箱解析唯一 Apple Mail 账户名（只读）")
def cmd_apple_mail_resolve_account(
    ctx: typer.Context,
    sender: Annotated[str, typer.Option("--sender")],
) -> int:
    provider_account = _apple_mail_provider().resolve_account(sender)
    emit({"ok": True, "sender": sender, "provider_account": provider_account}, as_json=_state(ctx).json)
    return 0


# ---------------------------------------------------------------------------
# Typer execution entry & parser introspection helper
# ---------------------------------------------------------------------------


class _TyperParser:
    """Thin introspection adapter backed directly by the Typer command tree."""

    def __init__(self, typer_app: typer.Typer) -> None:
        self._cmd = typer.main.get_command(typer_app)

    def parse_args(self, argv: list[str] | None = None) -> SimpleNamespace:
        raw_argv = list(sys.argv[1:] if argv is None else argv)
        try:
            ctx = self._cmd.make_context("opl-relay", list(raw_argv))
            params: dict[str, Any] = dict(ctx.params)
            current_cmd = self._cmd
            current_ctx = ctx
            while hasattr(current_cmd, "resolve_command"):
                remaining = list(getattr(current_ctx, "_protected_args", [])) + list(current_ctx.args)
                if not remaining:
                    raise typer.BadParameter("missing subcommand")
                name, subcmd, rest = current_cmd.resolve_command(current_ctx, remaining)
                if subcmd is None:
                    raise typer.BadParameter(f"unknown command: {name}")
                current_ctx = subcmd.make_context(name, rest, parent=current_ctx)
                params.update(current_ctx.params)
                current_cmd = subcmd
        except typer.Exit as exc:
            raise SystemExit(exc.exit_code) from None
        except typer.TyperException as exc:
            exc.show()
            raise SystemExit(getattr(exc, "exit_code", 2)) from None
        if "as_json" in params:
            params["json"] = params.pop("as_json")
        params["func"] = lambda _args=None, _argv=raw_argv: main(_argv)
        return SimpleNamespace(**params)


def build_parser() -> _TyperParser:
    return _TyperParser(app)


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in raw_argv
    command = typer.main.get_command(app)
    try:
        result = command.main(args=raw_argv, prog_name="opl-relay", standalone_mode=False)
        return int(result) if isinstance(result, int) else 0
    except typer.Exit as exc:
        return int(exc.exit_code)
    except typer.TyperException as exc:
        exc.show()
        raise SystemExit(getattr(exc, "exit_code", 2)) from None
    except Exception as exc:
        return fail(str(exc), as_json=as_json)


if __name__ == "__main__":
    raise SystemExit(main())
