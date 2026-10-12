"""Microsoft personal/work mail through delegated OAuth and Graph.

Provider identities/cursors live in Relay's existing private mail database;
tokens live only in Keychain. No password, application secret or send permission
is needed. All requests opt into Microsoft's case-sensitive immutable IDs.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import MailAccount, keychain_get_secret
from .keychain import set_oauth_secret
from .message import extract_attachments, parse_headers
from .store import (connect_email_store, fetch_raw_email_by_storage_ref,
                    get_message_by_storage_ref, reconcile_folder,
                    record_mailbox_move, upsert_email_message)

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
READ_SCOPES = ("User.Read", "Mail.Read", "offline_access")
WRITE_SCOPES = ("User.Read", "Mail.ReadWrite", "offline_access")


class GraphError(RuntimeError):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code
        super().__init__(f"Microsoft request failed ({status}, {code})")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward credentials to an unexpected host.


def _http(url, *, method="GET", headers=None, body=None, timeout=30, raw=False):
    request = Request(url, data=body, headers=headers or {}, method=method)
    try:
        with build_opener(NoRedirect).open(request, timeout=max(0.01, timeout)) as response:
            value = response.read()
            return value if raw else (json.loads(value) if value else {})
    except HTTPError as exc:
        # Error descriptions may echo identifiers or credentials. Return codes
        # only; do not propagate raw bodies/URLs into logs.
        try:
            error = json.loads(exc.read()).get("error", "http_error")
            code = error.get("code", "http_error") if isinstance(error, dict) else error
        except (ValueError, AttributeError):
            code = "http_error"
        raise GraphError(exc.code, str(code)) from None
    except (URLError, TimeoutError, OSError):
        raise GraphError(0, "network_unavailable") from None


def _authority(account):
    if account.graph is None:
        raise ValueError("Microsoft Graph account required")
    return f"https://login.microsoftonline.com/{account.graph.tenant}/oauth2/v2.0"


def _token_request(account, parameters):
    return _http(_authority(account) + "/token", method="POST",
                 headers={"Content-Type": "application/x-www-form-urlencoded"},
                 body=urlencode({"client_id": account.graph.client_id, **parameters}).encode())


def _save_token(account, token):
    if not token.get("access_token") or not token.get("refresh_token"):
        raise ValueError("Microsoft did not return renewable delegated credentials")
    token = {**token, "expires_at": time.time() + int(token.get("expires_in", 3600)),
             "client_id": account.graph.client_id, "email": account.email.lower()}
    serialized = json.dumps(token, separators=(",", ":"))
    set_oauth_secret(account.graph.credential_ref, serialized)
    if keychain_get_secret(account.graph.credential_ref) != serialized:
        raise RuntimeError("OAuth Keychain readback mismatch")


def login_graph(account: MailAccount, *, write=False, notify=None, timeout=600):
    """PKCE authorization-code flow; the browser receives no client secret.

    notify receives the login URL. Bind only loopback, validate Host and state,
    suppress access logs, and save tokens only after /me matches this account.
    """
    authority = _authority(account)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(32)
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            query = parse_qs(urlsplit(self.path).query)
            valid = (urlsplit(self.path).path == "/" and
                     self.headers.get("Host") == f"localhost:{self.server.server_port}" and
                     secrets.compare_digest(query.get("state", [""])[0], state))
            if valid:
                result.update(code=query.get("code", [""])[0], error=query.get("error", [""])[0])
            self.send_response(200 if valid else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(("Microsoft login received. You can return to Codex." if valid
                              else "Invalid login callback.").encode())

    with HTTPServer(("127.0.0.1", 0), Callback) as server:
        redirect = f"http://localhost:{server.server_port}"
        scopes = WRITE_SCOPES if write else READ_SCOPES
        url = authority + "/authorize?" + urlencode({
            "client_id": account.graph.client_id, "response_type": "code",
            "redirect_uri": redirect, "response_mode": "query", "scope": " ".join(scopes),
            "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
            "login_hint": account.email, "prompt": "select_account",
        })
        if notify is not None:
            notify(url)
        deadline = time.monotonic() + timeout
        while not result and time.monotonic() < deadline:
            server.timeout = min(1, max(0.01, deadline - time.monotonic()))
            server.handle_request()
    if not result.get("code"):
        raise ValueError("Microsoft login was cancelled or timed out")
    token = _token_request(account, {"grant_type": "authorization_code", "code": result["code"],
                                   "redirect_uri": redirect, "code_verifier": verifier,
                                   "scope": " ".join(scopes)})
    client = GraphClient(account, token=token["access_token"])
    client.identity()
    _save_token(account, token)
    return {"ok": True, "account": account.account_id, "provider": "microsoft_graph",
            "email": account.email, "credential_configured": True,
            "scopes": token.get("scope", "").split(), "send_allowed": False}


class GraphClient:
    def __init__(self, account, *, token=None, deadline=None):
        self.account, self.deadline = account, deadline
        self.token = token
        self.scopes = set()

    def _access_token(self, *, force_refresh=False):
        if self.token and not force_refresh:
            return self.token
        # Serialize rotation of the same refresh token across background hosts.
        from .paths import default_state_dir
        path = default_state_dir() / (self.account.account_id + ".oauth.lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as lock:
            lock_deadline = self.deadline or time.monotonic() + 30
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = lock_deadline - time.monotonic()
                    if remaining <= 0:
                        raise GraphError(0, "oauth_refresh_busy") from None
                    time.sleep(min(0.1, remaining))
            token = json.loads(keychain_get_secret(self.account.graph.credential_ref))
            if (token.get("client_id") != self.account.graph.client_id or
                    token.get("email") != self.account.email.lower()):
                raise ValueError("OAuth credentials belong to a different Relay account/application")
            if force_refresh or token.get("expires_at", 0) < time.time() + 120:
                fresh = _token_request(self.account, {"grant_type": "refresh_token",
                                                     "refresh_token": token["refresh_token"]})
                fresh.setdefault("refresh_token", token["refresh_token"])
                fresh.setdefault("scope", token.get("scope", ""))
                _save_token(self.account, fresh)
                token = fresh
            self.scopes = set(token.get("scope", "").lower().split())
            self.token = token["access_token"]
        return self.token

    def request(self, path, *, method="GET", data=None, raw=False):
        url = path if path.startswith("https://") else GRAPH_ROOT + path
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname != "graph.microsoft.com" or
                parsed.port not in {None, 443} or parsed.username or parsed.password or
                not parsed.path.startswith("/v1.0/")):
            raise ValueError("Refusing an off-origin Microsoft Graph continuation URL")
        remaining = self.deadline - time.monotonic() if self.deadline else 30
        if remaining <= 0:
            raise GraphError(0, "sync_time_budget_exceeded")
        if method != "GET":
            if method == "POST" and (parsed.path == "/v1.0/me/messages" or parsed.path.endswith("/move")):
                from .native import Himalaya, NativeError
                tool = Himalaya(self.account, token=self._access_token(), deadline=self.deadline)
                try:
                    if parsed.path.endswith("/move"):
                        from urllib.parse import unquote
                        remote_id = unquote(parsed.path.split("/")[-2])
                        moved = tool.move(remote_id, destination=data["destinationId"])
                        # The native move returns a new movable ID. Resolve that
                        # receipt, since the previous ID may no longer exist.
                        return self.request(f"/me/messages/{quote(moved['id'], safe='')}?$select=id")
                    created = tool.create_draft(base64.b64decode(data, validate=True))
                    # Himalaya returns movable Graph IDs. Bind the durable draft
                    # to ImmutableId using a read-only Graph owner lookup.
                    identity = self.request(f"/me/messages/{quote(created['id'], safe='')}?$select=id")
                    return {**created, "id": identity["id"]}
                except NativeError as exc:
                    raise GraphError(0, "native_" + exc.code) from None
            raise ValueError("Unsupported Graph mutation; use the typed mail backend")
        headers = {"Authorization": "Bearer " + self._access_token(),
                   "Prefer": 'IdType="ImmutableId", odata.maxpagesize=100',
                   "Accept": "application/octet-stream" if raw else "application/json"}
        body = data if isinstance(data, bytes) else json.dumps(data).encode() if data is not None else None
        if body is not None:
            headers["Content-Type"] = "text/plain" if isinstance(data, bytes) else "application/json"
        return _http(url, method=method, headers=headers, body=body,
                     timeout=min(30, remaining), raw=raw)

    def identity(self):
        value = self.request("/me?$select=id,mail,userPrincipalName")
        addresses = {str(value.get(k) or "").lower() for k in ("mail", "userPrincipalName")}
        if self.account.email.lower() not in addresses:
            raise ValueError("Signed-in Microsoft mailbox does not match the configured email")
        return {"email": self.account.email, "verified": True}

    def pages(self, path):
        while path:
            page = self.request(path)
            yield from page.get("value", [])
            path = page.get("@odata.nextLink")

    def folders(self):
        roles = {}
        for name, canonical in (("inbox", "INBOX"), ("sentitems", "Sent Items"),
                                ("archive", "Archive"), ("deleteditems", "Deleted Items"),
                                ("junkemail", "Junk Email"), ("drafts", "Drafts")):
            try:
                item = self.request(f"/me/mailFolders/{name}?$select=id")
                roles[item["id"]] = canonical
            except GraphError as exc:
                if exc.status != 404:
                    raise
        items = []

        def visit(path, prefix=""):
            for item in self.pages(path):
                name = roles.get(item["id"], item["displayName"])
                item = {**item, "path": prefix + name}
                items.append(item)
                if item.get("childFolderCount"):
                    visit(f"/me/mailFolders/{quote(item['id'], safe='')}/childFolders?$top=100",
                          item["path"] + "/")

        visit("/me/mailFolders?$top=100")
        return items

    def metadata(self, remote_id):
        return self.request(f"/me/messages/{quote(remote_id, safe='')}?$select=id,parentFolderId,internetMessageId,changeKey")

    def mime(self, remote_id):
        from .native import Himalaya, NativeError
        try:
            return Himalaya(self.account, token=self._access_token(), deadline=self.deadline).mime(remote_id)
        except NativeError as exc:
            raise GraphError(0, "native_" + exc.code) from None


def _schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS graph_message_ids (
          uid INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
          remote_id TEXT NOT NULL, UNIQUE(account_id,remote_id));
        CREATE TABLE IF NOT EXISTS graph_folder_members (
          account_id TEXT NOT NULL, folder_id TEXT NOT NULL, remote_id TEXT NOT NULL,
          change_key TEXT NOT NULL, generation INTEGER NOT NULL,
          PRIMARY KEY(account_id,folder_id,remote_id));
        CREATE TABLE IF NOT EXISTS graph_folder_cursors (
          account_id TEXT NOT NULL, folder_id TEXT NOT NULL,
          link TEXT NOT NULL DEFAULT '', generation INTEGER NOT NULL DEFAULT 1,
          baseline INTEGER NOT NULL DEFAULT 1,
          PRIMARY KEY(account_id,folder_id));
    """)


def _uid(conn, account, remote_id):
    conn.execute("INSERT OR IGNORE INTO graph_message_ids(account_id,remote_id) VALUES (?,?)", (account, remote_id))
    return conn.execute("SELECT uid FROM graph_message_ids WHERE account_id=? AND remote_id=?", (account, remote_id)).fetchone()[0]


def _slug(item):
    from .folders import legacy_folder_slug
    # Graph already has an immutable folder-id suffix; keep its issued identities.
    return legacy_folder_slug(item["path"]) + "--" + hashlib.sha256(item["id"].encode()).hexdigest()[:10]


def _now():
    return datetime.now(timezone.utc).isoformat()


def _store_mime(conn, account, item, remote_id, raw):
    headers = parse_headers(raw)
    uid = _uid(conn, account, remote_id)
    return upsert_email_message(conn, account_id=account, folder=item["path"], folder_slug=_slug(item),
                                uid=uid, uidvalidity=1, message_id=headers["message_id"],
                                subject=headers["subject"], sender=headers["from"],
                                recipient=headers["to"], date_iso=headers["date"],
                                raw_sha256=hashlib.sha256(raw).hexdigest(), raw_eml=raw,
                                attachments=extract_attachments(raw), ingest_ts=_now())


def sync_graph_account(*, account, db_path, state_dir, mode="incremental", limit_per_folder=None,
                       dry_run=False, scope="all", folder="", max_duration_sec=120):
    from .sync import should_sync_folder
    from .store import ACTIVE_FOLDERS, HISTORY_FOLDERS
    seconds = 120 if max_duration_sec is None else float(max_duration_sec)
    if seconds <= 0:
        raise ValueError("max_duration_sec must be greater than zero")
    client = GraphClient(account, deadline=time.monotonic() + seconds)
    summary = {"ok": True, "account": account.account_id, "provider": "microsoft_graph",
               "dry_run": dry_run, "mode": mode, "scope": scope, "folders": [],
               "new_messages": 0, "timed_out": False, "max_duration_sec": seconds,
               "next_folders": []}
    conn = None
    try:
        client.identity()
        all_folders = client.folders()
        selected = [f for f in all_folders if should_sync_folder(f["path"], account.include_folders, account.exclude_folders)]
        if folder:
            selected = [f for f in selected if folder in {f["path"], _slug(f)}]
            if not selected:
                raise ValueError("requested folder not found in configured Graph scope")
        elif scope != "all":
            names = ACTIVE_FOLDERS if scope == "active" else HISTORY_FOLDERS
            selected = [f for f in selected if f["path"].lower() in names or
                        (scope == "history" and f["path"].lower().startswith(("archive/", "archives/", "bill/", "bill ")))]
        if dry_run:
            summary["folders"] = [{"folder": f["path"], "folder_slug": _slug(f),
                                   "remote_count": f.get("totalItemCount", 0), "fetch_skipped": True,
                                   "complete": False} for f in selected]
            return summary
        conn = connect_email_store(db_path)
        _schema(conn)
        for index, item in enumerate(selected):
            summary["next_folders"] = [f["path"] for f in selected[index:]]
            folder_id, slug = item["id"], _slug(item)
            conn.execute("UPDATE folder_snapshots SET complete=0 WHERE account_id=? AND folder_slug=?", (account.account_id, slug))
            conn.execute("INSERT OR IGNORE INTO graph_folder_cursors(account_id,folder_id) VALUES (?,?)", (account.account_id, folder_id))
            conn.commit()
            cursor = conn.execute("SELECT link,generation,baseline FROM graph_folder_cursors WHERE account_id=? AND folder_id=?", (account.account_id, folder_id)).fetchone()
            link, generation, baseline = cursor
            initial = f"/me/mailFolders/{quote(folder_id, safe='')}/messages/delta?$select=id,parentFolderId,changeKey"
            link = link or initial
            fetched, complete, errors, reset = 0, False, [], False
            try:
                while True:
                    try:
                        page = client.request(link)
                    except GraphError as exc:
                        if exc.status == 410 and not reset:
                            generation, baseline, reset = generation + 1, 1, True
                            link = initial
                            conn.execute("UPDATE graph_folder_cursors SET link='',generation=?,baseline=1 WHERE account_id=? AND folder_id=?", (generation, account.account_id, folder_id))
                            conn.commit()
                            continue
                        raise
                    entries = page.get("value", [])
                    need_raw = []
                    for entry in entries:
                        if "@removed" in entry:
                            continue
                        known = conn.execute("SELECT change_key FROM graph_folder_members WHERE account_id=? AND folder_id=? AND remote_id=?", (account.account_id, folder_id, entry["id"])).fetchone()
                        if not known or not entry.get("changeKey") or known[0] != entry["changeKey"]:
                            need_raw.append(entry["id"])
                    if limit_per_folder is not None:
                        need_raw = need_raw[:max(0, limit_per_folder - fetched)]
                    # Outlook limits concurrent mailbox requests. Keep four
                    # reads in flight and all SQLite/cursor writes in this owner.
                    if need_raw:
                        client._access_token()
                    with ThreadPoolExecutor(max_workers=4) as pool:
                        pending = {remote_id: pool.submit(client.mime, remote_id) for remote_id in need_raw}
                        for remote_id in pending:
                            try:
                                pending[remote_id] = pending[remote_id].result()
                            except GraphError as exc:
                                pending[remote_id] = exc
                    for entry in entries:
                        remote_id = entry["id"]
                        if "@removed" in entry:
                            conn.execute("DELETE FROM graph_folder_members WHERE account_id=? AND folder_id=? AND remote_id=?", (account.account_id, folder_id, remote_id))
                            uid = conn.execute("SELECT uid FROM graph_message_ids WHERE account_id=? AND remote_id=?", (account.account_id, remote_id)).fetchone()
                            if uid:
                                conn.execute("UPDATE email_messages SET present=0 WHERE account_id=? AND folder_slug=? AND uid=?", (account.account_id, slug, uid[0]))
                            conn.commit()
                            continue
                        known = conn.execute("SELECT change_key FROM graph_folder_members WHERE account_id=? AND folder_id=? AND remote_id=?", (account.account_id, folder_id, remote_id)).fetchone()
                        change_key = entry.get("changeKey", "")
                        # An interrupted page is replayed; already persisted MIME
                        # is reused so a bounded run can eventually finish it.
                        if not known or not change_key or known[0] != change_key:
                            if limit_per_folder is not None and fetched >= limit_per_folder:
                                raise GraphError(0, "message_fetch_limit_reached")
                            raw = pending[remote_id]
                            if isinstance(raw, GraphError):
                                raise raw
                            _store_mime(conn, account.account_id, item, remote_id, raw)
                            fetched += 1
                        conn.execute("INSERT INTO graph_folder_members VALUES (?,?,?,?,?) ON CONFLICT(account_id,folder_id,remote_id) DO UPDATE SET change_key=excluded.change_key,generation=excluded.generation", (account.account_id, folder_id, remote_id, change_key, generation))
                        conn.commit()
                    next_link, delta_link = page.get("@odata.nextLink"), page.get("@odata.deltaLink")
                    if not next_link and not delta_link:
                        raise GraphError(0, "incomplete_delta_response")
                    link = next_link or delta_link
                    if delta_link:
                        if baseline:
                            conn.execute("DELETE FROM graph_folder_members WHERE account_id=? AND folder_id=? AND generation<>?", (account.account_id, folder_id, generation))
                        baseline, complete = 0, True
                    conn.execute("UPDATE graph_folder_cursors SET link=?,baseline=? WHERE account_id=? AND folder_id=?", (link, baseline, account.account_id, folder_id))
                    conn.commit()
                    if complete:
                        break
            except GraphError as exc:
                errors.append(str(exc))
                summary["ok"] = False
                summary["timed_out"] = exc.code == "sync_time_budget_exceeded" or time.monotonic() >= client.deadline
            if complete:
                uids = [r[0] for r in conn.execute("SELECT i.uid FROM graph_folder_members m JOIN graph_message_ids i ON i.account_id=m.account_id AND i.remote_id=m.remote_id WHERE m.account_id=? AND m.folder_id=?", (account.account_id, folder_id))]
                reconcile_folder(conn, account=account.account_id, folder=item["path"], folder_slug=slug,
                                 uidvalidity=1, uids=uids, checked_at=_now(), complete=True)
            summary["new_messages"] += fetched
            summary["folders"].append({"folder": item["path"], "folder_slug": slug, "remote_count": len(uids) if complete else item.get("totalItemCount", 0),
                                       "membership_verified": complete,
                                       "new_messages": fetched, "complete": complete, "errors": errors})
            if summary["timed_out"]:
                break
            summary["next_folders"] = [f["path"] for f in selected[index+1:]]
        return summary
    except GraphError as exc:
        return {**summary, "ok": False, "error": str(exc),
                "timed_out": exc.code == "sync_time_budget_exceeded"}
    finally:
        if conn is not None:
            conn.close()


def move_graph_messages(*, account, db_path, destination, storage_refs, apply, exact_folder=""):
    client = GraphClient(account)
    client.identity()
    folders = client.folders()
    names = {"archive": "Archive", "trash": "Deleted Items", "bill": "Bill"}
    target_name = exact_folder or names[destination]
    targets = [f for f in folders if f["path"] == target_name]
    if len(targets) != 1:
        raise ValueError("Graph destination folder is missing or ambiguous; no folders are created")
    target = targets[0]
    conn = connect_email_store(db_path)
    receipts, prepared = [], []
    try:
        _schema(conn)
        for ref in storage_refs:
            message = get_message_by_storage_ref(conn, ref)
            if not message or message["account_id"] != account.account_id or not message.get("present") or message.get("deleted"):
                raise ValueError("Graph move requires an exact current Relay reference for this account")
            row = conn.execute("SELECT remote_id FROM graph_message_ids WHERE account_id=? AND uid=?", (account.account_id, message["uid"])).fetchone()
            source = [f for f in folders if _slug(f) == message["folder_slug"]]
            if not row or len(source) != 1 or source[0]["id"] == target["id"]:
                raise ValueError("Graph move identity/source/target is invalid")
            remote_id = row[0]
            meta, raw = client.metadata(remote_id), client.mime(remote_id)
            if meta["parentFolderId"] != source[0]["id"] or hashlib.sha256(raw).hexdigest() != message["raw_sha256"]:
                raise ValueError("Graph source has changed; sync and review again")
            if parse_headers(raw)["message_id"] != message["message_id"]:
                raise ValueError("Graph Message-ID does not match Relay evidence")
            receipt = {"storage_ref": ref, "source_folder": message["folder"], "destination_folder": target_name,
                       "method": "graph_move", "status": "ready"}
            receipts.append(receipt)
            prepared.append((remote_id, source[0], message, raw, meta, receipt))
        result = {"ok": True, "phase": "apply" if apply else "preflight", "apply": apply,
                  "account": account.account_id, "destination_folder": target_name,
                  "requested": len(storage_refs), "ready": len(prepared), "moved": 0, "messages": receipts}
        if not apply:
            return result
        client._access_token()
        if not any(s in client.scopes for s in ("mail.readwrite", "https://graph.microsoft.com/mail.readwrite")):
            raise ValueError("Mail.ReadWrite consent required; run account login --write")
        for remote_id, source, message, raw, meta, receipt in prepared:
            try:
                if client.metadata(remote_id) != meta:
                    raise ValueError("Graph source changed after preflight")
                moved = client.request(f"/me/messages/{quote(remote_id, safe='')}/move", method="POST", data={"destinationId": target["id"]})
                target_id = moved.get("id")
                if not target_id:
                    raise ValueError("Graph move identity is missing")
                readback = client.metadata(target_id)
                if (readback["parentFolderId"] != target["id"] or
                        hashlib.sha256(client.mime(target_id)).hexdigest() != message["raw_sha256"]):
                    raise ValueError("Graph move readback is uncertain")
                query = urlencode({"$filter": "internetMessageId eq '" + message["message_id"].replace("'", "''") + "'",
                                   "$select": "id", "$top": "100"})
                if list(client.pages(f"/me/mailFolders/{quote(source['id'], safe='')}/messages?" + query)):
                    raise ValueError("Graph source absence is unconfirmed")
                receipt["operation_ref"] = record_mailbox_move(conn, account_id=account.account_id,
                        storage_ref=message["storage_ref"], source_folder=message["folder"], source_uid=message["uid"],
                        destination_folder=target_name, method="graph_move", raw_sha256=message["raw_sha256"], occurred_at=_now())
                receipt["destination_storage_ref"] = _store_mime(conn, account.account_id, target, target_id, raw)
                conn.execute("DELETE FROM graph_folder_members WHERE account_id=? AND folder_id=? AND remote_id=?", (account.account_id, source["id"], remote_id))
                conn.execute("UPDATE folder_snapshots SET complete=0 WHERE account_id=? AND folder_slug IN (?,?)", (account.account_id, _slug(source), _slug(target)))
                conn.commit()
                receipt["status"] = "moved"
                result["moved"] += 1
            except Exception:
                receipt["status"] = "unknown"
                result.update(ok=False, error="Move outcome requires read-only reconciliation; do not retry")
                break
        return result
    finally:
        conn.close()


def reconcile_graph_moves(*, account, db_path, destination, storage_refs, exact_folder=""):
    """Observe a bounded unknown result, record proof, never issue a POST."""
    client = GraphClient(account)
    client.identity()
    folders = client.folders()
    name = exact_folder or {"archive": "Archive", "trash": "Deleted Items", "bill": "Bill"}[destination]
    targets = [f for f in folders if f["path"] == name]
    if len(targets) != 1:
        raise ValueError("Reconciliation destination is missing or ambiguous")
    target = targets[0]
    conn = connect_email_store(db_path)
    entries = []
    try:
        _schema(conn)
        for ref in dict.fromkeys(storage_refs):
            message = get_message_by_storage_ref(conn, ref)
            if not message or message["account_id"] != account.account_id:
                raise ValueError("Reconciliation requires exact references for the selected account")
            binding = conn.execute("SELECT remote_id FROM graph_message_ids WHERE account_id=? AND uid=?",
                                   (account.account_id, message["uid"])).fetchone()
            if not binding:
                raise ValueError("Graph identity binding is missing")
            remote_id = binding[0]
            try:
                meta = client.metadata(remote_id)
            except GraphError as exc:
                if exc.status != 404:
                    raise
                # The native move can replace even the prior immutable binding.
                # Search only the approved target and require one exact identity;
                # the complete MIME hash is still verified below.
                query = urlencode({"$filter": "internetMessageId eq '" + message["message_id"].replace("'", "''") + "'",
                                   "$select": "id,parentFolderId", "$top": "100"})
                matches = list(client.pages(f"/me/mailFolders/{quote(target['id'], safe='')}/messages?" + query))
                if len(matches) != 1:
                    entries.append({"storage_ref": ref, "status": "unknown", "destination_folder": name})
                    continue
                remote_id, meta = matches[0]["id"], matches[0]
            raw = client.mime(remote_id)
            source = next((f for f in folders if _slug(f) == message["folder_slug"]), None)
            identical = (hashlib.sha256(raw).hexdigest() == message["raw_sha256"] and
                         parse_headers(raw)["message_id"] == message["message_id"])
            entry = {"storage_ref": ref, "status": "unknown", "destination_folder": name}
            entries.append(entry)
            if not identical:
                continue
            if meta["parentFolderId"] == target["id"]:
                if source:
                    query = urlencode({"$filter": "internetMessageId eq '" + message["message_id"].replace("'", "''") + "'",
                                       "$select": "id", "$top": "100"})
                    if list(client.pages(f"/me/mailFolders/{quote(source['id'], safe='')}/messages?" + query)):
                        continue
                previous = conn.execute("SELECT operation_ref FROM mailbox_operations WHERE storage_ref=? AND destination_folder=? AND raw_sha256=?",
                                        (ref, name, message["raw_sha256"])).fetchone()
                operation = previous[0] if previous else record_mailbox_move(conn,
                    account_id=account.account_id, storage_ref=ref, source_folder=message["folder"],
                    source_uid=message["uid"], destination_folder=name, method="graph_move_reconciled",
                    raw_sha256=message["raw_sha256"], occurred_at=_now())
                target_ref = _store_mime(conn, account.account_id, target, remote_id, raw)
                if source:
                    conn.execute("DELETE FROM graph_folder_members WHERE account_id=? AND folder_id=? AND remote_id=?",
                                 (account.account_id, source["id"], binding[0]))
                conn.execute("UPDATE folder_snapshots SET complete=0 WHERE account_id=? AND folder_slug IN (?,?)",
                             (account.account_id, message["folder_slug"], _slug(target)))
                conn.commit()
                entry.update(status="moved", operation_ref=operation, destination_storage_ref=target_ref)
            elif source and meta["parentFolderId"] == source["id"]:
                entry["status"] = "not_moved"
        return {"ok": all(e["status"] != "unknown" for e in entries), "phase": "reconcile",
                "remote_writes": 0, "messages": entries}
    finally:
        conn.close()
