from __future__ import annotations

import hashlib
import imaplib
from dataclasses import dataclass
from typing import Any

from .config import MailAccount, keychain_get_secret, load_account
from .message import parse_headers
from .store import (
    connect_email_store,
    fetch_raw_email_by_storage_ref,
    get_message_by_storage_ref,
    record_mailbox_move,
)
from .sync import connect_imap, now_iso, parse_fetch_uid_rfc822_map


class MailboxMoveFailure(RuntimeError):
    def __init__(self, code: str, message: str, *, state: str = "not_moved") -> None:
        super().__init__(message)
        self.code = code
        self.state = state


@dataclass(frozen=True)
class ImapMailbox:
    name: str
    flags: frozenset[str]


def _consume_quoted(value: str) -> tuple[str, str]:
    if not value.startswith('"'):
        raise ValueError("expected quoted IMAP string")
    out: list[str] = []
    escaped = False
    for index, character in enumerate(value[1:], start=1):
        if escaped:
            out.append(character)
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if character == '"':
            return "".join(out), value[index + 1 :]
        out.append(character)
    raise ValueError("unterminated quoted IMAP string")


def _consume_imap_value(value: str) -> tuple[str, str]:
    value = value.lstrip()
    if value.startswith('"'):
        return _consume_quoted(value)
    if not value:
        return "", ""
    token, _, rest = value.partition(" ")
    return token, rest


def parse_imap_list_entry(raw_line: bytes) -> ImapMailbox | None:
    line = raw_line.decode("utf-8", errors="replace").strip()
    if not line.startswith("("):
        return None
    close_index = line.find(")")
    if close_index < 0:
        return None
    flags = frozenset(flag.casefold() for flag in line[1:close_index].split())
    try:
        _, rest = _consume_imap_value(line[close_index + 1 :])
        name, _ = _consume_imap_value(rest)
    except ValueError:
        return None
    if not name or name.upper() == "NIL":
        return None
    return ImapMailbox(name=name, flags=flags)


def _quoted_mailbox(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _list_mailboxes(client: imaplib.IMAP4) -> list[ImapMailbox]:
    typ, lines = client.list()
    if typ != "OK" or not lines:
        raise MailboxMoveFailure("mailbox_list_unavailable", "无法读取 IMAP 文件夹列表")
    mailboxes = [
        entry
        for raw_line in lines
        if isinstance(raw_line, bytes)
        for entry in [parse_imap_list_entry(raw_line)]
        if entry is not None
    ]
    if not mailboxes:
        raise MailboxMoveFailure("mailbox_list_invalid", "IMAP 文件夹列表无法解析")
    return mailboxes


def _resolve_destination(destination: str, mailboxes: list[ImapMailbox]) -> str:
    special_use = f"\\{destination}".casefold()
    special_matches = [mailbox for mailbox in mailboxes if special_use in mailbox.flags]
    if len(special_matches) == 1:
        return special_matches[0].name
    if len(special_matches) > 1:
        raise MailboxMoveFailure(
            "destination_ambiguous",
            f"检测到多个 IMAP {destination} 文件夹，拒绝选择其中之一",
        )
    candidates = {
        "archive": {"archive", "archives"},
        "trash": {"trash", "deleted items", "deleted messages", "bin"},
    }[destination]
    name_matches = [
        mailbox
        for mailbox in mailboxes
        if mailbox.name.casefold() in candidates
    ]
    if len(name_matches) == 1:
        return name_matches[0].name
    if len(name_matches) > 1:
        raise MailboxMoveFailure(
            "destination_ambiguous",
            f"检测到多个名为 {destination} 的文件夹，拒绝选择其中之一",
        )
    raise MailboxMoveFailure(
        "destination_missing",
        f"未找到 IMAP {destination} 文件夹，未创建任何新文件夹",
    )


def _capabilities(client: imaplib.IMAP4) -> set[str]:
    typ, values = client.capability()
    if typ != "OK":
        raise MailboxMoveFailure("capability_unavailable", "无法读取 IMAP 能力集")
    capabilities: set[str] = set()
    for value in values or []:
        text = value.decode("ascii", errors="ignore") if isinstance(value, bytes) else str(value)
        capabilities.update(token.upper() for token in text.split())
    return capabilities


def _select(client: imaplib.IMAP4, folder: str, *, readonly: bool) -> None:
    typ, _ = client.select(_quoted_mailbox(folder), readonly=readonly)
    if typ != "OK":
        raise MailboxMoveFailure("source_unavailable", f"无法打开 IMAP 文件夹：{folder}")


def _fetch_raw(client: imaplib.IMAP4, uid: int) -> bytes:
    typ, data = client.uid("fetch", str(uid), "(UID BODY.PEEK[])")
    if typ != "OK":
        raise MailboxMoveFailure("source_unavailable", f"无法读取 IMAP UID {uid}")
    raw = parse_fetch_uid_rfc822_map(data).get(uid)
    if not raw:
        raise MailboxMoveFailure("source_missing", f"IMAP UID {uid} 不存在")
    return raw


def _search_uids(client: imaplib.IMAP4, *criteria: str) -> list[int]:
    typ, values = client.uid("search", *criteria)
    if typ != "OK":
        raise MailboxMoveFailure("search_unavailable", "IMAP 搜索失败")
    payload = values[0] if values else b""
    if isinstance(payload, bytes):
        tokens = payload.split()
    else:
        tokens = str(payload).encode("ascii", errors="ignore").split()
    return [int(token) for token in tokens if token.isdigit()]


def _remote_source_matches(client: imaplib.IMAP4, message: dict[str, Any]) -> None:
    _select(client, str(message["folder"]), readonly=True)
    raw = _fetch_raw(client, int(message["uid"]))
    expected_hash = str(message["raw_sha256"])
    if hashlib.sha256(raw).hexdigest() != expected_hash:
        raise MailboxMoveFailure(
            "source_hash_mismatch",
            "远端邮件内容已变化，拒绝移动",
        )
    remote_message_id = str(parse_headers(raw).get("message_id") or "")
    if remote_message_id != str(message["message_id"]):
        raise MailboxMoveFailure(
            "source_message_id_mismatch",
            "远端 Message-ID 与本地证据不一致，拒绝移动",
        )


def _destination_uid_with_hash(
    client: imaplib.IMAP4,
    *,
    destination_folder: str,
    message_id: str,
    raw_sha256: str,
) -> int | None:
    _select(client, destination_folder, readonly=True)
    uids = _search_uids(client, "HEADER", "Message-ID", _quoted_mailbox(message_id))
    for uid in uids:
        raw = _fetch_raw(client, uid)
        if hashlib.sha256(raw).hexdigest() == raw_sha256:
            return uid
    return None


def _source_uid_exists(client: imaplib.IMAP4, *, source_folder: str, uid: int) -> bool:
    _select(client, source_folder, readonly=True)
    return uid in _search_uids(client, "UID", str(uid))


def _preflight_message(
    client: imaplib.IMAP4,
    *,
    message: dict[str, Any],
    destination_folder: str,
) -> None:
    if not str(message["message_id"]):
        raise MailboxMoveFailure(
            "message_id_missing",
            "本地邮件缺少 Message-ID，无法验证目标副本",
        )
    if str(message["folder"]) == destination_folder:
        raise MailboxMoveFailure(
            "already_in_destination",
            "邮件已经位于目标文件夹，拒绝重复移动",
        )
    _remote_source_matches(client, message)
    existing_target = _destination_uid_with_hash(
        client,
        destination_folder=destination_folder,
        message_id=str(message["message_id"]),
        raw_sha256=str(message["raw_sha256"]),
    )
    if existing_target is not None:
        raise MailboxMoveFailure(
            "destination_already_contains_message",
            "目标文件夹已存在相同邮件，拒绝产生重复副本",
        )


def _verify_moved_message(
    client: imaplib.IMAP4,
    *,
    message: dict[str, Any],
    destination_folder: str,
) -> None:
    target_uid = _destination_uid_with_hash(
        client,
        destination_folder=destination_folder,
        message_id=str(message["message_id"]),
        raw_sha256=str(message["raw_sha256"]),
    )
    if target_uid is None:
        raise MailboxMoveFailure(
            "target_not_verified",
            "IMAP 已响应移动，但未能在目标文件夹验证相同原文",
            state="unknown",
        )
    if _source_uid_exists(
        client,
        source_folder=str(message["folder"]),
        uid=int(message["uid"]),
    ):
        raise MailboxMoveFailure(
            "source_still_present",
            "目标副本已出现，但源邮件仍存在",
            state="unknown",
        )


def _move_with_uid_move(
    client: imaplib.IMAP4,
    *,
    message: dict[str, Any],
    destination_folder: str,
) -> str:
    _select(client, str(message["folder"]), readonly=False)
    try:
        typ, _ = client.uid("move", str(message["uid"]), _quoted_mailbox(destination_folder))
    except Exception as exc:
        raise MailboxMoveFailure(
            "move_result_unknown",
            f"UID MOVE 未返回可验证结果：{type(exc).__name__}",
            state="unknown",
        ) from exc
    if typ != "OK":
        raise MailboxMoveFailure("move_rejected", "IMAP 拒绝 UID MOVE")
    _verify_moved_message(client, message=message, destination_folder=destination_folder)
    return "uid_move"


def _move_with_uidplus_fallback(
    client: imaplib.IMAP4,
    *,
    message: dict[str, Any],
    destination_folder: str,
) -> str:
    _select(client, str(message["folder"]), readonly=False)
    try:
        typ, _ = client.uid("copy", str(message["uid"]), _quoted_mailbox(destination_folder))
    except Exception as exc:
        raise MailboxMoveFailure(
            "copy_result_unknown",
            f"UID COPY 未返回可验证结果：{type(exc).__name__}",
            state="unknown",
        ) from exc
    if typ != "OK":
        raise MailboxMoveFailure("copy_rejected", "IMAP 拒绝 UID COPY")
    target_uid = _destination_uid_with_hash(
        client,
        destination_folder=destination_folder,
        message_id=str(message["message_id"]),
        raw_sha256=str(message["raw_sha256"]),
    )
    if target_uid is None:
        raise MailboxMoveFailure(
            "copied_target_not_verified",
            "UID COPY 后未能在目标文件夹验证相同原文",
            state="unknown",
        )
    _select(client, str(message["folder"]), readonly=False)
    try:
        typ, _ = client.uid("store", str(message["uid"]), "+FLAGS.SILENT", r"(\Deleted)")
    except Exception as exc:
        raise MailboxMoveFailure(
            "store_result_unknown",
            f"UID STORE 未返回可验证结果：{type(exc).__name__}",
            state="unknown",
        ) from exc
    if typ != "OK":
        raise MailboxMoveFailure("store_rejected", "IMAP 拒绝为源邮件设置删除标记")
    try:
        typ, _ = client.uid("expunge", str(message["uid"]))
    except Exception as exc:
        try:
            client.uid("store", str(message["uid"]), "-FLAGS.SILENT", r"(\Deleted)")
        except Exception:
            pass
        raise MailboxMoveFailure(
            "expunge_result_unknown",
            f"UID EXPUNGE 未返回可验证结果：{type(exc).__name__}",
            state="unknown",
        ) from exc
    if typ != "OK":
        try:
            client.uid("store", str(message["uid"]), "-FLAGS.SILENT", r"(\Deleted)")
        except Exception:
            pass
        raise MailboxMoveFailure(
            "expunge_rejected",
            "IMAP 拒绝 UID EXPUNGE；已尝试撤销删除标记",
            state="unknown",
        )
    _verify_moved_message(client, message=message, destination_folder=destination_folder)
    return "uid_copy_uid_expunge"


def _message_from_local_store(conn: Any, storage_ref: str, account_id: str) -> dict[str, Any]:
    message = get_message_by_storage_ref(conn, storage_ref)
    raw = fetch_raw_email_by_storage_ref(conn, storage_ref)
    if message is None or raw is None:
        raise MailboxMoveFailure("local_message_missing", "本地邮件证据不存在或已失效")
    if str(message["account_id"]) != account_id:
        raise MailboxMoveFailure("account_mismatch", "邮件不属于指定账号")
    local_hash = hashlib.sha256(raw).hexdigest()
    if local_hash != str(message["raw_sha256"]):
        raise MailboxMoveFailure("local_hash_mismatch", "本地邮件原文校验失败")
    return message


def _receipt(message: dict[str, Any], storage_ref: str) -> dict[str, object]:
    return {
        "storage_ref": storage_ref,
        "subject": str(message.get("subject") or ""),
        "source_folder": str(message.get("folder") or ""),
        "source_uid": int(message.get("uid") or 0),
    }


def _failed_receipt(
    storage_ref: str,
    failure: MailboxMoveFailure,
    message: dict[str, Any] | None = None,
) -> dict[str, object]:
    receipt = _receipt(message, storage_ref) if message is not None else {"storage_ref": storage_ref}
    receipt.update(
        {
            "status": "blocked",
            "error": {"code": failure.code, "message": str(failure)},
            "remote_state": failure.state,
        }
    )
    return receipt


def move_messages(
    *,
    config_path: Any,
    db_path: Any,
    account_id: str,
    destination: str,
    storage_refs: list[str],
    apply: bool,
) -> dict[str, object]:
    if destination not in {"archive", "trash"}:
        raise ValueError("destination must be archive or trash")
    unique_refs = list(dict.fromkeys(ref.strip() for ref in storage_refs if ref.strip()))
    if not unique_refs:
        raise ValueError("at least one storage_ref is required")

    account: MailAccount = load_account(config_path, account_id)
    conn = connect_email_store(db_path)
    client: imaplib.IMAP4 | None = None
    try:
        secret = keychain_get_secret(account.imap.credential_ref)
        client = connect_imap(account)
        login_type, _ = client.login(account.imap.username, secret)
        if login_type != "OK":
            raise MailboxMoveFailure("login_rejected", "IMAP 登录被拒绝")
        destination_folder = _resolve_destination(destination, _list_mailboxes(client))
        capabilities = _capabilities(client)
        if "MOVE" in capabilities:
            move_method = "uid_move"
        elif "UIDPLUS" in capabilities:
            move_method = "uid_copy_uid_expunge"
        else:
            raise MailboxMoveFailure(
                "move_unsupported",
                "服务器未提供 MOVE 或 UIDPLUS，拒绝使用不受控的 EXPUNGE",
            )

        receipts: list[dict[str, object]] = []
        prepared: list[tuple[dict[str, Any], dict[str, object]]] = []
        for storage_ref in unique_refs:
            message: dict[str, Any] | None = None
            try:
                message = _message_from_local_store(conn, storage_ref, account_id)
                _preflight_message(
                    client,
                    message=message,
                    destination_folder=destination_folder,
                )
                receipt = _receipt(message, storage_ref)
                receipt.update(
                    {
                        "status": "ready",
                        "destination_folder": destination_folder,
                        "method": move_method,
                    }
                )
                prepared.append((message, receipt))
                receipts.append(receipt)
            except MailboxMoveFailure as failure:
                receipts.append(_failed_receipt(storage_ref, failure, message))

        if len(prepared) != len(unique_refs):
            return {
                "ok": False,
                "phase": "preflight",
                "apply": apply,
                "account": account_id,
                "destination": destination,
                "destination_folder": destination_folder,
                "requested": len(unique_refs),
                "ready": len(prepared),
                "moved": 0,
                "messages": receipts,
                "error": {
                    "code": "preflight_failed",
                    "message": "至少一封邮件未通过实时校验，未执行任何移动",
                },
            }

        if not apply:
            return {
                "ok": True,
                "phase": "preflight",
                "apply": False,
                "account": account_id,
                "destination": destination,
                "destination_folder": destination_folder,
                "requested": len(unique_refs),
                "ready": len(prepared),
                "moved": 0,
                "messages": receipts,
            }

        moved = 0
        for message, receipt in prepared:
            try:
                _remote_source_matches(client, message)
                existing_target = _destination_uid_with_hash(
                    client,
                    destination_folder=destination_folder,
                    message_id=str(message["message_id"]),
                    raw_sha256=str(message["raw_sha256"]),
                )
                if existing_target is not None:
                    raise MailboxMoveFailure(
                        "destination_already_contains_message",
                        "执行前目标文件夹已出现相同邮件，停止批次",
                    )
                if move_method == "uid_move":
                    method = _move_with_uid_move(
                        client,
                        message=message,
                        destination_folder=destination_folder,
                    )
                else:
                    method = _move_with_uidplus_fallback(
                        client,
                        message=message,
                        destination_folder=destination_folder,
                    )
                occurred_at = now_iso()
                operation_ref = record_mailbox_move(
                    conn,
                    account_id=account_id,
                    storage_ref=str(message["storage_ref"]),
                    source_folder=str(message["folder"]),
                    source_uid=int(message["uid"]),
                    destination_folder=destination_folder,
                    method=method,
                    raw_sha256=str(message["raw_sha256"]),
                    occurred_at=occurred_at,
                )
                receipt.update(
                    {
                        "status": "moved",
                        "method": method,
                        "operation_ref": operation_ref,
                        "occurred_at": occurred_at,
                    }
                )
                moved += 1
            except MailboxMoveFailure as failure:
                receipt.update(_failed_receipt(str(message["storage_ref"]), failure, message))
                return {
                    "ok": False,
                    "phase": "execute",
                    "apply": True,
                    "account": account_id,
                    "destination": destination,
                    "destination_folder": destination_folder,
                    "requested": len(unique_refs),
                    "ready": len(prepared),
                    "moved": moved,
                    "messages": receipts,
                    "error": {
                        "code": "execution_stopped",
                        "message": "移动批次已在首个不确定或失败结果处停止",
                    },
                }
            except Exception as exc:
                receipt.update(
                    {
                        "status": "remote_moved_unrecorded",
                        "error": {
                            "code": "local_receipt_failed",
                            "message": f"远端移动已验证，但本地回执写入失败：{type(exc).__name__}",
                        },
                        "remote_state": "remote_moved",
                    }
                )
                return {
                    "ok": False,
                    "phase": "execute",
                    "apply": True,
                    "account": account_id,
                    "destination": destination,
                    "destination_folder": destination_folder,
                    "requested": len(unique_refs),
                    "ready": len(prepared),
                    "moved": moved,
                    "messages": receipts,
                    "error": {
                        "code": "local_receipt_failed",
                        "message": "远端状态已改变，请先核对回执再继续",
                    },
                }
        return {
            "ok": True,
            "phase": "complete",
            "apply": True,
            "account": account_id,
            "destination": destination,
            "destination_folder": destination_folder,
            "requested": len(unique_refs),
            "ready": len(prepared),
            "moved": moved,
            "messages": receipts,
        }
    finally:
        conn.close()
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass
