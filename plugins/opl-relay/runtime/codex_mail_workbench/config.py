from __future__ import annotations

import os
import pty
import select
import signal
import subprocess
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


KEYCHAIN_SERVICE = "codex-mail-workbench"
LOGIN_KEYCHAIN = str(Path.home() / "Library/Keychains/login.keychain-db")
SYSTEM_KEYCHAIN = "/Library/Keychains/System.keychain"


@dataclass(frozen=True)
class MailEndpoint:
    host: str
    port: int
    security: str
    username: str
    credential_ref: str
    fallback_keychain: str | None = None


@dataclass(frozen=True)
class CredentialSecret:
    value: str
    source: str


@dataclass(frozen=True)
class GraphEndpoint:
    client_id: str
    credential_ref: str
    tenant: str = "consumers"


@dataclass(frozen=True)
class MailAccount:
    account_id: str
    email: str
    imap: MailEndpoint | None
    include_folders: list[str]
    exclude_folders: list[str]
    provider: str = "imap"
    graph: GraphEndpoint | None = None
    backend: str = "himalaya"


def load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    with path.open("rb") as handle:
        parsed = tomllib.load(handle)
    if not isinstance(parsed, dict):
        raise ValueError("配置根节点必须是对象")
    return parsed


def _dict(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} 必须是对象")
    return value


def _str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串")
    return value.strip()


def _int(value: Any, field: str) -> int:
    if not isinstance(value, int):
        raise ValueError(f"{field} 必须是整数")
    return value


def _string_list(value: Any, field: str, default: list[str]) -> list[str]:
    if value is None:
        value = default
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ValueError(f"{field} 必须是字符串数组")
    return [x.strip() for x in value if x.strip()]


def _endpoint(raw: dict[str, Any], field: str) -> MailEndpoint:
    fallback = raw.get("fallback_keychain")
    if fallback is not None and fallback != SYSTEM_KEYCHAIN:
        raise ValueError(f"{field}.fallback_keychain 必须是系统钥匙串路径")
    return MailEndpoint(
        host=_str(raw.get("host"), f"{field}.host"),
        port=_int(raw.get("port"), f"{field}.port"),
        security=_str(raw.get("security"), f"{field}.security").lower(),
        username=_str(raw.get("username"), f"{field}.username"),
        credential_ref=_str(raw.get("credential_ref"), f"{field}.credential_ref"),
        fallback_keychain=fallback,
    )


def load_accounts_config(path: Path) -> dict[str, MailAccount]:
    data = load_toml(path)
    accounts_raw = data.get("accounts")
    if not isinstance(accounts_raw, list):
        raise ValueError("accounts 必须是数组")
    accounts: dict[str, MailAccount] = {}
    for idx, item in enumerate(accounts_raw):
        raw = _dict(item, f"accounts[{idx}]")
        account_id = _str(raw.get("account_id"), f"accounts[{idx}].account_id")
        folders = _dict(raw.get("folders", {}), f"accounts[{idx}].folders")
        provider = raw.get("provider", "imap")
        if provider not in {"imap", "microsoft_graph"}:
            raise ValueError("unsupported mail provider")
        graph = None
        backend = raw.get("backend", "himalaya")
        if backend not in {"himalaya"}:
            raise ValueError("unsupported mail backend")
        if provider == "microsoft_graph":
            value = _dict(raw.get("graph"), "graph")
            tenant = value.get("tenant", "consumers")
            if tenant not in {"consumers", "common", "organizations"}:
                raise ValueError("unsupported Microsoft tenant authority")
            graph = GraphEndpoint(_str(value.get("client_id"), "graph.client_id"),
                                  _str(value.get("credential_ref"), "graph.credential_ref"), tenant)
        account = MailAccount(
            account_id=account_id,
            email=_str(raw.get("email"), f"accounts[{idx}].email"),
            imap=_endpoint(_dict(raw.get("imap"), f"accounts[{idx}].imap"), f"accounts[{idx}].imap") if provider == "imap" else None,
            provider=provider,
            graph=graph,
            backend=backend,
            include_folders=_string_list(
                folders.get("include"), f"accounts[{idx}].folders.include", ["*"]
            ),
            exclude_folders=_string_list(
                folders.get("exclude"), f"accounts[{idx}].folders.exclude", []
            ),
        )
        if account_id in accounts:
            raise ValueError(f"重复 account_id: {account_id}")
        accounts[account_id] = account
    return accounts


def load_account(path: Path, account_id: str) -> MailAccount:
    accounts = load_accounts_config(path)
    if account_id not in accounts:
        raise KeyError(f"找不到 account_id={account_id}")
    return accounts[account_id]


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def add_account(
    path: Path,
    *,
    account_id: str,
    email: str,
    host: str,
    port: int,
    security: str,
    username: str,
    credential_ref: str,
    include_folders: list[str],
    exclude_folders: list[str],
    provider: str = "imap",
    client_id: str = "",
    tenant: str = "consumers",
    backend: str = "himalaya",
) -> MailAccount:
    """Append one account to the private Profile TOML without exposing secrets."""

    accounts = load_accounts_config(path) if path.exists() else {}
    if account_id in accounts:
        raise ValueError(f"account_id already exists: {account_id}")
    if provider not in {"imap", "microsoft_graph"}:
        raise ValueError("unsupported mail provider")
    if tenant not in {"consumers", "common", "organizations"}:
        raise ValueError("unsupported Microsoft tenant authority")
    if backend not in {"himalaya"}:
        raise ValueError("unsupported mail backend")
    account = MailAccount(
        account_id=_str(account_id, "account_id"),
        email=_str(email, "email"),
        imap=MailEndpoint(
            host=_str(host, "imap.host"),
            port=_int(port, "imap.port"),
            security=_str(security, "imap.security").lower(),
            username=_str(username, "imap.username"),
            credential_ref=_str(credential_ref, "imap.credential_ref"),
        ) if provider == "imap" else None,
        provider=provider,
        graph=GraphEndpoint(_str(client_id, "graph.client_id"), credential_ref, tenant) if provider == "microsoft_graph" else None,
        backend=backend,
        include_folders=_string_list(include_folders, "folders.include", ["*"]),
        exclude_folders=_string_list(exclude_folders, "folders.exclude", []),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    # Append only the new account. Preserve comments, unknown fields, and every
    # existing account's configuration byte-for-byte.
    lines = [
                "[[accounts]]",
                f"account_id = {_toml_string(account.account_id)}",
                f"email = {_toml_string(account.email)}",
                f"provider = {_toml_string(provider)}",
                f"backend = {_toml_string(backend)}",
                "",
    ]
    if account.imap is not None:
        endpoint = account.imap
        lines += ["[accounts.imap]", f"host = {_toml_string(endpoint.host)}",
                  f"port = {endpoint.port}", f"security = {_toml_string(endpoint.security)}",
                  f"username = {_toml_string(endpoint.username)}",
                  f"credential_ref = {_toml_string(endpoint.credential_ref)}", ""]
    else:
        endpoint = account.graph
        assert endpoint is not None
        lines += ["[accounts.graph]", f"client_id = {_toml_string(endpoint.client_id)}",
                  f"tenant = {_toml_string(endpoint.tenant)}",
                  f"credential_ref = {_toml_string(endpoint.credential_ref)}", ""]
    lines += ["[accounts.folders]",
              "include = [" + ", ".join(_toml_string(v) for v in account.include_folders) + "]",
              "exclude = [" + ", ".join(_toml_string(v) for v in account.exclude_folders) + "]", ""]
    original = path.read_text(encoding="utf-8") if path.exists() else "version = 1\n"
    path.write_text(original.rstrip() + "\n\n" + "\n".join(lines), encoding="utf-8")
    return account


def keychain_get_secret(
    credential_ref: str,
    *,
    service: str = KEYCHAIN_SERVICE,
    fallback_keychain: str | None = None,
) -> str:
    return keychain_read_secret(credential_ref, service=service,
                                fallback_keychain=fallback_keychain).value


def keychain_read_secret(
    credential_ref: str,
    *,
    service: str = KEYCHAIN_SERVICE,
    fallback_keychain: str | None = None,
) -> CredentialSecret:
    if fallback_keychain is not None and fallback_keychain != SYSTEM_KEYCHAIN:
        raise ValueError("不支持的后备钥匙串路径")
    cmd = ["/usr/bin/security", "find-generic-password", "-s", service,
           "-a", credential_ref, "-w"]
    for path, source in ((LOGIN_KEYCHAIN, "login"), (fallback_keychain, "system_fallback")):
        if source == "system_fallback" and path is None:
            continue
        try:
            result = subprocess.run([*cmd, path], check=False,
                                    capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired:
            continue
        if result.returncode == 0 and result.stdout.rstrip("\r\n"):
            return CredentialSecret(result.stdout.rstrip("\r\n"), source)
    raise RuntimeError(f"Keychain 读取失败: account={credential_ref}")


def keychain_has_secret(
    credential_ref: str,
    *,
    service: str = KEYCHAIN_SERVICE,
    fallback_keychain: str | None = None,
) -> bool:
    """Check presence without reading or printing the secret."""

    try:
        keychain_read_secret(credential_ref, service=service,
                             fallback_keychain=fallback_keychain)
        return True
    except RuntimeError:
        return False


def keychain_set_secret(
    credential_ref: str,
    secret: str,
    *,
    service: str = KEYCHAIN_SERVICE,
) -> None:
    """Store a secret through security's private TTY prompts, then verify it."""

    if not isinstance(secret, str) or not secret:
        raise ValueError("credential secret must not be empty")
    command = ["/usr/bin/security", "add-generic-password", "-U", "-s", service,
               "-a", _str(credential_ref, "credential_ref"), "-T", "/usr/bin/security", "-w"]
    pid, master = pty.fork()
    if pid == 0:
        os.execv(command[0], command)
    prompts = (b"password data for new item:", b"retype password for new item:")
    seen = bytearray()
    sent = 0
    status = None
    deadline = time.monotonic() + 15
    try:
        while time.monotonic() < deadline:
            finished, child_status = os.waitpid(pid, os.WNOHANG)
            if finished:
                status = os.waitstatus_to_exitcode(child_status)
                break
            if select.select([master], [], [], 0.1)[0]:
                try:
                    seen.extend(os.read(master, 4096))
                except OSError:
                    pass
                if sent < len(prompts) and prompts[sent] in seen:
                    os.write(master, secret.encode("utf-8") + b"\n")
                    sent += 1
                    seen.clear()
        if status is None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
    finally:
        os.close(master)
    if status != 0 or sent != 2 or keychain_get_secret(credential_ref, service=service) != secret:
        raise RuntimeError(f"Keychain 写入失败: account={credential_ref}")
