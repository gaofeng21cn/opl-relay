from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlencode

from .config import load_accounts_config
from .message import extract_text_body
from .paths import default_profile_workspace
from .reply_context import reply_context
from .server_drafts import _ledger, content_snapshot, server_draft
from .store import (
    connect_email_store_readonly, fetch_raw_email_by_storage_ref,
    get_message_by_storage_ref, list_messages, search_messages,
)


def _now():
    return datetime.now(timezone.utc).isoformat()


class Workbench:
    def __init__(self, *, config_path, db_path, ledger_path, profile_path=None):
        self.config_path = Path(config_path)
        self.db_path = Path(db_path)
        self.ledger_path = Path(ledger_path)
        self.profile = Path(profile_path or default_profile_workspace()).resolve()
        self.attachments_root = self.profile / "data" / "relay" / "workbench-attachments"

    def _accounts(self):
        return load_accounts_config(self.config_path) if self.config_path.exists() else {}

    def _account(self, account_id):
        if account_id not in self._accounts():
            raise ValueError("请选择已配置的邮箱")
        return self._accounts()[account_id]

    def _conn(self, *, readonly=False):
        if readonly:
            conn = sqlite3.connect(self.ledger_path.resolve().as_uri() + "?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            return conn
        conn = _ledger(self.ledger_path)
        conn.execute("""CREATE TABLE IF NOT EXISTS workbench_edits (
            id TEXT PRIMARY KEY, account_id TEXT NOT NULL, source_ref TEXT NOT NULL,
            body TEXT NOT NULL, attachment_ids TEXT NOT NULL, revision INTEGER NOT NULL,
            state TEXT NOT NULL, server_request_id TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS workbench_attachments (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, sha256 TEXT NOT NULL,
            size INTEGER NOT NULL, relative_path TEXT NOT NULL)""")
        conn.commit()
        return conn

    def _source(self, account_id, storage_ref):
        self._account(account_id)
        conn = connect_email_store_readonly(self.db_path)
        if conn is None:
            raise ValueError("尚无邮件缓存，请先同步邮箱")
        try:
            meta = get_message_by_storage_ref(conn, storage_ref)
            raw = fetch_raw_email_by_storage_ref(conn, storage_ref)
        finally:
            conn.close()
        if not meta or meta["account_id"] != account_id or not raw:
            raise ValueError("原邮件不属于所选邮箱")
        if hashlib.sha256(raw).hexdigest() != meta["raw_sha256"]:
            raise ValueError("原邮件校验失败")
        return meta, raw, BytesParser(policy=policy.default).parsebytes(raw)

    def accounts(self, **_):
        return {"accounts": [{"account_id": item.account_id, "email": item.email}
                             for item in self._accounts().values()]}

    def messages(self, *, account_id, query="", scope="active", folder="", limit=50):
        self._account(account_id)
        if scope not in {"active", "history"} or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("无效的邮件查询范围")
        conn = connect_email_store_readonly(self.db_path)
        if conn is None:
            return {"messages": [], "freshness": "尚未同步"}
        try:
            if folder in {"inbox", "sent"}:
                names = [row[0] for row in conn.execute(
                    "SELECT folder FROM mail_folders WHERE account_id=? AND role=?", (account_id, folder))]
                if len(names) > 1:
                    raise ValueError("该邮箱存在多个同类文件夹，请指定精确文件夹")
                folder = names[0] if names else ("INBOX" if folder == "inbox" else "Sent Items")
            args = dict(account_ids=[account_id], scope=scope, folder_slug=folder or None, limit=limit)
            rows = (search_messages(conn, queries=[query], include_body=True, **args)
                    if query.strip() else list_messages(conn, **args))
            return {"messages": rows, "freshness": "本地同步快照", "limit": limit}
        finally:
            conn.close()

    def read(self, *, account_id, storage_ref):
        meta, raw, message = self._source(account_id, storage_ref)
        html = message.get_body(preferencelist=("html",))
        attachments = [{"index": index, "name": part.get_filename() or "attachment",
                        "type": part.get_content_type(), "size": len(part.get_payload(decode=True) or b"")}
                       for index, part in enumerate(message.iter_attachments())]
        return {"message": {**meta, "headers": content_snapshot(raw)["headers"],
                            "body_text": extract_text_body(raw),
                            "body_html": html.get_content() if html else "",
                            "attachments": attachments}}

    def sync(self, *, account_id):
        self._account(account_id)
        from .sync import sync_account
        return sync_account(config_path=self.config_path, db_path=self.db_path,
                            account_id=account_id, scope="active", max_duration_sec=60)

    def drafts(self, *, account_id):
        self._account(account_id)
        if not self.ledger_path.exists():
            return {"drafts": []}
        conn = self._conn(readonly=True)
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            items = [dict(row) for row in conn.execute(
                "SELECT id,revision,state,updated_at,source_ref,server_request_id FROM workbench_edits "
                "WHERE account_id=? ORDER BY updated_at DESC LIMIT 100", (account_id,))] if "workbench_edits" in tables else []
            local_requests = {row["server_request_id"] for row in items}
            if "server_draft_requests" not in tables:
                return {"drafts": items}
            for row in conn.execute("SELECT request_id,state,raw FROM server_draft_requests "
                                    "WHERE account_id=? AND state IN ('verified','attempted','sent') "
                                    "ORDER BY rowid DESC LIMIT 100", (account_id,)):
                if row["request_id"] in local_requests:
                    continue
                message = BytesParser(policy=policy.default).parsebytes(bytes(row["raw"]))
                items.append({"id": row["request_id"], "server_request_id": row["request_id"],
                              "state": "server", "subject": str(message.get("Subject", ""))})
            return {"drafts": items}
        finally:
            conn.close()

    def reply(self, *, account_id, storage_ref, body="", request_id):
        self._source(account_id, storage_ref)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,70}", request_id):
            raise ValueError("请提供稳定的草稿请求标识")
        if not isinstance(body, str) or len(body) > 200000:
            raise ValueError("草稿正文过长")
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM workbench_edits WHERE id=?", (request_id,)).fetchone()
            if row:
                if row["account_id"] != account_id or row["source_ref"] != storage_ref:
                    raise ValueError("请求标识已用于另一封邮件")
            else:
                conn.execute("INSERT INTO workbench_edits VALUES (?,?,?,?,?,?,?,?,?)",
                             (request_id, account_id, storage_ref, body, "[]", 1, "editing",
                              "wb-" + uuid.uuid4().hex, _now()))
                conn.commit()
        finally:
            conn.close()
        return self.draft(account_id=account_id, draft_id=request_id)

    def _edit(self, conn, account_id, draft_id):
        row = conn.execute("SELECT * FROM workbench_edits WHERE id=? AND account_id=?",
                           (draft_id, account_id)).fetchone()
        if row is None:
            raise ValueError("找不到该邮箱的编辑草稿")
        return dict(row)

    def _attachment(self, conn, attachment_id):
        row = conn.execute("SELECT * FROM workbench_attachments WHERE id=?", (attachment_id,)).fetchone()
        if row is None:
            raise ValueError("附件不存在，请重新添加")
        path = (self.attachments_root / row["relative_path"]).resolve()
        if not path.is_relative_to(self.attachments_root.resolve()):
            raise ValueError("附件路径校验失败")
        content = path.read_bytes()
        if len(content) != row["size"] or hashlib.sha256(content).hexdigest() != row["sha256"]:
            raise ValueError("附件内容发生变化，请重新添加")
        return {"id": row["id"], "name": row["name"], "size": row["size"], "sha256": row["sha256"]}, path

    def draft(self, *, account_id, draft_id):
        self._account(account_id)
        conn = self._conn(readonly=True)
        try:
            edit = self._edit(conn, account_id, draft_id)
            edit["attachment_ids"] = json.loads(edit["attachment_ids"])
            edit["attachments"] = [self._attachment(conn, value)[0] for value in edit["attachment_ids"]]
        finally:
            conn.close()
        context = reply_context(config_path=self.config_path, db_path=self.db_path,
                                account_id=account_id, storage_ref=edit["source_ref"])
        signature_path = self.profile / "templates" / "mail-signature.txt"
        signature = signature_path.read_text(encoding="utf-8").strip() if signature_path.exists() else ""
        edit.update(to=context["reply_to"], cc=context["reply_cc"],
                    subject=context["headers"]["Subject"], quote=context["body_text"],
                    signature=signature, from_address=self._account(account_id).email)
        return {"draft": edit}

    def save(self, *, account_id, draft_id, revision, body, attachment_ids):
        self._account(account_id)
        if type(revision) is not int or not isinstance(body, str) or len(body) > 200000:
            raise ValueError("无效的草稿内容或版本")
        if (not isinstance(attachment_ids, list) or len(attachment_ids) > 10
                or not all(isinstance(value, str) for value in attachment_ids)
                or len(set(attachment_ids)) != len(attachment_ids)):
            raise ValueError("每封邮件最多十个不同附件")
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = self._edit(conn, account_id, draft_id)
            if row["state"] != "editing" or row["revision"] != revision:
                raise ValueError("草稿已被更新或提交，请重新读取；当前输入仍在编辑器中")
            attachments = [self._attachment(conn, value)[0] for value in attachment_ids]
            if sum(value["size"] for value in attachments) > 25 * 1024 * 1024:
                raise ValueError("附件总大小不能超过25 MiB")
            conn.execute("UPDATE workbench_edits SET body=?,attachment_ids=?,revision=revision+1,updated_at=? WHERE id=?",
                         (body, json.dumps(attachment_ids), _now(), draft_id))
            conn.commit()
        finally:
            conn.close()
        return self.draft(account_id=account_id, draft_id=draft_id)

    def upload(self, *, name, data, sha256):
        if (not isinstance(name, str) or not name or name in {".", ".."}
                or Path(name).name != name or "\\" in name or len(name) > 200):
            raise ValueError("无效的附件文件名")
        if not isinstance(data, str) or len(data) > 14 * 1024 * 1024:
            raise ValueError("单个附件不能超过10 MiB")
        content = base64.b64decode(data, validate=True)
        digest = hashlib.sha256(content).hexdigest()
        if len(content) > 10 * 1024 * 1024 or digest != sha256:
            raise ValueError("附件字节或校验和不一致")
        identity = hashlib.sha256((name + ":" + digest).encode()).hexdigest()
        relative = Path(identity) / name
        path = self.attachments_root / relative
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not path.exists():
            with path.open("xb") as handle:
                os.chmod(path, 0o600)
                handle.write(content)
        conn = self._conn()
        try:
            conn.execute("INSERT OR IGNORE INTO workbench_attachments VALUES (?,?,?,?,?)",
                         (identity, name, digest, len(content), str(relative)))
            conn.commit()
            attachment = self._attachment(conn, identity)[0]
        finally:
            conn.close()
        return {"attachment": attachment}

    def attachment(self, *, account_id, storage_ref, index):
        _, _, message = self._source(account_id, storage_ref)
        parts = list(message.iter_attachments())
        if type(index) is not int or not 0 <= index < len(parts):
            raise ValueError("附件索引无效")
        part = parts[index]
        content = part.get_payload(decode=True) or b""
        if len(content) > 10 * 1024 * 1024:
            raise ValueError("该附件超过工作台预览限制")
        return {"attachment": {"name": part.get_filename() or "attachment",
                               "type": part.get_content_type(), "size": len(content),
                               "data": base64.b64encode(content).decode()}}

    def uploaded_attachment(self, *, attachment_id):
        conn = self._conn(readonly=True)
        try:
            meta, path = self._attachment(conn, attachment_id)
            return {"attachment": {**meta, "type": mimetypes.guess_type(meta["name"])[0] or "application/octet-stream",
                                   "data": base64.b64encode(path.read_bytes()).decode()}}
        finally:
            conn.close()

    def publish(self, *, account_id, draft_id, revision):
        draft = self.draft(account_id=account_id, draft_id=draft_id)["draft"]
        if type(revision) is not int or draft["revision"] != revision:
            raise ValueError("草稿版本已改变，请重新审核")
        if not draft["body"].strip() or not draft["signature"]:
            raise ValueError("请填写回复正文，并配置批准的英文签名")
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = self._edit(conn, account_id, draft_id)
            if row["revision"] != revision:
                raise ValueError("草稿版本已改变，请重新审核")
            inspect = row["state"] != "editing"
            paths = [self._attachment(conn, value)[1] for value in draft["attachment_ids"]]
            conn.execute("UPDATE workbench_edits SET state='publishing',updated_at=? WHERE id=?", (_now(), draft_id))
            conn.commit()
        finally:
            conn.close()
        try:
            result = server_draft(config_path=self.config_path, db_path=self.db_path,
                                  ledger_path=self.ledger_path, account_id=account_id,
                                  request_id=draft["server_request_id"], source_ref=draft["source_ref"],
                                  body=draft["body"], signature=draft["signature"], attachments=paths,
                                  apply=not inspect, inspect=inspect)
        except ValueError:
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                remote = conn.execute("SELECT state FROM server_draft_requests WHERE request_id=?",
                                      (draft["server_request_id"],)).fetchone()
                if not inspect and (remote is None or remote["state"] == "prepared"):
                    conn.execute("UPDATE workbench_edits SET state='editing' WHERE id=?", (draft_id,))
                    conn.commit()
            finally:
                conn.close()
            raise
        conn = self._conn()
        try:
            conn.execute("UPDATE workbench_edits SET state='published',updated_at=? WHERE id=?", (_now(), draft_id))
            conn.commit()
        finally:
            conn.close()
        return {"server_draft": self._server_result(account_id, draft["server_request_id"], result)}

    def _server_result(self, account_id, request_id, result):
        conn = _ledger(self.ledger_path)
        try:
            row = conn.execute("SELECT raw FROM server_draft_requests WHERE request_id=? AND account_id=?",
                               (request_id, account_id)).fetchone()
            if not row:
                raise ValueError("找不到该邮箱的服务器草稿")
            message = BytesParser(policy=policy.default).parsebytes(bytes(row["raw"]))
            attachments = [{"name": part.get_filename() or "attachment",
                            "size": len(part.get_payload(decode=True) or b"")}
                           for part in message.iter_attachments()]
            return {**result, "attachments": attachments}
        finally:
            conn.close()

    def inspect(self, *, account_id, request_id):
        self._account(account_id)
        result = server_draft(config_path=self.config_path, db_path=self.db_path,
                              ledger_path=self.ledger_path, account_id=account_id,
                              request_id=request_id, inspect=True)
        return {"server_draft": self._server_result(account_id, request_id, result)}

    def send(self, *, account_id, request_id, approval, confirmed):
        if confirmed is not True:
            raise ValueError("请先完整审核当前草稿，再明确确认发送")
        self._account(account_id)
        from .server_send import send_server_draft
        result = send_server_draft(config_path=self.config_path, ledger_path=self.ledger_path,
                                  account_id=account_id, request_id=request_id, approval=approval)
        return {"delivery": result}

    def dispatch(self, action, payload):
        actions = {name: getattr(self, name) for name in (
            "accounts", "messages", "read", "sync", "drafts", "reply", "draft", "save",
            "upload", "attachment", "uploaded_attachment", "publish", "inspect", "send")}
        if action not in actions or not isinstance(payload, dict):
            raise ValueError("不支持的工作台操作")
        return {"ok": True, **actions[action](**payload)}


def bundle_root():
    return Path(__file__).resolve().parents[2] / "workbench"


def connection_config(*, host="generic", output=None):
    from . import __version__

    if host not in {"generic", "codex", "dsh", "opl"}:
        raise ValueError("不支持的工作台宿主")
    package = bundle_root().parent
    entry = {"command": sys.executable,
             "args": [str(package / "bin" / "opl-relay"), "--json", "workbench", "mcp"],
             "env": {"OPL_PROFILE_WORKSPACE": str(default_profile_workspace().resolve())}}
    if host == "dsh":
        manifest = {"name": "@one-person-lab/opl-relay-dsh", "version": __version__,
                    "private": True, "type": "module", "license": "Apache-2.0",
                    "files": ["cordis.patch.yml"], "dsh": {"bundle": {"patch": "./cordis.patch.yml"}}}
        patch = [{"insert": [{"id": "opl-relay-mcp", "name": "@deepseek-ai/dsh-mcp-client",
                              "config": {"serverName": "opl-relay", "transport": "stdio",
                                         **entry, "failOnStartupError": True}}]}]
        config = {"package.json": manifest, "cordis.patch.yml": patch}
    else:
        config = {"mcp_servers" if host == "codex" else "mcpServers": {"opl-relay": entry}}
    result = {"ok": True, "host": host, "config": config}
    if output:
        if host != "dsh":
            raise ValueError("仅 DSH 配置包支持 --output；其他宿主读取输出的连接配置")
        target = Path(output).expanduser().resolve()
        if target.exists() and any(target.iterdir()):
            raise ValueError("配置包目录必须为空，避免覆盖现有安装或用户配置")
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name, content in config.items():
            path = target / name
            with path.open("x", encoding="utf-8") as handle:
                os.chmod(path, 0o600)
                handle.write(json.dumps(content, ensure_ascii=False, indent=2) + "\n")
        result["bundle_path"] = str(target)
    return result


def open_workbench(*, focus=None):
    profile = default_profile_workspace().resolve()
    bundle = bundle_root()
    bundle_hash = hashlib.sha256((bundle / "server.mjs").read_bytes() + (bundle / "index.html").read_bytes()).hexdigest()
    state = profile / "data" / "relay" / "workbench-server.json"
    if state.exists():
        try:
            saved = json.loads(state.read_text())
            request = Request(saved["url"].split("#")[0] + "health", headers={"Authorization": "Bearer " + saved["token"]})
            with urlopen(request, timeout=2) as response:
                health = json.load(response)
                if health.get("profile") == str(profile):
                    if health.get("bundle_sha256") == bundle_hash and health.get("cli") == str(bundle.parent / "bin" / "opl-relay"):
                        return _presentation(saved, focus)
                    if health.get("pid") == saved["pid"]:
                        os.kill(saved["pid"], signal.SIGTERM)
        except (OSError, ValueError, KeyError):
            pass
    state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    log = state.parent / "workbench-server.log"
    with log.open("ab") as handle:
        os.chmod(log, 0o600)
        process = subprocess.Popen(["node", str(bundle_root() / "server.mjs"), "--serve",
                                    "--python", sys.executable, "--ready-file", str(state)],
                                   stdin=subprocess.DEVNULL, stdout=handle, stderr=handle,
                                   start_new_session=True, env={**os.environ, "OPL_PROFILE_WORKSPACE": str(profile)})
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ValueError("工作台未启动，请检查 Node.js 与私有运行日志")
        if state.exists():
            saved = json.loads(state.read_text())
            if saved.get("pid") == process.pid:
                return _presentation(saved, focus)
        time.sleep(0.1)
    raise ValueError("工作台启动尚未确认，请先检查现有进程再重试")


def _presentation(saved, focus):
    url = saved["url"]
    if focus:
        base, token = url.split("#", 1)
        url = base + "?" + urlencode(focus) + "#" + token
    return {"ok": True, "url": url, "pid": saved["pid"]}
