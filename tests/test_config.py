from pathlib import Path

import pytest

from types import SimpleNamespace

from codex_mail_workbench import config as credential_config
from codex_mail_workbench.config import add_account, load_accounts_config


def test_load_accounts_config_parses_toml(tmp_path: Path) -> None:
    config = tmp_path / "accounts.toml"
    config.write_text(
        """
version = 1
[[accounts]]
account_id = "work"
email = "work@example.com"
[accounts.imap]
host = "imap.example.com"
port = 993
security = "ssl"
username = "work@example.com"
credential_ref = "keychain.work.imap"
[accounts.folders]
include = ["*"]
exclude = ["Archive"]
""".strip(),
        encoding="utf-8",
    )

    accounts = load_accounts_config(config)

    assert list(accounts) == ["work"]
    assert accounts["work"].imap.host == "imap.example.com"
    assert accounts["work"].imap.credential_ref == "keychain.work.imap"
    assert not hasattr(accounts["work"], "smtp")
    assert accounts["work"].include_folders == ["*"]
    assert accounts["work"].exclude_folders == ["Archive"]


def test_load_accounts_config_requires_credential_ref(tmp_path: Path) -> None:
    config = tmp_path / "accounts.toml"
    config.write_text(
        """
[[accounts]]
account_id = "work"
email = "work@example.com"
[accounts.imap]
host = "imap.example.com"
port = 993
security = "ssl"
username = "work@example.com"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="credential_ref"):
        load_accounts_config(config)


def test_add_account_writes_private_metadata_without_a_secret(tmp_path: Path) -> None:
    config = tmp_path / "data" / "accounts.toml"

    account = add_account(
        config,
        account_id="work",
        email="work@example.com",
        host="imap.example.com",
        port=993,
        security="ssl",
        username="work@example.com",
        credential_ref="keychain.work.imap",
        include_folders=["INBOX"],
        exclude_folders=["Archive"],
    )

    assert account.account_id == "work"
    loaded = load_accounts_config(config)
    assert loaded["work"].imap.credential_ref == "keychain.work.imap"
    assert "password" not in config.read_text(encoding="utf-8").casefold()


def test_keychain_fallback_is_explicit_and_reports_source(monkeypatch) -> None:
    paths = []

    def fake_run(command, **kwargs):
        paths.append(command[-1])
        return SimpleNamespace(returncode=51 if len(paths) == 1 else 0,
                               stdout="fallback-secret\n")

    monkeypatch.setattr(credential_config.subprocess, "run", fake_run)
    result = credential_config.keychain_read_secret(
        "work-imap", fallback_keychain=credential_config.SYSTEM_KEYCHAIN)
    assert result.value == "fallback-secret"
    assert result.source == "system_fallback"
    assert paths == [credential_config.LOGIN_KEYCHAIN, credential_config.SYSTEM_KEYCHAIN]


def test_keychain_without_fallback_fails_closed(monkeypatch) -> None:
    paths = []

    def fake_run(command, **kwargs):
        paths.append(command[-1])
        return SimpleNamespace(returncode=51, stdout="")

    monkeypatch.setattr(credential_config.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="Keychain 读取失败"):
        credential_config.keychain_read_secret("work-imap")
    assert paths == [credential_config.LOGIN_KEYCHAIN]
