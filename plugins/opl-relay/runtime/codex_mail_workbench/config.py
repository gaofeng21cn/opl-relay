from __future__ import annotations

import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


KEYCHAIN_SERVICE = "codex-mail-workbench"


@dataclass(frozen=True)
class MailEndpoint:
    host: str
    port: int
    security: str
    username: str
    credential_ref: str


@dataclass(frozen=True)
class MailAccount:
    account_id: str
    email: str
    imap: MailEndpoint
    include_folders: list[str]
    exclude_folders: list[str]


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
    return MailEndpoint(
        host=_str(raw.get("host"), f"{field}.host"),
        port=_int(raw.get("port"), f"{field}.port"),
        security=_str(raw.get("security"), f"{field}.security").lower(),
        username=_str(raw.get("username"), f"{field}.username"),
        credential_ref=_str(raw.get("credential_ref"), f"{field}.credential_ref"),
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
        account = MailAccount(
            account_id=account_id,
            email=_str(raw.get("email"), f"accounts[{idx}].email"),
            imap=_endpoint(_dict(raw.get("imap"), f"accounts[{idx}].imap"), f"accounts[{idx}].imap"),
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
) -> MailAccount:
    """Append one account to the private Profile TOML without exposing secrets."""

    accounts = load_accounts_config(path) if path.exists() else {}
    if account_id in accounts:
        raise ValueError(f"account_id already exists: {account_id}")
    account = MailAccount(
        account_id=_str(account_id, "account_id"),
        email=_str(email, "email"),
        imap=MailEndpoint(
            host=_str(host, "imap.host"),
            port=_int(port, "imap.port"),
            security=_str(security, "imap.security").lower(),
            username=_str(username, "imap.username"),
            credential_ref=_str(credential_ref, "imap.credential_ref"),
        ),
        include_folders=_string_list(include_folders, "folders.include", ["*"]),
        exclude_folders=_string_list(exclude_folders, "folders.exclude", []),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["version = 1", ""]
    for item in [*accounts.values(), account]:
        lines.extend(
            [
                "[[accounts]]",
                f"account_id = {_toml_string(item.account_id)}",
                f"email = {_toml_string(item.email)}",
                "",
                "[accounts.imap]",
                f"host = {_toml_string(item.imap.host)}",
                f"port = {item.imap.port}",
                f"security = {_toml_string(item.imap.security)}",
                f"username = {_toml_string(item.imap.username)}",
                f"credential_ref = {_toml_string(item.imap.credential_ref)}",
                "",
                "[accounts.folders]",
                "include = [" + ", ".join(_toml_string(value) for value in item.include_folders) + "]",
                "exclude = [" + ", ".join(_toml_string(value) for value in item.exclude_folders) + "]",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")
    return account


def keychain_get_secret(
    credential_ref: str,
    *,
    service: str = KEYCHAIN_SERVICE,
) -> str:
    cmd = ["security", "find-generic-password", "-s", service, "-a", credential_ref, "-w"]
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout.strip()
    raise RuntimeError(f"Keychain 读取失败: account={credential_ref}")


def keychain_has_secret(
    credential_ref: str,
    *,
    service: str = KEYCHAIN_SERVICE,
) -> bool:
    """Check presence without reading or printing the secret."""

    result = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-a", credential_ref],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def keychain_set_secret(
    credential_ref: str,
    secret: str,
    *,
    service: str = KEYCHAIN_SERVICE,
) -> None:
    """Store a secret through stdin so it never appears in process arguments."""

    if not isinstance(secret, str) or not secret:
        raise ValueError("credential secret must not be empty")
    result = subprocess.run(
        [
            "security",
            "add-generic-password",
            "-U",
            "-s",
            service,
            "-a",
            _str(credential_ref, "credential_ref"),
            "-w",
        ],
        input=secret + "\n",
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Keychain 写入失败: account={credential_ref}")
