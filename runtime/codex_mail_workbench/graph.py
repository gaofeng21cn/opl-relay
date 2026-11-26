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
import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import httpx
import msal

from .config import MailAccount
from .keychain import get_oauth_secret, set_oauth_secret
from .message import extract_attachments, parse_headers
from .schema import ensure_mail_schema
from .store import (
    connect_email_store,
    get_message_by_storage_ref,
    reconcile_folder,
    record_mailbox_move,
    upsert_email_message,
)

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
READ_SCOPES = ("User.Read", "Mail.Read")
WRITE_SCOPES = ("User.Read", "Mail.ReadWrite")
_MSAL_RESERVED_SCOPES = frozenset({"openid", "profile", "offline_access"})


class GraphError(RuntimeError):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code
        super().__init__(f"Microsoft request failed ({status}, {code})")


class _MsalHttpResponse:
    """Minimal response adapter for MSAL's http_client contract."""

    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise GraphError(self.status_code, "http_error")


class _HttpxNoRedirectClient:
    """Fail-closed httpx client for MSAL that never follows redirects."""

    def __init__(self, *, timeout: float = 30.0):
        self._timeout = max(0.01, float(timeout))

    def _send(self, method: str, url: str, *, params=None, data=None, headers=None, timeout=None):
        effective_timeout = max(0.01, float(timeout or self._timeout))
        try:
            with httpx.Client(follow_redirects=False, timeout=effective_timeout) as client:
                response = client.request(method, url, params=params, data=data, headers=headers)
        except (httpx.HTTPError, TimeoutError, OSError):
            raise GraphError(0, "network_unavailable") from None
        if 300 <= response.status_code < 400:
            raise GraphError(response.status_code, "redirect_blocked")
        return _MsalHttpResponse(response.status_code, response.text)

    def get(self, url: str, params=None, headers=None, **kwargs):
        return self._send("GET", url, params=params, headers=headers, timeout=kwargs.get("timeout"))

    def post(self, url: str, params=None, data=None, headers=None, **kwargs):
        return self._send("POST", url, params=params, data=data, headers=headers, timeout=kwargs.get("timeout"))


def _extract_error_code(response: httpx.Response) -> str:
    # Error descriptions may echo identifiers or credentials. Return codes
    # only; do not propagate raw bodies/URLs into logs.
    if 300 <= response.status_code < 400:
        return "redirect_blocked"
    try:
        error = response.json().get("error", "http_error")
        code = error.get("code", "http_error") if isinstance(error, dict) else error
    except (ValueError, AttributeError):
        code = "http_error"
    return str(code or "http_error")


def _http(url, *, method="GET", headers=None, body=None, timeout=30, raw=False):
    try:
        with httpx.Client(follow_redirects=False, timeout=max(0.01, float(timeout))) as client:
            response = client.request(method, url, headers=headers or {}, content=body)
    except (httpx.HTTPError, TimeoutError, OSError):
        raise GraphError(0, "network_unavailable") from None
    if response.status_code >= 300:
        raise GraphError(response.status_code, _extract_error_code(response))
    value = response.content
    return value if raw else (json.loads(value) if value else {})


def _authority(account: MailAccount) -> str:
    if account.graph is None:
        raise ValueError("Microsoft Graph account required")
    return f"https://login.microsoftonline.com/{account.graph.tenant}"


def _filter_msal_scopes(raw_scopes) -> list[str]:
    if isinstance(raw_scopes, str):
        items = raw_scopes.split()
    else:
        items = list(raw_scopes or ())
    filtered = [scope for scope in items if scope and scope.lower() not in _MSAL_RESERVED_SCOPES]
    return filtered or list(READ_SCOPES)


def _msal_app(
    account: MailAccount,
    cache: msal.SerializableTokenCache,
    *,
    timeout: float = 30.0,
) -> msal.PublicClientApplication:
    if account.graph is None:
        raise ValueError("Microsoft Graph account required")
    return msal.PublicClientApplication(
        client_id=account.graph.client_id,
        authority=_authority(account),
        token_cache=cache,
        http_client=_HttpxNoRedirectClient(timeout=timeout),
        instance_discovery=False,
    )


def _save_token_cache(
    account: MailAccount,
    cache: msal.SerializableTokenCache,
    *,
    scope: str,
    allow_interaction: bool = False,
) -> None:
    if account.graph is None:
        raise ValueError("Microsoft Graph account required")
    if not any(cache.search(cache.CredentialType.REFRESH_TOKEN)):
        raise ValueError("Microsoft did not return renewable delegated credentials")
    envelope = {
        "format": "msal_cache_v1",
        "client_id": account.graph.client_id,
        "email": account.email.lower(),
        "scope": scope,
        "msal_cache": cache.serialize(),
    }
    serialized = json.dumps(envelope, separators=(",", ":"))
    set_oauth_secret(account.graph.credential_ref, serialized, allow_interaction=allow_interaction)
    if get_oauth_secret(account.graph.credential_ref, allow_interaction=allow_interaction) != serialized:
        raise RuntimeError("OAuth Keychain readback mismatch")


def login_graph(account: MailAccount, *, write=False, notify=None, timeout=600):
    """PKCE authorization-code flow via MSAL; the browser receives no client secret.

    notify receives the login URL. Bind only loopback, validate Host and state,
    suppress access logs, and save the MSAL token cache to Keychain only after
    /me matches this account.
    """
    cache = msal.SerializableTokenCache()
    app = _msal_app(account, cache, timeout=min(30.0, max(1.0, float(timeout))))
    scopes = list(WRITE_SCOPES if write else READ_SCOPES)
    auth_response: dict[str, str] = {}
    expected_state = ""

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            parsed = urlsplit(self.path)
            query = {key: values[0] for key, values in parse_qs(parsed.query).items() if values}
            valid = (
                parsed.path == "/"
                and self.headers.get("Host") == f"localhost:{self.server.server_port}"
                and bool(expected_state)
                and secrets.compare_digest(query.get("state", ""), expected_state)
            )
            if valid:
                auth_response.update(query)
            self.send_response(200 if valid else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write((
                "Microsoft login received. You can return to Codex."
                if valid
                else "Invalid login callback."
            ).encode())

    with HTTPServer(("127.0.0.1", 0), Callback) as server:
        redirect = f"http://localhost:{server.server_port}"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            flow = app.initiate_auth_code_flow(
                scopes=scopes,
                redirect_uri=redirect,
                login_hint=account.email,
                prompt="select_account",
                response_mode="query",
            )
        expected_state = str(flow.get("state") or "")
        if notify is not None:
            notify(flow["auth_uri"])
        deadline = time.monotonic() + timeout
        while not auth_response and time.monotonic() < deadline:
            server.timeout = min(1, max(0.01, deadline - time.monotonic()))
            server.handle_request()
    if not auth_response.get("code"):
        raise ValueError("Microsoft login was cancelled or timed out")
    token = app.acquire_token_by_auth_code_flow(flow, auth_response)
    if not isinstance(token, dict) or not token.get("access_token"):
        code = str((token or {}).get("error") or "oauth_exchange_failed")
        raise GraphError(400, code)
    granted_scope = token.get("scope") or " ".join(scopes)
    if isinstance(granted_scope, list):
        granted_scope = " ".join(str(item) for item in granted_scope)
    client = GraphClient(account, token=token["access_token"])
    client.identity()
    _save_token_cache(account, cache, scope=str(granted_scope), allow_interaction=True)
    return {
        "ok": True,
        "account": account.account_id,
        "provider": "microsoft_graph",
        "email": account.email,
        "credential_configured": True,
        "scopes": str(granted_scope).split(),
        "send_allowed": False,
    }


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
            stored = json.loads(get_oauth_secret(self.account.graph.credential_ref))
            if (
                stored.get("client_id") != self.account.graph.client_id
                or str(stored.get("email") or "").lower() != self.account.email.lower()
            ):
                raise ValueError("OAuth credentials belong to a different Relay account/application")

            timeout = min(30.0, max(0.01, lock_deadline - time.monotonic()))
            scopes = _filter_msal_scopes(stored.get("scope"))
            cache = msal.SerializableTokenCache()
            app = _msal_app(self.account, cache, timeout=timeout)

            if "msal_cache" in stored:
                cache.deserialize(str(stored["msal_cache"]))
                accounts = app.get_accounts(username=self.account.email) or app.get_accounts()
                msal_account = accounts[0] if accounts else None
                result = app.acquire_token_silent(
                    scopes,
                    account=msal_account,
                    force_refresh=force_refresh,
                )
                if not isinstance(result, dict) or not result.get("access_token"):
                    code = str((result or {}).get("error") or "oauth_refresh_failed")
                    raise GraphError(400, code)
                granted = result.get("scope") or stored.get("scope") or " ".join(scopes)
                if isinstance(granted, list):
                    granted = " ".join(str(item) for item in granted)
                if cache.has_state_changed:
                    _save_token_cache(self.account, cache, scope=str(granted), allow_interaction=False)
                self.scopes = set(str(granted).lower().split())
                self.token = str(result["access_token"])
            else:
                # Transparent upgrade path for pre-MSAL Keychain credentials.
                if (
                    not force_refresh
                    and stored.get("access_token")
                    and stored.get("expires_at", 0) >= time.time() + 120
                ):
                    self.scopes = set(str(stored.get("scope", "")).lower().split())
                    self.token = str(stored["access_token"])
                else:
                    refresh_token = str(stored.get("refresh_token") or "")
                    if not refresh_token:
                        raise ValueError("Microsoft OAuth credentials lack a refresh token")
                    result = app.acquire_token_by_refresh_token(refresh_token, scopes=scopes)
                    if not isinstance(result, dict) or not result.get("access_token"):
                        code = str((result or {}).get("error") or "oauth_refresh_failed")
                        raise GraphError(400, code)
                    granted = result.get("scope") or stored.get("scope") or " ".join(scopes)
                    if isinstance(granted, list):
                        granted = " ".join(str(item) for item in granted)
                    _save_token_cache(self.account, cache, scope=str(granted), allow_interaction=False)
                    self.scopes = set(str(granted).lower().split())
                    self.token = str(result["access_token"])
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
    ensure_mail_schema(conn)


def _uid(conn, account, remote_id):
    conn.execute("INSERT OR IGNORE INTO graph_message_ids(account_id,remote_id) VALUES (?,?)", (account, remote_id))
    return conn.execute("SELECT uid FROM graph_message_ids WHERE account_id=? AND remote_id=?", (account, remote_id)).fetchone()[0]


def _slug(item):
    from .folders import legacy_folder_slug
    # Graph already has an immutable folder-id suffix; keep its issued identities.
    return legacy_folder_slug(item["path"]) + "--" + hashlib.sha256(item["id"].encode()).hexdigest()[:10]


def _now():
    return datetime.now(UTC).isoformat()


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
    from .store import ACTIVE_FOLDERS, HISTORY_FOLDERS
    from .sync import should_sync_folder
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
