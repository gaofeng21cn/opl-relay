from __future__ import annotations

import hashlib
import imaplib
import json
import re
import socket
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .config import MailAccount, keychain_get_secret, load_account
from .message import extract_attachments, parse_headers
from .paths import default_config_path, default_db_path, default_sync_state_dir
from .store import connect_email_store, upsert_email_message


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def sanitize_folder_name(folder: str) -> str:
    text = folder.strip()
    if not text:
        return "UNKNOWN"
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
    text = text.replace("\\", "_").replace("/", "__")
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text)
    cleaned = text.strip("._")
    if cleaned:
        return cleaned
    return f"folder_{digest}"


def parse_imap_list_name(raw_line: bytes) -> str:
    line = raw_line.decode("utf-8", errors="replace")
    match = re.match(r'^\(.*\)\s+"[^"]*"\s+(.*)$', line)
    if not match:
        return line.strip()
    name = match.group(1).strip()
    if name.startswith('"') and name.endswith('"'):
        name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return name


def should_sync_folder(name: str, include: Iterable[str], exclude: Iterable[str]) -> bool:
    if name in exclude:
        return False
    include_set = list(include)
    return "*" in include_set or name in include_set


def connect_imap(account: MailAccount, timeout_sec: float = 60.0) -> imaplib.IMAP4:
    security = account.imap.security.lower()
    if security == "ssl":
        client = imaplib.IMAP4_SSL(account.imap.host, account.imap.port)
    else:
        client = imaplib.IMAP4(account.imap.host, account.imap.port)
        if security == "starttls":
            client.starttls()
    sock = getattr(client, "sock", None)
    if sock is not None:
        sock.settimeout(timeout_sec)
    return client


def load_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def dump_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_fetch_uid_rfc822_map(data: list[object]) -> dict[int, bytes]:
    out: dict[int, bytes] = {}
    for item in data:
        if not (isinstance(item, tuple) and len(item) >= 2):
            continue
        meta, raw = item[0], item[1]
        if not (isinstance(meta, bytes) and isinstance(raw, bytes)):
            continue
        match = re.search(rb"UID\s+(\d+)", meta)
        if not match:
            continue
        out[int(match.group(1))] = raw
    return out


def sync_account(
    *,
    config_path: Path = default_config_path(),
    db_path: Path = default_db_path(),
    state_dir: Path = default_sync_state_dir(),
    account_id: str,
    mode: str = "incremental",
    limit_per_folder: int | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    account = load_account(config_path, account_id)
    secret = keychain_get_secret(account.imap.credential_ref)
    client = connect_imap(account)
    client.login(account.imap.username, secret)
    state_path = state_dir / f"{account_id}.json"
    state = load_json(state_path, {"account_id": account_id, "folders": {}})
    assert isinstance(state, dict)
    folders_state = state.setdefault("folders", {})
    assert isinstance(folders_state, dict)
    conn = connect_email_store(db_path)
    summary: dict[str, object] = {
        "ok": True,
        "account": account_id,
        "mode": mode,
        "folders": [],
        "new_messages": 0,
        "dry_run": dry_run,
    }
    try:
        typ, box_lines = client.list()
        if typ != "OK" or not box_lines:
            raise RuntimeError("无法读取 IMAP 文件夹列表")
        sync_folders = [
            parse_imap_list_name(raw)
            for raw in box_lines
            if isinstance(raw, bytes)
        ]
        sync_folders = [
            folder
            for folder in sync_folders
            if should_sync_folder(folder, account.include_folders, account.exclude_folders)
        ]
        for folder_name in sync_folders:
            folder_slug = sanitize_folder_name(folder_name)
            select_name = folder_name.replace("\\", "\\\\").replace('"', '\\"')
            typ_sel, _ = client.select(f'"{select_name}"', readonly=True)
            if typ_sel != "OK":
                continue
            typ_search, uid_data = client.uid("search", "ALL")
            if typ_search != "OK":
                continue
            remote_uids = sorted(int(u) for u in (uid_data[0] or b"").split() if u)
            folder_state = folders_state.get(folder_name, {})
            last_uid = int(folder_state.get("last_uid_synced", 0) or 0) if isinstance(folder_state, dict) else 0
            scan_uids = remote_uids if mode == "initial" else [u for u in remote_uids if u > last_uid]
            if limit_per_folder is not None:
                max_scan = max(0, int(limit_per_folder))
                if mode == "incremental":
                    scan_uids = scan_uids[:max_scan]
                else:
                    scan_uids = scan_uids[-max_scan:]
            folder_new = 0
            fetched_uids: list[int] = []
            for uid in scan_uids:
                try:
                    typ_fetch, fetched = client.uid("fetch", str(uid), "(UID BODY.PEEK[])")
                except (socket.timeout, TimeoutError, OSError, imaplib.IMAP4.error):
                    continue
                if typ_fetch != "OK":
                    continue
                raw_map = parse_fetch_uid_rfc822_map(fetched)
                raw_msg = raw_map.get(uid)
                if not raw_msg:
                    continue
                fetched_uids.append(uid)
                headers = parse_headers(raw_msg)
                raw_hash = hashlib.sha256(raw_msg).hexdigest()
                if not dry_run:
                    upsert_email_message(
                        conn,
                        account_id=account_id,
                        folder=folder_name,
                        folder_slug=folder_slug,
                        uid=uid,
                        uidvalidity=None,
                        message_id=headers["message_id"],
                        subject=headers["subject"],
                        sender=headers["from"],
                        recipient=headers["to"],
                        date_iso=headers["date"],
                        raw_sha256=raw_hash,
                        raw_eml=raw_msg,
                        attachments=extract_attachments(raw_msg),
                        ingest_ts=now_iso(),
                    )
                folder_new += 1
            if remote_uids and not dry_run:
                state_uid = max(remote_uids)
                if limit_per_folder is not None:
                    state_uid = max(last_uid, max(fetched_uids, default=last_uid))
                folders_state[folder_name] = {
                    "folder_name": folder_name,
                    "last_uid_synced": state_uid,
                    "last_sync_at": now_iso(),
                    "remote_count_last_seen": len(remote_uids),
                }
            summary["new_messages"] = int(summary["new_messages"]) + folder_new
            assert isinstance(summary["folders"], list)
            summary["folders"].append(
                {
                    "folder": folder_name,
                    "folder_slug": folder_slug,
                    "remote_count": len(remote_uids),
                    "scanned": len(scan_uids),
                    "new_messages": folder_new,
                }
            )
        if not dry_run:
            dump_json(state_path, state)
        return summary
    finally:
        conn.close()
        try:
            client.logout()
        except Exception:
            pass
