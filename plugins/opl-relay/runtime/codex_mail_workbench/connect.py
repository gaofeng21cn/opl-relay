from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from typing import Any

from .config import load_accounts_config
from .workbench import Workbench


SCHEMA_VERSION = "opl-relay-connect.v1"
CAPABILITIES = ("mailbox.search", "mailbox.read", "draft.create")
_SEARCH_FIELDS = {"query", "scope", "folder", "limit"}
_READ_FIELDS = {"storage_ref"}
_DRAFT_FIELDS = {"storage_ref", "body", "request_id"}
_ACCOUNT_FIELDS = {"account", "account_id", "mailbox", "email", "credential", "token", "secret"}


class ConnectError(ValueError):
    def __init__(self, code: str, message: str, *, disposition: str = "not_dispatched"):
        super().__init__(message)
        self.code = code
        self.disposition = disposition


def _revision(account: Any) -> str:
    fields = asdict(account)
    if fields.get("imap"):
        fields["imap"].pop("credential_ref", None)
        fields["imap"].pop("fallback_keychain", None)
    if fields.get("graph"):
        fields["graph"].pop("credential_ref", None)
    material = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _mailbox_ref(account_id: str) -> str:
    opaque = hashlib.sha256(("opl-relay-mailbox-v1\0" + account_id).encode("utf-8")).hexdigest()
    return f"relay-mailbox://{opaque}"


def _public_mailboxes(config_path) -> list[dict[str, str]]:
    accounts = load_accounts_config(config_path)
    return [
        {
            "mailbox_ref": _mailbox_ref(account.account_id),
            "account_id": account.account_id,
            "email": account.email,
            "revision": _revision(account),
        }
        for account in sorted(accounts.values(), key=lambda item: item.account_id)
    ]


def _bad_fields(value: dict[str, Any], allowed: set[str]) -> bool:
    return bool(set(value) - allowed) or bool(set(value) & _ACCOUNT_FIELDS)


def _account_for_ref(config_path, mailbox_ref: object) -> dict[str, Any]:
    if not isinstance(mailbox_ref, str):
        raise ConnectError("INVALID_CREDENTIAL_HANDLE", "连接引用无效")
    for mailbox in _public_mailboxes(config_path):
        if mailbox["mailbox_ref"] == mailbox_ref:
            return mailbox
    raise ConnectError("MAILBOX_NOT_FOUND", "邮箱连接不存在或已变更")


class ConnectProvider:
    def __init__(self, *, workbench: Workbench):
        self.workbench = workbench
        self.config_path = workbench.config_path

    def dispatch(self, request: object) -> dict[str, Any]:
        dispatched = False
        try:
            if not isinstance(request, dict) or request.get("schema_version") != SCHEMA_VERSION:
                raise ConnectError("INVALID_REQUEST", "连接请求无效")
            operation = request.get("operation")
            if operation == "list" and set(request) == {"schema_version", "operation"}:
                return {"ok": True, "disposition": "completed", "result": {"mailboxes": _public_mailboxes(self.config_path)}}
            if operation == "inspect" and set(request) == {"schema_version", "operation", "mailbox_ref"}:
                mailbox = _account_for_ref(self.config_path, request["mailbox_ref"])
                return {"ok": True, "disposition": "completed", "result": {"mailbox": mailbox, "capabilities": list(CAPABILITIES)}}
            if operation != "invoke" or set(request) != {
                "schema_version", "operation", "credential_handle", "capability", "input"
            }:
                raise ConnectError("INVALID_REQUEST", "连接请求无效")

            handle = request["credential_handle"]
            if not isinstance(handle, dict) or set(handle) != {"opaque", "revision"}:
                raise ConnectError("INVALID_CREDENTIAL_HANDLE", "连接引用无效")
            mailbox = _account_for_ref(self.config_path, handle["opaque"])
            if handle["revision"] != mailbox["revision"]:
                raise ConnectError("STALE_CREDENTIAL_HANDLE", "邮箱连接配置已变更")
            capability = request["capability"]
            args = request["input"]
            if not isinstance(args, dict):
                raise ConnectError("INVALID_ARGUMENTS", "调用参数无效")
            account_id = mailbox["account_id"]

            if capability == "mailbox.search":
                if _bad_fields(args, _SEARCH_FIELDS):
                    raise ConnectError("INVALID_ARGUMENTS", "调用参数无效")
                query = args.get("query", "")
                scope = args.get("scope", "active")
                folder = args.get("folder", "")
                limit = args.get("limit", 50)
                if (not isinstance(query, str) or len(query) > 5000 or
                        not isinstance(scope, str) or not isinstance(folder, str) or
                        type(limit) is not int or not 1 <= limit <= 100):
                    raise ConnectError("INVALID_ARGUMENTS", "调用参数无效")
                result = self.workbench.messages(account_id=account_id, query=query, scope=scope, folder=folder, limit=limit)
                result["source"] = "relay-local-cache"
            elif capability == "mailbox.read":
                if _bad_fields(args, _READ_FIELDS) or not isinstance(args.get("storage_ref"), str):
                    raise ConnectError("INVALID_ARGUMENTS", "调用参数无效")
                result = self.workbench.read(account_id=account_id, storage_ref=args["storage_ref"])
                result["source"] = "relay-local-cache"
            elif capability == "draft.create":
                if _bad_fields(args, _DRAFT_FIELDS):
                    raise ConnectError("INVALID_ARGUMENTS", "调用参数无效")
                if not isinstance(args.get("storage_ref"), str) or not isinstance(args.get("request_id"), str):
                    raise ConnectError("INVALID_ARGUMENTS", "调用参数无效")
                body = args.get("body", "")
                request_id = args["request_id"]
                if (not isinstance(body, str) or len(body) > 200000 or
                        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,70}", request_id)):
                    raise ConnectError("INVALID_ARGUMENTS", "调用参数无效")
                self.workbench.read(account_id=account_id, storage_ref=args["storage_ref"])
                dispatched = True
                result = self.workbench.reply(account_id=account_id, storage_ref=args["storage_ref"],
                                              body=body, request_id=request_id)
                result.update({"persistence": "relay-local-review", "send_allowed": False})
            else:
                raise ConnectError("CAPABILITY_UNAVAILABLE", "请求的邮箱能力不可用")

            return {"ok": True, "disposition": "completed", "result": result}
        except ConnectError as exc:
            return {"ok": False, "disposition": exc.disposition, "error": {"code": exc.code, "message": str(exc)}}
        except Exception:
            return {
                "ok": False,
                "disposition": "unknown_after_dispatch" if dispatched else "not_dispatched",
                "error": {"code": "PROVIDER_ERROR", "message": "Relay 邮箱连接操作失败"},
            }


def create_provider(*, config_path, db_path, ledger_path, profile_path=None) -> ConnectProvider:
    return ConnectProvider(workbench=Workbench(config_path=config_path, db_path=db_path,
                                                ledger_path=ledger_path, profile_path=profile_path))
