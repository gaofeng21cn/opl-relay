from pathlib import Path

import pytest

from codex_mail_workbench.config import load_accounts_config


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
