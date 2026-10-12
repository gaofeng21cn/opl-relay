"""Typed Himalaya operations; Relay retains identities, review and readback.

No generic shell, send, delete, flag or arbitrary-URL operation is exposed.
Account metadata comes from Relay; credentials exist only in Keychain and the
child environment, never in the generated configuration or process arguments.
"""
from __future__ import annotations

import json
import base64
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from .config import keychain_get_secret, load_toml
from .paths import default_state_dir
from .folders import normalize_folder


class NativeError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__("Mail backend failed: " + code)


class Himalaya:
    def __init__(self, account, *, token=None, deadline=None):
        self.account, self.token, self.deadline = account, token, deadline
        config = load_toml(default_state_dir() / "tools.toml")
        self.executable = config["himalaya"]["executable"]
        if not Path(self.executable).is_absolute() or not os.access(self.executable, os.X_OK):
            raise NativeError("himalaya_not_installed")

    def _run(self, argv, *, raw=False, body=None, mutation=False):
        remaining = self.deadline - time.monotonic() if self.deadline else 120
        if remaining <= 0:
            raise NativeError("time_budget_exceeded")
        account = self.account
        if not account.graph:
            argv = list(argv)
            if "--mailbox" in argv:
                index = argv.index("--mailbox") + 1
                argv[index] = self._mailbox_name(argv[index])
            if argv[:2] == ["imap", "status"]:
                argv[2] = self._mailbox_name(argv[2])
            if argv[:2] in (["imap", "move"], ["imap", "copy"]):
                argv[3] = self._mailbox_name(argv[3])
        quote = json.dumps
        lines = [f"[accounts.{quote(account.account_id)}]", f"email = {quote(account.email)}"]
        if account.graph:
            secret = self.token
            if not secret:
                raise NativeError("oauth_token_required")
            lines.append('msgraph.auth.token.command = ["/usr/bin/printenv", "OPL_MAIL_BACKEND_SECRET"]')
        else:
            endpoint = account.imap
            secret = keychain_get_secret(endpoint.credential_ref, fallback_keychain=endpoint.fallback_keychain)
            scheme = "imaps" if endpoint.security == "ssl" else "imap"
            lines.extend([f"imap.server = {quote(f'{scheme}://{endpoint.host}:{endpoint.port}')}",
                          f"imap.sasl.login.username = {quote(endpoint.username)}",
                          'imap.sasl.login.password.command = ["/usr/bin/printenv", "OPL_MAIL_BACKEND_SECRET"]'])
            if endpoint.security == "starttls":
                lines.append("imap.starttls = true")
            elif endpoint.security != "ssl":
                raise NativeError("encrypted_transport_required")
        # No persistent parallel account config and no credential-bearing file.
        with tempfile.TemporaryDirectory(prefix="mail-tool-", dir=default_state_dir()) as directory:
            path = Path(directory) / "config.toml"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            path.chmod(0o600)
            command = [self.executable, "--config", str(path), "--account", account.account_id,
                       "--log-level", "off"]
            if not raw:
                command.append("--json")
            env = dict(os.environ, OPL_MAIL_BACKEND_SECRET=secret, NO_COLOR="1")
            env.pop("RUST_LOG", None)
            try:
                remaining = self.deadline - time.monotonic() if self.deadline else 120
                if remaining <= 0:
                    raise NativeError("time_budget_exceeded")
                is_mutation = mutation or body is not None or "move" in argv
                result = subprocess.run(command + argv, input=body, capture_output=True,
                                        timeout=min(60, remaining) if is_mutation else remaining, env=env)
            except subprocess.TimeoutExpired:
                raise NativeError("timeout_outcome_unknown" if mutation or body is not None or "move" in argv else "timeout") from None
            if result.returncode:
                # Upstream errors may echo remote content or secret-helper output.
                raise NativeError("command_outcome_unknown" if mutation or body is not None or "move" in argv else "command_failed")
            if raw:
                return result.stdout
            try:
                return json.loads(result.stdout)
            except ValueError:
                raise NativeError("invalid_json") from None

    def mime(self, remote_id, *, folder=""):
        if self.account.graph:
            return self._run(["msgraph", "messages", "get", remote_id, "--raw"], raw=True)
        return self._run(["message", "read", str(remote_id), "--mailbox", folder, "--raw"], raw=True)

    def create_draft(self, raw, *, folder=""):
        if self.account.graph:
            result = self._run(["msgraph", "messages", "create"], body=raw)
            # 2.2.1 emits a JSON Message, not a Graph Message object. This
            # anchored, version-bound receipt contains the returned remote ID.
            message = result if isinstance(result, str) else result.get("message", "")
            match = re.fullmatch(r"Microsoft Graph draft `([^`\s]+)` successfully created", message)
            if not match:
                raise NativeError("draft_create_outcome_unknown")
            return {"id": match[1]}
        return self._run(["message", "add", "--mailbox", folder, "--flag", "draft"], body=raw)

    def move(self, remote_id, *, source="", destination):
        if self.account.graph:
            result = self._run(["msgraph", "messages", "move", remote_id, destination])
            message = result if isinstance(result, str) else result.get("message", "")
            match = re.fullmatch(r"Microsoft Graph message moved to `([^`\s]+)`", message)
            if not match:
                raise NativeError("move_outcome_unknown")
            return {"id": match[1]}
        return self._run(["imap", "move", self._uid(remote_id), destination, "--mailbox", source])

    @staticmethod
    def _uid(value):
        if not str(value).isdigit() or int(value) <= 0:
            raise NativeError("invalid_uid")
        return str(int(value))

    def folders(self):
        boxes = self._run(["imap", "list", "--all"])["mailboxes"]
        self._folder_names = {box["name"] for box in boxes}
        return [{"name": "INBOX" if b["name"].casefold() == "inbox" else b["name"],
                 "flags": frozenset(a.casefold() for a in b["attributes"])} for b in boxes]

    def _mailbox_name(self, name):
        return name if name in getattr(self, "_folder_names", ()) else normalize_folder(name)

    def status(self, folder):
        result = self._run(["imap", "status", folder])
        epoch, count = result["uid_validity"], result["messages"]
        if not isinstance(epoch, int) or epoch <= 0 or not isinstance(count, int) or count < 0:
            raise NativeError("invalid_folder_identity")
        return {"uidvalidity": epoch, "count": count}

    def ids(self, folder, *, since=None):
        argv = ["imap", "search", "--mailbox", folder]
        if since is not None:
            argv += ["--since", since.strftime("%Y-%m-%d")]
        result = self._run(argv)
        if result.get("uid_mode") is not True:
            raise NativeError("uid_identity_required")
        return [int(self._uid(item["id"])) for item in result["ids"]]

    def metadata(self, folder, uids):
        if not uids:
            return {}
        result = self._run(["imap", "fetch", ",".join(self._uid(uid) for uid in uids),
                            "--mailbox", folder, "--size", "--flags"])
        return {int(self._uid(m["uid"])): m for m in result["messages"]}

    def fetch_bodies(self, folder, uids):
        """Native UID batch FETCH; Base64 preserves even non-UTF-8 octets."""
        requested = {int(self._uid(uid)) for uid in uids}
        result = self._run(["imap", "fetch", ",".join(self._uid(uid) for uid in uids),
                            "--mailbox", folder, "--body"])
        messages = {}
        for item in result["messages"]:
            uid = int(self._uid(item["uid"]))
            if uid not in requested or uid in messages:
                raise NativeError("unexpected_fetch_identity")
            try:
                messages[uid] = base64.b64decode(item["body"], validate=True)
            except (ValueError, TypeError):
                raise NativeError("invalid_fetch_body") from None
        return messages

    def capabilities(self):
        raw = self._fixed_commands(["R1 CAPABILITY"], read_only=True)
        match = re.search(rb"(?im)^\* CAPABILITY ([^\r\n]+)", raw)
        if not match:
            raise NativeError("capability_unavailable")
        return set(match[1].decode("ascii").upper().split())

    @staticmethod
    def _quote(value):
        if any(c in value for c in "\r\n\x00") or "\\r" in value or "\\n" in value:
            raise NativeError("invalid_imap_identity")
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def find_message(self, folder, message_id):
        # Gmail TEXT search does not reliably index Message-ID. Use an exact
        # header criterion through Himalaya's fixed raw-command seam instead.
        raw = self._fixed_commands([f"R1 EXAMINE {self._folder_token(folder)}",
            f"R2 UID SEARCH HEADER Message-ID {self._quote(message_id)}"], read_only=True)
        match = re.search(rb"(?im)^\* SEARCH([^\r\n]*)", raw)
        if not match:
            raise NativeError("search_result_unavailable")
        return [int(self._uid(uid.decode())) for uid in match[1].split()]

    def _folder_token(self, folder):
        folder = self._mailbox_name(folder)
        # The raw CLI seam sends bytes verbatim. Match IMAP's mailbox identity
        # encoding; typed Himalaya commands handle this mapping themselves.
        def encode(match):
            value = base64.b64encode(match.group().encode("utf-16-be")).decode().rstrip("=").replace("/", ",")
            return "&" + value + "-"
        self._quote(folder)  # Reject literal and CLI-decoded control characters.
        encoded = re.sub(r"[^\x20-\x7e]+", encode, folder.replace("&", "&-"))
        return self._quote(encoded)

    def _fixed_commands(self, commands, *, read_only=False):
        # Only owner-built CAPABILITY, identity search and exact UID cleanup
        # use this private seam. It is never exposed as an arbitrary CLI tool.
        result = self._run(["imap", "raw", "--", "\r\n".join(commands)], raw=True,
                           mutation=not read_only)
        result = result.replace(b"\\r\\n", b"\r\n")
        for line in commands:
            tag = line.split(" ", 1)[0].encode()
            if not re.search(rb"(?im)^" + tag + rb" OK(?: |\r?$)", result):
                raise NativeError("command_outcome_unknown" if not read_only else "command_rejected")
        return result

    def copy(self, uid, *, source, destination):
        return self._run(["imap", "copy", self._uid(uid), destination, "--mailbox", source], mutation=True)

    def remove_exact(self, folder, uid):
        folder_token = self._folder_token(folder)
        uid = self._uid(uid)
        if "UIDPLUS" not in self.capabilities():
            raise NativeError("uidplus_required")
        return self._fixed_commands([f"R1 SELECT {folder_token}",
            f"R2 UID STORE {uid} +FLAGS.SILENT (\\Deleted)", f"R3 UID EXPUNGE {uid}"])

    def check(self):
        result = self._run(["account", "check"])
        if not result.get("backends") or not all(b.get("ok") for b in result["backends"]):
            raise NativeError("connection_check_failed")
        return result

    def search(self, query, *, folder, limit=20):
        if self.account.graph:
            # Shared multi-field OR queries can fail inside Exchange's query
            # planner. Use Himalaya's Graph-native search and keep its IDs for
            # Relay's subsequent immutable-identity and MIME verification.
            result = self._run(["msgraph", "messages", "list", "--folder", folder,
                "--search", json.dumps(query), "--top", str(limit),
                "--select", "id,internetMessageId"])
            return {"envelopes": [{"id": m["id"], "message-id": m.get("internetMessageId", "")}
                                  for m in result["messages"]]}
        # Quote data inside Himalaya's query grammar, separately from argv.
        value = json.dumps(query)
        expression = [f"(from {value} or to {value} or subject {value} or body {value})"]
        return self._run(["envelope", "search", "--mailbox", folder, "--page-size", str(limit),
                          *expression, "order", "by", "date", "desc"])
