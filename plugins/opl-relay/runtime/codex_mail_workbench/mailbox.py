from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from .config import MailAccount, load_account
from .native import Himalaya, NativeError
from .message import parse_headers
from .store import (
    connect_email_store,
    fetch_raw_email_by_storage_ref,
    get_message_by_storage_ref,
    record_mailbox_move,
)
from .sync import now_iso


class MailboxMoveFailure(RuntimeError):
    def __init__(self, code: str, message: str, *, state: str = "not_moved") -> None:
        super().__init__(message)
        self.code = code
        self.state = state


@dataclass(frozen=True)
class ImapMailbox:
    name: str
    flags: frozenset[str]


def _list_mailboxes(client):
    return [ImapMailbox(**box) for box in client.folders()]


def _capabilities(client):
    return client.capabilities()


def _move_with_uid_move(client, *, message, destination_folder):
    try:
        client.move(message["uid"], source=message["folder"], destination=destination_folder)
    except Exception as exc:
        raise MailboxMoveFailure("move_result_unknown", "Native MOVE result unknown", state="unknown") from exc
    _verify_moved_message(client, message=message, destination_folder=destination_folder)
    return "himalaya_uid_move"


def _move_with_uidplus_fallback(client, *, message, destination_folder):
    # SYSU has UIDPLUS but no MOVE. The backend executes every protocol command;
    # Relay retains copy verification before permitting exact source removal.
    try:
        client.copy(message["uid"], source=message["folder"], destination=destination_folder)
        if _destination_uid_with_hash(client, destination_folder=destination_folder,
                message_id=message["message_id"], raw_sha256=message["raw_sha256"]) is None:
            raise ValueError("copied target not verified")
        _remote_source_matches(client, message)
        client.remove_exact(message["folder"], message["uid"])
    except Exception as exc:
        raise MailboxMoveFailure("copy_move_result_unknown", "Native copy/remove result unknown", state="unknown") from exc
    _verify_moved_message(client, message=message, destination_folder=destination_folder)
    return "himalaya_uid_copy_uid_expunge"


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
        "bill": {"bill"},
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


def _find_all_mail(mailboxes: list[ImapMailbox]) -> ImapMailbox | None:
    matches = [mailbox for mailbox in mailboxes if "\\all" in mailbox.flags]
    return matches[0] if len(matches) == 1 else None


def _resolve_exact_folder(folder: str, mailboxes: list[ImapMailbox]) -> str:
    target = folder.strip()
    exact_matches = [mailbox for mailbox in mailboxes if mailbox.name == target]
    matches = exact_matches or [
        mailbox
        for mailbox in mailboxes
        if mailbox.name.casefold() == target.casefold()
    ]
    if len(matches) > 1:
        raise MailboxMoveFailure("destination_ambiguous", f"检测到多个匹配的文件夹：{target}")
    if not matches:
        raise MailboxMoveFailure(
            "destination_missing",
            f"未找到现有文件夹：{target}（不创建新文件夹）",
        )
    if "\\noselect" in matches[0].flags:
        raise MailboxMoveFailure("destination_not_selectable", f"目标文件夹不可选中：{target}")
    return matches[0].name


def _resolve_move_target(
    *,
    destination: str,
    exact_folder: str,
    mailboxes: list[ImapMailbox],
    capabilities: set[str],
) -> tuple[str, bool]:
    # Gmail archive is a MOVE from Inbox to All Mail, preserving the existing
    # All Mail copy while removing the Inbox membership.
    if exact_folder:
        return _resolve_exact_folder(exact_folder, mailboxes), False
    all_mail = _find_all_mail(mailboxes) if "X-GM-EXT-1" in capabilities else None
    if destination == "archive" and all_mail is not None:
        if "\\noselect" in all_mail.flags:
            raise MailboxMoveFailure("destination_not_selectable", "All Mail 文件夹不可选中")
        return all_mail.name, True
    try:
        resolved_folder = _resolve_destination(destination, mailboxes)
    except MailboxMoveFailure:
        if destination == "archive" and all_mail is not None:
            return all_mail.name, True
        raise
    resolved_mailbox = next(
        (mailbox for mailbox in mailboxes if mailbox.name == resolved_folder), None
    )
    if destination == "archive" and resolved_mailbox is not None and "\\noselect" in resolved_mailbox.flags:
        if all_mail is None:
            raise MailboxMoveFailure(
                "destination_not_selectable",
                "Archive 文件夹不可选中，且未找到 All Mail 文件夹",
            )
        return all_mail.name, True
    return resolved_folder, False


def _remote_source_matches(client: Himalaya, message: dict[str, Any]) -> None:
    if client.status(message["folder"])["uidvalidity"] != int(message["uidvalidity"]):
        raise MailboxMoveFailure("source_epoch_changed", "邮件文件夹身份已变化，拒绝使用旧 UID")
    raw = client.mime(message["uid"], folder=message["folder"])
    if client.status(message["folder"])["uidvalidity"] != int(message["uidvalidity"]):
        raise MailboxMoveFailure("source_epoch_changed", "读取期间邮件文件夹身份已变化")
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
    client: Himalaya,
    *,
    destination_folder: str,
    message_id: str,
    raw_sha256: str,
) -> int | None:
    uids = client.find_message(destination_folder, message_id)
    for uid in uids:
        raw = client.mime(uid, folder=destination_folder)
        if hashlib.sha256(raw).hexdigest() == raw_sha256:
            return uid
    return None


def _source_uid_exists(client: Himalaya, *, source_folder: str, uid: int) -> bool:
    return uid in client.metadata(source_folder, [uid])


def _preflight_message(
    client: Himalaya,
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


def _preflight_gmail_archive(
    client: Himalaya,
    *,
    message: dict[str, Any],
    all_mail_folder: str,
) -> None:
    if not str(message["message_id"]):
        raise MailboxMoveFailure(
            "message_id_missing",
            "本地邮件缺少 Message-ID，无法验证目标副本",
        )
    if str(message["folder"]).casefold() != "inbox":
        raise MailboxMoveFailure("source_not_inbox", "Gmail 归档只接受收件箱中的邮件")
    _remote_source_matches(client, message)
    if _destination_uid_with_hash(
        client,
        destination_folder=all_mail_folder,
        message_id=str(message["message_id"]),
        raw_sha256=str(message["raw_sha256"]),
    ) is None:
        raise MailboxMoveFailure(
            "all_mail_copy_missing",
            "All Mail 中未找到同一原文，拒绝移除 Inbox 标签",
        )


def _verify_moved_message(
    client: Himalaya,
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


def _message_from_local_store(conn: Any, storage_ref: str, account_id: str) -> dict[str, Any]:
    message = get_message_by_storage_ref(conn, storage_ref)
    raw = fetch_raw_email_by_storage_ref(conn, storage_ref)
    if message is None or raw is None:
        raise MailboxMoveFailure("local_message_missing", "本地邮件证据不存在或已失效")
    if not message.get("present", False):
        raise MailboxMoveFailure("local_message_not_current", "历史引用可读取，但不能据此操作旧文件夹；请同步并定位当前引用")
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
    exact_folder: str = "",
) -> dict[str, object]:
    if destination and exact_folder:
        raise ValueError("use either destination or exact_folder, not both")
    if not destination and not exact_folder:
        raise ValueError("destination or exact_folder is required")
    if destination and destination not in {"archive", "trash", "bill"}:
        raise ValueError("destination must be archive, trash, or bill")
    unique_refs = list(dict.fromkeys(ref.strip() for ref in storage_refs if ref.strip()))
    if not unique_refs:
        raise ValueError("at least one storage_ref is required")

    account: MailAccount = load_account(config_path, account_id)
    if account.graph:
        from .graph import move_graph_messages
        return move_graph_messages(account=account, db_path=db_path, destination=destination,
                                   storage_refs=unique_refs, apply=apply, exact_folder=exact_folder)
    conn = connect_email_store(db_path)
    client: Himalaya | None = None
    try:
        client = Himalaya(account)
        capabilities = _capabilities(client)
        mailboxes = _list_mailboxes(client)
        destination_folder, gmail_archive = _resolve_move_target(
            destination=destination,
            exact_folder=exact_folder,
            mailboxes=mailboxes,
            capabilities=capabilities,
        )
        if gmail_archive:
            if "MOVE" not in capabilities:
                raise MailboxMoveFailure(
                    "move_unsupported",
                    "Gmail 未提供 UID MOVE，拒绝使用未验证的标签修改代替归档",
                )
            move_method = "uid_move_to_all_mail"
        elif "MOVE" in capabilities:
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
                if gmail_archive:
                    _preflight_gmail_archive(
                        client,
                        message=message,
                        all_mail_folder=destination_folder,
                    )
                else:
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
                "destination": destination or exact_folder,
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
                "destination": destination or exact_folder,
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
                if gmail_archive:
                    _preflight_gmail_archive(
                        client,
                        message=message,
                        all_mail_folder=destination_folder,
                    )
                    _move_with_uid_move(
                        client,
                        message=message,
                        destination_folder=destination_folder,
                    )
                    method = "uid_move_to_all_mail"
                else:
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
                    "destination": destination or exact_folder,
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
                    "destination": destination or exact_folder,
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
            "destination": destination or exact_folder,
            "destination_folder": destination_folder,
            "requested": len(unique_refs),
            "ready": len(prepared),
            "moved": moved,
            "messages": receipts,
        }
    finally:
        conn.close()
