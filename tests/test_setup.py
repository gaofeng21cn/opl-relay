import io
import json
import os
import subprocess
import sys
from pathlib import Path

from codex_mail_workbench import cli


def run_setup(tmp_path: Path, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    root = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = str(root / "plugins" / "opl-relay" / "runtime")
    env["OPL_PROFILE_WORKSPACE"] = str(tmp_path / "profile")
    return subprocess.run(
        [sys.executable, "-m", "codex_mail_workbench.cli", "--json", *args],
        env=env,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )


def test_setup_init_creates_a_profile_ready_for_account_configuration(tmp_path: Path) -> None:
    result = run_setup(tmp_path, "setup", "init")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["setup"]["workspace"]["ready"] is True
    assert payload["setup"]["readiness"] == "partial"
    assert "data/relay/accounts.toml" in payload["setup"]["created"]
    assert payload["setup"]["accounts_configured"] == 0


def test_account_add_reports_safe_next_steps_without_password(tmp_path: Path) -> None:
    run_setup(tmp_path, "setup", "init")
    result = run_setup(
        tmp_path,
        "account",
        "add",
        "--id",
        "work",
        "--email",
        "work@example.com",
        "--host",
        "imap.example.com",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["account"]["credential_configured"] is False
    assert "credential set" in payload["next_actions"][0]
    assert "password" not in result.stdout.casefold()


def test_credential_set_uses_keychain_and_never_prints_secret(monkeypatch, capsys, tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    config = profile / "data" / "relay" / "accounts.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        'version = 1\n\n[[accounts]]\naccount_id = "work"\nemail = "work@example.com"\n'
        '\n[accounts.imap]\nhost = "imap.example.com"\nport = 993\nsecurity = "ssl"\n'
        'username = "work@example.com"\ncredential_ref = "keychain.work.imap"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPL_PROFILE_WORKSPACE", str(profile))
    calls: dict[str, object] = {}

    def fake_store(ref: str, secret: str) -> None:
        calls.update(ref=ref, secret=secret)

    monkeypatch.setattr(cli, "keychain_set_secret", fake_store)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("top-secret\n"))

    assert cli.main(["--json", "--config", str(config), "credential", "set", "--account", "work", "--secret-stdin"]) == 0
    output = capsys.readouterr().out
    assert calls == {"ref": "keychain.work.imap", "secret": "top-secret"}
    assert "top-secret" not in output
