import json
import os
import subprocess
import tomllib
from pathlib import Path

from codex_mail_workbench import __version__
from codex_mail_workbench.workspace import initialize_workspace


ROOT = Path(__file__).parents[1]
PACKAGE_PATH = ROOT / "opl-package.json"
LEGACY_PACKAGE_PATH = ROOT / "packages" / "opl-relay" / "package.json"
LEGACY_PLUGIN_DIR = ROOT / "plugins" / "opl-relay"
ICON_PATH = ROOT / "assets" / "icon.png"
SKILL_PATH = ROOT / "skills" / "opl-relay" / "SKILL.md"

CAPABILITY_IDS = {
    "communications.mail.v1",
    "personal.context.v1",
    "personal.memory.v1",
    "knowledge.obsidian.v1",
}
VIEW_TYPES = {"list_detail", "activity_log", "approval_diff"}
ACTION_REFS = {
    "communications.mail.v1#workbench.open",
    "communications.mail.v1#sync.incremental",
    "communications.mail.v1#draft.create",
    "communications.mail.v1#draft.reply_all",
    "communications.mail.v1#draft.create_from_persona",
    "communications.mail.v1#draft.inspect",
    "communications.mail.v1#draft.open",
    "communications.mail.v1#draft.send",
    "personal.context.v1#build",
    "personal.memory.v1#inspect",
}

DATA_REFS = {
    "communications.mail.v1#recent",
    "communications.mail.v1#triage.evidence",
    "communications.mail.v1#draft.inspect",
    "personal.memory.v1#search",
    "personal.memory.v1#people",
}


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_package_identity_capabilities_and_version_are_aligned() -> None:
    package = load_json(PACKAGE_PATH)
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert package["schema_version"] == "opl.package.v2"
    identity = package["identity"]
    assert identity["id"] == "opl-relay"
    assert identity["intent"] == "app"
    assert identity["version"] == project["project"]["version"] == __version__
    assert identity["license"] == "Apache-2.0"
    assert set(project["project"]["scripts"]) == {"opl-relay"}

    provides = package["provides"]
    assert provides["cli_bin"] == [{"name": "opl-relay", "path": "./bin/opl-relay"}]
    assert [s["id"] for s in provides["skills"]] == ["opl-relay", "codex-mail-workbench"]
    for s in provides["skills"]:
        assert (ROOT / s["path"] / "SKILL.md").is_file()
    assert provides["mcp_servers"] == [
        {
            "id": "mail-workbench",
            "transport": "stdio",
            "command": "node",
            "args": ["./workbench-dist/server.mjs"],
        }
    ]
    assert provides["connection_providers"][0]["provider_type"] == "opl-relay.mailbox"
    assert provides["connection_providers"][0]["entrypoint"]["argv"] == ["./bin/opl-relay", "--json", "connect"]
    assert [c["id"] for c in provides["connection_providers"][0]["capabilities"]] == [
        "mailbox.search",
        "mailbox.read",
        "draft.create",
    ]
    assert package["host_hints"]["codex"]["sync_enabled"] is True
    assert package["host_hints"]["codex"]["plugin_name"] == "opl-relay"
    assert package["distribution"]["oci"] == "ghcr.io/gaofeng21cn/one-person-lab-packages/opl-relay"

    skill = SKILL_PATH.read_text(encoding="utf-8")
    assert all(capability_id in skill for capability_id in CAPABILITY_IDS)


def test_package_descriptor_has_one_root_authority_and_zero_mirrors() -> None:
    assert PACKAGE_PATH.is_file()
    assert not LEGACY_PACKAGE_PATH.exists()
    assert not LEGACY_PLUGIN_DIR.exists()
    assert not (ROOT / ".codex-plugin").exists()
    assert not (ROOT / ".agents" / "plugins" / "marketplace.json").exists()
    assert not (ROOT / "plugin.json").exists()
    assert (ROOT / "workbench-dist" / "server.mjs").is_file()
    assert (ROOT / "workbench-dist" / "index.html").is_file()
    assert (ROOT / "workbench-dist" / "THIRD_PARTY_NOTICES.txt").is_file()


def test_plugin_default_prompts_fit_codex_limits() -> None:
    package = load_json(PACKAGE_PATH)
    prompts = package["identity"]["default_prompts"]
    assert isinstance(prompts, list) and 1 <= len(prompts) <= 3
    assert all(isinstance(prompt, str) and 0 < len(prompt) <= 128 for prompt in prompts)


def test_package_uses_family_brand_artwork() -> None:
    package = load_json(PACKAGE_PATH)
    icon = ICON_PATH.read_bytes()

    assert package["identity"]["icon"] == "assets/icon.png"
    assert package["identity"]["repository"] == "https://github.com/gaofeng21cn/opl-relay"
    assert package["host_hints"]["codex"]["brand_color"] == "#1260E8"
    assert icon.startswith(b"\x89PNG\r\n\x1a\n")
    assert icon == (ROOT / "assets/branding/opl-relay-logo.png").read_bytes()


def test_app_contributions_are_role_neutral_and_reference_existing_cli_actions() -> None:
    package = load_json(PACKAGE_PATH)
    declarative = package["ui"]["declarative"]
    handler = package["provides"]["contribution_handler"]

    nav_ids = [item["id"] for item in declarative["navigation"]]
    view_ids = [item["id"] for item in declarative["views"]]
    assert len(nav_ids) == len(set(nav_ids))
    assert len(view_ids) == len(set(view_ids))
    assert {item["view_id"] for item in declarative["navigation"]} <= set(view_ids)
    assert {item["view_type"] for item in declarative["views"]} == VIEW_TYPES
    assert {item["data_ref"] for item in declarative["views"]} == DATA_REFS

    actions_by_ref = {
        action["action_ref"]: action
        for view in declarative["views"]
        for action in view.get("actions", [])
    }
    assert set(actions_by_ref) == ACTION_REFS
    assert all(view["slot"] == "workbench.panel" for view in declarative["views"])

    views_by_id = {item["id"]: item for item in declarative["views"]}
    assert views_by_id["relay.people"]["view_type"] == "list_detail"
    assert views_by_id["relay.people"]["data_ref"] == "personal.memory.v1#people"
    assert views_by_id["relay.memory"]["view_type"] == "activity_log"
    assert views_by_id["relay.memory"]["data_ref"] == "personal.memory.v1#search"

    assert handler == {
        "kind": "cli",
        "argv": ["./bin/opl-relay", "--json", "app-contribution"],
        "transport": "stdin_json_stdout_json",
    }
    assert actions_by_ref["communications.mail.v1#draft.send"]["confirm"] is True
    assert actions_by_ref["communications.mail.v1#draft.create_from_persona"]["confirm"] is True
    assert all(
        action.get("confirm", False) is False
        for ref, action in actions_by_ref.items()
        if ref not in {"communications.mail.v1#draft.send", "communications.mail.v1#draft.create_from_persona"}
    )
    assert package["ui"]["mcp_apps"][0]["mcp_server_id"] == "mail-workbench"

    serialized = json.dumps(declarative)
    assert "standard_agent" not in serialized


def test_app_contribution_abi_executes_from_package_root(tmp_path: Path) -> None:
    handler = load_json(PACKAGE_PATH)["provides"]["contribution_handler"]
    profile_workspace = tmp_path / "profile"
    initialize_workspace(profile_workspace)

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["OPL_PROFILE_WORKSPACE"] = str(profile_workspace)
    result = subprocess.run(
        handler["argv"],
        cwd=ROOT,
        input=json.dumps(
            {
                "schema_version": "opl-package-app-contribution-request.v1",
                "operation": "read",
                "package_id": "opl-relay",
                "ref": "communications.mail.v1#recent",
                "input": {},
            }
        ),
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response["schema_version"] == "opl-package-app-contribution-response.v1"
    assert response["ok"] is True
    assert response["ref"] == "communications.mail.v1#recent"
    assert response["operation"] == "read"
    assert response["result"]["messages"] == []
    assert response["result"]["kind"] == "data"
    assert response["result"]["state"] == "ready"
    assert response["result"]["data"]["items"] == []


def test_launcher_falls_back_to_uv_when_repo_venv_lacks_dependencies(tmp_path: Path) -> None:
    import shutil
    import sys

    repo = tmp_path / "repo"
    launcher = repo / "bin" / "opl-relay"
    runtime_dir = repo / "runtime"
    venv_python = repo / ".venv" / "bin" / "python"
    fake_bin = tmp_path / "bin"
    fake_uv = fake_bin / "uv"
    uv_log = tmp_path / "uv-argv.json"
    sitecustomize = tmp_path / "isolated-site" / "sitecustomize.py"

    launcher.parent.mkdir(parents=True)
    runtime_dir.mkdir(parents=True)
    venv_python.parent.mkdir(parents=True)
    fake_bin.mkdir(parents=True)
    sitecustomize.parent.mkdir(parents=True)

    shutil.copy2(ROOT / "bin" / "opl-relay", launcher)
    launcher.chmod(0o755)

    # Simulate a stale .venv missing required dependencies like typer.
    venv_python.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n", encoding="utf-8")
    venv_python.chmod(0o755)

    # Simulate uv handling the fallback re-exec cleanly.
    fake_uv.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        f"Path({str(uv_log)!r}).write_text(json.dumps(sys.argv[1:]), encoding='utf-8')\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)

    # Force importlib.util.find_spec('typer') -> None in the initial interpreter so fallback runs.
    sitecustomize.write_text(
        "import importlib.util\n"
        "_orig = importlib.util.find_spec\n"
        "def _patched(name, *args, **kwargs):\n"
        "    if name == 'typer':\n"
        "        return None\n"
        "    return _orig(name, *args, **kwargs)\n"
        "importlib.util.find_spec = _patched\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("_OPL_RELAY_UV_REEXEC", None)
    env.pop("_OPL_RELAY_VENV_REEXEC", None)
    env["PYTHONPATH"] = str(sitecustomize.parent)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"

    result = subprocess.run(
        [sys.executable, str(launcher), "--version"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    recorded = json.loads(uv_log.read_text(encoding="utf-8"))
    assert recorded[:3] == ["run", "--quiet", "--no-project"]
    assert "typer>=0.15,<1" in recorded
    assert str(launcher.resolve()) in recorded
    assert recorded[-1] == "--version"

    # When .venv satisfies runtime requirements, launcher uses .venv and does not call uv.
    uv_log.unlink()
    venv_python.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if sys.argv[1:2] == ['-c']:\n"
        "    sys.exit(0)\n"
        "print('venv-ok')\n",
        encoding="utf-8",
    )
    venv_python.chmod(0o755)
    result_venv = subprocess.run(
        [sys.executable, str(launcher), "--version"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result_venv.returncode == 0, result_venv.stderr
    assert "venv-ok" in result_venv.stdout
    assert not uv_log.exists()


def _install_fake_dist(site_dir: Path, name: str, version: str, extra_files: dict[str, str] | None = None) -> None:
    pkg_dir = site_dir / name
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "__init__.py").write_text(f"__version__ = {version!r}\n", encoding="utf-8")
    dist_info = site_dir / f"{name}-{version}.dist-info"
    dist_info.mkdir(parents=True, exist_ok=True)
    (dist_info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n", encoding="utf-8")
    for rel, content in (extra_files or {}).items():
        target = site_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def test_launcher_rejects_incompatible_pydantic_v1_in_venv_and_current_interpreter(tmp_path: Path) -> None:
    import shutil
    import sys

    if sys.version_info < (3, 11):
        return

    repo = tmp_path / "repo"
    launcher = repo / "bin" / "opl-relay"
    runtime_dir = repo / "runtime"
    venv_python = repo / ".venv" / "bin" / "python"
    fake_bin = tmp_path / "bin"
    fake_uv = fake_bin / "uv"
    uv_log = tmp_path / "uv-argv.json"
    v1_site = tmp_path / "v1-site"
    v2_site = tmp_path / "v2-site"

    launcher.parent.mkdir(parents=True)
    runtime_dir.mkdir(parents=True)
    venv_python.parent.mkdir(parents=True)
    fake_bin.mkdir(parents=True)
    shutil.copy2(ROOT / "bin" / "opl-relay", launcher)
    launcher.chmod(0o755)

    for pkg, ver in (("httpx", "0.28.1"), ("msal", "1.31.1"), ("pydantic", "1.10.18"), ("typer", "0.15.2")):
        _install_fake_dist(v1_site, pkg, ver)
    for pkg, ver in (("httpx", "0.28.1"), ("msal", "1.31.1"), ("pydantic", "2.10.6"), ("typer", "0.15.2")):
        _install_fake_dist(v2_site, pkg, ver, {"pydantic_core/__init__.py": ""})

    fake_uv.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        f"Path({str(uv_log)!r}).write_text(json.dumps(sys.argv[1:]), encoding='utf-8')\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)

    # 1. .venv has all 4 modules, but pydantic is 1.10.18 -> reject .venv and fall back to uv.
    venv_python.write_text(
        f"#!/bin/sh\nPYTHONPATH={str(v1_site)!r} exec {sys.executable!r} -S \"$@\"\n",
        encoding="utf-8",
    )
    venv_python.chmod(0o755)
    isolated_site = tmp_path / "empty-site"
    isolated_site.mkdir()
    (isolated_site / "sitecustomize.py").write_text(
        "import importlib.util\n"
        "_orig = importlib.util.find_spec\n"
        "importlib.util.find_spec = lambda n, *a, **kw: None if n in {'httpx','msal','pydantic','typer'} else _orig(n, *a, **kw)\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.pop("_OPL_RELAY_UV_REEXEC", None)
    env.pop("_OPL_RELAY_VENV_REEXEC", None)
    env["PYTHONPATH"] = str(isolated_site)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"

    res_v1_venv = subprocess.run([sys.executable, str(launcher), "--version"], check=False, capture_output=True, text=True, env=env)
    assert res_v1_venv.returncode == 0, res_v1_venv.stderr
    recorded = json.loads(uv_log.read_text(encoding="utf-8"))
    assert recorded[:5] == ["run", "--quiet", "--no-project", "--python", ">=3.11"]
    assert "pydantic>=2.10,<3" in recorded

    # 2. Current interpreter itself has pydantic 1.10.18 via -S PYTHONPATH -> reject current interpreter and fall back to uv.
    uv_log.unlink()
    env_cur_v1 = os.environ.copy()
    env_cur_v1.pop("_OPL_RELAY_UV_REEXEC", None)
    env_cur_v1.pop("_OPL_RELAY_VENV_REEXEC", None)
    env_cur_v1["PYTHONPATH"] = str(v1_site)
    env_cur_v1["PATH"] = f"{fake_bin}{os.pathsep}{env_cur_v1.get('PATH', '')}"
    res_cur_v1 = subprocess.run([sys.executable, "-S", str(launcher), "--version"], check=False, capture_output=True, text=True, env=env_cur_v1)
    assert res_cur_v1.returncode == 0, res_cur_v1.stderr
    assert uv_log.is_file()

    # 3. .venv has compatible versions (pydantic 2.10.6 + pydantic_core) -> accept .venv without uv.
    uv_log.unlink()
    venv_python.write_text(
        "#!/bin/sh\n"
        f"if [ \"$1\" = \"-c\" ]; then PYTHONPATH={str(v2_site)!r} exec {sys.executable!r} -S \"$@\"; fi\n"
        "echo venv-v2-ok\n",
        encoding="utf-8",
    )
    venv_python.chmod(0o755)
    res_v2_venv = subprocess.run([sys.executable, str(launcher), "--version"], check=False, capture_output=True, text=True, env=env)
    assert res_v2_venv.returncode == 0, res_v2_venv.stderr
    assert "venv-v2-ok" in res_v2_venv.stdout
    assert not uv_log.exists()
